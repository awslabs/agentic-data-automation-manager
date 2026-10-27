#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The data source tools' MCP function: a thin dispatcher to the provider services.

Invoked by AgentCore Gateway as a tool target for search_schema and
execute_statement. Routes each call to the provider Lambda that serves it.
"""

import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from agent_provider import gateway_event
from shared.provider_client import (
    CONNECTION_REGISTRY_SSM,
    ENGINE_PROVIDER_SSM,
    SCHEMA_PROVIDER_SSM,
    ProviderError,
    call_provider,
)
from shared.statement_limits import LimitError, page

logger = logging.getLogger()
logger.setLevel(logging.INFO)


# The limits every statement against a data source runs with, set by an administrator.
_LIMITS = ("statementTimeoutSeconds", "rowCap")


def _connections(user_id: str) -> list[dict]:
    """The caller's connections, from the connection registry, each with its
    data source's registered name and limits."""
    return call_provider(CONNECTION_REGISTRY_SSM, "list_connections", {"user_id": user_id})


def _resolve(user_id: str, data_source: str) -> dict:
    """The caller's connection that `data_source` names; the registry decides."""
    return call_provider(CONNECTION_REGISTRY_SSM, "resolve_connection", {"user_id": user_id, "database": data_source})


def _limits_line(connection: dict) -> str:
    """What the agent should know before writing a statement for this data source."""
    if not all(f in connection for f in _LIMITS):
        return "-- Statements are refused until an administrator sets this data source's limits\n"
    return (f"-- Each statement is cancelled after {connection['statementTimeoutSeconds']} seconds "
            f"and returns at most {connection['rowCap']} rows\n")


def _invoke_provider(provider_ssm_key: str, operation: str, args: dict) -> dict:
    try:
        return call_provider(provider_ssm_key, operation, args)
    except ProviderError as e:
        return gateway_event.error(str(e))


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    logger.info("Received event keys: %s", list(event.keys()))

    tool_name = gateway_event.tool_name(context)

    # Raises if the gateway sent no subject, and deliberately not inside the
    # try below: an unattributable tool call is a wiring fault in our own
    # infrastructure, and returning it to the agent as a tool error would let
    # the model retry, apologise, or invent an answer. It should page us.
    user_id = gateway_event.user_id(event)
    # The full verified subject, never a prefix: this line is the audit record
    # that ties a statement to the person who caused it.
    logger.info("Tool: %s, user: %s", tool_name, user_id)

    # What the agent can act on comes back as a tool error. Anything else is a fault in
    # our wiring, raised for the gateway to fail the call and the log to show.
    try:
        return _dispatch(tool_name, event, user_id)
    except (ProviderError, LimitError) as e:
        logger.warning("Tool %s refused: %s", tool_name, e)
        return gateway_event.error(str(e))


def _handle_execute_statement(data_source: str, statement: str, user_id: str,
                              start_row=None, max_rows=None) -> dict:
    """Run a statement against one of the caller's registered data sources.

    The agent names a data source; the registry turns that into the caller's own
    connection and credentials. Nothing about a host, port, or secret crosses
    the tool boundary, and no connection is opened that the caller did not
    register themselves.
    """
    if not statement.strip():
        return gateway_event.error("No statement provided.")

    try:
        connection = _resolve(user_id, data_source)
    except ProviderError as e:
        return gateway_event.error(str(e))
    if "unavailable" in connection:
        return gateway_event.error(connection["unavailable"])

    engine = connection.get("engine", "")
    ssm_key = ENGINE_PROVIDER_SSM.get(engine)
    if not ssm_key:
        return gateway_event.error(
            f"Connection {connection.get('connectionId')} names engine "
            f"{engine!r}, which has no provider function."
        )

    # Who ran what where, but not the statement: that is the data source's to log
    # (its own query logging), along with the database user it ran as.
    logger.info(
        "execute_statement user=%s connection=%s engine=%s data_source=%s",
        user_id, connection["connectionId"], engine, data_source,
    )
    if not all(f in connection for f in _LIMITS):
        return gateway_event.error(
            f"{connection['name']} has no statement timeout and row cap set. An administrator sets them on the "
            "Admin page; a connection to a database that is not registered must be removed and added again.")
    try:
        start_row, rows = page(start_row, max_rows, connection["rowCap"])
    except LimitError as e:
        return gateway_event.error(f"{e} The row cap for {connection['name']} is {connection['rowCap']}.")
    result = _invoke_provider(ssm_key, "execute_statement", {
        "statement": statement,
        "user_id": user_id,
        "connection_id": connection["connectionId"],
        "row_cap": rows,
        "start_row": start_row,
    })
    if isinstance(result, dict) and result.get("truncated"):
        # Said in words, so a partial result is never taken for the whole answer.
        result["message"] = (
            f"Rows {start_row + 1} to {result['next_start_row']} of a larger result. For more, call "
            f"execute_statement again with the same data_source and statement and start_row="
            f"{result['next_start_row']}" + (f", or a max_rows up to {connection['rowCap']}"
                                             if rows < connection["rowCap"] else "") + ".")
    if isinstance(result, dict) and "rows" in result:
        logger.info(
            "execute_statement returned %d row(s) from %s", len(result["rows"]), data_source,
        )
    else:
        logger.info("execute_statement returned no result set from %s", data_source)
    return gateway_event.result(result)


# How to write statements for each engine, as the engine's provider says it. Text that
# belongs to the engine, not to any user, and grants nothing, so it is kept for the
# life of the container instead of being fetched on every search.
_statement_best_practices: dict[str, str] = {}


