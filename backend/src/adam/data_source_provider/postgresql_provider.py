#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""PostgreSQL DataSourceProvider — queries PostgreSQL (Aurora or RDS) via pg8000.

Logs in with the Credential it is given. Scans information_schema to generate
rich CREATE TABLE DDL including primary keys, foreign keys,
unique constraints, and check constraints.
"""

import logging
from typing import Any

import pg8000.native

from adam.data_source_provider.data_source_provider import (
    Column,
    DataSourceProvider,
    comment_ddl,
    create_table_ddl,
    failure_response,
    json_value,
    rows_response,
)
from adam.data_source_provider.tls import verified_ssl_context

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class PostgreSQLProvider(DataSourceProvider):
    """Database provider backed by PostgreSQL via pg8000."""


    STATEMENT_BEST_PRACTICES = """
1. Use the search_schema tool with the user's question to find relevant tables.
2. PostgreSQL identifiers are lowercase by default. Do not quote identifiers unless they contain uppercase letters.
3. Use ILIKE for case-insensitive string matching instead of wrapping both sides in LOWER().
4. Before guessing values, run exploratory queries (LIMIT 1, SELECT DISTINCT).
5. Generate SELECT statements, run them with execute_statement, and retrieve results.
6. If no results, simplify by removing a WHERE clause or selecting distinct values from that column.
7. Use PostgreSQL features: window functions, CTEs, LIMIT n, COALESCE, generate_series. Qualify tables with their \
schema.
8. Only execute SELECT statements. Never modify data.
"""

    ADMIN_BEST_PRACTICES = """
User accounts, one per person or role:
1. Grant CONNECT on the database, USAGE on each schema, and SELECT only on the tables and views that person should see.
2. Don't register a superuser or a table owner: an owner can alter and drop its own tables.
3. Every statement runs in a read-only transaction, which blocks DML and DDL.

