#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Unit tests for the DataSourceProvider base and OracleProvider.

Tests the base class dispatch with a minimal subclass, and validates the engine
implementations' construction, SQL safety checks and DDL formatting without
needing a live database.
"""

import pytest

from adam.data_source_provider.credential_provider import Credential
from adam.data_source_provider.data_source_provider import (
    Column,
    DataSourceProvider,
    create_table_ddl,
    rows_response,
)
from adam.data_source_provider.oracle_provider import OracleProvider
from adam.data_source_provider.postgresql_provider import PostgreSQLProvider
from adam.shared.schema_types import ObjectRef, SchemaObject


def _table(name: str, schema: str = "s") -> SchemaObject:
    return SchemaObject(schema=schema, name=name, kind="table",
                        definition=f"CREATE TABLE {schema}.{name} (\n    id NUMBER\n);")


class _FakeProvider(DataSourceProvider):
    """Minimal concrete provider: answers from fixed tables, touches nothing."""

    CATALOG = "SELECT schema_name, object_name, kind FROM catalog"
    STATEMENT_BEST_PRACTICES = "Fake dialect: use TAKE n."
    ADMIN_BEST_PRACTICES = "Fake accounts: grant READ only."

    def __init__(self, tables: list[SchemaObject] | None = None, *, truncated: bool = False,
                 failure: str = "", registered: dict | None = None):
        super().__init__(registered or {}, Credential("u", "p"), "test-subject")
        self.tables = tables or []
        self.truncated = truncated
        self.failure = failure
        self.statements: list[str] = []

    def execute_statement(self, statement, *, timeout_seconds, row_cap, start_row=0):
        self.statements.append(statement)
        self.limits = (timeout_seconds, row_cap)
        self.start_row = start_row
        if self.failure:
            return {"status": {"code": 500}, "error": self.failure}
        if statement == self.CATALOG:
            rows = [[t.schema, t.name, t.kind] for t in self.tables]
            return rows_response(["SCHEMA_NAME", "OBJECT_NAME", "KIND"], rows, self.truncated)
        return rows_response(["N"], [[1]], False)

    def object_ddl(self, schema, name, kind):
        return next(t.definition for t in self.tables if (t.schema, t.name) == (schema, name))

    def visible_objects(self):
        return self._listed_by(self.CATALOG)


class TestTheInterfaceAnEngineMustImplement:

    REQUIRED = {"object_ddl", "execute_statement", "visible_objects",
                "STATEMENT_BEST_PRACTICES", "ADMIN_BEST_PRACTICES"}

    @pytest.mark.parametrize("missing", sorted(REQUIRED))
    def test_a_source_that_leaves_one_out_cannot_be_built_and_is_told_which(self, missing):
        members = {n: getattr(_FakeProvider, n) for n in self.REQUIRED - {missing}}
        with pytest.raises(TypeError, match=missing):
            type("Source", (DataSourceProvider,), members)({}, Credential("u", "p"), "sub")


class TestSqlBestPractices:
    """The only channel for telling an agent which dialect a database speaks."""

    def test_the_operation_returns_the_engines_text(self):
        assert _FakeProvider().handle({"operation": "get_statement_best_practices"}) == \
            {"response": "Fake dialect: use TAKE n."}


class TestAdminBestPractices:
    """Advice for the administrator, shown on the admin page; never a gate on registering."""

    def test_the_scanner_serves_its_engines_without_opening_a_connection(self, monkeypatch):
        from adam.data_source_provider import scanner_handler
        monkeypatch.setenv("ENGINE", "oracle")
        monkeypatch.setattr(scanner_handler, "_open", lambda *a: pytest.fail("opened a connection"))
        result = scanner_handler.handler({"operation": "get_admin_best_practices", "args": {}}, None)
        assert result == {"response": OracleProvider.ADMIN_BEST_PRACTICES}

    def test_a_users_provider_does_not_serve_it(self):
        """Only the admin page reads it, through the scanner the admin function may call."""
        assert "Unknown operation" in _FakeProvider().handle({"operation": "get_admin_best_practices"})["error"]


class TestAListedCatalogIsNeverPartial:

    def test_the_catalog_statement_runs_through_execute_statement(self):
        provider = _FakeProvider([_table("T1", schema="S")])
        assert provider.visible_object_refs() == [ObjectRef("S", "T1")]
        assert provider.statements == [_FakeProvider.CATALOG]

    def test_a_catalog_longer_than_the_row_cap_is_an_error_not_a_short_list(self):
        with pytest.raises(RuntimeError, match="more than"):
            _FakeProvider([_table("T1")], truncated=True).visible_object_refs()

    def test_a_failing_catalog_query_is_an_error_not_an_empty_list(self):
        with pytest.raises(RuntimeError, match="boom"):
            _FakeProvider(failure="boom").visible_object_refs()


class TestHandleDispatch:
    """handle() is the one place the provider-Lambda operations are routed."""

    def test_a_users_provider_serves_no_schema_scan(self):
        """Scans are the scanner's, logged in as its own login and written to its bucket; a
        user's connection lists only the names its user can see (visible_object_refs)."""
        assert "Unknown operation" in _FakeProvider([_table("t1")]).handle(
            {"operation": "scan_schema", "args": {}})["error"]

    def test_visible_object_refs_returns_schema_and_name(self):
        provider = _FakeProvider([_table("T1", schema="S")])
        assert provider.handle({"operation": "visible_object_refs", "args": {}}) == \
            {"response": [{"schema": "s", "name": "t1"}]}

    def test_unknown_operation_is_an_error(self):
        assert "Unknown operation" in _FakeProvider().handle({"operation": "nope"})["error"]


