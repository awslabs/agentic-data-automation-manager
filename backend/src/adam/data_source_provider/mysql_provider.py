#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""MySQL DataSourceProvider — queries MySQL (Aurora or RDS) via PyMySQL.

Logs in with the Credential it is given. A connection is one database: the
tables listed and scanned are the ones in the connection's database that the
logged-in user holds a privilege on. DDL is the server's own SHOW CREATE output,
which carries keys, foreign keys, checks and comments.
"""

import logging
import re
from typing import Any

import pymysql
import pymysql.cursors

from adam.data_source_provider.data_source_provider import (
    DataSourceProvider,
    failure_response,
    json_value,
    rows_response,
)
from adam.data_source_provider.tls import verified_ssl_context

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# A table option that is state, not schema: it changes on every insert.
_AUTO_INCREMENT_OPTION = re.compile(r" AUTO_INCREMENT=\d+")


def _quoted(identifier: str) -> str:
    return "`" + identifier.replace("`", "``") + "`"


class MySQLProvider(DataSourceProvider):
    """Database provider backed by MySQL via PyMySQL."""

    ENGINE_LABEL = "MySQL"


    STATEMENT_BEST_PRACTICES = """
1. Use the search_schema tool with the user's question to find relevant tables.
2. Quote identifiers with backticks only when they are reserved words or contain special characters.
3. String comparison follows the column's collation, usually case-insensitive; do not wrap both sides in LOWER().
4. Before guessing values, run exploratory queries (LIMIT 1, SELECT DISTINCT).
5. Generate SELECT statements, run them with execute_statement, and retrieve results.
6. If no results, simplify by removing a WHERE clause or selecting distinct values from that column.
7. Use MySQL features: window functions, CTEs (WITH), LIMIT n, COALESCE, DATE_FORMAT. Qualify tables with their \
database.
8. Only execute SELECT statements. Never modify data.
"""

    ADMIN_BEST_PRACTICES = """
User accounts, one per person or role:
1. Grant SELECT only on the tables and views in the connection's database that person should see.
2. Don't grant ALL, CREATE, DROP, ALTER, FILE, SUPER or EXECUTE on stored programs: a program's own statements \
are not bound by the statement timeout.
3. Every statement runs in a read-only transaction, which blocks DML and DDL.

Scanner account, one per database:
4. Grant REFERENCES on each table to index. It makes the table's definition visible without allowing queries on \
its rows.
5. For each view to index, grant SHOW VIEW and SELECT: MySQL shows a view's definition only to an account that can \
also query it.
"""

    @staticmethod
    def timeout_statement(timeout_seconds: int) -> str:
        """The session setting that cancels a statement running longer than this.

        MySQL's applies to SELECT only, which is all an agent sends."""
        return f"SET SESSION max_execution_time = {int(timeout_seconds) * 1000}"

    @property
    def conn(self) -> pymysql.connections.Connection:
        """The session, opened on first use as the credential this provider was given."""
        if self._db is None:
            host, port, database = self._location()
            self._db = pymysql.connect(
                user=self._credential.username,
                password=self._credential.password,
                host=host,
                port=port,
                database=database,
                ssl=verified_ssl_context(),
                charset="utf8mb4",
                autocommit=False,
            )
            logger.info("Connected to %s at %s:%s/%s as database user %s for subject %s",
                        self.ENGINE_LABEL, self._row["host"], self._row["port"], self._row["database"],
                        self._credential.username, self._subject)
        return self._db

    def visible_objects(self) -> list[tuple[str, str, str]]:
        # information_schema.tables lists only what the logged-in user holds a
        # privilege on, including through roles that are active.
        return self._listed_by("""
            SELECT table_schema AS schema_name, table_name AS object_name,
                   CASE table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END AS kind
            FROM information_schema.tables
            WHERE table_schema = DATABASE() AND table_type IN ('BASE TABLE', 'VIEW')
            ORDER BY 1, 2
        """)

    def execute_statement(self, statement: str, *, timeout_seconds: int, row_cap: int,
                             start_row: int = 0) -> dict[str, Any]:
        statement = statement.strip().rstrip(";")
        try:
            conn = self.conn
            with conn.cursor() as setup:
                # Every transaction in the session, not just the next: DDL ends a
                # transaction in these engines, and the one it starts is read-only too.
                setup.execute("SET SESSION TRANSACTION READ ONLY")
                setup.execute(self.timeout_statement(timeout_seconds))
                # The server stops producing rows here, so the ones past the cap are never
                # sent. A LIMIT in the statement overrides it; the timeout bounds those.
                setup.execute(f"SET SESSION sql_select_limit = {int(start_row) + int(row_cap) + 1}")
            # Unbuffered, so a result is never held in memory beyond the page returned.
            # Multiple statements are refused by the driver's defaults.
            cursor = conn.cursor(pymysql.cursors.SSCursor)
            cursor.execute(statement)
            columns = [d[0] for d in cursor.description] if cursor.description else []
            skipped = 0
            while columns and skipped < start_row:
                chunk = cursor.fetchmany(min(row_cap, start_row - skipped))
                if not chunk:
                    break
                skipped += len(chunk)
            fetched = cursor.fetchmany(row_cap + 1) if columns else []
            truncated = len(fetched) > row_cap
            cursor.close()
            conn.rollback()
            values = [[json_value(v) for v in row] for row in fetched[:row_cap]]
            return rows_response(columns, values, truncated, start_row)
        except pymysql.MySQLError as e:
            # The session may hold an unread result; the next call opens a new one.
            self.close()
            return failure_response(e)

    def object_ddl(self, schema: str, name: str, kind: str) -> str:
        """The server's own DDL for one table or view, qualified by its database. Raises on failure."""
        word = "VIEW" if kind == "view" else "TABLE"
        with self.conn.cursor() as cursor:
            cursor.execute(f"SHOW CREATE {word} {_quoted(schema)}.{_quoted(name)}")
            row = cursor.fetchone()
        if not row or not row[1]:
            raise ValueError(f"No definition for {schema}.{name}; it does not exist or this "
                             "connection cannot see it.")
        # The server names the object without its database; the agent needs it qualified.
        unqualified = f" {word} {_quoted(name)} "
        if unqualified not in row[1]:
            raise ValueError(f"SHOW CREATE {word} for {schema}.{name} did not name it as expected: {row[1][:200]}")
        ddl = row[1].replace(unqualified, f" {word} {_quoted(schema)}.{_quoted(name)} ", 1)
        return ddl if kind == "view" else _AUTO_INCREMENT_OPTION.sub("", ddl)
