#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Schema ingestion: the schema service's side of scanning, and the index's only writer.

It never connects to a database or reads a secret. The database provider of each
engine scans; this function asks it to over the scan bus and turns what it reports
into index entries (shared/scan_events.py has the events).

Invoked with one of:

    {"targetId": "<id>"}    request a scan of a registered target (the admin
                            routes and its schedule send this)
    {"purge": {"identity": ..., "engine": ...}}
                            remove the entries this deployment made for a
                            database from the index; refused while a version
                            not disabled is registered there
    {"disabled": "<id>"}    finish disabling a version the admin routes disabled:
                            tell the database providers (RegistrationDisabled), which
                            delete its users' logins, and purge its index entries
                            unless another version still registered is there

and, from the scan bus, the providers' events:

    ProviderRegistered / ProviderDeregistered   record which engines have a provider
    ScanCompleted / ScanFailed                  record a scan's result

A scan is requested only for a target in this service's scan-target list, and
only through the provider registered for its engine. A result is taken only for
the scan the target is still waiting on, and only from that provider's results
bucket at the key the request implies.
"""

import json
import logging
import uuid

import boto3

from adam.schema_provider import scan_providers, scan_targets
from adam.schema_provider.glue_data_catalog_schema_provider import provider_from_env
from adam.shared import scan_events
from adam.shared.environment import require_env
from adam.shared.schema_types import SchemaObject

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class ScanRefused(PermissionError):
    """A refusal written for the administrator; the admin routes show its message."""


def _registered_targets() -> set[str]:
    """The database identities of the versions registered and not disabled."""
    return {t["identity"].strip().lower() for t in scan_targets.read_all() if not t["disabled"]}


# ── Requests ────────────────────────────────────────────────────────────────

def _request_scan(target_id: str) -> dict:
    target = scan_targets.get(target_id)
    if target is None:
        raise ScanRefused(f"{target_id} is not a registered scan target.")
    if target["disabled"]:
        raise ScanRefused(f"{target['identity']} (version {target['version']}) is disabled; it is no longer scanned.")
    if scan_providers.get(target["engine"]) is None:
        message = (f"No {target['engine']} database provider is registered with this deployment, "
                   "so nothing can scan this database.")
        scan_targets.record_status(target_id, scan_targets.status("failed", message))
        raise ScanRefused(message)

    request_id = str(uuid.uuid4())
    scan_targets.record_status(target_id, scan_targets.status("scanning", "Scanning."), request_id=request_id)
    try:
        scan_events.publish(boto3.client("events"), scan_events.SCHEMA_SERVICE, scan_events.SCAN_REQUESTED, {
            "requestId": request_id, "targetId": target_id, "engine": target["engine"],
            "location": {f: target[f] for f in (*scan_targets.FIELDS, *scan_targets.TRANSPORT_FIELDS) if f in target},
            "scannerSecretArn": target["scannerSecretArn"],
        })
    except Exception:
        scan_targets.record_status(target_id, scan_targets.status(
            "failed", "The scan could not be requested; the ingestion function's log has the details."),
            answering=request_id)
        raise
    return {"targetId": target_id, "requestId": request_id}


def _purge(request) -> dict:
    """Remove the index entries this deployment made for a database, once no version
    registered there is in use."""
    if not isinstance(request, dict) or not request.get("identity") or not request.get("engine"):
        raise ValueError("Expected {'purge': {'identity': ..., 'engine': ...}}.")
    identity = str(request["identity"]).strip().lower()
    if identity in _registered_targets():
        raise ScanRefused(f"{identity} is a registered scan target; its index entries are kept.")
    stats = provider_from_env().publish_objects(data_source=identity, engine=request["engine"], objects=[])
    return {"database": identity, **stats}



def _disabled(target_id: str) -> dict:
    """Tell the database providers a version is disabled, so they delete its users' logins,
    and purge its index entries unless a version still registered is at the same place.
    Safe to repeat: the admin routes run it again when an earlier run failed."""
    target = scan_targets.get(target_id)
    if target is None:
        raise ScanRefused(f"{target_id} is not a registered scan target.")
    if not target["disabled"]:
        raise ScanRefused(f"{target['identity']} (version {target['version']}) is not disabled.")
    scan_events.publish(boto3.client("events"), scan_events.SCHEMA_SERVICE, scan_events.REGISTRATION_DISABLED,
                        {"targetId": target_id})
    identity = target["identity"].strip().lower()
    if identity in _registered_targets():
        return {"database": identity, "removed": 0, "kept": 0, "sharedWithAnotherVersion": True}
    stats = provider_from_env().publish_objects(data_source=identity, engine=target["engine"], objects=[])
    return {"database": identity, **stats, "sharedWithAnotherVersion": False}

# ── Events from the providers ───────────────────────────────────────────────

def _registered(detail: dict, account: str, region: str) -> None:
    entry = scan_providers.register(detail.get("engine"), detail.get("scannerFunctionArn"),
                                    detail.get("resultsBucket"), account=account, region=region)
    logger.info("Registered the %s provider: %s", entry["engine"], entry["scannerFunctionArn"])


def _deregistered(detail: dict) -> None:
    if scan_providers.deregister(detail.get("engine"), detail.get("scannerFunctionArn")):
        logger.info("Deregistered the %s provider: %s", detail.get("engine"), detail.get("scannerFunctionArn"))
    else:
        logger.info("Kept the %s provider: %s is no longer the one registered", detail.get("engine"),
                    detail.get("scannerFunctionArn"))


def _waiting_target(detail: dict) -> dict | None:
    """The target this answer is for, if it is still waiting on exactly this scan."""
    target = scan_targets.get(str(detail.get("targetId") or ""))
    if target is None or target["disabled"] or target.get("scanRequestId") != detail.get("requestId"):
        logger.warning("Ignoring a stale or unknown scan answer: %s", json.dumps(detail)[:500])
        return None
    return target


def _completed(detail: dict) -> None:
    target = _waiting_target(detail)
    if target is None:
        return
    request_id = target["scanRequestId"]
    try:
        provider = scan_providers.get(target["engine"])
        expected_key = scan_events.scan_result_key(target["targetId"], request_id)
        if provider is None or detail.get("bucket") != provider["resultsBucket"] or detail.get("key") != expected_key:
            raise ScanRefused("The scan result did not come from the registered provider's results bucket.")
        body = boto3.client("s3").get_object(Bucket=provider["resultsBucket"], Key=expected_key)["Body"].read()
        objects = [SchemaObject.from_dict(o) for o in json.loads(body)]
        stats = provider_from_env().publish_objects(data_source=target["identity"], engine=target["engine"],
                                                    objects=objects)
    except Exception as e:
        logger.exception("Could not index scan %s of %s", request_id, target["identity"])
        message = str(e) if isinstance(e, ScanRefused) else (
            f"The scan finished but could not be indexed ({type(e).__name__}); "
            "the ingestion function's log has the details.")
        scan_targets.record_status(target["targetId"], scan_targets.status("failed", message), answering=request_id)
        return
    logger.info("Indexed %d object(s) of %s: %s", len(objects), target["identity"], stats)
    scan_targets.record_status(target["targetId"], scan_targets.status(
        "scanned", f"Scanned {len(objects)} object(s)."), answering=request_id)


def _failed(detail: dict) -> None:
    target = _waiting_target(detail)
    if target is None:
        return
    message = str(detail.get("message") or "The scan failed.")[:2000]
    scan_targets.record_status(target["targetId"], scan_targets.status("failed", message),
                               answering=target["scanRequestId"])


def _on_event(event: dict) -> None:
    # The bus rules deliver only the providers' source; checked again so a misrouted
    # rule cannot feed this function another service's events.
    expected = scan_events.source(require_env("STACK_NAME"), scan_events.DATABASE_PROVIDER)
    if event.get("source") != expected:
        raise ScanRefused(f"Events from {event.get('source')!r} are not taken; expected {expected!r}.")
    detail, detail_type = event.get("detail") or {}, event["detail-type"]
    if detail_type == scan_events.PROVIDER_REGISTERED:
        return _registered(detail, account=event.get("account", ""), region=event.get("region", ""))
    if detail_type == scan_events.PROVIDER_DEREGISTERED:
        return _deregistered(detail)
    if detail_type == scan_events.SCAN_COMPLETED:
        return _completed(detail)
    if detail_type == scan_events.SCAN_FAILED:
        return _failed(detail)
    raise ScanRefused(f"{detail_type!r} is not an event this function takes.")


def handler(event: dict, context) -> dict | None:
    event = event or {}
    if "detail-type" in event:
        return _on_event(event)
    if "purge" in event:
        return _purge(event["purge"])
    if "disabled" in event:
        return _disabled(str(event["disabled"] or ""))
    if "targetId" in event:
        return _request_scan(str(event["targetId"] or ""))
    raise ValueError("Expected {'targetId': ...}, {'purge': {...}}, {'disabled': ...} or a scan bus event.")
