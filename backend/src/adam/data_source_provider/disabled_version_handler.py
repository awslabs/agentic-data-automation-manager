#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Deletes the logins users saved for a database version an administrator disabled.

Invoked by the scan bus rule for RegistrationDisabled (shared/scan_events.py), which only
the schema service publishes. Each of that version's connections loses its secret and is
kept as a marker, so the Connections page can say the version was disabled and whether a
newer one replaces it. Nothing is copied to any other version: a user connects to a new
version with their own login.

Safe to repeat: the event is retried on failure, and the admin routes publish it again
when a disabling is run again. The schema service already refuses the version on every
call, so a call made before this runs dials nothing.
"""

import logging
from datetime import UTC, datetime

import boto3
from botocore.exceptions import BotoCoreError, ClientError

from adam.data_source_provider import connection_registry as registry
from adam.shared import scan_events
from adam.shared.environment import require_env

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def handler(event, context):
    expected = scan_events.source(require_env("STACK_NAME"), scan_events.SCHEMA_SERVICE)
    if event.get("source") != expected or event.get("detail-type") != scan_events.REGISTRATION_DISABLED:
        raise ValueError(f"Expected {scan_events.REGISTRATION_DISABLED} from {expected!r}.")
    target_id = str((event.get("detail") or {}).get("targetId") or "")
    if not target_id:
        raise ValueError("A RegistrationDisabled event must name the version it disables.")

    secrets = boto3.client("secretsmanager")
    disabled_at = datetime.now(UTC).isoformat(timespec="seconds")
    forgotten = 0
    for row in registry.connections_for_version(target_id):
        secret_name = row.get("secretName")
        if secret_name:
            try:
                secrets.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
            except secrets.exceptions.ResourceNotFoundException:
                pass
            except (ClientError, BotoCoreError):
                # Raised, not skipped: the event is retried, and the row keeps its secretName
                # until its secret is gone.
                logger.exception("Could not delete the login of connection %s", row["connectionId"])
                raise
        registry.forget_login(row["userId"], row["connectionId"], disabled_at)
        forgotten += 1
    logger.info("Deleted the saved logins of %d connection(s) to disabled version %s", forgotten, target_id)
    return {"targetId": target_id, "connections": forgotten}
