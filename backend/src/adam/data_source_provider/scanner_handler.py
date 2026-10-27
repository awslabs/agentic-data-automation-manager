#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""One engine's schema scanner: the database provider service's side of scanning.

Each engine has its own scanner function (ENGINE says which), logged in as the
scanner account of the database it is asked about, never as a user. It is
reached two ways:

- ScanRequested events on the schema service's scan bus, which its stack's rule
  delivers for its engine only. It scans, writes the DDL to this service's
  results bucket and answers ScanCompleted or ScanFailed on the bus
  (shared/scan_events.py).
- Direct calls from the schema service's admin function, the only principal its
  stack lets invoke it, for what the admin page must know at once:

      probe                   {location, scannerSecretArn} -> {ok}: the login works
      store_login             {identity, username, password} -> {scannerSecretArn}
      remove_login            {scannerSecretArn}
      get_admin_best_practices  -> how to set up this engine's accounts

Scanner logins are this service's secrets, under /<stack>/scanners/. This function
reads, creates and deletes only those (its IAM says the same), so no event or call
can make it send a scanner's password to a user's secret or read one.
"""

import json
import logging
import re
import uuid

import boto3
import botocore.exceptions

from adam.data_source_provider import engines
from adam.data_source_provider.secrets_manager_credential_provider import (
    SecretsManagerPasswordProvider,
)
from adam.shared import scan_events
from adam.shared.environment import require_env

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Recorded beside the scanner's database user in the connect log.
SCAN_SUBJECT = "schema-scanner"
LOCATION_FIELDS = ("host", "port", "database")

_ARN = re.compile(r"^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:(?P<name>.+)$")


class ScanRefused(ValueError):
    """A refusal written for the administrator; the admin page shows its message."""


def _scanner_path() -> str:
    return f"/{require_env('STACK_NAME')}/scanners/"


def _require_scanner_secret(arn: object) -> str:
    """The ARN of a secret under this stack's scanner path, or ScanRefused."""
    match = _ARN.match(str(arn or ""))
    if not match or not match["name"].startswith(_scanner_path()):
        raise ScanRefused(f"A scanner login must be a secret under {_scanner_path()} in this account and region.")
    return str(arn)


def _location(value) -> dict:
    if not isinstance(value, dict) or not all(isinstance(value.get(f), str) and value[f].strip()
                                              for f in LOCATION_FIELDS):
        raise ScanRefused("The request does not say where the database is (host, port and database).")
    if not isinstance(value.get("tls", False), bool) or not isinstance(value.get("tlsCertificate", ""), str):
        raise ScanRefused("The request's TLS setting is not true or false, or its certificate is not text.")
    return {**{f: value[f].strip() for f in LOCATION_FIELDS},
            **{k: value[k] for k in ("tls", "tlsCertificate") if k in value}}


def _open(location: dict, secret_arn: object, *, discover_tls: bool = False):
    """This engine's provider for the database at `location`, logged in as its scanner.
    `discover_tls`: the probe that decides whether a database is reached over TLS."""
    engine = require_env("ENGINE")
    row = {"connectionId": f"scanner for {engine}", "engine": engine, **location, "discoverTls": discover_tls,
           "dsn": engines.build_dsn(engine, *(location[f] for f in LOCATION_FIELDS)),
           "secretName": _require_scanner_secret(secret_arn)}
    try:
        credential = SecretsManagerPasswordProvider().credential_for(row)
    except botocore.exceptions.ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        message = e.response.get("Error", {}).get("Message", "")
        if code == "DecryptionFailure" or (code == "AccessDeniedException" and "KMS" in message):
            raise ScanRefused(
                "The scanner login is encrypted with a KMS key this stack may not use. The key's "
                f"administrator must allow kms:Decrypt on it for the role {require_env('SCANNER_ROLE_NAME')}.") from e
        if code in ("AccessDeniedException", "ResourceNotFoundException"):
            raise ScanRefused(f"No scanner login with that ARN exists under {_scanner_path()} "
                              "in this account and region.") from e
        raise
    return engines.provider_class(engine)(row, credential, SCAN_SUBJECT)


