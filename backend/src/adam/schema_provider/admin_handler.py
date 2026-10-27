#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Administrator routes: which databases may be scanned, with which scanner
secret, on what schedule, running a scan, and each engine's advice for the source's administrator
setting up its accounts.

Every route requires membership of the Cognito `admin` group, checked here from
the verified token -- hiding the tab in the UI is cosmetic, this is the control.

The scan-target list is the schema service's own table (scan_targets). Engines
are the ones whose database provider registered with this service
(scan_providers); each provider's scanner function is what this function calls
for what must be known at once: whether a scanner login works, storing and
removing scanner logins, and the engine's advice for the source's administrator. Scans themselves go
through the ingestion function and the scan bus.

A target's scanner login is a secret under /<stack>/scanners/, the database
provider service's path: either one the administrator's secrets process created
there, given by ARN, or one the scanner function creates from a username and
password typed here. Saving checks the scanner can log in, then starts a scan in
the background; the ingestion function records its progress on the target
(queued, scanning, scanned or failed), which the list shows. A password is passed
straight to the scanner function, which stores it in Secrets Manager: it is never
stored elsewhere, logged, or sent back.

Each registration is a version of a database (scan_targets): where the database is and how
it is reached are fixed when the version is created, so a user's password, saved for one
version, only ever goes there. Changing them is a new version, with its own scanner login;
users connect to it themselves, and older versions keep their connections until disabled.
An edit changes only what sends nothing anywhere new: the database's name and limits, a
version's schedule, and its scanner login, which is checked where the version already is.
Every creation, edit and disabling records who made it, from the verified token.

Disabling a version refuses it to users at once, then deletes its schedule, the scanner
secret if this stack created it, and (through the ingestion function and the database
providers) its users' saved logins and its index entries. Each step is safe to repeat, so a
disabling that fails part-way is run again. A version is never deleted.

