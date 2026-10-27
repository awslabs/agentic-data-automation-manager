#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Scanning, end to end: the schema service asks on the scan bus, a database
provider's scanner scans, and the schema service indexes what it reports.

Real: both handlers, the scanner's provider code, Postgres, DynamoDB Local (the
scan-target and provider tables) and moto (Secrets Manager and S3). The bus is
the one stand-in: each published event is handed to the handler whose rule
matches it, as EventBridge would. The rules themselves are checked against the
synthesized templates in tests/unit/test_cdk_security.py. The Glue index is a
recorder, as everywhere outside AWS.

What must hold: a scan is requested only for a registered target, and only
through the provider registered for its engine; the scanner logs in only with a
login under the scanner path; and a result is indexed only for the scan the
target is still waiting on, only from that provider's bucket.
"""

import json
import uuid

import boto3
import pytest

from adam.data_source_provider import registration_handler, scanner_handler
from adam.schema_provider import scan_providers, scan_targets, schema_ingestion_handler
from adam.schema_provider.source_identity import source_identity
from adam.shared import scan_events
from tests.integration import seed

pytestmark = pytest.mark.integration

STACK = "test"
ACCOUNT, REGION = "123456789012", "us-east-1"
SCANNER_ARN = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:test-PostgresScanner"
PUBLISH = scan_events.publish  # the real one; `world` replaces it with the in-process bus


class _Index:
    def __init__(self):
        self.published = None

    def publish_objects(self, *, data_source, engine, objects):
        self.published = (data_source, engine, list(objects))
        return {"created": len(self.published[2]), "updated": 0, "removed": 0, "kept": 0}


class _Bus:
    """Delivers each event to the handler whose rule matches it, as EventBridge does,
    and keeps what was published. `hold` keeps answers back, to deliver them later."""

    def __init__(self):
        self.published, self.held, self.hold = [], [], False

    def event(self, service, detail_type, detail):
        return {"source": scan_events.source(STACK, service), "detail-type": detail_type,
                "detail": json.loads(json.dumps(detail)), "account": ACCOUNT, "region": REGION}

    def publish(self, _client, service, detail_type, detail):
        event = self.event(service, detail_type, detail)
        self.published.append(event)
        if detail_type == scan_events.SCAN_REQUESTED:
            scanner_handler.handler(event, None)
        elif self.hold:
            self.held.append(event)
        else:
            schema_ingestion_handler.handler(event, None)


def _table(name: str, key: str) -> str:
    table = f"{name}-{uuid.uuid4().hex[:8]}"
    boto3.client("dynamodb").create_table(
        TableName=table, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"}],
        KeySchema=[{"AttributeName": key, "KeyType": "HASH"}])
    return table


@pytest.fixture
def world(scan_targets_table, postgres, moto_endpoint, monkeypatch):
    """A registered Postgres provider and a scanner login for the real database."""
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", moto_endpoint)
    providers = _table("scan-providers", "engine")
    bucket = f"scan-results-{uuid.uuid4().hex[:8]}"
    boto3.client("s3").create_bucket(Bucket=bucket)
    for name, value in {"STACK_NAME": STACK, "SCAN_PROVIDERS_TABLE": providers, "SCAN_BUS_NAME": "bus",
                        "ENGINE": "postgres", "SCAN_RESULTS_BUCKET": bucket,
                        "SCANNER_ROLE_NAME": "the-scanner-role"}.items():
        monkeypatch.setenv(name, value)
    index, bus = _Index(), _Bus()
    monkeypatch.setattr(schema_ingestion_handler, "provider_from_env", lambda: index)
    monkeypatch.setattr(scan_events, "publish", bus.publish)
    scan_providers.register("postgres", SCANNER_ARN, bucket, account=ACCOUNT, region=REGION)
    login = scanner_handler.handler({"operation": "store_login", "args": {
        "identity": "postgres", "username": seed.OWNER, "password": postgres.owner_password}}, None)
    location = {"host": postgres.host, "port": str(postgres.port), "database": postgres.database}

    def register(**fields) -> dict:
        entry = {"targetId": str(uuid.uuid4()), "engine": "postgres", **location,
                 "scannerSecretArn": login["response"]["scannerSecretArn"], "scannerSecretManaged": True,
                 "disabled": False, "createdBy": "test-admin", **fields}
        entry.setdefault("lineageId", entry["targetId"])
        entry.setdefault("version", 1)
        entry.setdefault("identity", source_identity(scan_targets.connection(entry)))
        scan_targets.create(entry)
        return entry

    try:
        yield {"index": index, "bus": bus, "bucket": bucket, "register": register, "location": location,
               "secret": login["response"]["scannerSecretArn"], "postgres": postgres}
    finally:
        boto3.client("dynamodb").delete_table(TableName=providers)


def _status(target_id: str) -> tuple[str, str]:
    target = scan_targets.get(target_id)
    return target["scanStatus"], target["scanMessage"]


class TestARegisteredTargetIsScannedByItsProvider:

    def test_the_scan_is_indexed_under_where_the_database_is(self, world):
        target = world["register"]()
        schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        database, engine, tables = world["index"].published
        assert (database, engine) == (target["identity"], "postgres")
        assert {f"{t.schema}.{t.name}" for t in tables} >= {"wealth.clients", "wealth.accounts"}
        assert _status(target["targetId"]) == ("scanned", f"Scanned {len(tables)} object(s).")

    def test_the_request_carries_the_targets_location_and_login_and_nothing_else(self, world):
        target = world["register"]()
        schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        (request,) = [e for e in world["bus"].published if e["detail-type"] == scan_events.SCAN_REQUESTED]
        assert request["source"] == f"{STACK}.schema-service"
        assert set(request["detail"]) == {"requestId", "targetId", "engine", "location", "scannerSecretArn"}
        assert request["detail"]["location"] == world["location"]
        assert request["detail"]["scannerSecretArn"] == world["secret"]

    def test_the_result_is_left_in_the_providers_bucket_under_the_request(self, world):
        target = world["register"]()
        requested = schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        key = scan_events.scan_result_key(target["targetId"], requested["requestId"])
        body = boto3.client("s3").get_object(Bucket=world["bucket"], Key=key)["Body"].read()
        assert len(json.loads(body)) == len(world["index"].published[2])


class TestOnlyRegisteredTargetsAreScanned:

    def test_an_unknown_target_is_refused_before_anything_is_published(self, world):
        with pytest.raises(schema_ingestion_handler.ScanRefused, match="not a registered scan target"):
            schema_ingestion_handler.handler({"targetId": "t-unknown"}, None)
        assert world["bus"].published == [] and world["index"].published is None

    def test_a_disabled_version_is_refused(self, world):
        target = world["register"](disabled=True)
        with pytest.raises(schema_ingestion_handler.ScanRefused, match="is disabled"):
            schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        assert world["bus"].published == []

    def test_an_engine_with_no_registered_provider_is_refused_and_says_so(self, world):
        """EventBridge would drop the request silently; the admin page must say why."""
        target = world["register"]()
        scan_providers.deregister("postgres", scan_providers.get("postgres")["scannerFunctionArn"])
        with pytest.raises(schema_ingestion_handler.ScanRefused, match="No postgres database provider"):
            schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        assert world["bus"].published == []
        assert _status(target["targetId"])[0] == "failed"

    def test_an_item_that_is_not_a_scan_target_is_an_error_not_permission(self, world, scan_targets_table):
        boto3.client("dynamodb").put_item(TableName=scan_targets_table, Item={
            "targetId": {"S": "x"}, "identity": {"S": "postgres://h:1/d"}})
        with pytest.raises(RuntimeError, match="not a scan target"):
            schema_ingestion_handler.handler({"targetId": "x"}, None)


class TestTheScannerLogsInOnlyAsAScanner:

    def test_a_wrong_password_fails_the_scan_with_the_databases_reason(self, world):
        target = world["register"]()
        boto3.client("secretsmanager").put_secret_value(
            SecretId=world["secret"], SecretString=json.dumps({"username": seed.OWNER, "password": "wrong"}))
        schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        status, message = _status(target["targetId"])
        assert status == "failed" and "password" in message
        assert world["index"].published is None

    @pytest.mark.parametrize("name", [f"/{STACK}/connections/user-1/c-1", "/dba/scanner", f"/{STACK}/scannersX/s"])
    def test_a_target_naming_any_other_secret_is_refused_before_it_is_read(self, world, name, monkeypatch):
        """A target pointing at a user's connection secret must not make the scanner read it."""
        from adam.data_source_provider import secrets_manager_credential_provider as smcp

        read, real = [], smcp.SecretsManagerPasswordProvider.credential_for
        monkeypatch.setattr(smcp.SecretsManagerPasswordProvider, "credential_for",
                            lambda self, row: read.append(row["secretName"]) or real(self, row))
        other = boto3.client("secretsmanager").create_secret(
            Name=name + uuid.uuid4().hex[:6], SecretString=json.dumps({"username": seed.OWNER, "password": "x"}))
        target = world["register"](scannerSecretArn=other["ARN"])
        schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        assert read == []  # the scanner never asked for it
        status, message = _status(target["targetId"])
        assert status == "failed" and f"/{STACK}/scanners/" in message
        assert world["index"].published is None

    def test_a_request_for_another_engine_is_refused(self, world):
        target = world["register"]()
        event = world["bus"].event(scan_events.SCHEMA_SERVICE, scan_events.SCAN_REQUESTED, {
            "requestId": "r-1", "targetId": target["targetId"], "engine": "oracle",
            "location": world["location"], "scannerSecretArn": world["secret"]})
        scanner_handler.handler(event, None)
        (answer,) = world["bus"].published
        assert answer["detail-type"] == scan_events.SCAN_FAILED and "postgres scanner" in answer["detail"]["message"]

    def test_the_login_check_works_and_reports_the_databases_reason(self, world):
        check = {"operation": "probe", "args": {"location": world["location"], "scannerSecretArn": world["secret"]}}
        assert scanner_handler.handler(check, None) == {"response": {"ok": True}}
        boto3.client("secretsmanager").put_secret_value(
            SecretId=world["secret"], SecretString=json.dumps({"username": seed.OWNER, "password": "wrong"}))
        result = scanner_handler.handler(check, None)
        assert "could not log in" in result["error"] and "password" in result["error"]


