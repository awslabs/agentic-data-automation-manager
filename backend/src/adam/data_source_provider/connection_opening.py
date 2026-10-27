#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Opening a database provider for a registered connection.

A user's connection holds only their login and which registered database it is for.
Everything about the database -- its engine, where it is, how to reach it -- is read
from its registration at the moment of the call, so an administrator who moves a
database moves every user's connection with it. Pick the engine's class, give it that
and the credential issued for the user's connection, and close it when the call ends.
"""

import logging
from contextlib import contextmanager
from typing import Any

from adam.data_source_provider import engines
from adam.data_source_provider.connection_registry import (
    ConnectionError_,
    get_connection,
    registered_database,
    require_user_id,
)

# What a connection is: the user's login (its row) and its registered database, which says
# the engine, where it is, its statement limits and, for an engine that records it, whether
# to use TLS and the certificate to pin. Nothing else on a row is read.
_LOGIN = ("userId", "connectionId", "targetId", "secretName")
_FROM_REGISTRATION = ("engine", "host", "port", "database", "statementTimeoutSeconds", "rowCap",
                      "tls", "tlsCertificate")

logger = logging.getLogger(__name__)

# No session outlives its invocation, and none is cached: a warm container would otherwise
# keep a database identity the user's current login no longer grants.


def provider_class(engine: str, *, described_as: str = ""):
    """The provider class for `engine`; an unknown engine is the caller's connection error."""
    try:
        return engines.provider_class(engine, described_as=described_as)
    except engines.UnknownEngine as e:
        raise ConnectionError_(str(e)) from None


def build_provider(connection: dict[str, str], subject: str):
    """Instantiate this connection's engine provider, logged in as its credential.

    The engine picks the class and the class reads its own fields from the row,
    when it first connects. Nothing here knows what any engine's connection looks
    like. `subject` is the app user the call is for, recorded with the database
    user in the connect log.
    """
    cls = provider_class(connection.get("engine", ""),
                         described_as=f"Connection {connection.get('connectionId', '?')}")

    from adam.data_source_provider.credential_provider import (
        credential_provider_from_env,
    )
    credential = credential_provider_from_env().credential_for(connection)
    return cls(connection, credential, subject)


def registered(row: dict) -> dict:
    """The user's connection as it is now: their row, with its database as registered.

    The registration is asked of the schema service, never taken from a caller, so a
    user's password only ever goes to a database an administrator registered.
    """
    database = registered_database(row.get("targetId", ""))
    location = {f: database[f] for f in _FROM_REGISTRATION if f in database}
    return {**{f: row[f] for f in _LOGIN if f in row}, **location,
            "dsn": engines.build_dsn(location["engine"], location["host"], location["port"], location["database"])}


def open_connection(user_id: str, connection_id: str) -> tuple[Any, dict[str, str]]:
    """Return a provider for this user's connection, as its database is registered now.

    The provider connects on first use. Nothing is reused between invocations --
    see the note above the imports.
    """
    user_id = require_user_id(user_id)
    connection = registered(get_connection(user_id, connection_id))
    return build_provider(connection, user_id), connection


@contextmanager
def connection_for(user_id: str, connection_id: str):
    """Open a database session for the duration of one invocation, then close it: the only
    way a handler reaches a database, so a session never outlives its call."""
    provider, connection = open_connection(user_id, connection_id)
    try:
        yield provider, connection
    finally:
        provider.close()
