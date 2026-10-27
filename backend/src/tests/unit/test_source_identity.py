#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Two connections are the same database when they reach the same place.

The schema index is keyed on this identity, so it must not depend on the display
name a user types: anyone can register a connection under any name.
"""

import pytest

from adam.schema_provider.source_identity import source_identity


def _pg(host="db.example.com", port="5432", database="wealth", name="wealth"):
    return {"engine": "postgres", "host": host, "port": port, "database": database, "displayName": name}


def _ora(dsn="db.example.com:1521/FREEPDB1", name="FREEPDB1"):
    return {"engine": "oracle", "dsn": dsn, "database": name}


class TestSameDatabase:

    def test_the_display_name_does_not_matter(self):
        assert source_identity(_pg(name="a")) == source_identity(_pg(name="b"))

    def test_case_and_whitespace_do_not_matter(self):
        assert source_identity(_pg(host=" DB.Example.COM ")) == source_identity(_pg())

    def test_a_port_given_as_a_number_or_a_string_is_the_same(self):
        assert source_identity(_pg(port=5432)) == source_identity(_pg(port="5432"))

    def test_oracle_is_identified_by_its_dsn(self):
        assert source_identity(_ora("DB.example.com:1521/freepdb1")) == source_identity(_ora())


class TestDifferentDatabase:

    @pytest.mark.parametrize("other", [
        _pg(host="evil.example.com"),
        _pg(port="5433"),
        _pg(database="other"),
        {**_pg(), "engine": "oracle", "dsn": "db.example.com:5432/wealth"},
    ])
    def test_a_different_place_is_a_different_identity(self, other):
        assert source_identity(other) != source_identity(_pg())

    def test_different_oracle_services_differ(self):
        assert source_identity(_ora("db.example.com:1521/OTHER")) != source_identity(_ora())


class TestRefusesWhatItCannotIdentify:

    @pytest.mark.parametrize("row", [
        {},
        {"engine": "postgres"},
        {"engine": "postgres", "host": "h", "port": "5432"},
        {"engine": "oracle"},
        {"engine": "oracle", "dsn": ""},
        {"engine": "db2", "host": "h", "port": "1", "database": "d"},
    ])
    def test_an_incomplete_or_unknown_connection_raises(self, row):
        with pytest.raises(ValueError):
            source_identity(row)
