#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The MySQL and MariaDB providers, on the real engines (docker-compose.test.yml).

What is under test is the engines' behaviour: which tables information_schema
shows a user, what SHOW CREATE reveals to an account with only REFERENCES, and
whether a read-only transaction stops DDL. Statements run as the schema owner,
who holds ALL on the database, so a write that fails was stopped by the
provider's session, not by the account's privileges.
"""

import pytest

from adam.data_source_provider.credential_provider import Credential
from adam.data_source_provider.mariadb_provider import MariaDBProvider
from adam.data_source_provider.mysql_provider import MySQLProvider
from adam.shared.schema_types import ObjectRef
from tests.integration import seed

pytestmark = pytest.mark.integration

OWN = {ObjectRef("wealth", t) for t in ("clients", "accounts", "audit_log", "v_clients_restricted")}
PROVIDERS = {"mysql": MySQLProvider, "mariadb": MariaDBProvider}


@pytest.fixture(params=["mysql", "mariadb"])
def target(request):
    return request.getfixturevalue(request.param)


def _provider(target, user, password):
    return PROVIDERS[target.engine](
        {"host": target.host, "port": str(target.port), "database": target.database},
        Credential(user, password), "test")


@pytest.fixture
def owner(target):
    provider = _provider(target, seed.OWNER, target.owner_password)
    yield provider
    provider.close()


@pytest.fixture
def analyst(target):
    provider = _provider(target, seed.ANALYST, target.analyst_password)
    yield provider
    provider.close()


def _admin(target, *statements):
    admin = target.admin()
    try:
        with admin.cursor() as cursor:
            for statement in statements:
                cursor.execute(statement)
            return cursor.fetchall()
    finally:
        admin.close()


def _audit_rows(target) -> int:
    """Counted by root, outside the provider, so the provider cannot hide a change."""
    return _admin(target, "SELECT COUNT(*) FROM wealth.audit_log")[0][0]


def _tables(target) -> set[str]:
    rows = _admin(target, "SELECT table_name FROM information_schema.tables WHERE table_schema = 'wealth'")
    return {r[0] for r in rows}


@pytest.fixture
def restore(target):
    """Put the seeded rows and tables back after a test that may have changed them."""
    yield
    _admin(target, "DROP TABLE IF EXISTS wealth.sneaky", "DELETE FROM wealth.audit_log",
           "INSERT INTO wealth.audit_log VALUES (1, 'original')")


def _run(provider, statement, **limits):
    return provider.execute_statement(statement, **{"timeout_seconds": 30, "row_cap": 1000, **limits})


class TestVisibility:
    def test_a_grant_through_a_role_is_visible(self, analyst):
        """The analyst's only grant is SELECT on the view, through ANALYST_ROLE."""
        assert analyst.visible_object_refs() == [ObjectRef("wealth", "v_clients_restricted")]

    def test_the_owner_sees_their_tables_and_views(self, owner):
        assert set(owner.visible_object_refs()) == OWN

    def test_only_the_connections_database_is_listed(self, owner):
        """The system schemas, and other_db, which the owner can read too, are another connection's."""
        assert _run(owner, "SELECT COUNT(*) AS n FROM other_db.elsewhere")["status"]["code"] == 200
        assert {ref.schema for ref in owner.visible_object_refs()} == {"wealth"}

    def test_a_role_that_is_not_the_default_grants_nothing(self, target, analyst):
        """The engine's rule, not the provider's: a granted role is inactive until it is
        the account's default, and without it the account may not open the database."""
        to = "TO" if target.engine == "mysql" else "FOR"
        _admin(target, f"SET DEFAULT ROLE NONE {to} analyst")
        try:
            with pytest.raises(RuntimeError, match="Access denied for user 'analyst'"):
                analyst.visible_object_refs()
        finally:
            _admin(target, f"SET DEFAULT ROLE analyst_role {to} analyst")


class TestProbe:
    def test_a_login_that_works_is_ok_and_one_that_does_not_says_why(self, target, owner):
        assert owner.probe() == {"ok": True}
        with pytest.raises(RuntimeError, match="Access denied"):
            _provider(target, seed.OWNER, "the-wrong-password").probe()


