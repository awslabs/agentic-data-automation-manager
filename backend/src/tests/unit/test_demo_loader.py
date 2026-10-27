#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The pieces of the demo loader that are not the data: lifting READ ONLY around a
write, and the port-forwards that reach private databases."""


import pytest

from demo_dataset import build


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, **binds):
        self.conn.statements.append(sql)

    def fetchone(self):
        return self.conn.row


class _AdminConn:
    """Records the SQL an admin session is asked to run."""

    def __init__(self, read_only: str | None):
        self.row = None if read_only is None else (read_only,)
        self.statements: list[str] = []

    def cursor(self):
        return _Cursor(self)


class TestOracleWritable:

    def test_a_writable_owner_is_left_exactly_as_it_was(self):
        admin = _AdminConn("NO")
        with build.oracle_writable(admin, "mcp_dev"):
            pass
        assert not any(s.startswith("ALTER USER") for s in admin.statements)

    def test_an_owner_that_does_not_exist_is_an_error(self):
        with pytest.raises(RuntimeError, match="does not exist"):
            with build.oracle_writable(_AdminConn(None), "nobody"):
                pass

    @pytest.mark.parametrize("name", ["x; DROP USER y", "a b", "", "1abc", "a-b"])
    def test_an_owner_name_that_is_not_a_plain_identifier_is_refused_before_any_sql(self, name):
        admin = _AdminConn("YES")
        with pytest.raises(ValueError, match="identifier"):
            with build.oracle_writable(admin, name):
                pass
        assert admin.statements == []
