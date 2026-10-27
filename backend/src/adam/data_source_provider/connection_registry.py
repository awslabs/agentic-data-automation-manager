#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The connection registry: each user's database connections.

Owned by the database provider service, whose stack holds the connections table.
Only this service's functions use this module: the connections API (the
Connections page), the registry function (how other services reach it), and the
providers, which read the one row they are asked to use. Every other service
calls the registry function instead of the table.

Every row is partitioned by userId. Its credentials are the secret that user
registered, one per (userId, connectionId), never shared between users. A row holds
which registered database it is for (targetId) and where its secret is, and nothing
about the database itself: that is read from the registration on every use
(connection_opening.registered), so a database an administrator moves keeps every
user's connection to it.
"""


import boto3
from boto3.dynamodb.conditions import Key

from adam.shared.environment import require_env
from adam.shared.provider_client import ProviderRefused, call_function

UNREGISTERED = "Its database is no longer registered. Remove this connection."
# The connections table's index on targetId: every user's connection to one database version.
TARGET_INDEX = "targetId-index"
MADE_BEFORE_REGISTRATION = ("This connection was made before databases were registered by name. "
                            "Remove it and add it again by choosing its database.")


class ConnectionError_(Exception):
    """Base for connection resolution failures."""


class ConnectionNotFound(ConnectionError_):
    """No connection of the user's matched the requested name."""


class AmbiguousConnectionName(ConnectionError_):
    """The requested name matched more than one of the user's connections."""


class DatabaseNotRegistered(ConnectionError_):
    """The schema service says the connection's database version is not registered, or is
    disabled; its message, written for the user, says which and what to do."""


def registered_database(target_id: str) -> dict:
    """A connection's database version as the schema service has it registered.

    Only the schema service's refusal means "not usable"; anything else that fails is
    an outage, raised as itself, not passed off as the user's mistake.
    """
    if not target_id:
        raise ConnectionError_(MADE_BEFORE_REGISTRATION)
    try:
        return call_function(require_env("SCHEMA_PROVIDER_FUNCTION_NAME"), "get_database", {"targetId": target_id})
    except ProviderRefused as e:
        raise DatabaseNotRegistered(str(e) or UNREGISTERED) from None


def _table():
    return boto3.resource("dynamodb").Table(require_env("CONNECTIONS_TABLE_NAME"))


def require_user_id(user_id) -> str:
    """Reject an empty caller identity instead of substituting one."""
    resolved = str(user_id or "").strip()
    if not resolved:
        raise ConnectionError_(
            "No authenticated user id was supplied; cannot resolve a database "
            "connection. The caller must pass the Cognito subject."
        )
    return resolved


def list_user_connections(user_id: str) -> list[dict]:
    """Every connection this user has registered."""
    table, items, start = _table(), [], None
    while True:
        args = {"KeyConditionExpression": Key("userId").eq(require_user_id(user_id))}
        if start:
            args["ExclusiveStartKey"] = start
        page = table.query(**args)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def get_connection(user_id: str, connection_id: str) -> dict:
    """The one row a provider was asked to use, by key."""
    item = _table().get_item(Key={"userId": require_user_id(user_id),
                                  "connectionId": str(connection_id or "")}).get("Item")
    if item is None:
        raise ConnectionNotFound(f"No connection {connection_id!r} for this user.")
    return item


def resolve(connections: list[dict], name: str) -> dict:
    """The one connection `name` refers to, from a user's connections.

    Matches, in order: exact connectionId, the name its database is registered
    under, the database name, then the engine. Raises rather than guessing when
    nothing or more than one matches; the message names the valid choices so the
    agent can correct itself without a separate enumeration tool.
    """
    wanted = (name or "").strip()
    if not wanted:
        raise ConnectionNotFound("No database was named. Pass the database name from the schema search results.")
    if not connections:
        raise ConnectionNotFound(
            "You have no database connections configured. Add one on the Connections page before querying.")

    def _choices() -> str:
        return ", ".join(f"{c.get('name') or c.get('database', '?')} ({c.get('engine', '?')})" for c in connections)

    for conn in connections:
        if conn.get("connectionId") == wanted:
            return conn
    lowered = wanted.lower()
    for field in ("name", "database", "engine"):
        matches = [c for c in connections if str(c.get(field) or "").lower() == lowered]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise AmbiguousConnectionName(
                f"{wanted!r} matches {len(matches)} of your connections by {field}. "
                f"Use the connectionId instead: " + ", ".join(c["connectionId"] for c in matches))
    raise ConnectionNotFound(f"No connection named {wanted!r}. Available: {_choices()}")


def connections_for_version(target_id: str) -> list[dict]:
    """Every user's connection to one database version, by its targetId index."""
    table, items, start = _table(), [], None
    while True:
        args = {"IndexName": TARGET_INDEX, "KeyConditionExpression": Key("targetId").eq(target_id)}
        if start:
            args["ExclusiveStartKey"] = start
        page = table.query(**args)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def forget_login(user_id: str, connection_id: str, disabled_at: str) -> None:
    """Keep the connection as a marker of a disabled version, with no secret to open."""
    _table().update_item(Key={"userId": require_user_id(user_id), "connectionId": connection_id},
                         UpdateExpression="SET versionDisabledAt = :d REMOVE secretName",
                         ConditionExpression="attribute_exists(connectionId)",
                         ExpressionAttributeValues={":d": disabled_at})


def put_connection(item: dict) -> None:
    _table().put_item(Item=item)


def delete_connection(user_id: str, connection_id: str) -> None:
    _table().delete_item(Key={"userId": require_user_id(user_id), "connectionId": connection_id})
