#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Fixtures for integration tests against real local services.

Start the services with `docker compose -f docker-compose.test.yml up -d`.
A missing service or password fails the tests that need it: a skip would report
green with nothing checked.
"""

import os
import socket
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.integration import seed

ORACLE_HOST = os.environ.get("TEST_ORACLE_HOST", "localhost")
ORACLE_PORT = int(os.environ.get("TEST_ORACLE_PORT", "51521"))
ORACLE_SERVICE = os.environ.get("TEST_ORACLE_SERVICE", "FREEPDB1")

PG_HOST = os.environ.get("TEST_PG_HOST", "localhost")
PG_PORT = int(os.environ.get("TEST_PG_PORT", "55432"))
PG_DATABASE = os.environ.get("TEST_PG_DATABASE", "postgres")

MYSQL_HOST = os.environ.get("TEST_MYSQL_HOST", "localhost")
MYSQL_PORT = int(os.environ.get("TEST_MYSQL_PORT", "53306"))

MARIADB_HOST = os.environ.get("TEST_MARIADB_HOST", "localhost")
MARIADB_PORT = int(os.environ.get("TEST_MARIADB_PORT", "53307"))

DDB_ENDPOINT = os.environ.get("TEST_DYNAMODB_ENDPOINT", "http://localhost:58000")
MOTO_ENDPOINT = os.environ.get("TEST_MOTO_ENDPOINT", "http://localhost:55000")


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)


# Each container's admin password, from the git-ignored .env.test the containers were
# started with (docker-compose.test.yml). None is written in this repository.
_ENV_TEST = Path(__file__).resolve().parents[4] / ".env.test"
_ADMIN_PASSWORD_KEYS = {"oracle": "ORACLE_PASSWORD", "postgres": "POSTGRES_PASSWORD",
                        "mysql": "MYSQL_ROOT_PASSWORD", "mariadb": "MARIADB_ROOT_PASSWORD"}


def admin_password(engine: str) -> str:
    """The test container's admin password, as .env.test gives it to the container."""
    key = _ADMIN_PASSWORD_KEYS[engine]
    values = dict(line.split("=", 1) for line in _ENV_TEST.read_text().splitlines()
                  if "=" in line) if _ENV_TEST.is_file() else {}
    if not values.get(key):
        pytest.fail(f"{key} is not in {_ENV_TEST}; generate the file as the README's Tests section shows.",
                    pytrace=False)
    return values[key]


def _require(name: str, host: str, port: int) -> None:
    try:
        socket.create_connection((host, port), timeout=1).close()
    except OSError:
        pytest.fail(f"{name} is not reachable at {host}:{port}. Start it with "
                    "`docker compose -f docker-compose.test.yml up -d`.", pytrace=False)


def validator_allowing_the_containers(validate_target):
    """The real host validator, except for the addresses the test containers answer on.

    They listen on 127.0.0.1, which the validator rightly refuses; the tests also reach
    them as localhost in any case. Every other host, including other loopback addresses,
    goes through the real check."""
    def validate(host: str) -> None:
        if host.lower() not in CONTAINER_HOSTS:
            validate_target(host)
    return validate


CONTAINER_HOSTS = {"localhost", "127.0.0.1"}


@pytest.fixture(autouse=True)
def local_containers_have_no_tls(monkeypatch):
    """The compose databases speak plain TCP; production always verifies (see tls.py)."""
    monkeypatch.setenv("DB_TLS_DISABLED", "true")


@dataclass(frozen=True)
class OracleTarget:
    dsn: str
    owner_password: str
    analyst_password: str


@dataclass(frozen=True)
class PostgresTarget:
    host: str
    port: int
    database: str
    owner_password: str
    analyst_password: str


@dataclass(frozen=True)
class MySQLTarget:
    """A MySQL or MariaDB server seeded with seed.mysql_seed; `engine` says which."""
    engine: str
    host: str
    port: int
    database: str
    admin_password: str
    owner_password: str
    analyst_password: str

    def admin(self):
        """A root session, autocommitting, for setting up and undoing grants."""
        import pymysql
        return pymysql.connect(host=self.host, port=self.port, user="root", password=self.admin_password,
                               autocommit=True)