Scanner account, one per database:
4. Grant CONNECT, USAGE on each schema, and REFERENCES on each table and view to index. REFERENCES makes an object's \
columns visible without allowing queries on its rows; constraints and comments come from pg_catalog, which any role \
can read.
"""

    @property
    def conn(self) -> pg8000.native.Connection:
        """The session, opened on first use as the credential this provider was given."""
        if self._db is None:
            host, port, database = self._location()
            self._db = pg8000.native.Connection(
                user=self._credential.username,
                password=self._credential.password,
                host=host,
                port=port,
                database=database,
                ssl_context=verified_ssl_context(),
            )
            logger.info("Connected to PostgreSQL at %s:%s/%s as database user %s for subject %s",
                        self._row["host"], self._row["port"], self._row["database"],
                        self._credential.username, self._subject)
        return self._db

    def visible_objects(self) -> list[tuple[str, str, str]]:
        # information_schema.tables already lists only what the logged-in user can
        # access, by ownership or any privilege, including through role membership.
        # The system catalogs are excluded because they are the dictionary itself, not
        # data anyone asked about.
        return self._listed_by("""
            SELECT table_schema AS schema_name, table_name AS object_name,
                   CASE table_type WHEN 'VIEW' THEN 'view' ELSE 'table' END AS kind
            FROM information_schema.tables
            WHERE table_type IN ('BASE TABLE', 'VIEW')
            AND table_schema NOT IN ('pg_catalog', 'information_schema')
            ORDER BY 1, 2
        """)

    def execute_statement(self, statement: str, *, timeout_seconds: int, row_cap: int,
                             start_row: int = 0) -> dict[str, Any]:
        statement = statement.strip().rstrip(";")
        try:
            conn = self.conn
            # A cursor, so a huge result is read row_cap at a time instead of being
            # materialised. DECLARE refuses data-modifying CTEs and SELECT ... INTO,
            # and the READ ONLY transaction refuses everything else that writes,
            # including a SELECT calling a function that tries to.
            conn.run("START TRANSACTION READ ONLY")
            try:
                conn.run(f"SET LOCAL statement_timeout = {int(timeout_seconds) * 1000}")
                conn.run("DECLARE mcp_cursor NO SCROLL CURSOR FOR\n" + statement)
                if start_row:
                    conn.run(f"MOVE FORWARD {int(start_row)} FROM mcp_cursor")
                fetched = conn.run(f"FETCH FORWARD {int(row_cap) + 1} FROM mcp_cursor")
                columns = [col["name"] for col in conn.columns] if conn.columns else []
            finally:
                conn.run("ROLLBACK")

            values = [[json_value(v) for v in row] for row in fetched[:row_cap]]
            return rows_response(columns, values, len(fetched) > row_cap, start_row)
        except pg8000.native.Error as e:
            return failure_response(e)

    def object_ddl(self, schema: str, name: str, kind: str) -> str:
        table = name
        ddl = (self._view_ddl(schema=schema, view=table) if kind == "view"
               else create_table_ddl(kind, schema, table, self._columns(schema, table),
                                     self._get_constraints_info(schema, table)))
        comments = comment_ddl("VIEW" if kind == "view" else "TABLE", f"{schema}.{table}",
                               *self._comments(schema, table))
        return f"{ddl}\n{comments}" if comments else ddl

    def _comments(self, schema: str, table: str) -> tuple[str | None, list[tuple[str, str]]]:
        """The table's comment and each commented column's, in column order."""
        rows = self.conn.run(
            "SELECT obj_description(format('%I.%I', :schema::text, :table::text)::regclass, 'pg_class')",
            schema=schema, table=table)
        columns = self.conn.run("""
            SELECT attname, col_description(attrelid, attnum)
            FROM pg_attribute
            WHERE attrelid = format('%I.%I', :schema::text, :table::text)::regclass
            AND attnum > 0 AND NOT attisdropped
            AND col_description(attrelid, attnum) IS NOT NULL
            ORDER BY attnum""", schema=schema, table=table)
        return (rows[0][0] if rows else None), [(name, text) for name, text in columns]

    def _view_ddl(self, *, schema: str, view: str) -> str:
        """A view's own definition, not a column list.

        The query text is what carries the meaning: a restricted view's value to
        the agent is which columns and rows it leaves out, and a bare column
        list says nothing about that. It is also the only rendering that is
        valid SQL -- `CREATE VIEW x (cols);` is not.
        """
        # Cast the binds: `format()` is variadic "any", so Postgres cannot infer
        # a parameter type and rejects the statement with 42P18.
        rows = self.conn.run(
            "SELECT pg_get_viewdef("
            "format('%I.%I', :schema::text, :view_name::text)::regclass, true)",
            schema=schema, view_name=view,
        )
        if not rows or not rows[0][0]:
            raise ValueError(
                f"No definition for view {schema}.{view}; it does not exist or "
                "this connection cannot see it."
            )
        return f"CREATE VIEW {schema}.{view} AS\n{rows[0][0].strip()}"

    def _get_constraints_info(self, schema_name: str, table_name: str) -> dict:
        # pg_constraint, not information_schema: information_schema shows a table's
        # constraints only to its owner (or, for some views, a holder of a privilege
        # other than SELECT), so a scanner that only reads would publish no keys.
        info = {"primary_keys": {}, "foreign_keys": {}, "unique_constraints": {}, "check_constraints": {}}
        of_this_table = """
        FROM pg_constraint c
        JOIN pg_class t ON t.oid = c.conrelid
        JOIN pg_namespace n ON n.oid = t.relnamespace
        """
        where = "WHERE n.nspname = :schema AND t.relname = :table"

        rows = self.conn.run(f"""
        SELECT c.conname, c.contype, a.attname
        {of_this_table}
        CROSS JOIN LATERAL unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        {where} AND c.contype IN ('p', 'u')
        ORDER BY c.conname, k.ord
        """, schema=schema_name, table=table_name)
        for constraint_name, constraint_type, column_name in rows:
            kind = "primary_keys" if constraint_type == "p" else "unique_constraints"
            info[kind].setdefault(constraint_name, []).append(column_name)

        rows = self.conn.run(f"""
        SELECT c.conname, a.attname, rn.nspname, rt.relname, ra.attname
        {of_this_table}
        JOIN pg_class rt ON rt.oid = c.confrelid
        JOIN pg_namespace rn ON rn.oid = rt.relnamespace
        CROSS JOIN LATERAL unnest(c.conkey, c.confkey) WITH ORDINALITY AS k(attnum, ref_attnum, ord)
        JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
        JOIN pg_attribute ra ON ra.attrelid = c.confrelid AND ra.attnum = k.ref_attnum
        {where} AND c.contype = 'f'
        ORDER BY c.conname, k.ord
        """, schema=schema_name, table=table_name)
        for constraint_name, column_name, ref_schema, ref_table, ref_column in rows:
            fk = info["foreign_keys"].setdefault(constraint_name, {
                "columns": [], "ref_schema": ref_schema, "ref_table": ref_table, "ref_columns": [],
            })
            fk["columns"].append(column_name)
            fk["ref_columns"].append(ref_column)

        rows = self.conn.run(f"""
        SELECT c.conname, pg_get_expr(c.conbin, c.conrelid)
        {of_this_table}
        {where} AND c.contype = 'c'
        ORDER BY c.conname
        """, schema=schema_name, table=table_name)
        for constraint_name, check_clause in rows:
            info["check_constraints"][constraint_name] = check_clause

        return info

    def _columns(self, schema: str, table: str) -> list[Column]:
        rows = self.conn.run("""
        SELECT column_name, data_type, character_maximum_length,
               numeric_precision, numeric_scale, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = :schema AND table_name = :table
        ORDER BY ordinal_position
        """, schema=schema, table=table)
        return [Column(name, self._format_data_type(data_type, char_max_len, num_precision, num_scale),
                       is_nullable == "NO", column_default)
                for name, data_type, char_max_len, num_precision, num_scale, is_nullable, column_default in rows]

    @staticmethod
    def _format_data_type(data_type: str, char_max_len: int | None, num_precision: int | None,
                          num_scale: int | None) -> str:
        if data_type == "character varying":
            return f"varchar({char_max_len})" if char_max_len else "varchar"
        elif data_type == "character":
            return f"char({char_max_len})" if char_max_len else "char"
        elif data_type == "numeric" and num_precision is not None:
            if num_scale is not None and num_scale > 0:
                return f"numeric({num_precision},{num_scale})"
            return f"numeric({num_precision})"
        return data_type

