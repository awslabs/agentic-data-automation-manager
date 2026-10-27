#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""execute_statement is read-only because the provider enforces it, not because
the statement happened to start with SELECT.

Run on real Oracle and Postgres, as the schema OWNER, who holds full DML rights
(see seed.py). If a hostile statement fails to change anything here, the
provider stopped it. Every statement below starts with SELECT or WITH, so the
old prefix check let all of them through.
"""

import pytest

from adam.data_source_provider.credential_provider import Credential
from adam.data_source_provider.oracle_provider import OracleProvider
from adam.data_source_provider.postgresql_provider import PostgreSQLProvider
from tests.integration import seed

pytestmark = pytest.mark.integration


def _reset(name, target):
    """Undo whatever a test did, using the engine's own driver, not the provider."""
    if name == "oracle":
        import oracledb
        with oracledb.connect(user=seed.OWNER, password=target.owner_password, dsn=target.dsn) as c:
            with c.cursor() as cur:
                cur.execute("DELETE FROM mcp_dev.audit_log")
                cur.execute("INSERT INTO mcp_dev.audit_log VALUES (1, 'original')")
            c.commit()
        return
    import pg8000.native
    c = pg8000.native.Connection(user=seed.OWNER, password=target.owner_password,
                                 host=target.host, port=target.port, database=target.database)
    try:
        c.run("DROP TABLE IF EXISTS wealth.stolen")
        # A query that writes: what the READ ONLY transaction exists to stop, since
        # DECLARE accepts any query.
        c.run("CREATE OR REPLACE FUNCTION wealth.write_audit() RETURNS int VOLATILE LANGUAGE sql AS "
              "$$ INSERT INTO wealth.audit_log VALUES (99, 'x') RETURNING id $$")
        c.run("DELETE FROM wealth.audit_log")
        c.run("INSERT INTO wealth.audit_log VALUES (1, 'original')")
    finally:
        c.close()


def _lower(row: dict) -> dict:
    """Oracle names columns in upper case, Postgres in lower; compare neither."""
    return {k.lower(): v for k, v in row.items()}


@pytest.fixture(params=["oracle", "postgres"])
def engine(request, oracle, postgres):
    """A real provider connected as the owner, plus the engine-specific SQL."""
    _reset(request.param, oracle if request.param == "oracle" else postgres)
    if request.param == "oracle":
        provider = OracleProvider(
            {"dsn": oracle.dsn, "tls": False}, Credential(seed.OWNER, oracle.owner_password), "test")
        sql = {
            "audit": "mcp_dev.audit_log", "clients": "mcp_dev.clients",
            "count_audit": "SELECT COUNT(*) AS n FROM mcp_dev.audit_log",
            "sleep": "SELECT COUNT(*) FROM all_objects a, all_objects b, all_objects c",
            "timed_out": "DPY-4024",
        }
    else:
        provider = PostgreSQLProvider(
            {"host": postgres.host, "port": postgres.port, "database": postgres.database},
            Credential(seed.OWNER, postgres.owner_password), "test")
        sql = {
            "audit": "wealth.audit_log", "clients": "wealth.clients",
            "count_audit": "SELECT COUNT(*) AS n FROM wealth.audit_log",
            "sleep": "SELECT pg_sleep(60)",
            "timed_out": "canceling statement due to statement timeout",
        }
    yield request.param, provider, sql
    provider.close()
    _reset(request.param, oracle if request.param == "oracle" else postgres)


def _audit_rows(provider, sql) -> int:
    result = _run(provider, sql["count_audit"])
    assert result["status"]["code"] == 200, result
    return int(next(iter(result["rows"][0].values())))


def _run(provider, statement):
    """An agent statement, with ordinary limits; TestResourceLimits sets its own."""
    return provider.execute_statement(statement, timeout_seconds=30, row_cap=1000)


# Overrides the engine fixture's params, for a test that applies to one engine only.
ORACLE_ONLY = pytest.mark.parametrize("engine", ["oracle"], indirect=True)
POSTGRES_ONLY = pytest.mark.parametrize("engine", ["postgres"], indirect=True)