A schedule is an EventBridge Scheduler schedule in the stack's schedule group
that invokes the ingestion function with the target's id. All the IAM it needs
was created at deploy time; nothing here creates a role or a policy.
"""

import json
import logging
import re
import ssl
import uuid
from datetime import UTC, datetime, timedelta

import boto3
from botocore.config import Config

from schema_provider import scan_providers, scan_targets
from schema_provider.source_identity import source_identity
from shared.environment import require_env
from shared.http_response import Rejected, bad_request, get_user_id, require_group, route_request, success
from shared.network_target import validate_target
from shared.provider_client import ProviderError, ProviderRefused, call_function
from shared.statement_limits import LimitError, checked

ADMIN_GROUP = "admin"

# API Gateway cuts an HTTP API request off at 30 seconds. The login check and a
# purge answer well within it; scans run in the background.
SCAN_WAIT_SECONDS = 25

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

_glue = None
_lambda_client = None
_scheduler = None


def _get_lambda():
    global _lambda_client
    if _lambda_client is None:
        # No retries: a retried purge or login check would only repeat a failure.
        _lambda_client = boto3.client("lambda", config=Config(
            read_timeout=SCAN_WAIT_SECONDS, retries={"max_attempts": 0}))
    return _lambda_client


def _get_glue():
    global _glue
    if _glue is None:
        _glue = boto3.client("glue")
    return _glue


def _get_scheduler():
    global _scheduler
    if _scheduler is None:
        _scheduler = boto3.client("scheduler")
    return _scheduler


def _same(a: str, b: str) -> bool:
    return a.strip().lower() == b.strip().lower()


def _table_counts() -> dict[str, int] | None:
    """Tables currently indexed per source database, or None when the index cannot be read.

    None rather than zeros: an unreadable index must not look like "never scanned".
    """
    index_database = require_env("SCHEMA_INDEX_DATABASE")
    counts: dict[str, int] = {}
    try:
        for page in _get_glue().get_paginator("get_tables").paginate(DatabaseName=index_database):
            for table in page.get("TableList", []):
                source = (table.get("Parameters") or {}).get("source_database", "").strip().lower()
                if source:
                    counts[source] = counts.get(source, 0) + 1
    except Exception:
        logger.exception("Could not count the tables in %s", index_database)
        return None
    return counts


# ── Request fields ──────────────────────────────────────────────────────────

def _registered_engines() -> list[str]:
    return [p["engine"] for p in scan_providers.read_all()]


def _scanner(engine: str) -> str:
    """The scanner function of the provider registered for `engine`, or Rejected."""
    provider = scan_providers.get(engine)
    if provider is None:
        raise Rejected(f"No {engine} database provider is registered with this deployment. "
                       f"Registered engines: {', '.join(_registered_engines()) or 'none'}.")
    return provider["scannerFunctionArn"]


def _call_scanner(engine: str, operation: str, args: dict, what: str):
    """One operation of the engine's scanner function. Its refusals are written for the
    admin and shown; any other failure is logged there and not echoed."""
    try:
        return call_function(_scanner(engine), operation, args)
    except ProviderRefused as e:
        raise Rejected(str(e)) from None
    except ProviderError as e:
        logger.error("%s failed: %s", what, e)
        raise ProviderError(f"{what} failed; see the {engine} scanner function's log.") from None


def _location(engine: str, values: dict) -> dict:
    """engine/host/port/database as typed (only the engine is lower-cased), checked."""
    engine = str(engine or "").strip().lower()
    fields = {f: str(values.get(f) or "").strip() for f in scan_targets.FIELDS}
    if not engine or not all(fields.values()):
        raise Rejected("Give the engine, host, port and database.")
    _scanner(engine)
    validate_target(fields["host"])
    return {"engine": engine, **fields}


def _identity(location: dict) -> str:
    return source_identity(scan_targets.connection({"targetId": "", **location}))


_ARN = re.compile(r"^arn:aws[a-z-]*:secretsmanager:[a-z0-9-]+:\d{12}:secret:(?P<name>.+)$")


def _scanner_path() -> str:
    """Where scanner logins live: the database provider service's scanner functions
    may read nothing else."""
    return f"/{require_env('STACK_NAME')}/scanners/"


def _secret_choice(body: dict, others: list[dict]) -> dict | None:
    """The scanner secret the request gives: {"arn"}, {"username", "password"}, or None."""
    arn = str(body.get("scannerSecretArn") or "").strip()
    username = str(body.get("scannerUsername") or "").strip()
    password = body.get("scannerPassword") or ""
    if arn and (username or password):
        raise Rejected("Give either the ARN of an existing scanner secret or the scanner's "
                       "username and password, not both.")
    if arn:
        match = _ARN.match(arn)
        if not match:
            raise Rejected("Give the ARN of the scanner secret (a Secrets Manager secret ARN).")
        if not match["name"].startswith(_scanner_path()):
            raise Rejected(f"A scanner secret must be created under {_scanner_path()}: the schema scan "
                           "may read no other secret.")
        # A secret the stack made is deleted with its target, so it is never shared.
        if any(t["scannerSecretArn"] == arn for t in others):
            raise Rejected("Another target uses that secret. Give this database its own.")
        return {"arn": arn}
    if username or password:
        if not (username and isinstance(password, str) and password):
            raise Rejected("Give both the scanner's username and its password.")
        return {"username": username, "password": password}
    return None


_RATE = re.compile(r"^rate\(\s*[1-9]\d*\s+(minute|minutes|hour|hours|day|days)\s*\)$")
_CRON = re.compile(r"^cron\(\s*\S+(\s+\S+){5}\s*\)$")


def _schedule_expression(value) -> str | None:
    """A rate(...) or cron(...) expression, or None for on demand only."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise Rejected("The schedule must be a rate(...) or cron(...) expression.")
    text = value.strip()
    if not text:
        return None
    if not (_RATE.match(text) or _CRON.match(text)):
        raise Rejected(f"{text!r} is not a schedule. Use rate(<n> minutes|hours|days), e.g. "
                       "rate(1 day), or cron(<minutes> <hours> <day-of-month> <month> "
                       "<day-of-week> <year>), e.g. cron(0 2 * * ? *), evaluated in UTC.")
    return text


# ── Scanner secrets ─────────────────────────────────────────────────────────