class TestDdl:
    def test_tables_carry_keys_foreign_keys_and_comments_qualified_by_database(self, owner):
        ddl = {t.ref.name: t for t in owner.scan_schema()}
        accounts = ddl["accounts"].definition
        assert accounts.startswith("CREATE TABLE `wealth`.`accounts` (")
        assert "PRIMARY KEY (`id`)" in accounts
        assert "REFERENCES `clients` (`id`)" in accounts
        assert "COMMENT 'Current balance in USD'" in accounts
        assert "COMMENT='One row per client''s account'" in accounts

    def test_the_auto_increment_counter_is_not_in_the_ddl(self, target, owner):
        """It changes on every insert, which would make every scan look like a schema change."""
        assert "AUTO_INCREMENT=" in _admin(target, "SHOW CREATE TABLE wealth.accounts")[0][1]
        accounts = next(t for t in owner.scan_schema() if t.ref.name == "accounts")
        assert "AUTO_INCREMENT=" not in accounts.definition

    def test_a_view_carries_its_definition(self, owner):
        view = next(t for t in owner.scan_schema() if t.ref.name == "v_clients_restricted")
        assert view.kind == "view"
        assert " VIEW `wealth`.`v_clients_restricted` AS select" in view.definition
        assert "email" not in view.definition


class TestAScannerSetUpAsAdvised:
    """The DBA advice for a scanner: REFERENCES on each table, which shows its definition
    and not its rows, and SHOW VIEW with SELECT on each view (here SELECT comes from the role)."""

    @pytest.fixture
    def scanner(self, target):
        grants = ["REFERENCES ON wealth.clients", "REFERENCES ON wealth.accounts",
                  "SHOW VIEW ON wealth.v_clients_restricted"]
        _admin(target, *(f"GRANT {g} TO analyst" for g in grants))
        provider = _provider(target, seed.ANALYST, target.analyst_password)
        yield provider
        provider.close()
        _admin(target, *(f"REVOKE {g} FROM analyst" for g in grants))

    def test_keys_comments_and_the_view_definition_are_in_the_ddl(self, scanner):
        ddl = {t.ref.name: t.definition for t in scanner.scan_schema()}
        assert set(ddl) == {"clients", "accounts", "v_clients_restricted"}
        assert "PRIMARY KEY (`id`)" in ddl["accounts"]
        assert "REFERENCES `clients` (`id`)" in ddl["accounts"]
        assert "COMMENT 'Current balance in USD'" in ddl["accounts"]
        assert " AS select" in ddl["v_clients_restricted"]

    def test_it_cannot_read_table_rows(self, scanner):
        result = _run(scanner, "SELECT * FROM wealth.accounts")
        assert result["status"]["code"] == 500
        assert "SELECT command denied" in result["error"]

    def test_without_show_view_the_scan_fails_naming_the_view(self, target, scanner):
        """A scan that cannot describe a visible object raises rather than publishing
        it without its definition."""
        _admin(target, "REVOKE SHOW VIEW ON wealth.v_clients_restricted FROM analyst")
        try:
            with pytest.raises(Exception, match="SHOW VIEW command denied.*v_clients_restricted"):
                scanner.scan_schema()
        finally:
            _admin(target, "GRANT SHOW VIEW ON wealth.v_clients_restricted TO analyst")


class TestWritesAreRefusedByTheDatabase:
    @pytest.mark.parametrize("write", [
        "DELETE FROM wealth.audit_log",
        "INSERT INTO wealth.audit_log VALUES (99, 'x')",
        "UPDATE wealth.audit_log SET note = 'x'",
        "REPLACE INTO wealth.audit_log VALUES (1, 'x')",
        "SELECT * FROM wealth.audit_log FOR UPDATE",
        "DROP TABLE wealth.audit_log",
        "TRUNCATE TABLE wealth.audit_log",
        "ALTER TABLE wealth.audit_log ADD COLUMN extra int",
        "RENAME TABLE wealth.audit_log TO wealth.sneaky",
        "CREATE TABLE wealth.sneaky (id int)",
        "CREATE TABLE wealth.sneaky AS SELECT * FROM wealth.clients",
    ])
    def test_it_is_refused_and_changes_nothing(self, target, owner, restore, write):
        """DDL ends a transaction in these engines, and still fails: every transaction
        in the session is read-only, not only the one the provider opened."""
        result = _run(owner, write)
        assert result["status"]["code"] == 500
        assert "READ ONLY transaction" in result["error"], result
        assert _audit_rows(target) == 1
        assert "sneaky" not in _tables(target)

    def test_a_stacked_statement_is_refused_before_it_runs(self, target, owner, restore):
        """Refused as a syntax error, so the driver does not allow stacking at all (a read-only
        session would refuse the DELETE even with stacking on)."""
        result = _run(owner, "SELECT 1; SELECT 2")
        assert result["status"]["code"] == 500 and "1064" in result["error"], result

    def test_a_refused_statement_leaves_the_next_one_working(self, owner, restore):
        _run(owner, "DELETE FROM wealth.audit_log")
        assert _run(owner, "SELECT COUNT(*) AS n FROM wealth.audit_log")["rows"] == [{"n": 1}]


