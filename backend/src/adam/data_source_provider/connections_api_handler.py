#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The Connections page's API: each user's own logins to the registered databases.

Part of the database provider service, which owns the connection registry
(connection_registry.py) and its table. Credentials go to Secrets Manager under
``/{STACK_NAME}/connections/{user_id}/{connection_id}``; nothing is stored in a
customer database.

A user connects to a database an administrator registered: they pick it from the
list the schema service serves (its list_databases and get_database operations)
and give only their own login. The connection keeps that login and which version of
the database it is for, nothing else. A version's location never changes, so a user's
password is only ever sent to where the version they chose is; when an administrator
publishes a new version, users connect to it themselves, with their login.

This module carries no database driver. A candidate login is checked by asking
the engine's provider function to log in with it (the `probe` operation): to do
that the candidate is stored under a fresh connection id, probed, and kept only
if it worked.
"""

import json
import logging
import uuid
from datetime import UTC, datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from adam.data_source_provider import connection_registry as registry, engines, login_attempts
from adam.shared.environment import require_env
from adam.shared.http_response import (
    Rejected,
    bad_request,
    get_user_id,
    route_request,
    success,
)
from adam.shared.network_target import validate_target
from adam.shared.provider_client import (
    ENGINE_PROVIDER_SSM,
    SCHEMA_PROVIDER_SSM,
    ProviderError,
    call_provider,
)

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_LOGIN_FAILED = "Could not log in to that database with those credentials."

_secrets = None


def _get_secrets():
    global _secrets
    if _secrets is None:
        _secrets = boto3.client("secretsmanager")
    return _secrets


def _stack_name() -> str:
    return require_env("STACK_NAME")


def _require_fields(body, *fields) -> dict:
    if not isinstance(body, dict):
        raise Rejected("Expected a JSON object.")
    missing = [f for f in fields if not str(body.get(f, "")).strip()]
    if missing:
        raise Rejected(f"Missing required field(s): {', '.join(missing)}.")
    return body


def _row_to_response(item: dict, databases: dict[str, dict], disabled: dict[str, dict] | None = None) -> dict:
    """databases: the registered versions in use, by target id; disabled: the disabled ones. A
    connection to a version not in use has no location; to a disabled one, its name and why."""
    database = databases.get(item.get("targetId", ""), {})
    retired = (disabled or {}).get(item.get("targetId", ""))
    where = {f: database.get(f, "") for f in ("engine", "host", "port", "database")}
    return {
        "connectionId": item.get("connectionId", ""),
        "databaseId": item.get("targetId"),
        "name": database.get("name") or (retired["name"] if retired else None),
        "version": database.get("version"),
        **where,
        "dsn": engines.build_dsn(*where.values()) if database else "",
        **({"disabled": retired["disabled"]} if retired else {}),
        "createdAt": item.get("createdAt", ""),
    }


def _databases() -> list[dict]:
    return call_provider(SCHEMA_PROVIDER_SSM, "list_databases", {})


def _chosen(database_id) -> dict:
    """The registered database a request picks for a new connection: the newest version of
    one in use, or a refusal."""
    try:
        entry = registry.registered_database(str(database_id or "").strip())
    except registry.ConnectionError_:
        raise Rejected("Choose one of the registered databases.") from None
    if not entry.get("latest"):
        raise Rejected("Choose one of the registered databases: that is an older version of one.")
    return entry


def _registration_of(item: dict) -> dict:
    """The database a stored connection is for, as registered now, or a refusal saying why not."""
    try:
        return registry.registered_database(item.get("targetId", ""))
    except registry.ConnectionError_ as e:
        raise Rejected(str(e)) from None


def _own(user_id: str, connection_id: str) -> dict:
    """The caller's connection, or a refusal that does not say whether it exists for someone else."""
    try:
        return registry.get_connection(user_id, connection_id)
    except registry.ConnectionNotFound:
        raise Rejected("Connection not found.") from None


def _store(user_id: str, entry: dict, username: str, password: str) -> dict:
    """Write a new secret and registry row for a login to the registered database `entry`;
    return the row: the login's secret and which database, and nothing about the database."""
    connection_id = str(uuid.uuid4())
    secret_name = f"/{_stack_name()}/connections/{user_id}/{connection_id}"
    _get_secrets().create_secret(
        Name=secret_name,
        Description=f"A login to the registered database {entry['name']}",
        SecretString=json.dumps({"username": username, "password": password}),
    )
    item = {
        "userId": user_id, "connectionId": connection_id, "targetId": entry["targetId"],
        "secretName": secret_name,
        "createdAt": datetime.now(UTC).isoformat(),
    }
    try:
        registry.put_connection(item)
    except (ClientError, BotoCoreError):
        _remove_secret(secret_name)
        raise
    return item


def _remove_secret(secret_name: str) -> None:
    try:
        _get_secrets().delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
    except (ClientError, BotoCoreError):
        logger.exception("Failed to delete secret %s", secret_name)


def _remove(item: dict) -> None:
    registry.delete_connection(item["userId"], item["connectionId"])
    # A connection to a disabled version has no secret left: its provider deleted it.
    if item.get("secretName"):
        _remove_secret(item["secretName"])


