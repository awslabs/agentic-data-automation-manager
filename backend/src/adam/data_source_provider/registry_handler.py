#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The connection registry's API for other services (the MCP dispatcher).

Other services never read the connections table; they invoke this function.
Each connection comes back as its database is registered now -- its name, where it
is and its statement limits, which this service gets from the schema service -- and
without where its secret is.
"""

import logging

from adam.data_source_provider import connection_registry as registry, engines
from adam.shared.provider_client import SCHEMA_PROVIDER_SSM, call_provider

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# What a caller learns about a connection: enough to route to it, name it and limit its
# statements, all the registration's. How to reach it is the provider's to read.
_FROM_REGISTRATION = ("engine", "host", "port", "database", "identity", "statementTimeoutSeconds", "rowCap")


def _described(connections: list[dict]) -> list[dict]:
    """Each connection as its database version is registered. One whose version is disabled,
    or not registered, says why, and carries nothing to route to."""
    listed = call_provider(SCHEMA_PROVIDER_SSM, "list_databases", {"include_disabled": True})
    databases = {d["targetId"]: d for d in listed["active"]}
    disabled = {d["targetId"]: d for d in listed["disabled"]}
    described = []
    for c in connections:
        shown = {"connectionId": c["connectionId"], "targetId": c.get("targetId", "")}
        registered = databases.get(shown["targetId"])
        if registered is None:
            retired = disabled.get(shown["targetId"])
            described.append({**shown, "name": retired["name"] if retired else c["connectionId"],
                              "unavailable": retired["disabled"] if retired else registry.UNREGISTERED})
            continue
        location = {f: registered[f] for f in _FROM_REGISTRATION if f in registered}
        described.append({**shown, "name": registered["name"], **location,
                          "dsn": engines.build_dsn(location["engine"], location["host"],
                                                   location["port"], location["database"])})
    return described


def handler(event, context):
    operation = event.get("operation", "")
    args = event.get("args", {})
    try:
        if operation == "list_connections":
            return {"response": _described(registry.list_user_connections(args.get("user_id")))}
        if operation == "resolve_connection":
            connections = _described(registry.list_user_connections(args.get("user_id")))
            return {"response": registry.resolve(connections, args.get("database", ""))}
    except registry.ConnectionError_ as e:
        # The caller named something this user has not registered: a message for the agent.
        return {"error": str(e)}
    return {"error": f"Unknown operation: {operation}"}
