#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The Redshift provider, as deployed, against a real Redshift Serverless workgroup.

Redshift has no local container, and the provider verifies the server's hostname
over TLS, which a tunnel to localhost cannot satisfy. So these tests drive the
deployed provider function, inside the VPC: its bundle, driver, TLS and
user-scoped secret role are all the ones that ship. The workgroup is the
RedshiftTestTarget stack (install_cdk.sh with REDSHIFT_TEST_TARGET=true), seeded
here through the Redshift Data API as its admin.

    TEST_DEPLOYED_PREFIX=AdamDev AWS_PROFILE=<deploy profile> \\
        python3 -m pytest -m deployed tests/deployed

A connection is registered the way the Connections API registers one: a secret
under /<prefix>/connections/<user>/ and a row in the connections table.
"""

import json
import os
import secrets
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass

import pytest

PREFIX = os.environ.get("TEST_DEPLOYED_PREFIX", "")
OWN = {("wealth", t) for t in ("clients", "accounts", "audit_log", "numbers", "v_clients_restricted")} | {
    ("public", "shared_lookup")}
# As many rows as one Redshift FETCH returns; the paging test doubles them.
NUMBERS = 1000


def _password() -> str:
    """Redshift wants upper and lower case and a digit."""
    return "Aa1" + secrets.token_hex(12)


@dataclass
class Deployed:
    workgroup: str
    host: str
    admin_secret_arn: str
    provider_function: str
    connections_table: str
    owner_password: str
    analyst_password: str
    # The workgroup as an administrator registers it: users' connections are logins to it.
    target_id: str
    # The Redshift scanner, and the bucket it writes each scan's DDL to.
    scanner_function: str
    results_bucket: str


class DataApi:
    """The admin's own session, through the Data API, outside the provider."""

    def __init__(self, workgroup: str, secret_arn: str):
        import boto3
        self._client = boto3.client("redshift-data")
        self._workgroup, self._secret = workgroup, secret_arn

    def run(self, sql: str, *, missing_ok: bool = False) -> list[list]:
        statement_id = self._client.execute_statement(
            WorkgroupName=self._workgroup, SecretArn=self._secret, Database="dev", Sql=sql)["Id"]
        while True:
            described = self._client.describe_statement(Id=statement_id)
            if described["Status"] in ("FINISHED", "FAILED", "ABORTED"):
                break
            time.sleep(0.5)
        if described["Status"] != "FINISHED":
            # A drop of something not there yet: the seed's IF EXISTS, for statements without one.
            if missing_ok and "does not exist" in described.get("Error", ""):
                return []
            raise RuntimeError(f"{sql[:80]!r}: {described.get('Error')}")
        if not described.get("HasResultSet"):
            return []
        records = self._client.get_statement_result(Id=statement_id)["Records"]
        return [[next(iter(field.values())) for field in record] for record in records]


