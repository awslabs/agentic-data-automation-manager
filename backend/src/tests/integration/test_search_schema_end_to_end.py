#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""search_schema end to end: ingestion fills the index, the dispatcher asks each
caller's own database what they can see, and the index answers only with that.

Real: the ingestion handler and each engine's scanner (moto for scanner logins
and scan results), the connection registry and its function (DynamoDB Local, moto
for secrets and SSM), the scan-target and provider tables, the schema and engine
provider handlers, Oracle and Postgres, and the dispatcher's search and execute
code. Thin stand-ins: the Lambda invoke hop and the scan bus (in-process routers
to the real handlers) and the schema index (an in-memory SchemaProvider; Glue's
SearchAssets has no local equivalent).
"""

import json
import logging
import os
import uuid

import boto3
import pytest

from adam.agent_provider import mcp_handler
from adam.data_source_provider import connection_registry, registry_handler, scanner_handler
from adam.data_source_provider.provider_handler import handler as provider_handler
from adam.schema_provider import (
    glue_data_catalog_schema_provider as schema_module,
    scan_providers,
    scan_targets,
    schema_ingestion_handler,
)
from adam.schema_provider.schema_provider import SchemaMatch, SchemaProvider
from adam.shared import provider_client as package_provider_client, scan_events
from shared import provider_client  # how the dispatcher Lambda imports it
from tests.integration import seed
from tests.integration.conftest import register_database
from tests.integration.lambda_router import LambdaRouter

pytestmark = pytest.mark.integration

ALICE, MALLORY = "user-alice", "user-mallory"


class InMemoryIndex(SchemaProvider):
    """The schema index without Glue: same publish and search contract."""

    def __init__(self):
        self.entries: dict[str, list[SchemaMatch]] = {}

    def _rank(self, query):
        for matches in self.entries.values():
            yield from matches

    def publish_objects(self, *, data_source, engine, objects):
        self.entries[data_source] = [
            SchemaMatch(data_source, engine, t.schema, t.name, t.definition) for t in objects]
        return {"created": len(self.entries[data_source]), "updated": 0, "removed": 0}


def _scan_bus(_client, service, detail_type, detail):
    """Each event to the handler whose rule matches it; a scan request to its engine's scanner."""
    event = {"source": scan_events.source("test", service), "detail-type": detail_type,
             "detail": json.loads(json.dumps(detail)), "account": "123456789012", "region": "us-east-1"}
    if detail_type != scan_events.SCAN_REQUESTED:
        return schema_ingestion_handler.handler(event, None)
    with LambdaRouter._lock:
        saved = os.environ.get("ENGINE")
        os.environ["ENGINE"] = detail["engine"]
        try:
            scanner_handler.handler(event, None)
        finally:
            os.environ["ENGINE"] = saved or ""


@pytest.fixture
def system(registry, scan_targets_table, oracle, postgres, moto_endpoint, monkeypatch):
    index = InMemoryIndex()
    monkeypatch.setattr(schema_module, "provider_from_env", lambda: index)
    monkeypatch.setattr(schema_ingestion_handler, "provider_from_env", lambda: index)
    # Scanning: the provider table, a results bucket, and both engines' scanners registered.
    providers, bucket = f"scan-providers-{uuid.uuid4().hex[:8]}", f"scan-results-{uuid.uuid4().hex[:8]}"
    boto3.client("dynamodb").create_table(
        TableName=providers, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "engine", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "engine", "KeyType": "HASH"}])
    for name, value in {"SCAN_PROVIDERS_TABLE": providers, "SCAN_BUS_NAME": "bus", "SCAN_RESULTS_BUCKET": bucket,
                        "SCANNER_ROLE_NAME": "test-scanner-role", "AWS_ENDPOINT_URL_S3": moto_endpoint}.items():
        monkeypatch.setenv(name, value)
    boto3.client("s3").create_bucket(Bucket=bucket)
    for engine in ("oracle", "postgres"):
        scan_providers.register(engine, f"arn:aws:lambda:us-east-1:123456789012:function:{engine}-scanner",
                                bucket, account="123456789012", region="us-east-1")
    monkeypatch.setattr(scan_events, "publish", _scan_bus)

    ssm = boto3.client("ssm")
    for key, function in {"oracle_provider_function_name": "oracle-fn",
                          "postgresql_provider_function_name": "pg-fn",
                          "schema_provider_function_name": "schema-fn",
                          "connection_registry_function_name": "registry-fn"}.items():
        ssm.put_parameter(Name=f"/test/{key}", Value=function, Type="String", Overwrite=True)
    monkeypatch.setenv("STACK_NAME", "test")
    router = LambdaRouter({
        "oracle-fn": provider_handler, "pg-fn": provider_handler,
        "schema-fn": schema_module.handler, "registry-fn": registry_handler.handler})
    # The dispatcher imports the client flat, the registry function as a package: two copies.
    for client in (provider_client, package_provider_client):
        monkeypatch.setattr(client, "_function_names", {})
        monkeypatch.setattr(client, "_lambda_client", router)
    return index, registry, oracle, postgres