def _failure(e: Exception) -> str:
    """What the admin page shows for a failed scan or login check."""
    # Refusals are written for the admin, and a RuntimeError carries the database's reason.
    if isinstance(e, ScanRefused | RuntimeError):
        return str(e)
    return f"The scan failed ({type(e).__name__}); the {require_env('ENGINE')} scanner function's log has the details."


# ── Direct calls from the admin function ────────────────────────────────────

def _probe(args: dict) -> dict:
    provider = _open(_location(args.get("location")), args.get("scannerSecretArn"), discover_tls=True)
    try:
        return provider.probe()
    except RuntimeError as e:
        # Only admins register targets, and they need the database's reason to fix the login.
        raise ScanRefused(f"The scanner could not log in: {e}") from e
    finally:
        provider.close()


def _store_login(args: dict) -> dict:
    username, password = args.get("username"), args.get("password")
    if not (isinstance(username, str) and username.strip() and isinstance(password, str) and password):
        raise ScanRefused("Give both the scanner's username and its password.")
    response = boto3.client("secretsmanager").create_secret(
        Name=f"{_scanner_path()}{uuid.uuid4()}",
        Description=f"Schema scanner login for {str(args.get('identity') or '')[:200]}",
        SecretString=json.dumps({"username": username.strip(), "password": password}),
    )
    return {"scannerSecretArn": response["ARN"]}


def _remove_login(args: dict) -> dict:
    arn = _require_scanner_secret(args.get("scannerSecretArn"))
    try:
        boto3.client("secretsmanager").delete_secret(SecretId=arn, ForceDeleteWithoutRecovery=True)
    except botocore.exceptions.ClientError as e:
        # One already gone is what was wanted.
        if e.response.get("Error", {}).get("Code") != "ResourceNotFoundException":
            raise
    return {"removed": arn}


_OPERATIONS = {
    "probe": _probe,
    "store_login": _store_login,
    "remove_login": _remove_login,
    "get_admin_best_practices": lambda args: engines.provider_class(require_env("ENGINE")).ADMIN_BEST_PRACTICES,
}


# ── Scan requests from the bus ──────────────────────────────────────────────

def _scan(detail: dict) -> None:
    """Scan the database a ScanRequested names and answer on the bus, whatever happens."""
    request_id, target_id = str(detail.get("requestId") or ""), str(detail.get("targetId") or "")
    if not request_id or not target_id:
        # Nothing to answer to: a request that names no scan is a fault in the sender.
        raise RuntimeError(f"A ScanRequested event without requestId and targetId: {detail!r}")
    engine = require_env("ENGINE")
    answer = {"requestId": request_id, "targetId": target_id, "engine": engine}
    events = boto3.client("events")
    try:
        if detail.get("engine") != engine:
            raise ScanRefused(f"This is the {engine} scanner; the request is for {detail.get('engine')!r}.")
        provider = _open(_location(detail.get("location")), detail.get("scannerSecretArn"))
        try:
            tables = provider.scan_schema()
        finally:
            provider.close()
        key = scan_events.scan_result_key(target_id, request_id)
        boto3.client("s3").put_object(
            Bucket=require_env("SCAN_RESULTS_BUCKET"), Key=key, ContentType="application/json",
            Body=json.dumps([t.to_dict() for t in tables]).encode())
    except Exception as e:
        logger.exception("Scan %s of target %s failed", request_id, target_id)
        scan_events.publish(events, scan_events.DATABASE_PROVIDER, scan_events.SCAN_FAILED,
                            {**answer, "message": _failure(e)})
        return
    logger.info("Scan %s of target %s found %d table(s) and view(s)", request_id, target_id, len(tables))
    scan_events.publish(events, scan_events.DATABASE_PROVIDER, scan_events.SCAN_COMPLETED,
                        {**answer, "bucket": require_env("SCAN_RESULTS_BUCKET"), "key": key, "tableCount": len(tables)})


def handler(event, context):
    event = event or {}
    if event.get("detail-type") == scan_events.SCAN_REQUESTED:
        return _scan(event.get("detail") or {})
    operation = _OPERATIONS.get(event.get("operation", ""))
    if operation is None:
        return {"error": f"Unknown operation: {event.get('operation')!r}"}
    try:
        return {"response": operation(event.get("args") or {})}
    except ScanRefused as e:
        return {"error": str(e)}