def _seed(api: DataApi, owner_password: str, analyst_password: str) -> None:
    api.run("DROP SCHEMA IF EXISTS wealth CASCADE")
    api.run("DROP TABLE IF EXISTS public.shared_lookup")
    api.run("DROP USER IF EXISTS analyst")
    api.run("DROP USER IF EXISTS mcp_dev")
    api.run("DROP ROLE analyst_role FORCE", missing_ok=True)
    numbers = ", ".join(f"({n})" for n in range(1, NUMBERS + 1))
    for sql in [
        f"CREATE USER mcp_dev PASSWORD '{owner_password}'",
        "CREATE SCHEMA wealth AUTHORIZATION mcp_dev",
        # Redshift's own rdsdb owns public, like its catalogs; a table in it is still a user's.
        "CREATE TABLE public.shared_lookup (id int)",
        "GRANT SELECT ON public.shared_lookup TO mcp_dev",
        "CREATE TABLE wealth.clients (id int PRIMARY KEY, name varchar(100) NOT NULL, email varchar(200))",
        "CREATE TABLE wealth.accounts (id int PRIMARY KEY, client_id int REFERENCES wealth.clients(id), "
        "balance decimal(12,2))",
        "CREATE TABLE wealth.audit_log (id int PRIMARY KEY, note varchar(100))",
        "CREATE TABLE wealth.numbers (n int)",
        "INSERT INTO wealth.clients VALUES (1, 'Ada', 'ada@example.com'), (2, 'Grace', 'grace@example.com')",
        "INSERT INTO wealth.accounts VALUES (10, 1, 100.50)",
        "INSERT INTO wealth.audit_log VALUES (1, 'original')",
        f"INSERT INTO wealth.numbers VALUES {numbers}",
        "CREATE VIEW wealth.v_clients_restricted AS SELECT id, name FROM wealth.clients",
        "COMMENT ON TABLE wealth.accounts IS 'One row per client''s account'",
        "COMMENT ON COLUMN wealth.accounts.balance IS 'Current balance in USD'",
        *(f"ALTER TABLE wealth.{t} OWNER TO mcp_dev"
          for t in ("clients", "accounts", "audit_log", "numbers", "v_clients_restricted")),
        "CREATE ROLE analyst_role",
        "GRANT USAGE ON SCHEMA wealth TO ROLE analyst_role",
        "GRANT SELECT ON wealth.v_clients_restricted TO ROLE analyst_role",
        f"CREATE USER analyst PASSWORD '{analyst_password}'",
        "GRANT ROLE analyst_role TO analyst",
    ]:
        api.run(sql)


@pytest.fixture(scope="module")
def deployed() -> Deployed:
    if not PREFIX:
        pytest.fail("TEST_DEPLOYED_PREFIX is not set; the Redshift tests run against a deployed stage.",
                    pytrace=False)
    import boto3
    outputs = {o["OutputKey"]: o["OutputValue"] for o in boto3.client("cloudformation").describe_stacks(
        StackName=f"{PREFIX}-RedshiftTestTarget")["Stacks"][0]["Outputs"]}
    function = boto3.client("ssm").get_parameter(
        Name=f"/{PREFIX}/redshift_provider_function_name")["Parameter"]["Value"]
    table = boto3.client("lambda").get_function_configuration(
        FunctionName=function)["Environment"]["Variables"]["CONNECTIONS_TABLE_NAME"]
    resources = {r["LogicalResourceId"]: r["PhysicalResourceId"]
                 for r in _stack_resources(f"{PREFIX}-DataSourceProviders")}
    scanner = next(v for k, v in resources.items() if k.startswith("RedshiftScannerFunction") and "Logs" not in k)
    bucket = next(v for k, v in resources.items() if k.startswith("ScanResultsBucket") and "Policy" not in k)
    target = Deployed(outputs["WorkgroupName"], outputs["Endpoint"], outputs["AdminSecretArn"], function, table,
                      _password(), _password(), f"integration-test-{uuid.uuid4()}", scanner, bucket)
    _seed(DataApi(target.workgroup, target.admin_secret_arn), target.owner_password, target.analyst_password)
    with _registered(target):
        yield target


def _stack_resources(stack: str) -> list[dict]:
    """Every resource of a stack: the API returns them a page (100) at a time."""
    import boto3
    pages = boto3.client("cloudformation").get_paginator("list_stack_resources").paginate(StackName=stack)
    return [r for page in pages for r in page["StackResourceSummaries"]]