class TestProviderHandlerRefusesUnattributableCalls:
    """A call that cannot be tied to a caller must fail, not default."""

    def test_missing_user_id_raises(self):
        from adam.data_source_provider.provider_handler import handler
        with pytest.raises(RuntimeError, match="user_id"):
            handler({"operation": "probe", "args": {"connection_id": "c"}}, None)

    def test_missing_connection_id_raises(self):
        from adam.data_source_provider.provider_handler import handler
        with pytest.raises(RuntimeError, match="connection_id"):
            handler({"operation": "probe", "args": {"user_id": "u"}}, None)


class TestOracleProvider:
    def test_format_data_type_varchar2(self):
        assert OracleProvider._format_data_type("VARCHAR2", 100, None, None) == "VARCHAR2(100)"

    def test_format_data_type_number_with_scale(self):
        assert OracleProvider._format_data_type("NUMBER", 0, 10, 2) == "NUMBER(10,2)"

    def test_format_data_type_number_without_scale(self):
        assert OracleProvider._format_data_type("NUMBER", 0, 10, 0) == "NUMBER(10)"

    def test_format_data_type_passthrough(self):
        assert OracleProvider._format_data_type("CLOB", 0, None, None) == "CLOB"
        assert OracleProvider._format_data_type("DATE", 0, None, None) == "DATE"


class TestCreateTableDdl:
    """The CREATE TABLE every engine that describes tables by columns renders the same way."""

    COLUMNS = [Column("ID", "NUMBER(10)", True, None), Column("CLIENT_ID", "NUMBER(10)", False, None),
               Column("A", "NUMBER", False, " 0 "), Column("B", "NUMBER", False, None)]

    def _ddl(self, **constraints):
        return create_table_ddl("table", "S", "T", self.COLUMNS, {
            "primary_keys": {}, "foreign_keys": {}, "unique_constraints": {}, "check_constraints": {},
            **constraints})

    def test_one_column_keys_are_stated_on_their_column_and_the_rest_after(self):
        assert self._ddl(
            primary_keys={"PK": ["ID"]},
            foreign_keys={"FK": {"columns": ["CLIENT_ID"], "ref_schema": "S", "ref_table": "CLIENTS",
                                 "ref_columns": ["ID"]}},
            unique_constraints={"UQ": ["A", "B"]},
            check_constraints={"CK": "A > 0"},
        ) == ("CREATE TABLE S.T (\n"
              "    ID NUMBER(10) NOT NULL PRIMARY KEY,\n"
              "    CLIENT_ID NUMBER(10) REFERENCES S.CLIENTS (ID),\n"
              "    A NUMBER DEFAULT 0,\n"
              "    B NUMBER,\n"
              "    CONSTRAINT UQ UNIQUE (A, B),\n"
              "    CONSTRAINT CK CHECK (A > 0)\n);")

    def test_a_composite_primary_key_is_stated_after_the_columns(self):
        ddl = self._ddl(primary_keys={"PK": ["ID", "CLIENT_ID"]})
        assert "    CONSTRAINT PK PRIMARY KEY (ID, CLIENT_ID)" in ddl and " NOT NULL PRIMARY KEY" not in ddl

    def test_no_columns_is_an_error_not_an_empty_table(self):
        with pytest.raises(ValueError, match="no column definitions"):
            create_table_ddl("table", "S", "T", [], {"primary_keys": {}, "foreign_keys": {},
                                                     "check_constraints": {}})