def _apply_secret(choice: dict, engine: str, identity: str, undo) -> tuple[str, bool]:
    """The chosen secret's ARN, and whether the stack created it. A brought secret is
    used as given: whether the scanner may read it is found out by logging in."""
    if "arn" in choice:
        return choice["arn"], False
    arn = _call_scanner(engine, "store_login", {"identity": identity, **choice},
                        "Storing the scanner login")["scannerSecretArn"]
    undo.append(lambda: _remove_login(engine, arn))
    return arn, True


def _remove_login(engine: str, arn: str) -> None:
    _call_scanner(engine, "remove_login", {"scannerSecretArn": arn}, "Removing the scanner login")


def _retire_secret(target: dict) -> list[str]:
    """Delete a target's old secret if the stack made it (a brought one is the
    administrator's to keep). Returns what could not be done, for the admin."""
    if not target["scannerSecretManaged"]:
        return []
    try:
        _remove_login(target["engine"], target["scannerSecretArn"])
    except (Rejected, ProviderError) as e:
        return [f"The old scanner secret could not be deleted: {e} Delete it in Secrets Manager."]
    return []


# ── Schedules ───────────────────────────────────────────────────────────────

def _schedule(target_id: str) -> str | None:
    try:
        return _get_scheduler().get_schedule(
            Name=scan_targets.schedule_name(target_id), GroupName=require_env("SCAN_SCHEDULE_GROUP"),
        )["ScheduleExpression"]
    except _get_scheduler().exceptions.ResourceNotFoundException:
        return None


def _put_schedule(target_id: str, expression: str, exists: bool) -> None:
    put = _get_scheduler().update_schedule if exists else _get_scheduler().create_schedule
    try:
        put(Name=scan_targets.schedule_name(target_id), GroupName=require_env("SCAN_SCHEDULE_GROUP"),
            ScheduleExpression=expression, FlexibleTimeWindow={"Mode": "OFF"},
            Target={"Arn": require_env("SCHEMA_INGESTION_FUNCTION_ARN"),
                    "RoleArn": require_env("SCAN_SCHEDULER_ROLE_ARN"),
                    "Input": json.dumps({"targetId": target_id})})
    except _get_scheduler().exceptions.ValidationException as e:
        raise Rejected(f"EventBridge Scheduler refused the schedule: {e.response['Error']['Message']}") from e


def _delete_schedule(target_id: str) -> None:
    try:
        _get_scheduler().delete_schedule(Name=scan_targets.schedule_name(target_id),
                                         GroupName=require_env("SCAN_SCHEDULE_GROUP"))
    except _get_scheduler().exceptions.ResourceNotFoundException:
        pass


def _set_schedule(target_id: str, expression: str | None, current: str | None) -> None:
    if expression == current:
        return
    if expression is None:
        _delete_schedule(target_id)
    else:
        _put_schedule(target_id, expression, exists=current is not None)


# ── The ingestion function ──────────────────────────────────────────────────

def _invoke_ingestion(payload: dict, what: str) -> dict:
    resp = _get_lambda().invoke(
        FunctionName=require_env("SCHEMA_INGESTION_FUNCTION_ARN"),
        InvocationType="RequestResponse",
        Payload=json.dumps(payload),
    )
    result = json.loads(resp["Payload"].read())
    if resp.get("FunctionError"):
        message = result.get("errorMessage") or "failed"
        logger.error("%s failed: %s", what, message)
        # The ingestion function's refusals are written for the admin; anything
        # else is logged above and not echoed.
        if result.get("errorType") == "ScanRefused":
            raise Rejected(message)
        raise ProviderError(f"{what} failed; see the ingestion function's logs.")
    return result


def _require_login(target: dict) -> dict:
    """The engine's scanner function logs in as the target's scanner, as a scan would.

    Returns the target with what the login found about TLS, for an engine whose
    provider decides it (Oracle); the probe tries TLS first.
    """
    try:
        result = _call_scanner(target["engine"], "probe", {
            "location": {f: target[f] for f in scan_targets.FIELDS},
            "scannerSecretArn": target["scannerSecretArn"],
        }, f"Checking the scanner login for {target['identity']}")
    except ProviderError as e:
        raise Rejected("The scanner login could not be checked; the scanner function's log has the reason.") from e
    if not result.get("ok"):
        raise Rejected("The scanner login could not be checked.")
    if "tls" not in result:
        return target
    checked = {k: v for k, v in target.items() if k not in ("tlsWarning", "tlsCertificate", "tlsFingerprint")}
    found = {k: result[k] for k in ("tlsWarning", "tlsCertificate", "tlsFingerprint") if result.get(k)}
    return {**checked, "tls": result["tls"], **found}