def _target(target_id, name, engine, host, port, database, **limits):
    """Register a scan target as the admin routes write one: named, so users may connect to it."""
    return register_database(engine, host=host, port=port, database=database, target_id=target_id, name=name,
                             limits=limits)


def _registered(engine, host, port, database):
    """Its registration, named as the database, with ordinary limits. Returns the target id."""
    return _target(f"t-{engine}-{host}-{database}", database, engine, host, port, database,
                   statementTimeoutSeconds=30, rowCap=1000)


def _pg(registry, user, postgres, db_user, password, host=None):
    host = host or postgres.host
    return registry.add(user, "postgres", db_user, password, host=host, port=postgres.port,
                        database=postgres.database,
                        targetId=_registered("postgres", host, postgres.port, postgres.database))


def _ora(registry, user, oracle, db_user, password):
    host, rest = oracle.dsn.split(":", 1)
    port, service = rest.split("/", 1)
    target_id = _registered("oracle", host, port, service)
    # Named as before: the tests label Oracle results FREEPDB1.
    scan_targets._put({**scan_targets.get(target_id), "name": "FREEPDB1"})
    return registry.add(user, "oracle", db_user, password, dsn=oracle.dsn, database="FREEPDB1", targetId=target_id)


def _scan(registry, kind, username, password, **where):
    """Register the database for scanning with a scanner login, as an administrator does,
    and scan it through the scan bus and its engine's scanner."""
    if kind == "postgres":
        location = {f: str(where[f]) for f in ("host", "port", "database")}
    else:
        host, rest = where["dsn"].split(":", 1)
        location = dict(zip(("host", "port", "database"), (host, *rest.split("/", 1))))
    target_id = _registered(kind, **location)
    login = boto3.client("secretsmanager").create_secret(
        Name=f"/test/scanners/{uuid.uuid4()}", SecretString=json.dumps({"username": username, "password": password}))
    scan_targets._put({**scan_targets.get(target_id), "scannerSecretArn": login["ARN"]})
    schema_ingestion_handler.handler({"targetId": target_id}, None)
    target = scan_targets.get(target_id)
    assert target["scanStatus"] == "scanned", target["scanMessage"]


def _search(user, database="", query="clients"):
    return json.loads(mcp_handler._handle_search_schema(query, database, user, 8)["content"])


def _names(result):
    return sorted(t["object_name"] for t in result["objects"])


class TestOnlyWhatTheCallerCanSee:

    def test_each_caller_gets_their_own_grants(self, system):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        _pg(registry, MALLORY, postgres, seed.OWNER, postgres.owner_password)

        assert _names(_search(ALICE)) == ["wealth.v_clients_restricted"]
        assert _names(_search(MALLORY)) == ["wealth.accounts", "wealth.audit_log",
                                            "wealth.clients", "wealth.v_clients_restricted"]

    def test_both_engines_are_searched_without_naming_a_database(self, system):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _scan(registry, "oracle", username=seed.OWNER, password=oracle.owner_password,
              dsn=oracle.dsn, database="FREEPDB1")
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        _ora(registry, ALICE, oracle, seed.ANALYST, oracle.analyst_password)

        result = _search(ALICE)
        assert {t["engine"] for t in result["objects"]} == {"postgres", "oracle"}


