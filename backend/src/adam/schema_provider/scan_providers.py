#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The database providers registered with the schema service, one per engine.

The schema service's own table (SCAN_PROVIDERS_TABLE), keyed on engine. A
provider's stack registers it by publishing ProviderRegistered on the scan bus
when it deploys and ProviderDeregistered when it is removed
(shared/scan_events.py); the ingestion function records both here. The admin
routes offer only registered engines, and a scan for an engine with no provider
is refused, because EventBridge drops an event no rule matches without a word.

    {"engine": "oracle", "scannerFunctionArn": "<the provider's scanner function>",
     "resultsBucket": "<the bucket it writes scan results to>",
     "registeredAt": "<ISO-8601 UTC>"}
"""

import os
import re
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

_ENGINE = re.compile(r"^[a-z][a-z0-9]{0,31}$")
_FUNCTION_ARN = re.compile(
    r"^arn:aws[a-z-]*:lambda:(?P<region>[a-z0-9-]+):(?P<account>\d{12}):function:[A-Za-z0-9_-]{1,64}$")
_BUCKET = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_FIELDS = ("engine", "scannerFunctionArn", "resultsBucket", "registeredAt")


def _table():
    name = os.environ.get("SCAN_PROVIDERS_TABLE", "")
    if not name:
        raise RuntimeError("SCAN_PROVIDERS_TABLE is not set; this function cannot reach the provider list.")
    return boto3.resource("dynamodb").Table(name)


def _entry(item: dict) -> dict:
    entry = {f: item.get(f) for f in _FIELDS}
    if not all(isinstance(v, str) and v for v in entry.values()):
        raise RuntimeError(f"The provider list holds an item that is not a provider: {item!r}")
    return entry


def engine_name(value) -> str:
    """An engine name as registrations and targets spell it, or ValueError."""
    engine = str(value or "").strip().lower()
    if not _ENGINE.match(engine):
        raise ValueError(f"{value!r} is not an engine name.")
    return engine


def register(engine, scanner_function_arn, results_bucket, *, account: str, region: str) -> dict:
    """Record a provider. Its function must be in this account and region, so an event
    cannot point the admin routes at a function elsewhere."""
    match = _FUNCTION_ARN.match(str(scanner_function_arn or ""))
    if not match or match["account"] != account or match["region"] != region:
        raise ValueError(f"{scanner_function_arn!r} is not a function in account {account}, region {region}.")
    if not _BUCKET.match(str(results_bucket or "")):
        raise ValueError(f"{results_bucket!r} is not a bucket name.")
    entry = {"engine": engine_name(engine), "scannerFunctionArn": scanner_function_arn,
             "resultsBucket": results_bucket,
             "registeredAt": datetime.now(UTC).isoformat(timespec="seconds")}
    _table().put_item(Item=entry)
    return entry


def deregister(engine, scanner_function_arn) -> bool:
    """Remove the engine's entry if it is still this scanner's. False when another has replaced it.

    A stack that is replaced deploys its successor before it is deleted, so by then
    the successor's scanner is the one registered, and must stay registered.
    """
    if not scanner_function_arn:
        raise ValueError("A deregistration must name the scanner it removes.")
    try:
        _table().delete_item(Key={"engine": engine_name(engine)},
                             ConditionExpression="scannerFunctionArn = :arn",
                             ExpressionAttributeValues={":arn": scanner_function_arn})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def get(engine) -> dict | None:
    try:
        engine = engine_name(engine)
    except ValueError:
        return None
    item = _table().get_item(Key={"engine": engine}, ConsistentRead=True).get("Item")
    return _entry(item) if item else None


def read_all() -> list[dict]:
    table, items, start = _table(), [], None
    while True:
        args = {"ConsistentRead": True}
        if start:
            args["ExclusiveStartKey"] = start
        page = table.scan(**args)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return sorted((_entry(i) for i in items), key=lambda e: e["engine"])