class TestScannerLogins:

    def test_a_stored_login_is_under_the_scanner_path_and_can_be_removed(self, world):
        stored = scanner_handler.handler({"operation": "store_login", "args": {
            "identity": "x", "username": "u", "password": "p"}}, None)["response"]["scannerSecretArn"]
        assert f":secret:/{STACK}/scanners/" in stored
        scanner_handler.handler({"operation": "remove_login", "args": {"scannerSecretArn": stored}}, None)
        with pytest.raises(boto3.client("secretsmanager").exceptions.ResourceNotFoundException):
            boto3.client("secretsmanager").describe_secret(SecretId=stored)

    def test_removing_anything_outside_the_scanner_path_is_refused(self, world):
        other = boto3.client("secretsmanager").create_secret(
            Name=f"/{STACK}/connections/u/c-{uuid.uuid4().hex[:6]}", SecretString="{}")["ARN"]
        result = scanner_handler.handler({"operation": "remove_login", "args": {"scannerSecretArn": other}}, None)
        assert f"/{STACK}/scanners/" in result["error"]
        boto3.client("secretsmanager").describe_secret(SecretId=other)

    @pytest.mark.parametrize("args", [{"username": "u"}, {"password": "p"}, {"username": " ", "password": "p"}])
    def test_half_a_login_is_refused(self, world, args):
        assert "both" in scanner_handler.handler({"operation": "store_login", "args": args}, None)["error"]


