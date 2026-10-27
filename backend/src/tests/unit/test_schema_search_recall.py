#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""search_objects searches the whole query and each term, and unions the hits.

SearchAssets recall drops as SearchText grows: on the demo catalog the full
keyword string found only the Postgres goals table, while "clients" alone found
the Oracle CLIENTS table a cross-database question also needs.
"""

import pytest

from adam.schema_provider import glue_data_catalog_schema_provider as mod
from adam.schema_provider.schema_provider import VisibleObject
from adam.shared.schema_types import ObjectRef


class TestQueryTerms:

    def test_short_domain_terms_are_kept(self):
        assert mod._query_terms("IRA 529 rm") == ["IRA", "529", "rm"]

    def test_no_term_is_dropped_for_being_common(self):
        assert mod._query_terms("clients with goals") == ["clients", "with", "goals"]

    def test_duplicates_collapse_in_order(self):
        assert mod._query_terms("clients goals clients") == ["clients", "goals"]

    def test_there_is_no_cap(self):
        query = " ".join(f"t{i}" for i in range(50))
        assert len(mod._query_terms(query)) == 50

    def test_a_single_term_query_adds_no_terms(self):
        assert mod._query_terms("portfolio") == []


class _ReadError(Exception):
    pass


INDEX = "adamdev_schema_index"


class _FakeGlue:
    """Returns a different table per search text, as the real catalog does."""

    class exceptions:
        class EntityNotFoundException(Exception):
            pass

    BY_TEXT = {
        "private banking clients retirement goals": ["portfolio_goals"],
        "private": ["portfolio_goals"],
        "clients": ["mcp_dev_clients", "portfolio_goals", "someone_elses_table"],
        "goals": ["portfolio_goals"],
        "portfolio": ["portfolio_goals"],
    }
    LOCATION = {
        "mcp_dev_clients": ("oracle", '"salesdb".MCP_DEV.CLIENTS'),
        "portfolio_goals": ("postgresql", '"anycompany"."public"."portfolio_goals"'),
    }

    def __init__(self, fail_with=None):
        self.searched = []
        self.read = []
        self.fail_with = fail_with

    def search_assets(self, *, SearchText, FilterClause, MaxResults):
        self.searched.append(SearchText)
        items = []
        for n in self.BY_TEXT.get(SearchText, []):
            db = "other-teams-db" if n == "someone_elses_table" else INDEX
            items.append({"Id": f"arn:aws:glue:us-east-1:1:table/{db}/{n}",
                          "AssetDescription": f"CREATE TABLE {n} (...)"})
        return {"Items": items}

    def get_table(self, *, DatabaseName, Name):
        self.read.append(Name)
        if self.fail_with:
            raise self.fail_with
        classification, location = self.LOCATION[Name]
        return {"Table": {"Name": Name, "Parameters": {"classification": classification},
                          "StorageDescriptor": {"Location": location}}}


def _provider(glue):
    return mod.GlueDataCatalogSchemaProvider(
        index_database=INDEX, region="us-east-1", catalog_id="1", owner="T", glue_client=glue)


EVERYTHING = frozenset({
    VisibleObject("salesdb", ObjectRef("MCP_DEV", "CLIENTS")),
    VisibleObject("anycompany", ObjectRef("public", "portfolio_goals")),
})

QUERY = "private banking clients retirement goals"


class TestSearchTables:

    def test_the_query_and_every_term_are_searched(self):
        glue = _FakeGlue()
        _provider(glue).search_objects(QUERY, visible=EVERYTHING, limit=8)
        assert sorted(glue.searched) == sorted([QUERY, *QUERY.split()])

    def test_a_single_term_is_searched_once(self):
        glue = _FakeGlue()
        _provider(glue).search_objects("portfolio", visible=EVERYTHING, limit=8)
        assert glue.searched == ["portfolio"]

    def test_both_databases_come_back(self):
        results = _provider(_FakeGlue()).search_objects(QUERY, visible=EVERYTHING, limit=8)
        assert {r["engine"] for r in results} == {"oracle", "postgres"}

    def test_a_table_appears_once_even_when_several_terms_find_it(self):
        names = [r["object_name"] for r in
                 _provider(_FakeGlue()).search_objects(QUERY, visible=EVERYTHING, limit=8)]
        assert len(names) == len(set(names)), f"duplicates in {names}"

    def test_tables_outside_the_index_database_are_never_read(self):
        """The account's catalog holds other people's tables; they are not ours to return."""
        glue = _FakeGlue()
        _provider(glue).search_objects(QUERY, visible=EVERYTHING, limit=8)
        assert "someone_elses_table" not in glue.read

    def test_a_read_failure_raises(self):
        """A throttle or network error must not masquerade as 'no such table'."""
        with pytest.raises(_ReadError):
            _provider(_FakeGlue(fail_with=_ReadError())).search_objects(
                QUERY, visible=EVERYTHING, limit=8)

    def test_reading_stops_once_the_limit_is_met(self):
        """Each candidate costs a catalog read; past the limit they are not made."""
        glue = _FakeGlue()
        _provider(glue).search_objects(QUERY, visible=EVERYTHING, limit=1)
        assert len(glue.read) == 1
