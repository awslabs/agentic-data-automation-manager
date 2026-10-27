#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The base classes every data source provider implements.

DataSourceProvider is what callers use: scan a source's schema, execute one
read-only statement in its own language, list what the caller may see, and check
a login. Every source, SQL or not, subclasses it and implements those four.
Callers use only the public methods here; none branches on the engine name.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any

from adam.data_source_provider.credential_provider import Credential
from adam.shared.schema_types import ObjectRef, SchemaObject
from adam.shared.statement_limits import LimitError, checked, page, within

logger = logging.getLogger(__name__)


def unique_labels(columns: list[str]) -> list[str]:
    """The result's column names, each unique: a join returns `id` twice, and a row keyed
    by name would keep only the last. A repeat becomes `id_2`, `id_3`, ... ."""
    labels: list[str] = []
    for column in columns:
        label, n = column, 1
        while label in labels or (n > 1 and label in columns):
            n += 1
            label = f"{column}_{n}"
        labels.append(label)
    return labels


def rows_response(columns: list[str], values: list[list], truncated: bool, start_row: int = 0) -> dict[str, Any]:
    """Each row as a dict keyed by its column's unique label (unique_labels), which is also
    what `columns` reports. next_start_row is where the next page starts when the result
    had more rows."""
    labels = unique_labels(columns)
    rows = [dict(zip(labels, row, strict=True)) for row in values]
    return {"status": {"code": 200}, "columns": labels, "rows": rows, "truncated": truncated,
            "start_row": start_row, "next_start_row": start_row + len(rows) if truncated else None}


def comment_ddl(object_word: str, name: str, table_comment: str | None,
                column_comments: list[tuple[str, str]]) -> str:
    """COMMENT ON statements for one table or view and its columns.

    Comments are often the only place a coded name (PYMT_DT) is explained, and
    COMMENT ON is how DDL already carries them, so they travel in the same text.
    """
    def literal(text: str) -> str:
        return "'" + text.replace("'", "''") + "'"
    lines = [f"COMMENT ON {object_word} {name} IS {literal(table_comment)};"] if table_comment else []
    lines += [f"COMMENT ON COLUMN {name}.{column} IS {literal(comment)};"
              for column, comment in column_comments]
    return "\n".join(lines)


def json_value(val: Any) -> Any:
    """A driver's value as JSON can carry it: dates and times as ISO text, an interval as
    text, a whole Decimal as an int and any other as a float, bytes as hex."""
    if isinstance(val, (datetime, date, time)):
        return val.isoformat()
    if isinstance(val, timedelta):
        return str(val)
    if isinstance(val, Decimal):
        return float(val) if val % 1 else int(val)
    if isinstance(val, (bytes, bytearray)):
        return val.hex()
    return val


@dataclass(frozen=True)
class Column:
    """One column as a CREATE TABLE statement states it."""
    name: str
    type: str
    not_null: bool
    default: str | None


def create_table_ddl(kind: str, schema: str, table: str, columns: list[Column], constraints: dict) -> str:
    """CREATE TABLE for an engine that describes a table by its columns and constraints.

    `constraints` holds primary_keys and unique_constraints ({name: [columns]}), foreign_keys
    ({name: {columns, ref_schema, ref_table, ref_columns}}) and check_constraints ({name:
    condition}). A one-column primary or foreign key is stated on its column, the rest after.
    """
    if not columns:
        raise ValueError(f"{schema}.{table} produced no column definitions; it does not exist "
                         "or this connection cannot see it.")
    definitions = []
    for column in columns:
        definition = f"    {column.name} {column.type}" + (" NOT NULL" if column.not_null else "")
        if any(cols == [column.name] for cols in constraints["primary_keys"].values()):
            definition += " PRIMARY KEY"
        fk = next((fk for fk in constraints["foreign_keys"].values() if fk["columns"] == [column.name]), None)
        if fk:
            definition += f" REFERENCES {fk['ref_schema']}.{fk['ref_table']} ({', '.join(fk['ref_columns'])})"
        if column.default and column.default.strip():
            definition += f" DEFAULT {column.default.strip()}"
        definitions.append(definition)
    definitions += [f"    CONSTRAINT {name} PRIMARY KEY ({', '.join(cols)})"
                    for name, cols in constraints["primary_keys"].items() if len(cols) > 1]
    definitions += [f"    CONSTRAINT {name} UNIQUE ({', '.join(cols)})"
                    for name, cols in constraints.get("unique_constraints", {}).items()]
    definitions += [f"    CONSTRAINT {name} CHECK ({condition})"
                    for name, condition in constraints["check_constraints"].items()]
    return f"CREATE {kind.upper()} {schema}.{table} (\n" + ",\n".join(definitions) + "\n);"