class TestHostileStatementsChangeNothing:

    def test_select_for_update_is_refused_or_harmless(self, engine):
        name, provider, sql = engine
        result = _run(provider, f"SELECT * FROM {sql['audit']} FOR UPDATE")
        # A row lock held after the call would block other sessions; the
        # statement must be rejected, and the session must still be usable.
        assert result["status"]["code"] != 200
        assert _audit_rows(provider, sql) == 1

    @POSTGRES_ONLY
    def test_a_query_that_calls_a_writing_function_is_refused_by_the_read_only_transaction(self, engine):
        _, provider, sql = engine
        result = _run(provider, "SELECT wealth.write_audit()")
        assert "cannot execute INSERT in a read-only transaction" in result["error"], result
        assert _audit_rows(provider, sql) == 1

    @POSTGRES_ONLY
    def test_declare_refuses_a_data_modifying_cte(self, engine):
        _, provider, sql = engine
        result = _run(provider,
            f"WITH d AS (DELETE FROM {sql['audit']} RETURNING *) SELECT * FROM d")
        assert "DECLARE CURSOR must not contain data-modifying statements" in result["error"], result
        assert _audit_rows(provider, sql) == 1

    @POSTGRES_ONLY
    def test_declare_refuses_select_into(self, engine):
        _, provider, sql = engine
        result = _run(provider, f"SELECT * INTO wealth.stolen FROM {sql['clients']}")
        assert "SELECT ... INTO is not allowed here" in result["error"], result
        check = _run(provider,
            "SELECT COUNT(*) AS n FROM information_schema.tables WHERE table_name = 'stolen'")
        assert _lower(check["rows"][0])["n"] == 0

    def test_a_stacked_statement_is_refused(self, engine):
        _, provider, sql = engine
        result = _run(provider, f"SELECT 1; DELETE FROM {sql['audit']}")
        assert result["status"]["code"] != 200
        assert _audit_rows(provider, sql) == 1


class TestWritesAreRefusedByTheDatabase:
    """Run as the schema owner, who holds every DML and DDL right. Nothing in the
    provider reads these statements; the database refuses them."""

    @ORACLE_ONLY
    @pytest.mark.parametrize("write", [
        "DELETE FROM {audit}",
        "INSERT INTO {audit} VALUES (99, 'x')",
        "UPDATE {audit} SET note = 'x'",
    ])
    def test_a_data_change_is_refused_by_the_read_only_transaction_on_oracle(self, engine, write):
        _, provider, sql = engine
        result = _run(provider, write.format(audit=sql["audit"]))
        assert "ORA-01456" in result["error"], result
        assert _audit_rows(provider, sql) == 1

    @POSTGRES_ONLY
    @pytest.mark.parametrize("write", [
        "DELETE FROM {audit}",
        "INSERT INTO {audit} VALUES (99, 'x')",
        "UPDATE {audit} SET note = 'x'",
        "DROP TABLE {audit}",
        "TRUNCATE TABLE {audit}",
    ])
    def test_declare_accepts_only_a_query_on_postgres(self, engine, write):
        _, provider, sql = engine
        result = _run(provider, write.format(audit=sql["audit"]))
        assert f'syntax error at or near "{write.split()[0]}"' in result["error"], result
        assert _audit_rows(provider, sql) == 1


class TestResourceLimits:

    def test_a_runaway_statement_is_stopped_by_the_timeout(self, engine):
        import time
        _, provider, sql = engine
        started = time.monotonic()
        result = provider.execute_statement(sql["sleep"], timeout_seconds=2, row_cap=10)
        assert time.monotonic() - started < 10
        assert sql["timed_out"] in result["error"], result

    def test_a_result_larger_than_the_cap_is_truncated_and_says_so(self, engine, monkeypatch):
        name, provider, sql = engine
        series = ("SELECT level AS n FROM dual CONNECT BY level <= 50" if name == "oracle"
                  else "SELECT g AS n FROM generate_series(1, 50) g")
        result = provider.execute_statement(series, timeout_seconds=30, row_cap=5)
        assert result["status"]["code"] == 200
        assert len(result["rows"]) == 5
        assert result["truncated"] is True

    def test_start_row_pages_through_a_result_without_gaps_or_overlap(self, engine):
        name, provider, sql = engine
        series = ("SELECT level AS n FROM dual CONNECT BY level <= 12 ORDER BY 1" if name == "oracle"
                  else "SELECT g AS n FROM generate_series(1, 12) g ORDER BY 1")
        seen, start = [], 0
        while start is not None:
            result = provider.execute_statement(series, timeout_seconds=30, row_cap=5, start_row=start)
            seen += [_lower(r)["n"] for r in result["rows"]]
            start = result["next_start_row"]
        assert seen == list(range(1, 13))

    def test_a_small_result_is_not_marked_truncated(self, engine):
        _, provider, sql = engine
        result = _run(provider, f"SELECT * FROM {sql['clients']}")
        assert result["status"]["code"] == 200
        assert len(result["rows"]) == 2
        assert result["truncated"] is False

    def test_duplicate_column_names_in_a_join_still_work(self, engine):
        """Wrapping a statement in a subquery to cap rows breaks this on Oracle."""
        _, provider, sql = engine
        result = _run(provider,
            f"SELECT c.id, a.id FROM {sql['clients']} c JOIN "
            f"{sql['clients'].replace('clients', 'accounts')} a ON a.client_id = c.id")
        assert [c.lower() for c in result["columns"]] == ["id", "id_2"]
        assert [_lower(r) for r in result["rows"]] == [{"id": 1, "id_2": 10}]  # neither overwritten


class TestSelectStillWorks:

    def test_a_normal_select_returns_rows(self, engine):
        _, provider, sql = engine
        result = _run(provider, f"SELECT id, name FROM {sql['clients']} ORDER BY id")
        assert result["status"]["code"] == 200
        assert [_lower(r)["name"] for r in result["rows"]] == ["Ada", "Grace"]
        assert result["columns"]