class _Undo(list):
    """Steps that reverse what a request has done so far, run if it then fails."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            for step in reversed(self):
                step()
        return False


def _queue_scan(target: dict) -> dict:
    """Mark the version queued and start its scan; the ingestion function records the rest."""
    queued = scan_targets.status("queued", "Waiting for the scan to start.")
    scan_targets.record_status(target["targetId"], queued)
    _get_lambda().invoke(FunctionName=require_env("SCHEMA_INGESTION_FUNCTION_ARN"), InvocationType="Event",
                         Payload=json.dumps({"targetId": target["targetId"]}))
    return {**target, **queued}


def _scan_state(target: dict) -> dict:
    """The target's scan status as shown, or nulls when it was never scanned."""
    if target.get("scanStatus") is None:
        return dict.fromkeys(scan_targets.STATUS_FIELDS)
    state = {f: target[f] for f in scan_targets.STATUS_FIELDS}
    # A scan cut off by the scanner function's timeout never answers.
    limit = int(require_env("SCAN_TIMEOUT_SECONDS"))
    started = datetime.fromisoformat(state["scanUpdatedAt"])
    if state["scanStatus"] in ("queued", "scanning") and datetime.now(UTC) - started > timedelta(seconds=limit + 60):
        state["scanStatus"] = "failed"
        state["scanMessage"] = (f"The scan did not finish within {limit // 60} minutes; "
                                f"the {target['engine']} scanner function's log has the details.")
    return state


NAME_MAX = 64


def _name(value, others: list[dict]) -> str:
    """The name users choose the database by: required, at most NAME_MAX characters,
    and no other database's (ignoring case), so a user cannot pick the wrong one.
    `others` are the versions of every other database."""
    name = str(value or "").strip()
    if not name:
        raise Rejected("Give the database a name: users choose it by name on the Connections page.")
    if len(name) > NAME_MAX:
        raise Rejected(f"A name can be at most {NAME_MAX} characters.")
    if any(t.get("name") and _same(t["name"], name) for t in others):
        raise Rejected(f"Another database is already named {name!r}.")
    return name


def _limits(timeout_seconds, row_cap) -> dict:
    """The statement timeout and row cap users' statements get, checked."""
    try:
        timeout_seconds, row_cap = checked(timeout_seconds, row_cap)
    except LimitError as e:
        raise Rejected(str(e)) from None
    return {"statementTimeoutSeconds": timeout_seconds, "rowCap": row_cap}


def _view(target: dict, schedule: str | None, counts: dict[str, int] | None = None,
          everything: list[dict] | None = None) -> dict:
    newest = scan_targets.latest_active(scan_targets.lineage(target, everything))
    return {
        "name": target.get("name"),
        **{f: target.get(f) for f in scan_targets.LIMIT_FIELDS},
        **_scan_state(target),
        "targetId": target["targetId"], "lineageId": target["lineageId"], "version": target["version"],
        "latest": newest is not None and newest["targetId"] == target["targetId"],
        "identity": target["identity"], "engine": target["engine"],
        **{f: target[f] for f in scan_targets.FIELDS},
        "scannerSecretArn": target["scannerSecretArn"],
        "scannerSecretManaged": target["scannerSecretManaged"],
        "disabled": target["disabled"],
        "schedule": schedule,
        "tableCount": None if counts is None else counts.get(target["identity"].strip().lower(), 0),
        **{f: target.get(f) for f in ("createdBy", "createdAt", "updatedBy", "updatedAt",
                                      "disabledBy", "disabledAt")},
        **{f: target[f] for f in ("tls", "tlsWarning", "tlsFingerprint") if f in target},
    }