class TestTheAgentIsToldHowToWriteSqlForEachDatabase:
    """Without this the agent sends LIMIT 1 to Oracle and gets an error."""

    def _both(self, system):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _scan(registry, "oracle", username=seed.OWNER, password=oracle.owner_password,
              dsn=oracle.dsn, database="FREEPDB1")
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        _ora(registry, ALICE, oracle, seed.ANALYST, oracle.analyst_password)
        return postgres

    def test_each_database_in_the_results_comes_with_its_own_dialects_advice(self, system):
        postgres = self._both(system)
        practices = _search(ALICE)["statement_best_practices"]
        assert set(practices) == {"FREEPDB1", postgres.database}
        assert "FETCH FIRST" in practices["FREEPDB1"] and "no LIMIT clause" in practices["FREEPDB1"]
        assert "ILIKE" in practices[postgres.database]

    def test_only_databases_that_appear_in_the_results_are_included(self, system):
        self._both(system)
        practices = _search(ALICE, database="FREEPDB1")["statement_best_practices"]
        assert set(practices) == {"FREEPDB1"}

    def test_nothing_is_attached_when_nothing_matched(self, system):
        index, registry, oracle, postgres = system
        _ora(registry, ALICE, oracle, seed.ANALYST, oracle.analyst_password)  # never scanned
        assert _search(ALICE).get("statement_best_practices", {}) == {}