class TestResourceLimits:
    def test_a_runaway_statement_is_stopped_at_the_timeout(self, target, owner):
        """MySQL ends an interrupted SLEEP with the row 1, MariaDB with an error."""
        import time
        started = time.monotonic()
        result = _run(owner, "SELECT SLEEP(20) AS s", timeout_seconds=2)
        assert 1.5 < time.monotonic() - started < 10  # at the limit: not seconds read as milliseconds
        if target.engine == "mysql":
            assert result["rows"] == [{"s": 1}], result
        else:
            assert "max_statement_time exceeded" in result["error"], result

    def test_a_statement_inside_the_timeout_finishes(self, owner):
        assert _run(owner, "SELECT SLEEP(1) AS s", timeout_seconds=3)["rows"] == [{"s": 0}]

    def test_a_result_larger_than_the_cap_is_truncated_and_says_so(self, owner):
        result = _run(owner, "WITH RECURSIVE s (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 50) "
                             "SELECT n FROM s", row_cap=5)
        assert result["status"]["code"] == 200
        assert [r["n"] for r in result["rows"]] == [1, 2, 3, 4, 5]
        assert result["truncated"] is True

    def test_the_rows_past_the_cap_are_not_read(self, owner):
        """Rows are produced at half a second each, so producing all twenty takes ten
        seconds. Each row is larger than the server's network buffer, so it is sent as it
        is produced rather than all at the end."""
        import time
        slow_rows = ("WITH RECURSIVE s (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 20) "
                     "SELECT n, SLEEP(0.5) AS z, REPEAT('x', 65536) AS pad FROM s")
        started = time.monotonic()
        assert _run(owner, slow_rows, row_cap=2)["truncated"] is True
        assert _run(owner, "SELECT 1 AS one")["rows"] == [{"one": 1}]
        assert time.monotonic() - started < 5

    def test_a_limit_in_the_statement_is_still_capped(self, owner):
        result = _run(owner, "WITH RECURSIVE s (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 50) "
                             "SELECT n FROM s LIMIT 40", row_cap=5)
        assert len(result["rows"]) == 5 and result["truncated"] is True

    def test_start_row_pages_through_a_result_without_gaps_or_overlap(self, owner):
        series = ("WITH RECURSIVE s (n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM s WHERE n < 12) "
                  "SELECT n FROM s ORDER BY n")
        seen, start = [], 0
        for _ in range(4):  # 12 rows in pages of 5 is three; a fourth would mean pages repeat
            if start is None:
                break
            result = _run(owner, series, row_cap=5, start_row=start)
            assert result["status"]["code"] == 200, result
            seen += [r["n"] for r in result["rows"]]
            start = result["next_start_row"]
        assert seen == list(range(1, 13))

    def test_a_small_result_is_not_marked_truncated(self, owner):
        result = _run(owner, "SELECT id, name FROM wealth.clients ORDER BY id")
        assert result["rows"] == [{"id": 1, "name": "Ada"}, {"id": 2, "name": "Grace"}]
        assert result["truncated"] is False

    def test_duplicate_column_names_in_a_join_still_work(self, owner):
        """Wrapping a statement in a subquery to cap rows would fail on these."""
        result = _run(owner, "SELECT c.id, a.id FROM wealth.clients c JOIN wealth.accounts a ON a.client_id = c.id")
        assert result["columns"] == ["id", "id_2"]
        assert result["rows"] == [{"id": 1, "id_2": 10}]  # both values, neither overwritten

    def test_decimals_and_dates_come_back_as_json_values(self, owner):
        result = _run(owner, "SELECT balance, DATE '2026-10-15' AS d FROM wealth.accounts")
        assert result["rows"] == [{"balance": 100.5, "d": "2026-10-15"}]

