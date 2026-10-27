#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The scan-target list: the database versions an administrator registered.

Owned by the schema service, whose stack holds the scan-target table
(SCAN_TARGETS_TABLE), one item per version keyed on targetId. Only this service's
functions use it: the admin routes write versions, the ingestion function reads
them (it requests scans of these and no other databases) and records scan
progress, and the schema provider function serves `databases()` to other
services, which never read the table themselves.

A version is one registration of a database: where it is and how to reach it. Those are
its security fields (SECURITY_FIELDS), written once, when the version is created, and never
again: a user's connection is to one version, so a user's password only ever goes to the
place it was entered for. Changing any of them is a new version of the same database
(the same lineageId, the next version number), which users connect to themselves. An item:

    {"targetId": "<uuid>", "lineageId": "<the first version's targetId>", "version": 1,
     "identity": "postgres://host:5432/db",
     "engine": "postgres", "host": "Host", "port": "5432", "database": "db",
     "scannerSecretArn": "<secret ARN>", "scannerSecretManaged": false,
     "createdBy": "<username>", "createdBySub": "<sub>", "createdAt": "<ISO-8601 UTC>",
     "disabled": false}

And the name users pick the database by (`"name"`) and the limits every statement users
run against it gets (`"statementTimeoutSeconds"`, `"rowCap"`): the database's, kept the same
on all its versions. For Oracle, how the save found the database's TLS (`"tls"`, and
`"tlsCertificate"` when it is pinned), which are security fields too. Who last changed a
setting is `"updatedBy"`, `"updatedBySub"`, `"updatedAt"`.

A version is never deleted. Disabling it (`"disabled": true`, with `"disabledBy"`,
`"disabledBySub"`, `"disabledAt"`) refuses it to users at once and for good; its users'
connections stay as markers saying it was disabled, and whether a newer version replaces it.
Items written before versions were read as version 1 of their own lineage, created
"before versioning", with a removal begun (`"removing"`) read as disabled.

Beside those, the latest scan's progress, written by the ingestion function (and
set to queued by the admin routes when they start one):

    {"scanStatus": "queued" | "scanning" | "scanned" | "failed",
     "scanMessage": "<what happened>", "scanUpdatedAt": "<ISO-8601 UTC>"}

A version never scanned has none of them. A requested scan also sets
"scanRequestId", the id the provider answers with; an answer for any other id is
stale (the version was scanned again since) and is not recorded.