def _probe_stored(item: dict, entry: dict) -> str | None:
    """Ask the engine's provider to check a stored connection to the registered database
    `entry`; the provider reads the registration itself.

    Returns None when it is usable, or the message to show the user: a probe that does
    not log in raises. A failed login is a generic message: the driver's text can carry
    hostnames and account names, so it goes to the log.
    """
    try:
        call_provider(ENGINE_PROVIDER_SSM[entry["engine"]], "probe", {
            "user_id": item["userId"], "connection_id": item["connectionId"]})
    except ProviderError as e:
        logger.error("Probe of %s (%s) for user %s failed: %s", item["connectionId"], entry["engine"],
                     item["userId"], e)
        return _LOGIN_FAILED
    return None


def _probe_candidate(user_id: str, entry: dict, username: str, password: str) -> str | None:
    """Check a typed login against a registered database without keeping it; None means usable.
    A user whose typed logins were refused too often is refused here, before the database is asked."""
    try:
        login_attempts.require_allowed(user_id)
    except login_attempts.TooManyFailures as e:
        logger.warning("Login checks refused for user %s: too many refused logins", user_id)
        raise Rejected(str(e)) from None
    validate_target(entry["host"])
    candidate = _store(user_id, entry, username, password)
    try:
        problem = _probe_stored(candidate, entry)
    finally:
        _remove(candidate)
    if problem:
        login_attempts.record_failure(user_id)
    return problem


def _list_connections(event, path_params, query_params, body):
    user_id = get_user_id(event)
    listed = call_provider(SCHEMA_PROVIDER_SSM, "list_databases", {"include_disabled": True})
    databases = {d["targetId"]: d for d in listed["active"]}
    disabled = {d["targetId"]: d for d in listed["disabled"]}
    return success({"connections": [_row_to_response(item, databases, disabled)
                                    for item in registry.list_user_connections(user_id)]})


def _list_databases(event, path_params, query_params, body):
    """The registered databases a user can connect to, by name: each one's newest version."""
    get_user_id(event)
    return success({"databases": sorted(
        ({"databaseId": d["targetId"], "name": d["name"], "engine": d["engine"], "version": d["version"]}
         for d in _databases() if d["latest"]),
        key=lambda d: d["name"].lower())})


@bad_request
def _create_connection(event, path_params, query_params, body):
    user_id = get_user_id(event)
    body = _require_fields(body, "databaseId", "username", "password")
    entry = _chosen(body["databaseId"])

    # A connection that can't log in must not be saved: it would sit in the
    # user's list as broken and every search would report it unavailable.
    problem = _probe_candidate(user_id, entry, body["username"], body["password"])
    if problem:
        raise Rejected(problem)
    item = _store(user_id, entry, body["username"], body["password"])
    return success(_row_to_response(item, {entry["targetId"]: entry}), 201)


@bad_request
def _test_connection(event, path_params, query_params, body):
    """Check a login against a registered database, before saving."""
    user_id = get_user_id(event)
    body = _require_fields(body, "databaseId", "username", "password")
    entry = _chosen(body["databaseId"])
    problem = _probe_candidate(user_id, entry, body["username"], body["password"])
    if problem:
        return success({"success": False, "message": problem})
    return success({"success": True, "message": f"Connected to {entry['name']}"})


@bad_request
def _test_existing_connection(event, path_params, query_params, body):
    item = _own(get_user_id(event), path_params["connectionId"])
    entry = _registration_of(item)
    problem = _probe_stored(item, entry)
    if problem:
        return success({"success": False, "message": problem})
    return success({"success": True, "message": f"Connected to {entry['name']}"})


@bad_request
def _update_connection(event, path_params, query_params, body):
    """A new login for the connection, checked where its database is registered now. The
    login is all that changes: where the database is was never the connection's."""
    user_id = get_user_id(event)
    item = _own(user_id, path_params["connectionId"])
    body = _require_fields(body, "username", "password")
    entry = _registration_of(item)

    problem = _probe_candidate(user_id, entry, body["username"], body["password"])
    if problem:
        raise Rejected(problem)

    _get_secrets().update_secret(
        SecretId=item["secretName"],
        SecretString=json.dumps({"username": body["username"], "password": body["password"]}),
    )
    return success(_row_to_response(item, {entry["targetId"]: entry}))


@bad_request
def _delete_connection(event, path_params, query_params, body):
    item = _own(get_user_id(event), path_params["connectionId"])
    _remove(item)
    return success({"deleted": item["connectionId"]})


ROUTES = {
    "GET /api/connections": _list_connections,
    "GET /api/databases": _list_databases,
    "POST /api/connections": _create_connection,
    "POST /api/connections/test": _test_connection,
    "POST /api/connections/{connectionId}/test": _test_existing_connection,
    "PATCH /api/connections/{connectionId}": _update_connection,
    "DELETE /api/connections/{connectionId}": _delete_connection,
}


def handler(event, context):
    """The connections routes, and nothing else."""
    return route_request(event, ROUTES)