def _caller(event: dict) -> dict:
    """Who is making a change, from the verified token: the name people read, and the sub. The
    email, which the pool's pre token generation trigger adds to the access token, is the name
    users sign in with; without it, the username, which in an email-sign-in pool is an id."""
    sub = get_user_id(event)
    claims = event["requestContext"]["authorizer"]["jwt"]["claims"]
    name = str(claims.get("email") or claims.get("username") or claims.get("cognito:username") or "").strip()
    return {"by": name or sub, "sub": sub, "at": datetime.now(UTC).isoformat(timespec="seconds")}


def _only(body, allowed: tuple[str, ...], what: str) -> dict:
    """The request's fields, refusing any it may not set, by name: a field it does not know
    could be a security field spelt another way."""
    if not isinstance(body, dict):
        raise Rejected("Expected a JSON object.")
    refused = sorted(set(body) - set(allowed))
    if refused:
        raise Rejected(f"{what} cannot set {', '.join(refused)}. Where a database is and how it is reached are "
                       "fixed for each version: publish a new version to change them.")
    return body


def _same_certificate(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return a == b
    try:
        return ssl.PEM_cert_to_DER_cert(a.strip()) == ssl.PEM_cert_to_DER_cert(b.strip())
    except ValueError:
        return a.strip() == b.strip()


def _require_same_transport(target: dict, found: dict) -> None:
    """A login check against a stored version changes nothing about how it is reached: when the
    database now presents something else, the admin decides on a new version."""
    if found.get("tls") != target.get("tls") \
            or not _same_certificate(found.get("tlsCertificate"), target.get("tlsCertificate")):
        now = ("TLS with a different certificate" if found.get("tls") and target.get("tls")
               else "TLS" if found.get("tls") else "no TLS")
        raise Rejected(f"{target['identity']} now answers with {now}, not as version {target['version']} was saved. "
                       "Nothing was changed. If you trust what it presents now, publish a new version: users "
                       "then connect to it with their own logins.")


# ── Routes ──────────────────────────────────────────────────────────────────

def _find(target_id) -> dict:
    target = scan_targets.get(str(target_id or ""))
    if target is None:
        raise Rejected(f"{target_id or 'That target'} is not a registered scan target.")
    return target


def _require_active(target: dict) -> None:
    if target["disabled"]:
        raise Rejected(f"{target['identity']} (version {target['version']}) is disabled. Disable it again to finish "
                       "if that did not complete; to use the database, publish a new version.")


def _list_scan_targets(event, path_params, query_params, body):
    require_group(event, ADMIN_GROUP)
    counts = _table_counts()
    everything = scan_targets.read_all()
    return success({
        "targets": [_view(t, _schedule(t["targetId"]), counts, everything) for t in everything],
        "engines": _registered_engines(),
        # Where a brought secret must be created, so nobody has to know the stack's name.
        "scannerSecretPath": _scanner_path(),
    })


def _create_version(*, location: dict, choice: dict, schedule: str | None, settings: dict,
                    lineage_id: str | None, version: int, caller: dict) -> dict:
    """Check the scanner logs in where `location` says, then store the version, with what the
    login found about TLS, and scan it. A version that cannot be checked is not stored."""
    target_id = str(uuid.uuid4())
    identity = _identity(location)
    with _Undo() as undo:
        arn, managed = _apply_secret(choice, location["engine"], identity, undo)
        target = _require_login({
            "targetId": target_id, "lineageId": lineage_id or target_id, "version": version,
            **settings, "identity": identity, **location,
            "scannerSecretArn": arn, "scannerSecretManaged": managed, "disabled": False,
            "createdBy": caller["by"], "createdBySub": caller["sub"], "createdAt": caller["at"]})
        _set_schedule(target_id, schedule, None)
        undo.append(lambda: _delete_schedule(target_id))
        scan_targets.create(target)
    logger.info("Version %d of %s created by %s (%s): %s, scanner secret %s", version, target["lineageId"],
                caller["by"], caller["sub"], identity, arn)
    return _queue_scan(target)


@bad_request
def _add_scan_target(event, path_params, query_params, body):
    """A new database: its first version."""
    require_group(event, ADMIN_GROUP)
    caller = _caller(event)
    if not isinstance(body, dict):
        raise Rejected("Expected a JSON object.")
    location = _location(body.get("engine"), body)
    existing = scan_targets.read_all()
    choice = _secret_choice(body, existing)
    if choice is None:
        raise Rejected("Give the ARN of an existing scanner secret, or the scanner's username and password.")
    schedule = _schedule_expression(body.get("schedule"))
    identity = _identity(location)
    if any(_same(t["identity"], identity) for t in existing if not t["disabled"]):
        raise Rejected(f"{identity} is already registered. Publish a new version of it instead.")
    settings = {"name": _name(body.get("name"), existing),
                **_limits(body.get("statementTimeoutSeconds"), body.get("rowCap"))}
    target = _create_version(location=location, choice=choice, schedule=schedule, settings=settings,
                             lineage_id=None, version=1, caller=caller)
    return success(_view(target, schedule), 201)


_NEW_VERSION_FIELDS = (*scan_targets.FIELDS, "scannerSecretArn", "scannerUsername", "scannerPassword", "schedule")


@bad_request
def _add_version(event, path_params, query_params, body):
    """The next version of a database: where it is now, with its own scanner login. The
    database's name and limits carry over; its users stay on the versions they connected to."""
    require_group(event, ADMIN_GROUP)
    caller = _caller(event)
    body = _only(body, _NEW_VERSION_FIELDS, "A new version")
    current = _find((path_params or {}).get("targetId"))
    everything = scan_targets.read_all()
    versions = scan_targets.lineage(current, everything)
    newest = scan_targets.latest_active(versions) or versions[-1]
    location = _location(current["engine"], {f: body.get(f, newest[f]) for f in scan_targets.FIELDS})
    identity = _identity(location)
    if any(_same(t["identity"], identity) for t in everything
           if not t["disabled"] and t["lineageId"] != current["lineageId"]):
        raise Rejected(f"{identity} is registered as another database.")
    choice = _secret_choice(body, everything)
    if choice is None:
        raise Rejected("A new version needs its own scanner login: the ARN of an existing secret, "
                       "or the scanner's username and password.")
    settings = {f: newest[f] for f in scan_targets.DATABASE_SETTINGS if f in newest}
    target = _create_version(location=location, choice=choice,
                             schedule=_schedule_expression(body.get("schedule")), settings=settings,
                             lineage_id=current["lineageId"], version=versions[-1]["version"] + 1, caller=caller)
    return success(_view(target, _schedule(target["targetId"])), 201)


_SETTINGS_FIELDS = ("name", *scan_targets.LIMIT_FIELDS, "schedule", "scannerSecretArn", "scannerUsername",
                    "scannerPassword")


@bad_request
def _update_scan_target(event, path_params, query_params, body):
    """The settings that send nothing anywhere new: the database's name and limits (on every
    version of it), this version's schedule, and its scanner login, checked where it is."""
    require_group(event, ADMIN_GROUP)
    caller = _caller(event)
    body = _only(body, _SETTINGS_FIELDS, "An edit")
    current = _find((path_params or {}).get("targetId"))
    _require_active(current)
    everything = scan_targets.read_all()
    versions = scan_targets.lineage(current, everything)
    other_databases = [t for t in everything if t["lineageId"] != current["lineageId"]]

    settings = {}
    if "name" in body:
        settings["name"] = _name(body["name"], other_databases)
    if any(f in body for f in scan_targets.LIMIT_FIELDS):
        settings.update(_limits(body.get("statementTimeoutSeconds", current.get("statementTimeoutSeconds")),
                                body.get("rowCap", current.get("rowCap"))))
    settings = {k: v for k, v in settings.items() if current.get(k) != v}
    choice = _secret_choice(body, [t for t in everything if t["targetId"] != current["targetId"]])
    current_schedule = _schedule(current["targetId"])
    schedule = _schedule_expression(body["schedule"]) if "schedule" in body else current_schedule
    stamp = {"updatedBy": caller["by"], "updatedBySub": caller["sub"], "updatedAt": caller["at"]}

    target, warnings = current, []
    with _Undo() as undo:
        if choice is not None:
            arn, managed = _apply_secret(choice, current["engine"], current["identity"], undo)
            _require_same_transport(current, _require_login({**current, "scannerSecretArn": arn}))
            scan_targets.update(current["targetId"], {"scannerSecretArn": arn, "scannerSecretManaged": managed,
                                                      **stamp})
            undo.append(lambda: scan_targets.update(current["targetId"], {
                "scannerSecretArn": current["scannerSecretArn"],
                "scannerSecretManaged": current["scannerSecretManaged"]}))
            target = {**target, "scannerSecretArn": arn, "scannerSecretManaged": managed}
        if settings:
            for version in versions:
                scan_targets.update(version["targetId"], {**settings, **stamp})
            target = {**target, **settings}
        _set_schedule(current["targetId"], schedule, current_schedule)
    if settings or choice is not None or schedule != current_schedule:
        logger.info("Version %d of %s edited by %s (%s): %s", current["version"], current["lineageId"], caller["by"],
                    caller["sub"], json.dumps({**settings, **({"schedule": schedule} if schedule != current_schedule
                                                              else {}),
                                               **({"scannerSecretArn": target["scannerSecretArn"]}
                                                  if choice is not None else {})}))
    if choice is not None:
        warnings = _retire_secret(current) if target["scannerSecretArn"] != current["scannerSecretArn"] else []
        target = _queue_scan(target)
    return success({**_view(target, schedule), "warnings": warnings})


@bad_request
def _disable_scan_target(event, path_params, query_params, body):
    """Refuse the version to users from now on, then its schedule, the scanner secret the stack
    made, its users' saved logins (the database providers delete them), and its index entries
    unless another version is at the same place. Each step is safe to repeat, so a disabling
    that stopped part-way is run again."""
    require_group(event, ADMIN_GROUP)
    caller = _caller(event)
    target = _find((path_params or {}).get("targetId"))
    if not target["disabled"]:
        scan_targets.disable(target["targetId"], by=caller["by"], by_sub=caller["sub"], at=caller["at"])
        logger.info("Version %d of %s disabled by %s (%s): %s", target["version"], target["lineageId"],
                    caller["by"], caller["sub"], target["identity"])
    _delete_schedule(target["targetId"])
    if target["scannerSecretManaged"]:
        _remove_login(target["engine"], target["scannerSecretArn"])
    finished = _invoke_ingestion({"disabled": target["targetId"]},
                                 f"Finishing the disabling of {target['identity']}")
    return success({"disabled": target["targetId"], "identity": target["identity"], "version": target["version"],
                    "indexEntriesRemoved": finished["removed"], "indexEntriesKept": finished["kept"],
                    "sharedWithAnotherVersion": finished["sharedWithAnotherVersion"]})


@bad_request
def _start_scan(event, path_params, query_params, body):
    """Scan a registered version. The request names it; the ingestion function looks up its
    connection and scanner secret, never taking either from the request. The login is checked
    first, and a database that now presents other TLS than the version was saved with is not
    scanned: that is a new version's to record."""
    require_group(event, ADMIN_GROUP)
    target = _find(str((body or {}).get("targetId", "")).strip())
    _require_active(target)
    _require_same_transport(target, _require_login(target))
    target = _queue_scan(target)
    return success(_view(target, _schedule(target["targetId"])), 202)


@bad_request
def _admin_best_practices(event, path_params, query_params, body):
    """How an administrator should set up user and scanner accounts for one engine, from its provider."""
    require_group(event, ADMIN_GROUP)
    engine = str((path_params or {}).get("engine", "")).strip().lower()
    guidance = _call_scanner(engine, "get_admin_best_practices", {}, f"Reading the {engine} administrator guidance")
    return success({"engine": engine, "guidance": guidance})


ROUTES = {
    "GET /api/admin/best-practices/{engine}": _admin_best_practices,
    "GET /api/admin/scan-targets": _list_scan_targets,
    "POST /api/admin/scan-targets": _add_scan_target,
    "POST /api/admin/scan-targets/{targetId}/versions": _add_version,
    "PATCH /api/admin/scan-targets/{targetId}": _update_scan_target,
    "DELETE /api/admin/scan-targets/{targetId}": _disable_scan_target,
    "POST /api/admin/scans": _start_scan,
}


def handler(event, context):
    """The admin function: only these routes. It is its own function so that only its
    role can write the scan-target items and the schedules, and call the scanners."""
    return route_request(event, ROUTES)