def failure_response(error: Exception) -> dict[str, Any]:
    logger.error("execute_statement failed: %s", error)
    return {"status": {"code": 500}, "error": str(error)}


class DataSourceProvider(ABC):
    """What every data source implements, whatever language its statements are in.

    A provider is built for one call, from the calling user's registry row and
    the credential issued for it, and connects the first time it is used.
    Nothing is opened at construction, so building one is free, and `close()`
    ends the session. A source implements:

    - execute_statement(): run one read-only statement in the source's own
      language and return its rows. The statement is not inspected: the source
      refuses what the logged-in account may not do, and the provider puts the
      session in the source's own read-only mode, with the timeout and row cap
      it is given.
    - scan_schema(): the definition of every object this session can see.
    - visible_objects(): which objects this session can see.
    - probe(): prove a login works.

    and supplies, as class attributes, the text only it can write:

    - STATEMENT_BEST_PRACTICES: how to write statements for this source, for the agent.
    - ADMIN_BEST_PRACTICES: how to set up accounts for this source, for its administrator.
    """

    # For the statements the provider runs itself (the login check and the visible-
    # objects list). The agent's statements run with the limits an administrator set
    # for the data source, which the dispatcher sends with each one.
    INTERNAL_TIMEOUT_SECONDS: int = 30
    # A session that can see more objects than this is refused rather than searched
    # from a partial list.
    VISIBLE_OBJECTS_LIMIT: int = 1000

    # Declared abstract so a source that forgets one cannot be built, and the error
    # names it. Setting the attribute in the source is what satisfies them.

    @property
    @abstractmethod
    def STATEMENT_BEST_PRACTICES(self) -> str:
        """How to write statements for this source, for the agent that writes them.

        The agent has no other way to learn the dialect, and without this it sends
        `LIMIT 1` to Oracle. Returned with schema search results."""

    @property
    @abstractmethod
    def ADMIN_BEST_PRACTICES(self) -> str:
        """How an administrator should set up user and scanner accounts for this
        source, shown on the admin page.

        Advice, not a check: the stack does not refuse an account that ignores
        it, because a check that fails on some versions blocks every connection."""

    def __init__(self, connection: dict[str, str], credential: Credential, subject: str):
        # `subject` is the app user the call is for. It is logged with the
        # source's user at connect time: the half of the audit trail the
        # dispatcher cannot see.
        self._row = connection
        self._credential = credential
        self._subject = subject
        self._db = None

    @abstractmethod
    def execute_statement(self, statement: str, *, timeout_seconds: int, row_cap: int,
                          start_row: int = 0) -> dict[str, Any]:
        """Run one read-only statement, cancelled after timeout_seconds, skip start_row
        of its rows and return at most row_cap more ('truncated' and 'next_start_row'
        say whether there were more, and where the next page starts).

        Returns 'status', 'columns', 'rows' and 'truncated' on success, or
        'status' and 'error' on failure.
        """

    def scan_schema(self) -> list[SchemaObject]:
        """The definition of every object this session can see. Raises if any visible object
        cannot be described: an object published with a placeholder definition is one the
        agent writes wrong statements for."""
        return [SchemaObject(schema=schema, name=name, kind=kind, definition=self.object_ddl(schema, name, kind))
                for schema, name, kind in self.visible_objects()]

    @abstractmethod
    def object_ddl(self, schema: str, name: str, kind: str) -> str:
        """One object's definition. For a database: DDL with primary keys, foreign keys, unique
        and check constraints, and table and column comments -- the format text-to-SQL models
        expect for accurate query generation. Raises when it cannot be described."""

    @abstractmethod
    def visible_objects(self) -> list[tuple[str, str, str]]:
        """(schema, name, kind) of every object this session's user can see.

        Answered by the source for the logged-in user, including access granted
        through roles. This is the authority for which schema search results a
        caller may be shown, so it never returns a partial list: a failure or a
        list longer than VISIBLE_OBJECTS_LIMIT raises.
        """

    # The cheapest statement the source answers, to check a login.
    PROBE_STATEMENT = "SELECT 1"

    def probe(self) -> dict[str, Any]:
        """Check, when a user registers a connection, that the login works.

        Raises with the source's own message when it does not."""
        result = self.execute_statement(self.PROBE_STATEMENT, timeout_seconds=self.INTERNAL_TIMEOUT_SECONDS,
                                        row_cap=1)
        if result["status"]["code"] != 200:
            raise RuntimeError(result["error"])
        return {"ok": True}

    def _location(self) -> tuple[str, int, str]:
        """(host, port, database) of a source located by host, or ValueError naming what is missing."""
        missing = [f for f in ("host", "port", "database") if not self._row.get(f)]
        if missing:
            raise ValueError(f"Connection {self._row.get('connectionId', '?')} is missing {', '.join(missing)}.")
        return self._row["host"], int(self._row["port"]), self._row["database"]

    def close(self) -> None:
        """End the session, best effort. Safe before any use and when repeated.

        A failure to close must not mask the caller's own result or error, but
        it is logged so a leak is visible rather than silent.
        """
        db, self._db = self._db, None
        if db is None:
            return
        try:
            db.close()
        except Exception:
            logger.warning("Failed to close a data source session", exc_info=True)

    def _listed_by(self, statement: str) -> list[tuple[str, str, str]]:
        """visible_objects from a statement returning schema_name, object_name and kind.

        Never a partial list: a failure, or more than VISIBLE_OBJECTS_LIMIT rows, raises.
        """
        result = self.execute_statement(statement, timeout_seconds=self.INTERNAL_TIMEOUT_SECONDS,
                                        row_cap=self.VISIBLE_OBJECTS_LIMIT)
        if result["status"]["code"] != 200:
            raise RuntimeError(f"Could not list visible objects: {result['error']}")
        if result["truncated"]:
            raise RuntimeError(f"This session can see more than {self.VISIBLE_OBJECTS_LIMIT} objects; "
                               "refusing to work from a partial list.")
        rows = [{k.lower(): v for k, v in row.items()} for row in result["rows"]]
        return [(r["schema_name"], r["object_name"], r["kind"]) for r in rows]

    def visible_object_refs(self) -> list[ObjectRef]:
        return [ObjectRef(schema, name) for schema, name, _ in self.visible_objects()]

    def handle(self, event: dict) -> dict[str, Any]:
        """Dispatch a provider-Lambda event on event['operation']."""
        operation = event.get("operation", "")
        args = event.get("args", {})

        if operation == "execute_statement":
            # The administrator's limits for this data source, from its registration: a caller
            # may ask for less, never more. Refused while the administrator has set none.
            try:
                ceiling_seconds, ceiling_rows = checked(self._row.get("statementTimeoutSeconds"),
                                                        self._row.get("rowCap"))
                timeout_seconds = within(args.get("timeout_seconds"), ceiling_seconds,
                                         "The statement timeout (seconds)")
                row_cap = within(args.get("row_cap"), ceiling_rows, "The row cap")
                start_row, _ = page(args.get("start_row"), row_cap, row_cap)
            except LimitError as e:
                return {"error": str(e)}
            return {"response": self.execute_statement(
                args.get("statement", ""), timeout_seconds=timeout_seconds, row_cap=row_cap,
                start_row=start_row)}
        if operation == "get_statement_best_practices":
            return {"response": self.STATEMENT_BEST_PRACTICES}
        if operation == "probe":
            return {"response": self.probe()}
        if operation == "visible_object_refs":
            return {"response": [r.to_dict() for r in self.visible_object_refs()]}
        return {"error": f"Unknown operation: {operation}"}
