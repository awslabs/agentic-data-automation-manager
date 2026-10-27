#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The database, not a mirror of its grants, decides what a user may see.

Run against real Oracle and Postgres (docker-compose.test.yml) because the
behaviour under test is the engine's: whether its catalog views account for a
grant received through a role, and whether they list views at all. A fake cannot
be wrong about that the way the real thing was.
"""

import pytest

from adam.data_source_provider.credential_provider import Credential
from adam.data_source_provider.oracle_provider import OracleProvider
from adam.data_source_provider.postgresql_provider import PostgreSQLProvider
from adam.shared.schema_types import ObjectRef
from tests.integration import seed

pytestmark = pytest.mark.integration

OWN_ORACLE = {ObjectRef("mcp_dev", t) for t in ("clients", "accounts", "audit_log", "v_clients_restricted")}
OWN_POSTGRES = {ObjectRef("wealth", t) for t in ("clients", "accounts", "audit_log", "v_clients_restricted")}


def _oracle(target, user, password):
    return OracleProvider({"dsn": target.dsn, "tls": False}, Credential(user, password), "test")


def _postgres(target, user, password):
    return PostgreSQLProvider(
        {"host": target.host, "port": target.port, "database": target.database},
        Credential(user, password), "test")


@pytest.fixture
def oracle_owner(oracle):
    provider = _oracle(oracle, seed.OWNER, oracle.owner_password)
    yield provider
    provider.close()


@pytest.fixture
def oracle_analyst(oracle):
    provider = _oracle(oracle, seed.ANALYST, oracle.analyst_password)
    yield provider
    provider.close()


@pytest.fixture
def pg_owner(postgres):
    provider = _postgres(postgres, seed.OWNER, postgres.owner_password)
    yield provider
    provider.close()


@pytest.fixture
def pg_analyst(postgres):
    provider = _postgres(postgres, seed.ANALYST, postgres.analyst_password)
    yield provider
    provider.close()


class TestOracleVisibility:
    def test_a_grant_through_a_role_is_visible(self, oracle_analyst):
        """The regression. The analyst's only grant comes via ANALYST_ROLE.

        The previous query filtered `all_tab_privs` to `grantee = <user>`, which
        a role-granted privilege never matches, and it queried no view source at
        all. Measured against this seed it returned nothing for this user, so
        schema search showed them an empty catalog.
        """
        assert oracle_analyst.visible_object_refs() == [ObjectRef("mcp_dev", "v_clients_restricted")]

    def test_the_owner_sees_their_tables_and_views(self, oracle_owner):
        """Views included. The previous query read only `all_tables`."""
        assert set(oracle_owner.visible_object_refs()) == OWN_ORACLE

    def test_a_view_carries_its_definition(self, oracle_owner):
        view = next(t for t in oracle_owner.scan_schema() if t.ref == ObjectRef("mcp_dev", "v_clients_restricted"))
        assert view.kind == "view"
        # What the view leaves out is the point: a column list cannot show it.
        assert "select" in view.definition.lower()
        assert "email" not in view.definition.lower()

    def test_scanned_tables_carry_keys(self, oracle_owner):
        accounts = next(t for t in oracle_owner.scan_schema() if t.ref == ObjectRef("mcp_dev", "accounts"))
        assert "PRIMARY KEY" in accounts.definition
        assert "REFERENCES" in accounts.definition


class TestPostgresVisibility:
    def test_a_grant_through_a_role_is_visible(self, pg_analyst):
        """The previous query looked only in a schema named after the connecting
        user and asked for BASE TABLE only, so for this user, whose single grant
        is a view in another schema, it returned nothing."""
        assert pg_analyst.visible_object_refs() == [ObjectRef("wealth", "v_clients_restricted")]

    def test_the_owner_sees_their_tables_and_views(self, pg_owner):
        assert set(pg_owner.visible_object_refs()) == OWN_POSTGRES

    def test_a_view_carries_its_definition(self, pg_owner):
        view = next(t for t in pg_owner.scan_schema() if t.ref == ObjectRef("wealth", "v_clients_restricted"))
        assert view.kind == "view"
        assert "select" in view.definition.lower()
        assert "email" not in view.definition.lower()

    def test_scanned_tables_carry_keys(self, pg_owner):
        accounts = next(t for t in pg_owner.scan_schema() if t.ref == ObjectRef("wealth", "accounts"))
        assert "PRIMARY KEY" in accounts.definition
        assert "REFERENCES" in accounts.definition


class TestRefsAreComparableAcrossEngines:
    """Glue spells these lowercase and Oracle uppercase; both must match."""

    def test_case_does_not_affect_identity(self):
        assert ObjectRef("MCP_DEV", "CLIENTS") == ObjectRef("mcp_dev", "clients")
        assert len({ObjectRef("MCP_DEV", "CLIENTS"), ObjectRef("mcp_dev", "clients")}) == 1


class TestAPostgresScannerWithOnlyReferencesGetsTheKeys:
    """information_schema shows a table's constraints only to its owner, so a scanner
    that isn't one published DDL with no keys. README's minimum is REFERENCES, which
    shows metadata but no rows."""

    @pytest.fixture
    def pg_reader(self, postgres):
        import pg8000.native
        owner = pg8000.native.Connection(seed.OWNER, password=postgres.owner_password,
                                         host=postgres.host, port=int(postgres.port),
                                         database=postgres.database)
        owner.run(f"GRANT REFERENCES ON wealth.clients, wealth.accounts TO {seed.ANALYST}")
        provider = _postgres(postgres, seed.ANALYST, postgres.analyst_password)
        try:
            yield provider
        finally:
            provider.close()
            owner.run(f"REVOKE REFERENCES ON wealth.clients, wealth.accounts FROM {seed.ANALYST}")
            owner.close()

    def test_keys_and_references_are_in_the_ddl(self, pg_reader):
        accounts = next(t for t in pg_reader.scan_schema() if t.ref == ObjectRef("wealth", "accounts"))
        assert "id integer NOT NULL PRIMARY KEY" in accounts.definition
        assert "client_id integer REFERENCES wealth.clients (id)" in accounts.definition

    def test_it_cannot_read_rows(self, pg_reader):
        result = pg_reader.execute_statement("SELECT * FROM wealth.accounts", timeout_seconds=30, row_cap=10)
        assert "permission denied" in result["error"]


class TestCommentsAreInTheDdl:
    """A comment is often the only explanation of a coded name, so it is scanned with
    the table, as COMMENT ON statements, quotes escaped."""

    @pytest.mark.parametrize("engine", ["oracle", "postgres"])
    def test_table_column_and_view_comments(self, engine, oracle_owner, pg_owner):
        provider, schema, view_word = ((oracle_owner, "mcp_dev", "TABLE") if engine == "oracle"
                                       else (pg_owner, "wealth", "VIEW"))
        ddl = {t.ref.name: t.definition.lower() for t in provider.scan_schema()}
        assert f"comment on table {schema}.accounts is 'one row per client''s account';" in ddl["accounts"]
        assert f"comment on column {schema}.accounts.balance is 'current balance in usd';" in ddl["accounts"]
        assert (f"comment on {view_word.lower()} {schema}.v_clients_restricted "
                "is 'clients without contact details';") in ddl["v_clients_restricted"]
        assert "comment on" not in ddl["audit_log"]


class TestAnOracleScannerWithOnlyReferencesGetsKeysAndComments:
    """README's minimum for an Oracle scanner: REFERENCES on each table, which shows
    metadata but no rows."""

    @pytest.fixture
    def ora_reader(self, oracle):
        import oracledb
        owner = oracledb.connect(user=seed.OWNER, password=oracle.owner_password, dsn=oracle.dsn)
        owner.cursor().execute(f"GRANT REFERENCES ON mcp_dev.accounts TO {seed.ANALYST}")
        owner.cursor().execute(f"GRANT REFERENCES ON mcp_dev.clients TO {seed.ANALYST}")
        provider = _oracle(oracle, seed.ANALYST, oracle.analyst_password)
        try:
            yield provider
        finally:
            provider.close()
            owner.cursor().execute(f"REVOKE REFERENCES ON mcp_dev.accounts FROM {seed.ANALYST}")
            owner.cursor().execute(f"REVOKE REFERENCES ON mcp_dev.clients FROM {seed.ANALYST}")
            owner.close()

    def test_keys_references_and_comments_are_in_the_ddl(self, ora_reader):
        accounts = next(t for t in ora_reader.scan_schema() if t.ref == ObjectRef("mcp_dev", "accounts"))
        assert "ID NUMBER NOT NULL PRIMARY KEY" in accounts.definition
        assert "CLIENT_ID NUMBER REFERENCES MCP_DEV.CLIENTS (ID)" in accounts.definition
        assert "COMMENT ON COLUMN MCP_DEV.ACCOUNTS.BALANCE IS 'Current balance in USD';" in accounts.definition

    def test_it_cannot_read_rows(self, ora_reader):
        result = ora_reader.execute_statement("SELECT * FROM mcp_dev.accounts", timeout_seconds=30, row_cap=10)
        assert "ORA-41900" in result["error"]


class TestAnOracleTablesUniqueConstraintsAreInItsDdl:

    @pytest.fixture
    def with_unique(self, oracle):
        import oracledb
        owner = oracledb.connect(user=seed.OWNER, password=oracle.owner_password, dsn=oracle.dsn)
        owner.cursor().execute("CREATE TABLE mcp_dev.uq_probe (id NUMBER PRIMARY KEY, a NUMBER, b NUMBER, "
                               "CONSTRAINT uq_probe_ab UNIQUE (a, b))")
        provider = _oracle(oracle, seed.OWNER, oracle.owner_password)
        try:
            yield provider
        finally:
            provider.close()
            owner.cursor().execute("DROP TABLE mcp_dev.uq_probe PURGE")
            owner.close()

    def test_a_unique_constraint_is_stated(self, with_unique):
        ddl = with_unique.object_ddl("MCP_DEV", "UQ_PROBE", "table")
        assert "    CONSTRAINT UQ_PROBE_AB UNIQUE (A, B)" in ddl
        assert "ID NUMBER NOT NULL PRIMARY KEY" in ddl
