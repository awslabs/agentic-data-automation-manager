#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The events the schema service and the database providers exchange on the scan bus.

The schema service owns the bus. A provider registers its engine there when its
stack deploys, and the schema service asks for scans there; neither imports the
other's code or invokes the other's functions for a scan. The CDK rules match
these names; tests/unit/test_scan_events.py checks they agree.

    ProviderRegistered    provider -> schema   {engine, scannerFunctionArn, resultsBucket}
    ProviderDeregistered  provider -> schema   {engine, scannerFunctionArn}
    ScanRequested         schema -> provider   {requestId, targetId, engine, location, scannerSecretArn}
    ScanCompleted         provider -> schema   {requestId, targetId, engine, bucket, key, tableCount}
    ScanFailed            provider -> schema   {requestId, targetId, engine, message}
    RegistrationDisabled  schema -> provider   {targetId}

location is {host, port, database} as the administrator typed them, and for an engine whose
registration records it, tls and tlsCertificate. A deregistration names the scanner it
retires, so a stale one cannot remove its replacement. A completed scan's DDL is in the
provider's results bucket at key, because a schema can be larger than an event may carry.
A disabled version is never used again; the providers delete the logins users saved for it.
"""

import json
import os

PROVIDER_REGISTERED = "ProviderRegistered"
PROVIDER_DEREGISTERED = "ProviderDeregistered"
SCAN_REQUESTED = "ScanRequested"
SCAN_COMPLETED = "ScanCompleted"
SCAN_FAILED = "ScanFailed"
REGISTRATION_DISABLED = "RegistrationDisabled"

# Each side publishes under its own source, and each side's IAM allows only its own,
# so a provider cannot request a scan and the schema service cannot report one.
SCHEMA_SERVICE = "schema-service"
DATABASE_PROVIDER = "database-provider"


def source(stack_name: str, service: str) -> str:
    return f"{stack_name}.{service}"


def scan_result_key(target_id: str, request_id: str) -> str:
    """Where a provider writes one scan's DDL in its results bucket."""
    return f"scans/{target_id}/{request_id}.json"


def publish(events_client, service: str, detail_type: str, detail: dict) -> None:
    """Put one event on the scan bus (SCAN_BUS_NAME), raising if EventBridge refused it."""
    bus, stack = os.environ.get("SCAN_BUS_NAME", ""), os.environ.get("STACK_NAME", "")
    if not bus or not stack:
        raise RuntimeError("SCAN_BUS_NAME and STACK_NAME must be set to publish scan events.")
    response = events_client.put_events(Entries=[{
        "EventBusName": bus, "Source": source(stack, service),
        "DetailType": detail_type, "Detail": json.dumps(detail),
    }])
    # PutEvents reports a refused entry in the response, not as an exception.
    if response.get("FailedEntryCount"):
        entry = response["Entries"][0]
        raise RuntimeError(f"EventBridge refused the {detail_type} event: "
                           f"{entry.get('ErrorCode')} {entry.get('ErrorMessage')}")