@contextmanager
def _registered(target: Deployed):
    """Register the workgroup as a version, the way the admin routes do (scan_targets.create), for this module only.
    Named apart from anything an administrator registers, and removed afterwards."""
    from adam.schema_provider import scan_targets
    from adam.schema_provider.source_identity import source_identity

    previous = os.environ.get("SCAN_TARGETS_TABLE")
    os.environ["SCAN_TARGETS_TABLE"] = next(
        r["PhysicalResourceId"] for r in _stack_resources(f"{PREFIX}-GlueCatalog")
        if r["ResourceType"] == "AWS::DynamoDB::Table" and r["LogicalResourceId"].startswith("ScanTargetsTable"))
    location = {"engine": "redshift", "host": target.host, "port": "5439", "database": "dev"}
    scan_targets.create({
        "targetId": target.target_id, "lineageId": target.target_id, "version": 1, "name": target.target_id,
        **location, "createdBy": "deployed-tests",
        "identity": source_identity(scan_targets.connection({"targetId": target.target_id, **location})),
        "scannerSecretArn": target.admin_secret_arn, "scannerSecretManaged": False, "disabled": False,
        # Above what any test here asks for; a test that wants less asks for less.
        "statementTimeoutSeconds": 60, "rowCap": 2000,
    })
    try:
        yield
    finally:
        scan_targets._delete(target.target_id)
        if previous is None:
            os.environ.pop("SCAN_TARGETS_TABLE")
        else:
            os.environ["SCAN_TARGETS_TABLE"] = previous


@pytest.fixture(scope="module")
def admin(deployed) -> DataApi:
    return DataApi(deployed.workgroup, deployed.admin_secret_arn)


class Login:
    """One registered connection, used through the deployed provider function."""

    def __init__(self, deployed: Deployed, username: str, password: str):
        import boto3
        self._deployed = deployed
        self._lambda = boto3.client("lambda", config=_long_reads())
        self.user_id = f"integration-test-{uuid.uuid4()}"
        self.connection_id = str(uuid.uuid4())
        self._secret_name = f"/{PREFIX}/connections/{self.user_id}/{self.connection_id}"
        self._secrets, self._ddb = boto3.client("secretsmanager"), boto3.client("dynamodb")
        self._secrets.create_secret(Name=self._secret_name,
                                    SecretString=json.dumps({"username": username, "password": password}))
        # As the Connections API writes one: which registered database, and the login's secret.
        row = {"userId": self.user_id, "connectionId": self.connection_id,
               "targetId": deployed.target_id, "secretName": self._secret_name}
        self._ddb.put_item(TableName=deployed.connections_table, Item={k: {"S": v} for k, v in row.items()})

    def call(self, operation: str, **args):
        payload = {"operation": operation,
                   "args": {"user_id": self.user_id, "connection_id": self.connection_id, **args}}
        resp = self._lambda.invoke(FunctionName=self._deployed.provider_function, Payload=json.dumps(payload))
        body = json.loads(resp["Payload"].read())
        assert not resp.get("FunctionError"), body
        assert "error" not in body, body
        return body["response"]

    def run(self, statement: str, **limits):
        return self.call("execute_statement", statement=statement,
                         **{"timeout_seconds": 30, "row_cap": 1000, **limits})

    def remove(self) -> None:
        self._ddb.delete_item(TableName=self._deployed.connections_table,
                              Key={"userId": {"S": self.user_id}, "connectionId": {"S": self.connection_id}})
        self._secrets.delete_secret(SecretId=self._secret_name, ForceDeleteWithoutRecovery=True)


def _scanned(deployed: Deployed, username: str, password: str) -> dict[str, str]:
    """Each object's DDL, as a scan produces it: the deployed Redshift scanner, sent the
    ScanRequested the schema service sends, logging in with a scanner login kept where scanner
    logins live, and its result read from where it writes it."""
    import boto3
    secrets_client, s3 = boto3.client("secretsmanager"), boto3.client("s3")
    login = secrets_client.create_secret(Name=f"/{PREFIX}/scanners/integration-test-{uuid.uuid4()}",
                                         SecretString=json.dumps({"username": username, "password": password}))
    request_id = str(uuid.uuid4())
    key = f"scans/{deployed.target_id}/{request_id}.json"
    try:
        resp = boto3.client("lambda", config=_long_reads()).invoke(
            FunctionName=deployed.scanner_function, Payload=json.dumps({
                "detail-type": "ScanRequested",
                "detail": {"requestId": request_id, "targetId": deployed.target_id, "engine": "redshift",
                           "location": {"host": deployed.host, "port": "5439", "database": "dev"},
                           "scannerSecretArn": login["ARN"]}}))
        assert not resp.get("FunctionError"), resp["Payload"].read()
        body = s3.get_object(Bucket=deployed.results_bucket, Key=key)["Body"].read()
        return {o["name"]: o["definition"] for o in json.loads(body)}
    finally:
        s3.delete_object(Bucket=deployed.results_bucket, Key=key)
        secrets_client.delete_secret(SecretId=login["ARN"], ForceDeleteWithoutRecovery=True)