class TestEnginesConnectOnFirstUseAndCloseCleanly:

    @pytest.fixture
    def no_driver_calls(self, monkeypatch):
        """Any attempt to open a real connection fails the test."""
        import oracledb
        import pg8000.native

        def refuse(*a, **k):
            raise AssertionError("a connection was opened")
        monkeypatch.setattr(oracledb, "connect", refuse)
        monkeypatch.setattr(pg8000.native, "Connection", refuse)
        monkeypatch.setenv("DB_TLS_DISABLED", "true")

    def test_building_a_provider_opens_nothing(self, no_driver_calls):
        OracleProvider({"dsn": "h:1521/s"}, Credential("u", "p"), "sub")
        PostgreSQLProvider({"host": "h", "port": "5432", "database": "d"},
                               Credential("u", "p"), "sub")

    def test_first_use_is_what_connects(self, no_driver_calls):
        provider = OracleProvider({"dsn": "h:1521/s", "tls": False}, Credential("u", "p"), "sub")
        with pytest.raises(AssertionError, match="a connection was opened"):
            provider.conn

    def test_an_oracle_connection_without_a_dsn_fails_when_used_and_says_which(self, monkeypatch):
        provider = OracleProvider({"connectionId": "c1"}, Credential("u", "p"), "sub")
        with pytest.raises(ValueError, match="c1.*dsn"):
            provider.conn

    def test_a_postgres_connection_names_every_missing_field(self, monkeypatch):
        monkeypatch.setenv("DB_TLS_DISABLED", "true")
        provider = PostgreSQLProvider({"port": "5432"}, Credential("u", "p"), "sub")
        with pytest.raises(ValueError, match="host, database"):
            provider.conn

    def test_close_ends_the_session_once_and_is_safe_to_repeat(self):
        provider = OracleProvider({"dsn": "h:1521/s"}, Credential("u", "p"), "sub")
        provider.close()  # before any use: nothing to close

        class _Session:
            closed = 0

            def close(self):
                _Session.closed += 1
        provider._db = _Session()
        provider.close()
        provider.close()
        assert _Session.closed == 1 and provider._db is None


class TestTheAgentsStatementsRunWithTheAdministratorsLimits:
    """The registration's limits are the ceiling: whatever a caller sends, no statement runs
    longer or returns more than the administrator set."""

    REGISTERED = {"statementTimeoutSeconds": 30, "rowCap": 100}

    def _run(self, registered=REGISTERED, **args):
        provider = _FakeProvider(registered=registered)
        return provider, provider.handle({"operation": "execute_statement",
                                          "args": {"statement": "SELECT 1", **args}})

    def test_with_no_limits_asked_the_registrations_are_applied(self):
        provider, result = self._run()
        assert "response" in result and provider.limits == (30, 100)

    def test_a_caller_may_ask_for_less(self):
        provider, result = self._run(timeout_seconds=12, row_cap=34)
        assert "response" in result and provider.limits == (12, 34)

    @pytest.mark.parametrize("args", [{"timeout_seconds": 31}, {"row_cap": 101},
                                      {"timeout_seconds": 0}, {"row_cap": "thirty"}])
    def test_a_caller_asking_for_more_or_nonsense_runs_nothing(self, args):
        provider, result = self._run(**args)
        assert "error" in result and provider.statements == []

    @pytest.mark.parametrize("registered", [{}, {"statementTimeoutSeconds": 30}, {"rowCap": 100}])
    def test_a_data_source_without_both_limits_registered_runs_nothing(self, registered):
        provider, result = self._run(registered=registered, timeout_seconds=10, row_cap=10)
        assert "error" in result and provider.statements == []


class TestPages:

    def test_without_max_rows_a_page_is_1000_rows_or_the_cap_if_smaller(self):
        from adam.shared.statement_limits import page
        assert page(None, None, 5000) == (0, 1000)
        assert page(None, None, 200) == (0, 200)

    def test_max_rows_may_be_raised_to_the_cap_and_no_further(self):
        from adam.shared.statement_limits import LimitError, page
        assert page(2000, 4000, 5000) == (2000, 4000)
        with pytest.raises(LimitError, match="max_rows"):
            page(0, 5001, 5000)

    @pytest.mark.parametrize("start_row", [-1, "x", 1_000_001])
    def test_start_row_is_a_bounded_whole_number(self, start_row):
        from adam.shared.statement_limits import LimitError, page
        with pytest.raises(LimitError, match="start_row"):
            page(start_row, None, 1000)


class TestEveryValueInARowIsKept:
    """Each row is keyed by column name, so a result naming one column twice must not
    lose a value: every engine's provider builds its rows here."""

    def test_a_repeated_name_gets_its_own_label(self):
        result = rows_response(["id", "id", "name"], [[1, 10, "Ada"]], False)
        assert result["columns"] == ["id", "id_2", "name"]
        assert result["rows"] == [{"id": 1, "id_2": 10, "name": "Ada"}]

    def test_a_label_never_collides_with_a_real_column(self):
        result = rows_response(["id", "id", "id_2"], [[1, 10, 100]], False)
        assert result["columns"] == ["id", "id_3", "id_2"]
        assert result["rows"] == [{"id": 1, "id_3": 10, "id_2": 100}]