host/port/database are kept as typed; identity is source_identity of them.
scannerSecretManaged says the stack created the secret (and deletes it when the version
is disabled) rather than an administrator bringing their own.
"""

import os
from datetime import UTC, datetime

import boto3
from botocore.exceptions import ClientError

FIELDS = ("host", "port", "database")
_TEXT = ("targetId", "lineageId", "identity", "engine", *FIELDS, "scannerSecretArn", "createdBy")
_FLAGS = ("scannerSecretManaged", "disabled")
STATUS_FIELDS = ("scanStatus", "scanMessage", "scanUpdatedAt")
LIMIT_FIELDS = ("statementTimeoutSeconds", "rowCap")
# How to reach the database, as the admin's save found it (Oracle): TLS, and the certificate
# pinned when it is the database's own rather than one a CA in the bundle signed.
TRANSPORT_FIELDS = ("tls", "tlsCertificate")
# Where a user's password goes and what is trusted there: written when a version is created,
# never after (`update` refuses them by name).
SECURITY_FIELDS = ("engine", *FIELDS, "identity", *TRANSPORT_FIELDS, "tlsFingerprint", "tlsWarning")
# The database's own settings, the same on every version of it.
DATABASE_SETTINGS = ("name", *LIMIT_FIELDS)
# What `update` may write: the database's settings, the version's scanner login (checked against
# the version's own location before it is saved), and who changed it.
UPDATABLE = (*DATABASE_SETTINGS, "scannerSecretArn", "scannerSecretManaged",
             "updatedBy", "updatedBySub", "updatedAt")
_DISABLING = ("disabledBy", "disabledBySub", "disabledAt")
STATUSES = ("queued", "scanning", "scanned", "failed")
# Who created a version written before versions recorded it.
BEFORE_VERSIONING = "before versioning"


class VersionExists(RuntimeError):
    """A version with this targetId is already stored; versions are never overwritten."""


def _table():
    name = os.environ.get("SCAN_TARGETS_TABLE", "")
    if not name:
        raise RuntimeError("SCAN_TARGETS_TABLE is not set; this function cannot reach the scan-target list.")
    return boto3.resource("dynamodb").Table(name)


def _key(target_id: str) -> dict:
    return {"targetId": target_id}


def _legacy(item: dict) -> dict:
    """An item as versions are written: one from before them is version 1 of its own lineage,
    and a removal it had begun is a disabling."""
    item = dict(item)
    if "lineageId" not in item:
        item.setdefault("lineageId", item.get("targetId"))
        item.setdefault("version", 1)
        item.setdefault("createdBy", BEFORE_VERSIONING)
    if "disabled" not in item:
        item["disabled"] = item.pop("removing", False)
    item.pop("removing", None)
    return item


def _entry(item: dict) -> dict:
    item = _legacy(item)
    entry = {k: item.get(k) for k in (*_TEXT, *_FLAGS)}
    if not all(isinstance(entry[k], str) and entry[k] for k in _TEXT) \
            or not all(isinstance(entry[k], bool) for k in _FLAGS):
        raise RuntimeError(f"The scan-target list holds an item that is not a scan target: {item!r}")
    try:
        entry["version"] = int(item["version"])   # DynamoDB returns numbers as Decimal
    except (KeyError, TypeError, ValueError):
        raise RuntimeError(f"Scan target {entry['targetId']} has no readable version: {item!r}") from None
    if isinstance(item["version"], bool) or entry["version"] < 1:
        raise RuntimeError(f"Scan target {entry['targetId']} has no readable version: {item!r}")
    for field in ("createdBySub", "createdAt", "updatedBy", "updatedBySub", "updatedAt", *_DISABLING):
        if item.get(field) is not None:
            if not isinstance(item[field], str) or not item[field]:
                raise RuntimeError(f"Scan target {entry['targetId']} has an unreadable {field}: {item!r}")
            entry[field] = item[field]
    if item.get("name") is not None:
        if not isinstance(item["name"], str) or not item["name"].strip():
            raise RuntimeError(f"Scan target {entry['targetId']} has an unreadable name: {item!r}")
        entry["name"] = item["name"]
    for field in LIMIT_FIELDS:
        if item.get(field) is not None:
            entry[field] = int(item[field])
    if item.get("scanStatus") is not None:
        if item["scanStatus"] not in STATUSES \
                or not all(isinstance(item.get(k), str) for k in STATUS_FIELDS):
            raise RuntimeError(f"Scan target {entry['targetId']} has an unreadable scan status: {item!r}")
        entry.update({k: item[k] for k in STATUS_FIELDS})
    if item.get("tls") is not None:
        if not isinstance(item["tls"], bool) or not isinstance(item.get("tlsWarning", ""), str):
            raise RuntimeError(f"Scan target {entry['targetId']} has an unreadable TLS setting: {item!r}")
        entry["tls"] = item["tls"]
        if item.get("tlsWarning"):
            entry["tlsWarning"] = item["tlsWarning"]
        for field in ("tlsCertificate", "tlsFingerprint"):
            if item.get(field) is not None:
                if not item["tls"] or not isinstance(item[field], str) or not item[field]:
                    raise RuntimeError(f"Scan target {entry['targetId']} has an unreadable {field}: {item!r}")
                entry[field] = item[field]
    if item.get("scanRequestId") is not None:
        if not isinstance(item["scanRequestId"], str) or not item["scanRequestId"]:
            raise RuntimeError(f"Scan target {entry['targetId']} has an unreadable scan request id: {item!r}")
        entry["scanRequestId"] = item["scanRequestId"]
    return entry


def read_all() -> list[dict]:
    """Every version, disabled ones too. Consistent, so a version saved a moment ago is seen."""
    table, items, start = _table(), [], None
    while True:
        args = {"ConsistentRead": True}
        if start:
            args["ExclusiveStartKey"] = start
        page = table.scan(**args)
        items.extend(page.get("Items", []))
        start = page.get("LastEvaluatedKey")
        if not start:
            return [_entry(i) for i in items]


def get(target_id: str) -> dict | None:
    if not target_id:
        return None
    item = _table().get_item(Key=_key(target_id), ConsistentRead=True).get("Item")
    return _entry(item) if item else None


def lineage(target: dict, everything: list[dict] | None = None) -> list[dict]:
    """Every version of `target`'s database, oldest first."""
    everything = read_all() if everything is None else everything
    versions = [t for t in everything if t["lineageId"] == target["lineageId"]]
    return sorted(versions, key=lambda t: t["version"])


def create(entry: dict) -> None:
    """Store a new version. Refused if its targetId is already stored: a version's security
    fields are written here, once."""
    try:
        _table().put_item(Item=_entry(entry), ConditionExpression="attribute_not_exists(targetId)")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            raise VersionExists(f"Version {entry['targetId']} already exists; "
                                "versions are never overwritten.") from None
        raise


def update(target_id: str, values: dict) -> None:
    """Change a version's settings: only UPDATABLE ones, and only on a version that exists.

    Anything else -- a security field above all -- is refused by name, whatever the caller."""
    refused = sorted(set(values) - set(UPDATABLE))
    if refused:
        raise ValueError(f"These cannot be changed on a version: {', '.join(refused)}.")
    if not values:
        return
    _entry({**(get(target_id) or {"targetId": target_id}), **values})   # what it would read back as
    names = {f"#f{i}": field for i, field in enumerate(values)}
    _table().update_item(
        Key=_key(target_id),
        UpdateExpression="SET " + ", ".join(f"{name} = :v{name[2:]}" for name in names),
        ConditionExpression="attribute_exists(targetId)",
        ExpressionAttributeNames=names,
        ExpressionAttributeValues={f":v{name[2:]}": values[field] for name, field in names.items()})


