#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Schema search shows a caller only what their own databases say they can see.

The filter lives in SchemaProvider.search_objects, which implementations cannot
override, and its input is a required argument with no default. These tests pin
that shape, the dispatcher that feeds it, and the publish path that fills the
index the search reads.
"""


import pytest

from adam.schema_provider import (
    glue_data_catalog_schema_provider as glue_mod,
)
from adam.schema_provider.schema_provider import (
    SchemaMatch,
    SchemaProvider,
    VisibleObject,
)
from adam.shared.schema_types import ObjectRef, SchemaObject

CLIENTS = SchemaMatch("salesdb", "oracle", "MCP_DEV", "CLIENTS", "CREATE TABLE MCP_DEV.CLIENTS")
RESTRICTED = SchemaMatch("salesdb", "oracle", "MCP_DEV", "V_CLIENTS_RESTRICTED", "CREATE VIEW ...")
GOALS = SchemaMatch("anycompany", "postgres", "public", "portfolio_goals", "CREATE TABLE ...")


class _Ranked(SchemaProvider):
    """Ranks a fixed list and records how far it was read."""

    def __init__(self, matches):
        self.matches = matches
        self.yielded = 0

    def _rank(self, query):
        for m in self.matches:
            self.yielded += 1
            yield m

    def publish_objects(self, *, data_source, engine, objects):
        return {}


def _visible(*matches):
    return frozenset(m.key for m in matches)


class TestTheFilterCannotBeSkipped:

    def test_a_ranked_table_outside_visible_is_never_returned(self):
        results = _Ranked([CLIENTS, RESTRICTED, GOALS]).search_objects(
            "clients", visible=_visible(RESTRICTED), limit=8)
        assert [r["object_name"] for r in results] == ["MCP_DEV.V_CLIENTS_RESTRICTED"]

    def test_empty_visible_returns_nothing(self):
        """Empty means the caller can see no tables -- not 'no filter'."""
        provider = _Ranked([CLIENTS, GOALS])
        assert provider.search_objects("x", visible=frozenset(), limit=8) == []
        assert provider.yielded == 0

    def test_visible_is_required(self):
        with pytest.raises(TypeError):
            _Ranked([CLIENTS]).search_objects("x", limit=8)

    @pytest.mark.parametrize("not_a_frozenset", [None, [], set(), "everything"])
    def test_visible_must_be_a_frozenset(self, not_a_frozenset):
        with pytest.raises(TypeError, match="frozenset"):
            _Ranked([CLIENTS]).search_objects("x", visible=not_a_frozenset, limit=8)

    def test_an_implementation_cannot_override_search_tables(self):
        with pytest.raises(TypeError, match="_rank"):
            class _Bypass(SchemaProvider):
                def search_objects(self, query, *, visible, limit):
                    return []

                def _rank(self, query):
                    return iter(())

                def publish_objects(self, *, data_source, engine, objects):
                    return {}

    def test_the_same_table_in_another_database_is_not_visible(self):
        """Visibility is per database: two databases can both hold public.orders."""
        other_db = VisibleObject("some_other_db", ObjectRef("public", "portfolio_goals"))
        assert _Ranked([GOALS]).search_objects(
            "goals", visible=frozenset({other_db}), limit=8) == []

    def test_matching_ignores_case(self):
        """The database spells it MCP_DEV.CLIENTS; the visible set may say mcp_dev.clients."""
        visible = frozenset({VisibleObject("SALESDB", ObjectRef("mcp_dev", "clients"))})
        assert len(_Ranked([CLIENTS]).search_objects("c", visible=visible, limit=8)) == 1

    def test_ranking_stops_once_the_limit_is_met(self):
        provider = _Ranked([CLIENTS, RESTRICTED, GOALS])
        provider.search_objects("x", visible=_visible(CLIENTS, RESTRICTED, GOALS), limit=1)
        assert provider.yielded == 1


class TestSearchHandlerRequiresVisible:

    def test_missing_visible_is_refused(self):
        with pytest.raises(RuntimeError, match="visible"):
            glue_mod.handler({"operation": "search_objects",
                              "args": {"query": "x", "limit": 8}}, None)


# ── Publish ─────────────────────────────────────────────────────────────

class _FakeCatalog:
    def __init__(self, existing=()):
        self.tables = {t["Name"]: t for t in existing}
        self.assets = {}
        self.calls = []

    class exceptions:
        class EntityNotFoundException(Exception):
            pass

        class ConcurrentModificationException(Exception):
            pass

    def get_paginator(self, name):
        assert name == "get_tables"
        catalog = self

        class _P:
            def paginate(self, DatabaseName):
                return [{"TableList": list(catalog.tables.values())}]
        return _P()

    def create_table(self, *, DatabaseName, TableInput):
        assert TableInput["Name"] not in self.tables
        self.calls.append(("create", TableInput["Name"]))
        self.tables[TableInput["Name"]] = TableInput

    def update_table(self, *, DatabaseName, TableInput):
        assert TableInput["Name"] in self.tables
        self.calls.append(("update", TableInput["Name"]))
        self.tables[TableInput["Name"]] = TableInput

    def delete_table(self, *, DatabaseName, Name):
        self.calls.append(("delete", Name))
        del self.tables[Name]

    def update_asset(self, *, Identifier, Description):
        # Glue registers a new table's asset asynchronously: UpdateAsset fails
        # until it appears, as measured against the real service.
        if self.lag.get(Identifier, 0) > 0:
            self.lag[Identifier] -= 1
            raise self.exceptions.EntityNotFoundException(f"Asset not found: {Identifier}")
        # Another publish into the same index database is writing at the same moment.
        if self.contended.get(Identifier, 0) > 0:
            self.contended[Identifier] -= 1
            raise self.exceptions.ConcurrentModificationException("{}")
        self.assets[Identifier] = Description

    lag: dict = {}
    contended: dict = {}


class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def _catalog_provider(catalog, clock=None):
    clock = clock or _Clock()
    return glue_mod.GlueDataCatalogSchemaProvider(
        index_database="idx", region="us-east-1", catalog_id="123", owner="T", glue_client=catalog,
        clock=clock, sleep=clock.sleep)


def _ddl(schema, table, kind="table", ddl="CREATE TABLE ..."):
    return SchemaObject(schema=schema, name=table, kind=kind, definition=ddl)


class TestPublish:

    def test_tables_are_created_with_their_ddl_exactly_as_the_engine_reported_it(self):
        catalog = _FakeCatalog()
        ddl = "CREATE TABLE MCP_DEV.ACCOUNTS (\n  ID NUMBER REFERENCES MCP_DEV.CLIENTS (ID)\n);"
        stats = _catalog_provider(catalog).publish_objects(
            data_source="oracle://h:1521/salesdb", engine="oracle", objects=[_ddl("MCP_DEV", "ACCOUNTS", ddl=ddl)])
        assert stats == {"created": 1, "updated": 0, "removed": 0, "kept": 0}
        (description,) = catalog.assets.values()
        # No routing header: how to name this database depends on who is asking.
        assert description == ddl

    def test_what_is_published_reads_back_as_the_same_source(self):
        catalog = _FakeCatalog()
        _catalog_provider(catalog).publish_objects(
            data_source="salesdb_SALESPDB.paas.oracle.com", engine="oracle",
            objects=[_ddl("MCP_DEV", "CLIENTS")])
        (table,) = catalog.tables.values()
        identity = glue_mod._source_identity(table)
        assert (identity["database"], identity["engine"], identity["schema"], identity["table"]) == (
            "salesdb_SALESPDB.paas.oracle.com", "oracle", "MCP_DEV", "CLIENTS")

    def test_republishing_updates_and_removes_dropped_tables(self):
        catalog = _FakeCatalog()
        provider = _catalog_provider(catalog)
        provider.publish_objects(data_source="salesdb", engine="oracle",
                                objects=[_ddl("S", "A"), _ddl("S", "B")])
        stats = provider.publish_objects(data_source="salesdb", engine="oracle", objects=[_ddl("S", "A")])
        assert stats == {"created": 0, "updated": 1, "removed": 1, "kept": 0}
        assert len(catalog.tables) == 1

    def test_publishing_one_database_never_removes_another_databases_tables(self):
        catalog = _FakeCatalog()
        provider = _catalog_provider(catalog)
        provider.publish_objects(data_source="salesdb", engine="oracle", objects=[_ddl("S", "A")])
        provider.publish_objects(data_source="anycompany", engine="postgres", objects=[])
        assert len(catalog.tables) == 1

    def test_a_catalog_name_carries_a_hash_of_where_the_database_is_not_the_host(self):
        identity = "postgres://some-very-private-host.internal:5432/anycompany"
        name = glue_mod._catalog_name("postgres", identity, "public", "trades")
        assert "private" not in name and "internal" not in name and "5432" not in name
        assert name.startswith("postgres__") and name.endswith("__public__trades")
        assert len(name.split("__")[1]) == 12

    def test_the_same_database_always_gets_the_same_name_and_two_databases_never_do(self):
        a, b = "postgres://h1:5432/d", "postgres://h2:5432/d"
        assert glue_mod._catalog_name("postgres", a, "s", "t") == glue_mod._catalog_name("postgres", a, "s", "t")
        assert glue_mod._catalog_name("postgres", a, "s", "t") != glue_mod._catalog_name("postgres", b, "s", "t")

    def test_a_very_long_host_no_longer_lengthens_the_name(self):
        host = "x" * 400
        name = glue_mod._catalog_name("postgres", f"postgres://{host}:5432/d", "public", "trades")
        assert len(name) == len(glue_mod._catalog_name("postgres", "postgres://h:5432/d", "public", "trades"))
        assert len(name) < 255

    def test_republishing_replaces_entries_this_deployment_wrote_under_old_names(self):
        catalog = _FakeCatalog()
        provider = _catalog_provider(catalog)
        old = "oracle__oracle_h_1521_db__s__a"
        catalog.tables[old] = {"Name": old, "Parameters": {
            "source_database": "oracle://h:1521/db", "source_engine": "oracle", "created_by_stack": "T"}}
        stats = provider.publish_objects(data_source="oracle://h:1521/db", engine="oracle", objects=[_ddl("S", "A")])
        assert old not in catalog.tables
        assert stats["created"] == 1 and stats["removed"] == 1

    def test_every_table_it_creates_is_marked_as_this_deployments(self):
        catalog = _FakeCatalog()
        _catalog_provider(catalog).publish_objects(data_source="oracle://h:1521/db", engine="oracle",
                                                  objects=[_ddl("S", "A"), _ddl("S", "B")])
        assert len(catalog.tables) == 2
        for table in catalog.tables.values():
            params = table["Parameters"]
            assert (params["created_by_stack"], params["source_database"], params["source_engine"]) == \
                ("T", "oracle://h:1521/db", "oracle")

    def test_a_table_it_did_not_create_is_updated_but_never_marked(self):
        catalog = _FakeCatalog()
        name = glue_mod._catalog_name("oracle", "oracle://h:1521/db", "S", "A")
        catalog.tables[name] = {"Name": name, "Parameters": {
            "source_database": "oracle://h:1521/db", "source_engine": "oracle", "owner_note": "theirs"}}
        stats = _catalog_provider(catalog).publish_objects(data_source="oracle://h:1521/db", engine="oracle",
                                                          objects=[_ddl("S", "A")])
        assert stats["updated"] == 1
        params = catalog.tables[name]["Parameters"]
        assert "created_by_stack" not in params and params["owner_note"] == "theirs"
        assert params["source_kind"] == "table"

    def test_a_table_it_did_not_create_is_never_removed(self):
        """Neither when the source drops it nor when the database's entries are purged."""
        catalog = _FakeCatalog()
        provider = _catalog_provider(catalog)
        provider.publish_objects(data_source="oracle://h:1521/db", engine="oracle", objects=[_ddl("S", "A")])
        foreign = glue_mod._catalog_name("oracle", "oracle://h:1521/db", "S", "B")
        catalog.tables[foreign] = {"Name": foreign, "Parameters": {
            "source_database": "oracle://h:1521/db", "source_engine": "oracle"}}
        other_deployment = glue_mod._catalog_name("oracle", "oracle://h:1521/db", "S", "C")
        catalog.tables[other_deployment] = {"Name": other_deployment, "Parameters": {
            "source_database": "oracle://h:1521/db", "source_engine": "oracle", "created_by_stack": "Other"}}
        stats = provider.publish_objects(data_source="oracle://h:1521/db", engine="oracle", objects=[])
        assert stats == {"created": 0, "updated": 0, "removed": 1, "kept": 2}
        assert set(catalog.tables) == {foreign, other_deployment}

    def test_a_table_it_created_stays_marked_when_updated(self):
        catalog = _FakeCatalog()
        provider = _catalog_provider(catalog)
        provider.publish_objects(data_source="oracle://h:1521/db", engine="oracle", objects=[_ddl("S", "A")])
        provider.publish_objects(data_source="oracle://h:1521/db", engine="oracle",
                                 objects=[_ddl("S", "A", kind="view")])
        (table,) = catalog.tables.values()
        assert table["Parameters"]["created_by_stack"] == "T" and table["Parameters"]["source_kind"] == "view"

    def test_the_deployment_it_marks_tables_with_is_required(self):
        with pytest.raises(ValueError, match="owner"):
            glue_mod.GlueDataCatalogSchemaProvider(index_database="idx", region="us-east-1", catalog_id="1",
                                                   owner="", glue_client=_FakeCatalog())

    def test_two_tables_that_would_share_a_catalog_name_are_refused(self):
        catalog = _FakeCatalog()
        with pytest.raises(ValueError, match="both"):
            _catalog_provider(catalog).publish_objects(
                data_source="db", engine="postgres", objects=[_ddl("s", "a.b"), _ddl("s", "a_b")])
        assert catalog.calls == []  # nothing was written before the refusal

    def test_an_asset_that_appears_late_is_still_described(self):
        """The real failure: UpdateAsset right after CreateTable raised Asset not found."""
        catalog = _FakeCatalog()
        arn = "arn:aws:glue:us-east-1:123:table/idx/" + glue_mod._catalog_name("oracle", "salesdb", "s", "a")
        catalog.lag = {arn: 3}
        _catalog_provider(catalog).publish_objects(data_source="salesdb", engine="oracle",
                                                  objects=[_ddl("S", "A")])
        assert arn in catalog.assets

    def test_an_asset_another_publish_is_changing_is_described_once_it_is_free(self):
        """The real failure: two databases indexed at once both got ConcurrentModificationException."""
        catalog = _FakeCatalog()
        arn = "arn:aws:glue:us-east-1:123:table/idx/" + glue_mod._catalog_name("oracle", "salesdb", "s", "a")
        catalog.contended = {arn: 3}
        _catalog_provider(catalog).publish_objects(data_source="salesdb", engine="oracle",
                                                  objects=[_ddl("S", "A")])
        assert arn in catalog.assets

    def test_an_asset_that_stays_contended_fails_loudly(self):
        catalog = _FakeCatalog()
        arn = "arn:aws:glue:us-east-1:123:table/idx/" + glue_mod._catalog_name("oracle", "salesdb", "s", "a")
        catalog.contended = {arn: 10_000}
        with pytest.raises(RuntimeError, match="could not be described"):
            _catalog_provider(catalog).publish_objects(data_source="salesdb", engine="oracle",
                                                      objects=[_ddl("S", "A")])

    def test_an_asset_that_never_appears_fails_loudly(self):
        catalog = _FakeCatalog()
        arn = "arn:aws:glue:us-east-1:123:table/idx/" + glue_mod._catalog_name("oracle", "salesdb", "s", "a")
        catalog.lag = {arn: 10_000}
        with pytest.raises(RuntimeError, match="could not be described"):
            _catalog_provider(catalog).publish_objects(data_source="salesdb", engine="oracle",
                                                      objects=[_ddl("S", "A")])

    def test_an_engine_with_no_classification_is_refused(self):
        with pytest.raises(ValueError, match="classification"):
            _catalog_provider(_FakeCatalog()).publish_objects(data_source="db", engine="db2", objects=[])