def _long_reads():
    from botocore.config import Config
    # The provider's own limit is five minutes; the client must outwait it.
    return Config(read_timeout=330, retries={"max_attempts": 0})


@pytest.fixture(scope="module")
def owner(deployed):
    login = Login(deployed, "mcp_dev", deployed.owner_password)
    yield login
    login.remove()


@pytest.fixture(scope="module")
def analyst(deployed):
    login = Login(deployed, "analyst", deployed.analyst_password)
    yield login
    login.remove()


def _names(refs) -> set[tuple[str, str]]:
    return {(r["schema"], r["name"]) for r in refs}


@pytest.fixture
def restore(admin):
    yield
    admin.run("DROP TABLE IF EXISTS wealth.sneaky")
    admin.run("DELETE FROM wealth.audit_log")
    admin.run("INSERT INTO wealth.audit_log VALUES (1, 'original')")


def _audit_rows(admin) -> int:
    return int(admin.run("SELECT COUNT(*) FROM wealth.audit_log")[0][0])


def _tables(admin) -> set[str]:
    return {r[0] for r in admin.run("SELECT tablename FROM pg_tables WHERE schemaname = 'wealth'")}


class TestVisibility:
    def test_a_grant_through_a_role_is_visible(self, analyst):
        assert _names(analyst.call("visible_object_refs")) == {("wealth", "v_clients_restricted")}

    def test_the_owner_sees_their_tables_and_views_and_no_system_objects(self, owner):
        """Redshift lists some of its own views (pg_auto_copy's) to every user, who may not
        describe them, so a scan that included them failed."""
        assert _names(owner.call("visible_object_refs")) == OWN


class TestProbe:
    def test_a_login_that_works_is_ok(self, owner):
        assert owner.call("probe") == {"ok": True}


class TestDdl:
    @pytest.fixture(scope="class")
    def ddl(self, deployed):
        return _scanned(deployed, "mcp_dev", deployed.owner_password)

    def test_tables_carry_keys_and_comments(self, ddl):
        accounts = ddl["accounts"].lower()
        assert "primary key" in accounts
        assert "references wealth.clients" in accounts.replace('"', "")
        assert "comment on table wealth.accounts is 'one row per client''s account';" in accounts
        assert "comment on column wealth.accounts.balance is 'current balance in usd';" in accounts

    def test_a_view_carries_its_definition(self, ddl):
        view = ddl["v_clients_restricted"].lower().replace('"', "")
        assert "from wealth.clients" in view and "name" in view and "email" not in view


class TestWritesAreRefusedByTheDatabase:
    """As the schema owner, who may do all of these outside the provider."""

    @pytest.mark.parametrize("write", [
        "DELETE FROM wealth.audit_log",
        "INSERT INTO wealth.audit_log VALUES (99, 'x')",
        "UPDATE wealth.audit_log SET note = 'x'",
        "DROP TABLE wealth.audit_log",
        "TRUNCATE wealth.audit_log",
        "ALTER TABLE wealth.audit_log ADD COLUMN extra int",
        "CREATE TABLE wealth.sneaky (id int)",
        "CREATE TABLE wealth.sneaky AS SELECT * FROM wealth.clients",
        "SELECT * INTO wealth.sneaky FROM wealth.clients",
        "SELECT 1; DELETE FROM wealth.audit_log",
    ])
    def test_it_is_refused_and_changes_nothing(self, admin, owner, restore, write):
        result = owner.run(write)
        assert result["status"]["code"] == 500, result
        assert _audit_rows(admin) == 1
        assert "sneaky" not in _tables(admin) and "audit_log" in _tables(admin)
        # Nor changed in place: the row's value, and the table's columns.
        assert [r[0] for r in admin.run("SELECT note FROM wealth.audit_log")] == ["original"]
        assert "extra" not in {r[0] for r in admin.run(
            "SELECT column_name FROM information_schema.columns WHERE table_schema = 'wealth' "
            "AND table_name = 'audit_log'")}


