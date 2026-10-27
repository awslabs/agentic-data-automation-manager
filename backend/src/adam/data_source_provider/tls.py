#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""TLS settings for database connections.

Postgres, MySQL and MariaDB: the server certificate and hostname are verified against a CA bundle,
or TLS is turned off by an explicit, separate setting. Neither being set is an
error, never a silent fallback to an unverified connection.

    DB_TLS_CA_FILE    path to a PEM CA bundle (the RDS global bundle ships in
                      data_source_provider/certs/)
    DB_TLS_DISABLED   the exact word "true" -- local test containers only

Oracle uses the same context, over tcps, when its registered database offers TLS:
the admin's save decides that (oracle_provider.py). Redshift needs nothing here:
redshift_connector verifies against the CA bundle it ships.
"""

import os
import ssl


def verified_ssl_context() -> ssl.SSLContext | None:
    ca_file = os.environ.get("DB_TLS_CA_FILE", "")
    disabled = os.environ.get("DB_TLS_DISABLED", "") == "true"

    if disabled and ca_file:
        raise RuntimeError(
            "Both DB_TLS_DISABLED=true and DB_TLS_CA_FILE are set. Choose one: "
            "verify the server, or turn TLS off."
        )
    if disabled:
        return None
    if not ca_file:
        raise RuntimeError(
            "DB_TLS_CA_FILE is not set, so the database server's certificate "
            "cannot be verified. Point it at a CA bundle; set DB_TLS_DISABLED=true "
            "only for a local test container."
        )
    # create_default_context requires and verifies the certificate and hostname.
    return ssl.create_default_context(cafile=ca_file)