class TestTheIndexIsKeyedOnWhereTheDatabaseIs:

    def test_a_connection_to_a_different_host_does_not_see_the_indexed_ddl(self, system):
        """The attack: same database name, same table names, different place."""
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _pg(registry, ALICE, postgres, seed.OWNER, postgres.owner_password)
        # Mallory's connection reaches the same engine under another host string. Her
        # database really does have the tables; the index just was not built from it.
        _pg(registry, MALLORY, postgres, seed.OWNER, postgres.owner_password, host="127.0.0.1")

        assert _names(_search(ALICE))
        assert _search(MALLORY)["objects"] == []

    def test_results_are_routed_by_the_callers_own_name_for_the_database(self, system):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)

        (table,) = _search(ALICE)["objects"]
        assert table["data_source"] == postgres.database
        assert f"-- Pass data_source={postgres.database} to execute_statement" in table["definition"]
        assert "postgres://" not in json.dumps(table), "the internal identity must not reach the agent"

    def test_the_routing_name_returned_actually_runs_a_query(self, system):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)

        (table,) = _search(ALICE)["objects"]
        out = json.loads(mcp_handler._handle_execute_statement(
            table["data_source"], f"SELECT id, name FROM {table['object_name']} ORDER BY id", ALICE)["content"])
        assert [r["name"] for r in out["rows"]] == ["Ada", "Grace"]

    def test_neither_the_statement_nor_the_search_is_logged(self, system, caplog):
        """What a user asks and the rows it touches are theirs: the data source logs its own
        queries if its owner turns that on. The dispatcher logs who ran what where."""
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        asked = "clients"
        statement_marker = "statement_marker_7f3a"
        with caplog.at_level(logging.DEBUG):
            (table,) = _search(ALICE, query=asked)["objects"]
            out = json.loads(mcp_handler._handle_execute_statement(
                table["data_source"], f"SELECT id, name AS {statement_marker} FROM {table['object_name']}",
                ALICE)["content"])
        assert "rows" in out
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert statement_marker not in logged
        assert f"query={asked}" not in logged and f"'{asked}'" not in logged
        assert ALICE in logged  # the audit line: who ran it

    def test_both_tools_answer_the_event_the_gateway_sends(self, system):
        """Through the dispatcher's entry point, with the argument names the tool schema
        advertises and the tool name the gateway puts in the client context."""
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from adam.agent_provider.gateway_event import SUBJECT_ARGUMENT, UNTRUSTED_NOTICE
        from cdk_infra.agent_provider.gateway_tool_schema import TOOLS

        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        advertised = {t["name"]: set(t["inputSchema"]["properties"]) for t in TOOLS}

        def call(tool, **arguments):
            event = {**arguments, SUBJECT_ARGUMENT: ALICE}
            assert set(event) <= advertised[tool], "the gateway forwards only advertised arguments"
            context = type("Context", (), {"client_context": type("ClientContext", (), {
                "custom": {"bedrockAgentCoreToolName": f"target___{tool}"}})()})()
            return json.loads(mcp_handler.handler(event, context)["content"])

        found = call("search_schema", query="clients", limit=8)
        (table,) = found["objects"]
        out = call("execute_statement", data_source=table["data_source"],
                   statement=f"SELECT id, name FROM {table['object_name']} ORDER BY id")
        assert [r["name"] for r in out["rows"]] == ["Ada", "Grace"]
        # Definitions and rows are text no one here wrote: each result says it is data.
        assert found["notice"] == out["notice"] == UNTRUSTED_NOTICE

    def test_the_registry_gives_the_dispatcher_no_secret_and_no_owner(self, system):
        """The dispatcher needs where a connection points and its limits; the secret's
        name and the owning user stay inside the registry."""
        index, registry, oracle, postgres = system
        connection_id = _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        (described,) = registry_handler.handler(
            {"operation": "list_connections", "args": {"user_id": ALICE}}, None)["response"]
        assert described["connectionId"] == connection_id
        assert set(described) == {"connectionId", "engine", "host", "port", "database", "dsn", "identity",
                                  "targetId", "name", "statementTimeoutSeconds", "rowCap"}

    @pytest.mark.parametrize("tls, reached", [(True, True), (False, False)])
    def test_an_oracle_statement_follows_its_databases_tls_setting(self, system, monkeypatch, tmp_path, tls, reached):
        """Registry, dispatcher and provider, to a port that speaks only TLS: it answers
        when the database's saved setting says TLS, and plaintext gets nothing."""
        from tests.integration.tls_proxy import tls_listener

        index, registry, oracle, postgres = system
        host, rest = oracle.dsn.split(":", 1)
        port, service = rest.split("/", 1)
        monkeypatch.delenv("DB_TLS_DISABLED")
        with tls_listener(host, int(port), tmp_path) as listener:
            monkeypatch.setenv("DB_TLS_CA_FILE", listener.ca_file)
            target_id = _target("t-tls", "Banking", "oracle", listener.host, listener.port, service,
                                statementTimeoutSeconds=30, rowCap=1000, tls=tls)
            registry.add(ALICE, "oracle", seed.ANALYST, oracle.analyst_password,
                         dsn=f"{listener.host}:{listener.port}/{service}", database=service, targetId=target_id)
            out = json.loads(mcp_handler._handle_execute_statement("Banking", "SELECT 1 AS one FROM dual",
                                                                   ALICE)["content"])
        assert ("rows" in out) is reached, out
        assert bool(listener.handshakes) is reached

    @pytest.mark.parametrize("pinned, host, reached", [
        ("own", "localhost", True),         # the certificate it presents
        # The same certificate at an address it does not name: what a SCAN listener's hand-off to
        # a node by its IP looks like, so it must be accepted. Trust is the certificate itself.
        ("own", "127.0.0.1", True),
        ("another", "localhost", False),    # the same name, another key: not the database
    ])
    def test_an_oracle_statement_trusts_only_the_pinned_certificate(self, system, monkeypatch, tmp_path,
                                                                    pinned, host, reached):
        from tests.integration.tls_proxy import _self_signed, tls_listener

        index, registry, oracle, postgres = system
        db_host, rest = oracle.dsn.split(":", 1)
        port, service = rest.split("/", 1)
        monkeypatch.delenv("DB_TLS_DISABLED")
        monkeypatch.setenv("DB_TLS_CA_FILE", str(__import__("certifi").where()))  # trusts neither
        with tls_listener(db_host, int(port), tmp_path, self_signed=True) as listener:
            (tmp_path / "other").mkdir()
            certificate = listener.certificate if pinned == "own" else _self_signed(tmp_path / "other")[1].read_text()
            target_id = _target("t-pinned", "Banking", "oracle", host, listener.port, service,
                                statementTimeoutSeconds=30, rowCap=1000, tls=True, tlsCertificate=certificate)
            registry.add(ALICE, "oracle", seed.ANALYST, oracle.analyst_password,
                         dsn=f"{host}:{listener.port}/{service}", database=service, targetId=target_id)
            out = json.loads(mcp_handler._handle_execute_statement("Banking", "SELECT 1 AS one FROM dual",
                                                                   ALICE)["content"])
        assert ("rows" in out) is reached, out

    def test_a_name_the_caller_has_no_connection_for_is_refused(self, system):
        index, registry, oracle, postgres = system
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        out = json.loads(mcp_handler._handle_execute_statement("payroll", "SELECT 1", ALICE)["content"])
        assert out["error"].startswith("No connection named 'payroll'") and postgres.database in out["error"]

    def test_a_result_for_a_source_the_caller_did_not_search_is_dropped(self, system, monkeypatch):
        """Defence in depth: the schema service should never answer for such a source."""
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        dispatched = mcp_handler.call_provider

        def schema_service_answering_for_another_source(key, operation, args):
            answer = dispatched(key, operation, args)
            if operation == "search_objects":
                answer += [{**answer[0], "data_source": "postgres://other.example.com:5432/payroll",
                            "object_name": "payroll.salaries"}]
            return answer
        monkeypatch.setattr(mcp_handler, "call_provider", schema_service_answering_for_another_source)

        result = _search(ALICE)
        assert _names(result) == ["wealth.v_clients_restricted"]
        assert "other.example.com" not in json.dumps(result)

    def test_a_name_that_matches_two_of_the_callers_connections_runs_nothing(self, system, monkeypatch):
        """Picking either would run the statement as a login the caller did not choose."""
        index, registry, oracle, postgres = system
        first = _pg(registry, ALICE, postgres, seed.OWNER, postgres.owner_password)
        second = _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        engines = []
        dispatched = mcp_handler.call_provider
        monkeypatch.setattr(mcp_handler, "call_provider", lambda key, operation, args: (
            engines.append(key) if operation == "execute_statement" else None) or dispatched(key, operation, args))

        out = json.loads(mcp_handler._handle_execute_statement(
            postgres.database, "SELECT current_user AS who", ALICE)["content"])
        assert first in out["error"] and second in out["error"], out
        assert "rows" not in out and engines == []

    def test_a_registered_database_is_known_by_the_name_an_administrator_gave_it(self, system):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _target("t-wealth", "Wealth", "postgres", postgres.host, postgres.port, postgres.database,
                statementTimeoutSeconds=30, rowCap=1000)
        registry.add(ALICE, "postgres", seed.ANALYST, postgres.analyst_password, host=postgres.host,
                     port=postgres.port, database=postgres.database, targetId="t-wealth")
        (table,) = _search(ALICE)["objects"]
        assert table["data_source"] == "Wealth"
        assert "-- Pass data_source=Wealth to execute_statement" in table["definition"]
        # The agent may write it in any case.
        out = json.loads(mcp_handler._handle_execute_statement(
            "wealth", f"SELECT id, name FROM {table['object_name']} ORDER BY id", ALICE)["content"])
        assert [r["name"] for r in out["rows"]] == ["Ada", "Grace"]

    def test_the_agent_is_told_each_databases_limits_and_they_are_applied(self, system):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _target("t-capped", "Capped", "postgres", postgres.host, postgres.port, postgres.database,
                statementTimeoutSeconds=7, rowCap=1)
        registry.add(ALICE, "postgres", seed.ANALYST, postgres.analyst_password, host=postgres.host,
                     port=postgres.port, database=postgres.database, targetId="t-capped")
        (table,) = _search(ALICE)["objects"]
        assert "cancelled after 7 seconds and returns at most 1 rows" in table["definition"]
        out = json.loads(mcp_handler._handle_execute_statement(
            "Capped", f"SELECT id FROM {table['object_name']}", ALICE)["content"])
        assert len(out["rows"]) == 1 and out["truncated"] is True
        # A second database with its own, larger cap: each call gets its own database's limits.
        _target("t-roomy", "Roomy", "postgres", "127.0.0.1", postgres.port, postgres.database,
                statementTimeoutSeconds=30, rowCap=1000)
        registry.add(ALICE, "postgres", seed.ANALYST, postgres.analyst_password, host="127.0.0.1",
                     port=postgres.port, database=postgres.database, targetId="t-roomy")
        out = json.loads(mcp_handler._handle_execute_statement(
            "Roomy", f"SELECT id FROM {table['object_name']}", ALICE)["content"])
        assert len(out["rows"]) == 2 and out["truncated"] is False

    def test_a_capped_result_says_how_to_get_the_rest_and_the_next_page_completes_it(self, system):
        index, registry, oracle, postgres = system
        _target("t-paged", "Paged", "postgres", postgres.host, postgres.port, postgres.database,
                statementTimeoutSeconds=30, rowCap=1)
        registry.add(ALICE, "postgres", seed.OWNER, postgres.owner_password, host=postgres.host,
                     port=postgres.port, database=postgres.database, targetId="t-paged")
        statement = "SELECT id, name FROM wealth.clients ORDER BY id"
        first = json.loads(mcp_handler._handle_execute_statement("Paged", statement, ALICE)["content"])
        assert first["truncated"] is True and first["next_start_row"] == 1
        assert "start_row=1" in first["message"]
        second = json.loads(mcp_handler._handle_execute_statement(
            "Paged", statement, ALICE, start_row=first["next_start_row"])["content"])
        assert [r["name"] for r in first["rows"] + second["rows"]] == ["Ada", "Grace"]
        assert second["truncated"] is False and "message" not in second

    def test_max_rows_above_the_databases_cap_is_refused(self, system):
        index, registry, oracle, postgres = system
        _target("t-small", "Small", "postgres", postgres.host, postgres.port, postgres.database,
                statementTimeoutSeconds=30, rowCap=10)
        registry.add(ALICE, "postgres", seed.ANALYST, postgres.analyst_password, host=postgres.host,
                     port=postgres.port, database=postgres.database, targetId="t-small")
        result = json.loads(mcp_handler._handle_execute_statement(
            "Small", "SELECT 1", ALICE, max_rows=11)["content"])
        assert "max_rows" in result["error"] and "row cap for Small is 10" in result["error"]

    def test_a_database_without_limits_refuses_statements_and_says_why(self, system):
        index, registry, oracle, postgres = system
        _target("t-unset", "Unset", "postgres", postgres.host, postgres.port, postgres.database)
        registry.add(ALICE, "postgres", seed.ANALYST, postgres.analyst_password, host=postgres.host,
                     port=postgres.port, database=postgres.database, targetId="t-unset")
        result = mcp_handler._handle_execute_statement("Unset", "SELECT 1", ALICE)
        assert "no statement timeout and row cap set" in result["content"]