def _wait_until_readable_read_only(dsn: str, timeout: float = 60.0) -> None:
    """Oracle refuses to read a just-created table inside a READ ONLY transaction
    (ORA-01466) until the transaction's snapshot has moved past the DDL. A real
    deployment never reads tables seconds old; the freshly seeded test schema does."""
    import time

    import oracledb

    deadline = time.monotonic() + timeout
    with oracledb.connect(user=seed.OWNER, password=seed.ORACLE_PASSWORDS[seed.OWNER], dsn=dsn) as conn:
        while True:
            try:
                with conn.cursor() as cursor:
                    cursor.execute("SET TRANSACTION READ ONLY")
                    for table in ("clients", "accounts", "audit_log"):
                        cursor.execute(f"SELECT COUNT(*) FROM mcp_dev.{table}")
                        cursor.fetchall()
                conn.rollback()
                return
            except oracledb.DatabaseError as e:
                conn.rollback()
                if "ORA-01466" not in str(e) or time.monotonic() > deadline:
                    raise
                time.sleep(1)


@pytest.fixture(scope="session")
def containers():
    """Held for the whole run. The containers are shared by every checkout on this machine,
    and each run reseeds them with new passwords, so a second run waits for the first
    instead of breaking its logins."""
    import fcntl
    import sys
    import tempfile

    lock = open(Path(tempfile.gettempdir()) / "adam-test-containers.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("Another integration run is using the test containers; waiting for it.", file=sys.stderr)
        fcntl.flock(lock, fcntl.LOCK_EX)
    yield
    lock.close()


@pytest.fixture(scope="session")
def oracle(containers) -> OracleTarget:
    _require("Oracle", ORACLE_HOST, ORACLE_PORT)
    import oracledb

    dsn = f"{ORACLE_HOST}:{ORACLE_PORT}/{ORACLE_SERVICE}"
    with oracledb.connect(user="system", password=admin_password("oracle"), dsn=dsn) as admin:
        with admin.cursor() as cursor:
            for statement in seed.ORACLE_SEED:
                cursor.execute(statement)
        admin.commit()
    _wait_until_readable_read_only(dsn)
    return OracleTarget(dsn, seed.ORACLE_PASSWORDS[seed.OWNER], seed.ORACLE_PASSWORDS[seed.ANALYST])


@pytest.fixture(scope="session")
def postgres(containers) -> PostgresTarget:
    _require("Postgres", PG_HOST, PG_PORT)
    import pg8000.native

    admin = pg8000.native.Connection(user="postgres", password=admin_password("postgres"),
                                     host=PG_HOST, port=PG_PORT, database=PG_DATABASE)
    try:
        for statement in seed.POSTGRES_SEED:
            admin.run(statement)
    finally:
        admin.close()
    return PostgresTarget(PG_HOST, PG_PORT, PG_DATABASE,
                          seed.POSTGRES_PASSWORDS[seed.OWNER], seed.POSTGRES_PASSWORDS[seed.ANALYST])


def _mysql_family(engine: str, host: str, port: int, admin_password: str, statements: list[str]) -> MySQLTarget:
    _require(engine, host, port)
    target = MySQLTarget(engine.lower(), host, port, "wealth", admin_password,
                         seed.MYSQL_PASSWORDS[seed.OWNER], seed.MYSQL_PASSWORDS[seed.ANALYST])
    admin = target.admin()
    try:
        with admin.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
    finally:
        admin.close()
    return target


@pytest.fixture(scope="session")
def mysql(containers) -> MySQLTarget:
    return _mysql_family("MySQL", MYSQL_HOST, MYSQL_PORT, admin_password("mysql"), seed.MYSQL_SEED)


@pytest.fixture(scope="session")
def mariadb(containers) -> MySQLTarget:
    return _mysql_family("MariaDB", MARIADB_HOST, MARIADB_PORT, admin_password("mariadb"), seed.MARIADB_SEED)


@pytest.fixture
def dynamodb_endpoint() -> str:
    host, port = DDB_ENDPOINT.split("//")[1].split(":")
    _require("DynamoDB Local", host, int(port))
    return DDB_ENDPOINT


@pytest.fixture
def moto_endpoint() -> str:
    host, port = MOTO_ENDPOINT.split("//")[1].split(":")
    _require("moto (Secrets Manager)", host, int(port))
    return MOTO_ENDPOINT


class ConnectionRegistryHarness:
    """A real connection registry: rows in DynamoDB Local, secrets in moto, and the
    databases they are for registered in the schema service's own table."""

    def __init__(self, ddb, secrets, table: str):
        self._ddb, self._secrets, self.table = ddb, secrets, table

    def add(self, user_id: str, engine: str, username: str = "", password: str = "",
            targetId: str = "", **location) -> str:
        """Register a connection the way the Connections API does; return its id.

        The row holds the login's secret and which registered database it is for, and
        nothing about the database. Given a location instead of a targetId, the database
        is registered there first (or found, if it already is), as an administrator does.
        """
        import json
        import uuid

        if not targetId:
            targetId = register_database(engine, **location)
        connection_id = str(uuid.uuid4())
        secret_name = f"/test/connections/{user_id}/{connection_id}"
        self._secrets.create_secret(
            Name=secret_name, SecretString=json.dumps({"username": username, "password": password}))
        row = {"userId": user_id, "connectionId": connection_id, "targetId": targetId, "secretName": secret_name}
        self._ddb.put_item(TableName=self.table, Item={k: {"S": v} for k, v in row.items()})
        return connection_id

    def row(self, user_id: str, connection_id: str) -> dict:
        """The connection row as stored: which registered database, and its secret."""
        item = self._ddb.get_item(TableName=self.table,
                                  Key={"userId": {"S": user_id}, "connectionId": {"S": connection_id}})["Item"]
        return {k: v["S"] for k, v in item.items() if k not in ("userId", "connectionId", "createdAt")}

    def rotate(self, user_id: str, connection_id: str, username: str, password: str) -> None:
        import json
        self._secrets.put_secret_value(
            SecretId=f"/test/connections/{user_id}/{connection_id}",
            SecretString=json.dumps({"username": username, "password": password}))


@pytest.fixture
def local_aws(monkeypatch, dynamodb_endpoint, moto_endpoint):
    """Every AWS client the code under test makes goes to DynamoDB Local or moto."""
    for name, value in {
        "AWS_ACCESS_KEY_ID": "test", "AWS_SECRET_ACCESS_KEY": "test",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ENDPOINT_URL_DYNAMODB": dynamodb_endpoint,
        "AWS_ENDPOINT_URL_SECRETS_MANAGER": moto_endpoint,
        "AWS_ENDPOINT_URL_SSM": moto_endpoint,
        # As the stack configures the provider functions: each read assumes the reader role
        # tagged with the row's owner (moto serves STS; it does not evaluate IAM).
        "AWS_ENDPOINT_URL_STS": moto_endpoint,
        "CREDENTIAL_PROVIDER": "secrets_manager_user_scoped",
        "SECRET_READER_ROLE_ARN": "arn:aws:iam::123456789012:role/test-secret-reader",
        "DB_TLS_DISABLED": "true",
    }.items():
        monkeypatch.setenv(name, value)


def register_database(engine: str, dsn: str = "", host: str = "", port="", database: str = "", *,
                      target_id: str = "", name: str = "", limits: dict | None = None) -> str:
    """Register a database as the admin routes write one -- named (its database's name unless
    given), with `limits` (ordinary ones when None; {} for a database an administrator has not
    limited yet) and, for Oracle, no TLS (the test container has no TLS listener) -- unless it
    already is. Returns its target id."""
    from adam.schema_provider import scan_targets
    from adam.schema_provider.source_identity import source_identity

    if dsn:
        host, rest = dsn.split(":", 1)
        port, database = rest.split("/", 1)
    location = {"engine": engine, "host": str(host), "port": str(port), "database": database}
    target_id = target_id or f"t-{engine}-{host}-{port}-{database}"
    if scan_targets.get(target_id) is None:
        scan_targets.create({"targetId": target_id, "lineageId": target_id, "version": 1,
                             "name": name or database, **location,
                             "identity": source_identity(scan_targets.connection({"targetId": target_id, **location})),
                             "scannerSecretArn": "arn:aws:secretsmanager:us-east-1:1:secret:scanner",
                             "scannerSecretManaged": False, "disabled": False, "createdBy": "test-admin",
                             **({"tls": False} if engine == "oracle" else {}),
                             **({"statementTimeoutSeconds": 30, "rowCap": 1000} if limits is None else limits)})
    return target_id


@pytest.fixture
def registry(local_aws, scan_targets_table, monkeypatch):
    """The database provider's connections table in DynamoDB Local, secrets in moto, and
    the schema service a provider reads each connection's registered database from."""
    import uuid

    import boto3

    from adam.schema_provider import glue_data_catalog_schema_provider
    from adam.shared import provider_client
    from tests.integration.lambda_router import LambdaRouter

    table = f"connections-{uuid.uuid4().hex[:8]}"
    ddb = boto3.client("dynamodb")
    ddb.create_table(
        TableName=table, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"},
                              {"AttributeName": "connectionId", "AttributeType": "S"},
                              {"AttributeName": "targetId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"},
                   {"AttributeName": "connectionId", "KeyType": "RANGE"}],
        # As the stack declares it: every user's connection to one database version.
        GlobalSecondaryIndexes=[{"IndexName": "targetId-index",
                                 "KeySchema": [{"AttributeName": "targetId", "KeyType": "HASH"}],
                                 "Projection": {"ProjectionType": "INCLUDE", "NonKeyAttributes": ["secretName"]}}])
    # The registry reads it at call time, as each of the service's functions is given it.
    monkeypatch.setenv("CONNECTIONS_TABLE_NAME", table)
    # As the stack gives the providers the schema service's function; a test that routes
    # more functions replaces this router with its own, which includes it.
    monkeypatch.setenv("SCHEMA_PROVIDER_FUNCTION_NAME", "schema-fn")
    monkeypatch.setattr(provider_client, "_lambda_client",
                        LambdaRouter({"schema-fn": glue_data_catalog_schema_provider.handler}))
    try:
        yield ConnectionRegistryHarness(ddb, boto3.client("secretsmanager"), table)
    finally:
        ddb.delete_table(TableName=table)