class TestResourceLimits:
    def test_a_runaway_statement_is_stopped_by_the_timeout(self, owner):
        started = time.monotonic()
        result = owner.run("SELECT COUNT(*) FROM wealth.numbers a, wealth.numbers b, wealth.numbers c, "
                           "wealth.numbers d", timeout_seconds=2)
        assert "cancel" in result["error"].lower(), result  # the timeout's own cancellation, not any error
        assert time.monotonic() - started < 60

    def test_a_result_larger_than_the_cap_is_truncated_and_says_so(self, owner):
        result = owner.run("SELECT n FROM wealth.numbers ORDER BY n", row_cap=5)
        assert [r["n"] for r in result["rows"]] == [1, 2, 3, 4, 5]
        assert result["truncated"] is True

    def test_start_row_pages_through_a_result_without_gaps_or_overlap(self, owner):
        seen, start = [], 0
        for _ in range(4):  # 12 rows in pages of 5 is three; a fourth would mean pages repeat
            if start is None:
                break
            result = owner.run("SELECT n FROM wealth.numbers WHERE n <= 12 ORDER BY n", row_cap=5, start_row=start)
            seen += [r["n"] for r in result["rows"]]
            start = result["next_start_row"]
        assert seen == list(range(1, 13))

    def test_a_page_and_an_offset_larger_than_one_fetch(self, owner):
        """Redshift fetches at most 1000 rows a call; both the skip and the page take several."""
        doubled = "SELECT a.n FROM wealth.numbers a CROSS JOIN (SELECT 1 UNION ALL SELECT 2) b ORDER BY 1"
        first = owner.run(doubled, row_cap=1500)
        assert len(first["rows"]) == 1500 and first["truncated"] is True
        rest = owner.run(doubled, row_cap=1500, start_row=1500)
        assert len(rest["rows"]) == 500 and rest["truncated"] is False

    def test_duplicate_column_names_in_a_join_still_work(self, owner):
        result = owner.run("SELECT c.id, a.id FROM wealth.clients c JOIN wealth.accounts a ON a.client_id = c.id")
        assert result["columns"] == ["id", "id_2"]
        assert result["rows"] == [{"id": 1, "id_2": 10}]  # both values, neither overwritten

    def test_decimals_come_back_as_json_numbers(self, owner):
        assert owner.run("SELECT balance FROM wealth.accounts")["rows"] == [{"balance": 100.5}]


class TestAScannerSetUpAsAdvised:
    """The DBA advice for a scanner: REFERENCES on each table, which describes it without
    allowing queries on its rows, and SELECT on each view (here from the role)."""

    @pytest.fixture(scope="class")
    def scanner(self, admin, deployed):
        grants = "REFERENCES ON wealth.clients, wealth.accounts"
        admin.run(f"GRANT {grants} TO analyst")
        login = Login(deployed, "analyst", deployed.analyst_password)
        yield login
        login.remove()
        admin.run(f"REVOKE {grants} FROM analyst")

    def test_keys_comments_and_the_view_definition_are_in_the_ddl(self, scanner, deployed):
        ddl = {name: d.lower() for name, d in _scanned(deployed, "analyst", deployed.analyst_password).items()}
        assert set(ddl) == {"clients", "accounts", "v_clients_restricted"}
        assert "primary key" in ddl["accounts"]
        assert "comment on column wealth.accounts.balance" in ddl["accounts"]
        assert "select" in ddl["v_clients_restricted"]

    def test_it_cannot_read_table_rows(self, scanner):
        result = scanner.run("SELECT * FROM wealth.accounts")
        assert result["status"]["code"] == 500
        assert "permission denied" in result["error"]
