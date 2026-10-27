#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""MariaDB DataSourceProvider — queries MariaDB (RDS) via PyMySQL.

The MySQL provider, with MariaDB's statement timeout and its own advice text.
"""

from adam.data_source_provider.mysql_provider import MySQLProvider


class MariaDBProvider(MySQLProvider):
    """Database provider backed by MariaDB via PyMySQL."""

    ENGINE_LABEL = "MariaDB"

    STATEMENT_BEST_PRACTICES = """
1. Use the search_schema tool with the user's question to find relevant tables.
2. Quote identifiers with backticks only when they are reserved words or contain special characters.
3. String comparison follows the column's collation, usually case-insensitive; do not wrap both sides in LOWER().
4. Before guessing values, run exploratory queries (LIMIT 1, SELECT DISTINCT).
5. Generate SELECT statements, run them with execute_statement, and retrieve results.
6. If no results, simplify by removing a WHERE clause or selecting distinct values from that column.
7. Use MariaDB features: window functions, CTEs (WITH), LIMIT n, COALESCE, DATE_FORMAT. Qualify tables with their \
database.
8. Only execute SELECT statements. Never modify data.
"""

    ADMIN_BEST_PRACTICES = """
User accounts, one per person or role:
1. Grant SELECT only on the tables and views in the connection's database that person should see.
2. Don't grant ALL, CREATE, DROP, ALTER, FILE or SUPER.
3. Every statement runs in a read-only transaction, which blocks DML and DDL.

Scanner account, one per database:
4. Grant REFERENCES on each table to index. It makes the table's definition visible without allowing queries on \
its rows.
5. For each view to index, grant SHOW VIEW and SELECT: MariaDB shows a view's definition only to an account that \
can also query it.
"""

    @staticmethod
    def timeout_statement(timeout_seconds: int) -> str:
        """MariaDB's applies to every statement, in seconds."""
        return f"SET SESSION max_statement_time = {int(timeout_seconds)}"