@pytest.fixture
def scan_targets_table(local_aws, monkeypatch):
    """The schema service's own scan-target table, keyed on targetId alone."""
    import uuid

    import boto3

    table = f"scan-targets-{uuid.uuid4().hex[:8]}"
    ddb = boto3.client("dynamodb")
    ddb.create_table(
        TableName=table, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "targetId", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "targetId", "KeyType": "HASH"}])
    monkeypatch.setenv("SCAN_TARGETS_TABLE", table)
    try:
        yield table
    finally:
        ddb.delete_table(TableName=table)


@pytest.fixture
def oracle_account_mode(oracle):
    """Switch an Oracle account between READ ONLY and READ WRITE, as a DBA does.

    Whatever a test changed is put back to READ WRITE afterwards.
    """
    import oracledb

    touched: set[str] = set()

    def _alter(user: str, mode: str) -> None:
        with oracledb.connect(user="system", password=admin_password("oracle"), dsn=oracle.dsn) as admin:
            with admin.cursor() as cursor:
                cursor.execute(f"ALTER USER {user} {mode}")

    def set_mode(user: str, read_only: bool) -> None:
        _alter(user, "READ ONLY" if read_only else "READ WRITE")
        touched.add(user)

    yield set_mode
    for user in touched:
        _alter(user, "READ WRITE")


@pytest.fixture
def token_issuer(monkeypatch):
    """The user pool, served locally, as the agent runtime's verifier is configured to trust it.
    `token_issuer.token(sub)` is a genuine access token for the web client."""
    from adam.agent_provider.agent_runtime import agent
    from tests.signed_tokens import Jwks, claims, signed

    server = Jwks()
    server.configure(monkeypatch)
    monkeypatch.setattr(agent, "_identity_provider", None)
    server.token = lambda sub: signed(claims(server.issuer, sub=sub))
    try:
        yield server
    finally:
        server.close()