class TestOnlyTheAwaitedAnswerIsIndexed:

    def _request(self, world):
        world["bus"].hold = True
        target = world["register"]()
        requested = schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        (answer,) = world["bus"].held
        world["bus"].held.clear()
        return target, requested["requestId"], answer

    def test_an_answer_overtaken_by_a_newer_request_is_ignored(self, world):
        target, _, stale = self._request(world)
        schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)
        world["bus"].held.clear()
        schema_ingestion_handler.handler(stale, None)
        assert world["index"].published is None
        assert _status(target["targetId"]) == ("scanning", "Scanning.")

    @pytest.mark.parametrize("field,value", [("bucket", "someone-elses-bucket"), ("key", "scans/other/key.json")])
    def test_a_result_anywhere_but_the_providers_bucket_at_the_requests_key_is_refused(self, world, field, value):
        target, _, answer = self._request(world)
        answer["detail"][field] = value
        schema_ingestion_handler.handler(answer, None)
        assert world["index"].published is None
        assert _status(target["targetId"]) == (
            "failed", "The scan result did not come from the registered provider's results bucket.")

    def test_a_result_that_is_not_scanned_tables_fails_the_scan(self, world):
        target, request_id, answer = self._request(world)
        boto3.client("s3").put_object(Bucket=world["bucket"], Body=b'[{"schema": "s"}]',
                                      Key=scan_events.scan_result_key(target["targetId"], request_id))
        schema_ingestion_handler.handler(answer, None)
        assert world["index"].published is None and _status(target["targetId"])[0] == "failed"

    def test_an_answer_from_any_other_source_is_refused(self, world):
        target, _, answer = self._request(world)
        answer["source"] = f"{STACK}.schema-service"
        with pytest.raises(schema_ingestion_handler.ScanRefused, match="not taken"):
            schema_ingestion_handler.handler(answer, None)
        assert world["index"].published is None

    def test_a_reported_failure_is_recorded(self, world):
        target, request_id, _ = self._request(world)
        schema_ingestion_handler.handler(world["bus"].event(
            scan_events.DATABASE_PROVIDER, scan_events.SCAN_FAILED,
            {"requestId": request_id, "targetId": target["targetId"], "engine": "postgres", "message": "ORA-1"}), None)
        assert _status(target["targetId"]) == ("failed", "ORA-1")