class TestWhatTheDispatcherDoesWithEachCallersDatabases:

    def _scanned(self, system):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        return registry, postgres

    def test_a_caller_with_no_connections_finds_nothing(self, system):
        self._scanned(system)
        assert _search("user-with-nothing")["objects"] == []

    def test_a_database_that_cannot_answer_contributes_nothing_and_is_named(self, system):
        registry, postgres = self._scanned(system)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        _pg(registry, ALICE, postgres, seed.ANALYST, "the-wrong-password", host="127.0.0.1")

        result = _search(ALICE)
        assert _names(result) == ["wealth.v_clients_restricted"]
        assert [u["data_source"] for u in result["unavailable"]] == [postgres.database]
        assert result["unavailable"][0]["reason"]

    def test_naming_one_database_asks_only_that_one(self, system):
        registry, postgres = self._scanned(system)
        good = _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        _pg(registry, ALICE, postgres, seed.ANALYST, "the-wrong-password", host="127.0.0.1")

        result = _search(ALICE, database=good)
        assert _names(result) == ["wealth.v_clients_restricted"]
        assert "unavailable" not in result

    def test_two_connections_with_one_name_are_told_apart_by_connection_id(self, system):
        """execute_statement refuses a name that matches two connections, so the result
        must hand back something that matches exactly one."""
        registry, postgres = self._scanned(system)
        good = _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password, host="127.0.0.1")

        (table,) = _search(ALICE)["objects"]
        assert table["data_source"] == good
        out = json.loads(mcp_handler._handle_execute_statement(
            table["data_source"], f"SELECT id FROM {table['object_name']}", ALICE)["content"])
        assert len(out["rows"]) == 2


