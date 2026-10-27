#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The Lambda entry point every engine's database-provider function shares.

Every provider function's handler is `provider_handler.handler`; the engine comes
from the connection row.
"""

import logging

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    """Serve one operation against the caller's own database connection.

    There is no default connection and no stack-wide credential. The caller
    names which of their connections to use and the registry opens it with that
    connection's own secret, so two users hitting the same warm container never
    share a session.
    """
    from adam.data_source_provider.connection_opening import connection_for
    from adam.data_source_provider.connection_registry import ConnectionError_

    args = event.get("args", {})
    # Missing identity is an infrastructure fault, not a user mistake, so it
    # raises out of this function instead of becoming a tool error the agent can
    # retry or apologise around. Defaulting either of these to "" would push the
    # failure down into open_connection, where an empty user_id is
    # indistinguishable from a user who has registered nothing.
    user_id = args.get("user_id") or ""
    if not user_id:
        raise RuntimeError(
            "No user_id in provider args; refusing to open a database "
            "connection that cannot be attributed to a caller."
        )
    connection_id = args.get("connection_id") or ""
    if not connection_id:
        raise RuntimeError("No connection_id in provider args.")

    try:
        # Context-managed: the session is closed when this block exits, so its
        # lifetime is exactly this invocation's. Where the database is, and how to
        # reach it, comes from its registration, never from these args.
        with connection_for(user_id, connection_id) as (provider, _connection):
            return provider.handle(event)
    except ConnectionError_ as e:
        # This one IS a user error: they named a database they have not
        # registered. It belongs back in the conversation, not in a stack trace.
        logger.error("Could not open connection %s: %s", connection_id, e)
        return {"error": str(e)}