def _visible_objects(user_id: str, connections: list[dict]) -> tuple[list[dict], list[dict], dict[str, dict]]:
    """Ask each of these connections' data sources, as the caller, which objects they can see.

    Returns (visible, unavailable, routes). `visible` is keyed on each source's
    identity -- where it is -- never on the name the caller gave it; `routes`
    maps that identity back to the caller's own connection, for naming results.
    A source that cannot answer contributes no objects and is reported in
    `unavailable` -- never "everything".
    """
    def _ask(connection: dict) -> tuple[dict, list | str]:
        if "unavailable" in connection:
            return connection, connection["unavailable"]
        ssm_key = ENGINE_PROVIDER_SSM.get(connection.get("engine", ""))
        if not ssm_key:
            return connection, f"engine {connection.get('engine')!r} has no provider function"
        args = {"user_id": user_id, "connection_id": connection["connectionId"]}
        engine = connection["engine"]
        try:
            names = call_provider(ssm_key, "visible_object_refs", args)
            if engine not in _statement_best_practices:
                _statement_best_practices[engine] = call_provider(ssm_key, "get_statement_best_practices", args)
            return connection, names
        except ProviderError as e:
            return connection, str(e)

    visible, unavailable, routes = [], [], {}
    if not connections:
        return visible, unavailable, routes
    with ThreadPoolExecutor(max_workers=min(len(connections), 8)) as pool:
        for connection, answer in pool.map(_ask, connections):
            if isinstance(answer, str):
                logger.warning("No visibility from %s: %s", connection.get("database"), answer)
                unavailable.append({"data_source": connection.get("name", ""), "reason": answer})
                continue
            # Where the database is, as the schema service keys it and its index: never a name.
            identity = connection["identity"]
            routes.setdefault(identity, connection)
            visible.extend(
                {"data_source": identity, "schema": ref["schema"], "name": ref["name"]}
                for ref in answer
            )
    return visible, unavailable, routes


def _in_callers_terms(objects: list[dict], routes: dict[str, dict], connections_by_name: dict[str, int]) -> list[dict]:
    """Name each result by the caller's own connection and say how to query it.

    The index knows a data source only by where it is. The caller knows it by the
    name it is registered under (the registry supplies it), which is what
    execute_statement accepts; when two of their connections share a name the
    connection id is used instead, because a name that matches two is refused.
    """
    named = []
    for found in objects:
        connection = routes.get(found["data_source"])
        if connection is None:
            logger.error("Dropping a result for a data source the caller did not search: %s", found["data_source"])
            continue
        name = connection.get("name") or connection["database"]
        if connections_by_name.get(name.lower(), 0) > 1:
            name = connection["connectionId"]
        named.append({
            **found,
            "data_source": name,
            "definition": (f"-- Data source: {name} ({found['engine']})\n"
                           f"-- Pass data_source={name} to execute_statement\n"
                           f"{_limits_line(connection)}{found['definition']}"),
        })
    return named


def _handle_search_schema(query: str, data_source: str, user_id: str, limit: int) -> dict:
    """Search the schema index for objects relevant to the query that the caller can see.

    Visibility comes from the caller's own data sources, asked in the caller's own
    sessions; the schema provider applies it. This function only carries it
    between the two. Results carry the data source and engine they came from so
    the agent knows which data source to pass to execute_statement.
    """
    if not query.strip():
        return gateway_event.error("No search query provided.")

    logger.info("search_schema user=%s", user_id)
    try:
        all_connections = _connections(user_id)
        searched = [_resolve(user_id, data_source)] if data_source else all_connections
        visible, unavailable, routes = _visible_objects(user_id, searched)
    except ProviderError as e:
        return gateway_event.error(str(e))
    by_name: dict[str, int] = {}
    for connection in all_connections:
        key = connection["name"].lower()
        by_name[key] = by_name.get(key, 0) + 1

    try:
        found = call_provider(SCHEMA_PROVIDER_SSM, "search_objects", {
            "query": query,
            "visible": visible,
            "limit": limit,
        })
    except ProviderError as e:
        return gateway_event.error(str(e))

    found = _in_callers_terms(found, routes, by_name)
    # Advice for exactly the data sources in the answer, under the name the agent
    # passes to execute_statement.
    practices = {o["data_source"]: _statement_best_practices[o["engine"]] for o in found}

    # Which objects the agent was shown determines which statements it can write, so
    # the names it got back are as load-bearing as the statement it eventually sent.
    logger.info(
        "search_schema returned %d of %d visible object(s): %s", len(found), len(visible),
        ", ".join(f"{o['data_source']}/{o['engine']}:{o['object_name']}" for o in found) or "(none)",
    )
    result: dict[str, Any] = {"objects": found, "statement_best_practices": practices}
    if unavailable:
        result["unavailable"] = unavailable
    return gateway_event.result(result)


# Enough for a question spanning two data sources to get tables from both. The tool schema
# advertises it (tests/unit/test_identity_isolation.py checks the two agree).
_SEARCH_SCHEMA_DEFAULT_LIMIT = 8


def _dispatch(tool_name: str, event: dict, user_id: str) -> dict:
    if tool_name == "search_schema":
        return _handle_search_schema(
            event.get("query", ""), event.get("data_source", ""),
            user_id, event.get("limit", _SEARCH_SCHEMA_DEFAULT_LIMIT),
        )

    elif tool_name == "execute_statement":
        return _handle_execute_statement(
            event.get("data_source", ""), event.get("statement", ""), user_id,
            event.get("start_row"), event.get("max_rows"),
        )

    return gateway_event.error(f"Unknown tool: {tool_name}")