class TestRegistration:

    def test_a_provider_stacks_deploy_and_removal_register_and_remove_its_scanner(self, world, monkeypatch):
        """The provider stack's custom resource publishes; ingestion receives what EventBridge
        would deliver for that PutEvents entry. Only the EventBridge API is replaced."""
        entries = []

        class _EventBridge:
            def put_events(self, Entries):
                entries.extend(Entries)
                return {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}

        monkeypatch.setattr(scan_events, "publish", PUBLISH)
        client = boto3.client  # registration_handler.boto3 is the one boto3 module
        monkeypatch.setattr(boto3, "client", lambda service, **kw: _EventBridge() if service == "events"
                            else client(service, **kw))

        def deploy(request_type):
            entries.clear()
            registration_handler.handler({"RequestType": request_type, "ResourceProperties": {
                "Engine": "oracle", "ScannerFunctionArn": arn, "ResultsBucket": world["bucket"]}}, None)
            (entry,) = entries
            schema_ingestion_handler.handler({"source": entry["Source"], "detail-type": entry["DetailType"],
                                              "detail": json.loads(entry["Detail"]),
                                              "account": ACCOUNT, "region": REGION}, None)

        arn = f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:test-OracleScanner"
        deploy("Create")
        assert scan_providers.get("oracle")["scannerFunctionArn"] == arn
        deploy("Delete")
        assert scan_providers.get("oracle") is None

    def test_removing_a_replaced_stack_keeps_its_successors_scanner(self, world):
        """A renamed stack is deployed before the old one is deleted. The old one's
        deregistration names its own scanner, so the successor's stays registered."""
        bus = world["bus"]
        old, new = (f"arn:aws:lambda:{REGION}:{ACCOUNT}:function:test-{n}OracleScanner" for n in ("Old", "New"))
        for arn in (old, new):
            schema_ingestion_handler.handler(bus.event(scan_events.DATABASE_PROVIDER, scan_events.PROVIDER_REGISTERED, {
                "engine": "oracle", "scannerFunctionArn": arn, "resultsBucket": world["bucket"]}), None)
        schema_ingestion_handler.handler(bus.event(scan_events.DATABASE_PROVIDER, scan_events.PROVIDER_DEREGISTERED,
                                                   {"engine": "oracle", "scannerFunctionArn": old}), None)
        assert scan_providers.get("oracle")["scannerFunctionArn"] == new

    @pytest.mark.parametrize("arn", [
        "arn:aws:lambda:us-east-1:111122223333:function:elsewhere",
        "arn:aws:lambda:eu-west-1:123456789012:function:elsewhere",
        "arn:aws:iam::123456789012:role/not-a-function"])
    def test_a_function_outside_this_account_and_region_is_refused(self, world, arn):
        """Or a forged registration could point the admin routes, and their passwords, elsewhere."""
        event = world["bus"].event(scan_events.DATABASE_PROVIDER, scan_events.PROVIDER_REGISTERED, {
            "engine": "oracle", "scannerFunctionArn": arn, "resultsBucket": world["bucket"]})
        with pytest.raises(ValueError, match="not a function in account"):
            schema_ingestion_handler.handler(event, None)
        assert scan_providers.get("oracle") is None

    def test_a_registration_from_any_other_source_is_refused(self, world):
        event = world["bus"].event(scan_events.SCHEMA_SERVICE, scan_events.PROVIDER_REGISTERED, {
            "engine": "oracle", "scannerFunctionArn": SCANNER_ARN, "resultsBucket": world["bucket"]})
        with pytest.raises(schema_ingestion_handler.ScanRefused, match="not taken"):
            schema_ingestion_handler.handler(event, None)
        assert scan_providers.get("oracle") is None


class TestPurgingADatabaseThatMoved:

    def test_its_index_entries_are_replaced_with_nothing(self, world):
        result = schema_ingestion_handler.handler(
            {"purge": {"identity": "POSTGRES://Old:5432/d", "engine": "postgres"}}, None)
        assert world["index"].published == ("postgres://old:5432/d", "postgres", [])
        assert result["database"] == "postgres://old:5432/d"

    def test_a_registered_database_is_never_purged(self, world):
        world["register"](identity="postgres://old:5432/d")
        with pytest.raises(schema_ingestion_handler.ScanRefused, match="registered"):
            schema_ingestion_handler.handler(
                {"purge": {"identity": "postgres://old:5432/d", "engine": "postgres"}}, None)
        assert world["index"].published is None

    def test_a_database_whose_only_version_is_disabled_is_purged(self, world):
        world["register"](identity="postgres://old:5432/d", disabled=True)
        schema_ingestion_handler.handler({"purge": {"identity": "postgres://old:5432/d", "engine": "postgres"}}, None)
        assert world["index"].published == ("postgres://old:5432/d", "postgres", [])
