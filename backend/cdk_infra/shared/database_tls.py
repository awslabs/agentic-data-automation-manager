#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What a Lambda needs to verify a Postgres server's certificate.

Used by every function that can open a Postgres connection; see
adam/data_source_provider/tls.py for the runtime side.
"""

BUNDLE_FILE = "rds-global-bundle.pem"

# Where the bundle lands inside the Lambda package (/var/task is its root).
ENVIRONMENT = {
    "DB_TLS_CA_FILE": f"/var/task/adam/data_source_provider/certs/{BUNDLE_FILE}",
}