def disable(target_id: str, *, by: str, by_sub: str, at: str) -> None:
    """Refuse the version to users from now on. Repeating it keeps the first disabling."""
    try:
        _table().update_item(
            Key=_key(target_id),
            UpdateExpression="SET disabled = :t, disabledBy = :b, disabledBySub = :s, disabledAt = :a REMOVE removing",
            ConditionExpression="attribute_exists(targetId) AND (attribute_not_exists(disabled) OR disabled = :f)",
            ExpressionAttributeValues={":t": True, ":f": False, ":b": by, ":s": by_sub, ":a": at})
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
            raise
        if get(target_id) is None:
            raise


def _put(entry: dict) -> None:
    """Overwrite an item, whatever it holds. For tests that set up a state no route writes."""
    _table().put_item(Item=_entry(entry))


def _delete(target_id: str) -> None:
    """Delete an item. No route does: for tests that clean up what they registered."""
    _table().delete_item(Key=_key(target_id))


def latest_active(versions: list[dict]) -> dict | None:
    """The newest version not disabled, of one database's versions: the one new connections go to."""
    active = [t for t in versions if not t["disabled"]]
    return max(active, key=lambda t: t["version"]) if active else None


def databases() -> list[dict]:
    """The versions users' connections may use: each named one not disabled, with where it is
    and its statement limits, and never its scanner secret. `latest` marks the version of each
    database that new connections go to."""
    everything = read_all()
    return [database(t, everything) for t in everything if t.get("name") and not t["disabled"]]


def database(target: dict, everything: list[dict] | None = None) -> dict:
    """A version as other services see it."""
    newest = latest_active(lineage(target, everything))
    return {"targetId": target["targetId"], "lineageId": target["lineageId"], "version": target["version"],
            "latest": newest is not None and newest["targetId"] == target["targetId"],
            "name": target["name"], "engine": target["engine"],
            "identity": target["identity"], **{f: target[f] for f in FIELDS},
            **{f: target[f] for f in (*LIMIT_FIELDS, *TRANSPORT_FIELDS) if f in target}}


def refusal(target: dict | None, everything: list[dict] | None = None) -> str:
    """Why users may not use this version, written for them: it is not registered, or it was
    disabled, and then whether a newer version replaces it."""
    if target is None or not target.get("name"):
        return "That database is not registered."
    newest = latest_active(lineage(target, everything))
    if newest is not None and newest["version"] > target["version"]:
        return (f"{target['name']} was updated by an administrator. Remove this connection and connect to "
                f"{newest['name']} again with your login.")
    return f"{target['name']} was disabled by an administrator, and nothing replaces it. Remove this connection."


def disabled_databases() -> list[dict]:
    """The disabled versions, by targetId and name, with why each is refused, and nothing
    about where any of them is."""
    everything = read_all()
    return [{"targetId": t["targetId"], "name": t["name"], "disabled": refusal(t, everything)}
            for t in everything if t.get("name") and t["disabled"]]


def status(status: str, message: str) -> dict:
    """The status fields for a target, stamped now."""
    if status not in STATUSES:
        raise ValueError(f"{status!r} is not one of {STATUSES}.")
    return {"scanStatus": status, "scanMessage": message,
            "scanUpdatedAt": datetime.now(UTC).isoformat(timespec="seconds")}


def record_status(target_id: str, status_fields: dict, *, request_id: str | None = None,
                  answering: str | None = None) -> bool:
    """Write only a target's status (what the ingestion function may write).

    request_id starts a scan: it is stored with the status. answering records a
    provider's answer, only while that is still the target's scan. False when the
    target is gone or the answer is stale, so a scan that outlives a removal or an
    edit does not overwrite what came after it.
    """
    values = {":s": status_fields["scanStatus"], ":m": status_fields["scanMessage"],
              ":t": status_fields["scanUpdatedAt"]}
    update = "SET scanStatus = :s, scanMessage = :m, scanUpdatedAt = :t"
    condition = "attribute_exists(targetId)"
    if request_id is not None:
        update += ", scanRequestId = :r"
        values[":r"] = request_id
    if answering is not None:
        condition += " AND scanRequestId = :a"
        values[":a"] = answering
    try:
        _table().update_item(Key=_key(target_id), UpdateExpression=update,
                             ConditionExpression=condition, ExpressionAttributeValues=values)
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
            return False
        raise
    return True


def connection(entry: dict) -> dict:
    """Where the entry's database is, in the form source_identity reads."""
    if not all(entry[f] for f in FIELDS):
        raise ValueError(f"Scan target {entry['targetId']} does not say where its database is.")
    host, port, database = (entry[f] for f in FIELDS)
    if entry["engine"] == "oracle":
        return {"engine": "oracle", "dsn": f"{host}:{port}/{database}"}
    return {"engine": entry["engine"], "host": host, "port": port, "database": database}


def schedule_name(target_id: str) -> str:
    """The EventBridge Scheduler schedule that scans this version (one per version)."""
    return f"scan-{target_id}"
