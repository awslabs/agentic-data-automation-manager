#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A search hit must name something the agent can actually query.

A Glue table's name exists in no database. `StorageDescriptor.Location` holds the
real path, as publish writes it: the source's identity (engine and where it
lives), then the schema and table, each quoted. The identity is what the search
results are matched to the caller's connections on, so one `search_schema` hit
leads straight into a query.

Fixtures are built as publish builds a table: `_location(source_identity(...))`,
the engine's classification and `_catalog_name`.
"""

import pytest

from adam.schema_provider.glue_data_catalog_schema_provider import (
    _CLASSIFICATION_FROM_ENGINE,
    SourceIdentityError,
    _catalog_name,
    _location,
    _source_identity,
    _split_location,
)
from adam.schema_provider.source_identity import source_identity

PG = {"engine": "postgres", "host": "wealth.cluster.example.com", "port": "5432", "database": "anycompany"}
ORACLE = {"engine": "oracle", "dsn": "db.example.com:1521/salesdb_SALESPDB.paas.oracle.com"}


def _published(connection, schema, table):
    identity = source_identity(connection)
    return {
        "Name": _catalog_name(connection["engine"], identity, schema, table),
        "DatabaseName": "idx",
        "Parameters": {"classification": _CLASSIFICATION_FROM_ENGINE[connection["engine"]]},
        "StorageDescriptor": {"Location": _location(identity, schema, table)},
    }


def _pg_table():
    return _published(PG, "public", "positions")


def _oracle_table():
    return _published(ORACLE, "MCP_DEV", "CLIENTS")


class TestSplitLocation:

    def test_quoted_segments_containing_dots_and_colons_stay_whole(self):
        """An identity holds a host, a port and, for Oracle, a dotted service name."""
        assert _split_location(_oracle_table()["StorageDescriptor"]["Location"]) == [
            source_identity(ORACLE), "MCP_DEV", "CLIENTS",
        ]


class TestSourceIdentity:

    @pytest.mark.parametrize("connection, table", [(PG, _pg_table), (ORACLE, _oracle_table)])
    def test_the_database_is_the_connections_identity(self, connection, table):
        assert _source_identity(table())["database"] == source_identity(connection)

    def test_engine_comes_from_the_classification(self):
        """`postgresql` has to become `postgres`, the registry's spelling.

        execute_statement looks the engine up in _ENGINE_PROVIDER_SSM, which is keyed
        on `oracle` and `postgres`. Passing `postgresql` through finds no
        provider function.
        """
        assert _source_identity(_pg_table())["engine"] == "postgres"
        assert _source_identity(_oracle_table())["engine"] == "oracle"

    def test_qualified_name_is_schema_scoped(self):
        """Neither database puts these in the connecting user's default schema."""
        assert _source_identity(_pg_table())["qualified_name"] == "public.positions"
        assert _source_identity(_oracle_table())["qualified_name"] == "MCP_DEV.CLIENTS"

    def test_a_missing_location_raises(self):
        """Better no hit than a hit naming a table that does not exist."""
        table = _pg_table()
        table["StorageDescriptor"]["Location"] = ""
        with pytest.raises(SourceIdentityError, match="does not name a database"):
            _source_identity(table)

    def test_a_single_segment_location_raises(self):
        table = _pg_table()
        table["StorageDescriptor"]["Location"] = '"positions"'
        with pytest.raises(SourceIdentityError):
            _source_identity(table)

    def test_an_unmapped_classification_raises(self):
        """An engine must be added deliberately, not routed by guess.

        Defaulting the engine would send the statement to whichever provider
        happened to be first and fail with a driver error far from the cause.
        """
        table = _pg_table()
        table["Parameters"]["classification"] = "sqlserver"
        with pytest.raises(SourceIdentityError, match="maps to no engine"):
            _source_identity(table)

    def test_classification_matching_is_case_insensitive(self):
        table = _pg_table()
        table["Parameters"]["classification"] = "PostgreSQL"
        assert _source_identity(table)["engine"] == "postgres"


class TestPublishedLocationRoundTrips:
    """What publish writes is what search reads back as the source identity."""

    @pytest.mark.parametrize("database,schema,table", [
        ("anycompany", "public", "positions"),
        ("salesdb_SALESPDB.paas.oracle.com", "MCP_DEV", "CLIENTS"),
    ])
    def test_round_trip(self, database, schema, table):
        identity = _source_identity({
            "Name": "x", "Parameters": {"classification": "oracle"},
            "StorageDescriptor": {"Location": _location(database, schema, table)},
        })
        assert (identity["database"], identity["schema"], identity["table"]) == (
            database, schema, table)

    def test_an_identifier_with_a_quote_is_refused(self):
        with pytest.raises(ValueError, match="quote"):
            _location('we"ird', "s", "t")
