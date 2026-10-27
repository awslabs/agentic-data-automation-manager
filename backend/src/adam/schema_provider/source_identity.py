#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What makes two registrations the same database. Pure: no AWS calls.

The schema service keys each registered database, and its index, on this; it serves the
identity with each registration, so no other service computes one.
"""

from typing import Any

# Engines a connection names by host, port and database (Oracle names a DSN).
_LOCATED_BY_HOST = ("postgres", "mysql", "mariadb", "redshift")


def source_identity(connection: dict[str, Any]) -> str:
    """What makes two connections the same database: the engine and where it lives.

    The schema index is keyed on this, never on the display name. Anyone can
    register a connection under any name, so a name-keyed index would let a user
    who calls their own database "wealth" be shown the DDL indexed for someone
    else's "wealth". Raises if the connection does not say where it points,
    because an identity guessed from a name is the same hole.
    """
    engine = str(connection.get("engine", "")).strip().lower()
    if engine == "oracle":
        dsn = str(connection.get("dsn", "")).strip().lower()
        if not dsn:
            raise ValueError("An Oracle connection needs a dsn to be identified.")
        return f"oracle://{dsn}"
    if engine in _LOCATED_BY_HOST:
        parts = {k: str(connection.get(k, "")).strip().lower() for k in ("host", "port", "database")}
        missing = [k for k, v in parts.items() if not v]
        if missing:
            raise ValueError(f"A {engine} connection needs {', '.join(missing)} to be identified.")
        return f"{engine}://{parts['host']}:{parts['port']}/{parts['database']}"
    raise ValueError(f"Cannot identify a connection of engine {engine!r}.")
