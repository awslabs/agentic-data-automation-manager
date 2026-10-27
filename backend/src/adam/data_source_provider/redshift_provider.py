#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Redshift DataSourceProvider — queries Amazon Redshift (provisioned or Serverless) via redshift_connector.

Logs in with the Credential it is given, over TLS verified against the CA bundle
redshift_connector ships. DDL is Redshift's own SHOW TABLE and SHOW VIEW output,
which carries keys, distribution and sort keys, with table and column comments added.
"""

import logging
from typing import Any

import redshift_connector

from adam.data_source_provider.data_source_provider import (
    DataSourceProvider,
    comment_ddl,
    failure_response,
    json_value,
    rows_response,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Redshift's largest FETCH FORWARD count.
_FETCH_MAX = 1000


def _quoted(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


class RedshiftProvider(DataSourceProvider):
    """Database provider backed by Amazon Redshift via redshift_connector."""


    STATEMENT_BEST_PRACTICES = """
1. Use the search_schema tool with the user's question to find relevant tables.
2. Redshift identifiers are lowercase by default. Do not quote identifiers unless they contain uppercase letters.
3. Use ILIKE for case-insensitive string matching instead of wrapping both sides in LOWER().
4. Before guessing values, run exploratory queries (LIMIT 1, SELECT DISTINCT).
5. Generate SELECT statements, run them with execute_statement, and retrieve results.
6. If no results, simplify by removing a WHERE clause or selecting distinct values from that column.
7. Use Redshift features: window functions, CTEs, LIMIT n, COALESCE, DATEADD, DATEDIFF. Qualify tables with their \
schema. Primary and foreign keys are informational in Redshift: do not assume they are unique or enforced.
8. Only execute SELECT statements. Never modify data.
"""

    ADMIN_BEST_PRACTICES = """
User accounts, one per person or role:
1. Grant USAGE on each schema and SELECT only on the tables and views that person should see.
2. Don't register a superuser or a table owner: an owner can alter and drop its own tables.
3. Every statement runs in a read-only transaction, which blocks DML and DDL.

Scanner account, one per database:
4. Grant USAGE on each schema and REFERENCES on each table to index. REFERENCES makes a table's definition visible \
without allowing queries on its rows.
5. For each view to index, grant SELECT.
"""

    @property
    def conn(self) -> redshift_connector.Connection:
        """The session, opened on first use as the credential this provider was given."""
        if self._db is None:
            host, port, database = self._location()
            self._db = redshift_connector.connect(
                user=self._credential.username,
                password=self._credential.password,
                host=host,
                port=port,
                database=database,
                ssl=True,
                sslmode="verify-full",
            )
            # Transactions are begun here, explicitly read-only, not by the driver.
            self._db.autocommit = True
            logger.info("Connected to Redshift at %s:%s/%s as database user %s for subject %s",
                        self._row["host"], self._row["port"], self._row["database"],
                        self._credential.username, self._subject)
        return self._db

    def visible_objects(self) -> list[tuple[str, str, str]]:
        # information_schema.tables lists what the logged-in user holds a privilege on.
        # Objects owned by rdsdb (usesysid 1) are Redshift's own: its catalogs and system
        # views, some of which it lists to users who may not describe them. A schema's
        # owner says nothing: rdsdb owns public too.
        return self._listed_by("""
            SELECT t.table_schema AS schema_name, t.table_name AS object_name,
                   CASE t.table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END AS kind
            FROM information_schema.tables t
            JOIN pg_namespace n ON n.nspname = t.table_schema
            JOIN pg_class c ON c.relnamespace = n.oid AND c.relname = t.table_name
            WHERE t.table_type IN ('BASE TABLE', 'VIEW') AND c.relowner <> 1
            ORDER BY 1, 2
        """)

    def execute_statement(self, statement: str, *, timeout_seconds: int, row_cap: int,
                             start_row: int = 0) -> dict[str, Any]:
        statement = statement.strip().rstrip(";")
        try:
            cursor = self.conn.cursor()
            cursor.execute(f"SET statement_timeout TO {int(timeout_seconds) * 1000}")
            # A cursor, so a huge result is read a page at a time instead of being
            # materialised. DECLARE accepts only a query, which is what refuses every
            # write the integration tests try; READ ONLY is the backstop should a query
            # ever be able to call something that writes.
            cursor.execute("BEGIN READ ONLY")
            try:
                cursor.execute("DECLARE mcp_cursor NO SCROLL CURSOR FOR\n" + statement)
                self._fetch(cursor, start_row)
                fetched = self._fetch(cursor, row_cap + 1)
                columns = [d[0] for d in cursor.description] if cursor.description else []
            finally:
                cursor.execute("ROLLBACK")
            values = [[json_value(v) for v in row] for row in fetched[:row_cap]]
            return rows_response(columns, values, len(fetched) > row_cap, start_row)
        except redshift_connector.Error as e:
            # A failed statement can leave the session in an aborted transaction.
            self.close()
            return failure_response(e)

    @staticmethod
    def _fetch(cursor, count: int) -> list:
        rows: list = []
        while len(rows) < count:
            cursor.execute(f"FETCH FORWARD {min(_FETCH_MAX, count - len(rows))} FROM mcp_cursor")
            page = cursor.fetchall() or []
            rows += page
            if not page:
                break
        return rows

    def object_ddl(self, schema: str, name: str, kind: str) -> str:
        """Redshift's own DDL for one table or view, with its comments. Raises on failure."""
        cursor = self.conn.cursor()
        cursor.execute(f"SHOW {'VIEW' if kind == 'view' else 'TABLE'} {_quoted(schema)}.{_quoted(name)}")
        rows = cursor.fetchall()
        if not rows or not rows[0][0]:
            raise ValueError(f"No definition for {schema}.{name}; it does not exist or this "
                             "connection cannot see it.")
        ddl = rows[0][0].strip()
        comments = comment_ddl("VIEW" if kind == "view" else "TABLE", f"{schema}.{name}",
                               *self._comments(cursor, schema, name))
        return f"{ddl}\n{comments}" if comments else ddl

    @staticmethod
    def _comments(cursor, schema: str, name: str) -> tuple[str | None, list[tuple[str, str]]]:
        """The table's comment and each commented column's, in column order."""
        cursor.execute("""
            SELECT d.objsubid, a.attname, d.description
            FROM pg_description d
            JOIN pg_class c ON c.oid = d.objoid
            JOIN pg_namespace n ON n.oid = c.relnamespace
            LEFT JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = d.objsubid
            WHERE n.nspname = %s AND c.relname = %s
            ORDER BY d.objsubid""", (schema, name))
        rows = cursor.fetchall() or []
        table = next((text for sub, _, text in rows if sub == 0), None)
        return table, [(column, text) for sub, column, text in rows if sub != 0]