class TestTheDispatcherReadsNoTable:
    """The dispatcher's function is given no table and cannot reach DynamoDB; each
    provider function gets only what its own stack gives it."""

    def test_search_and_execute_go_through_the_registry_function(self, system, monkeypatch):
        index, registry, oracle, postgres = system
        _scan(registry, "postgres", username=seed.OWNER, password=postgres.owner_password,
              host=postgres.host, port=postgres.port, database=postgres.database)
        _pg(registry, ALICE, postgres, seed.ANALYST, postgres.analyst_password)

        dynamodb = {"AWS_ENDPOINT_URL_DYNAMODB": os.environ["AWS_ENDPOINT_URL_DYNAMODB"]}
        connections = {**dynamodb, "CONNECTIONS_TABLE_NAME": os.environ["CONNECTIONS_TABLE_NAME"]}
        router = provider_client._lambda_client
        router.environments = {"registry-fn": connections, "pg-fn": connections, "oracle-fn": connections,
                               "schema-fn": {**dynamodb, "SCAN_TARGETS_TABLE": os.environ["SCAN_TARGETS_TABLE"]}}
        monkeypatch.delenv("CONNECTIONS_TABLE_NAME")
        monkeypatch.delenv("SCAN_TARGETS_TABLE")
        monkeypatch.setenv("AWS_ENDPOINT_URL_DYNAMODB", "http://127.0.0.1:9")  # nothing listens
        with pytest.raises(RuntimeError, match="CONNECTIONS_TABLE_NAME"):
            connection_registry.list_user_connections(ALICE)
        router.invoked.clear()

        (table,) = _search(ALICE)["objects"]
        out = json.loads(mcp_handler._handle_execute_statement(
            table["data_source"], f"SELECT id, name FROM {table['object_name']} ORDER BY id", ALICE)["content"])
        assert [r["name"] for r in out["rows"]] == ["Ada", "Grace"]
        assert "registry-fn" in router.invoked
