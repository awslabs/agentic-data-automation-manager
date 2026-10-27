#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The admin routes: who may call them, and what they do to scan targets, their
scanner logins, their schedules, their index entries, and scans.

Real pieces: the route handlers, DynamoDB Local for the scan-target and provider
tables, the schema provider function that serves targets to users, moto Secrets
Manager for scanner logins, moto S3 for scan results, moto EventBridge Scheduler,
and the real ingestion and Postgres scanner handlers (in-process as the "Lambdas")
against the compose Postgres, so every save logs in as the scanner. The scan bus
hands each event to the handler whose rule matches it. moto does not enforce IAM,
so the scanner role's one secret grant -- scanner logins under /<stack>/scanners/,
nothing else -- is applied to its Secrets Manager client here. Only the schema
index is a stand-in.
"""

import json
import logging
import secrets
import uuid
from pathlib import Path

import boto3
import botocore.exceptions
import pytest

from adam.data_source_provider import disabled_version_handler, scanner_handler
from adam.data_source_provider.secrets_manager_credential_provider import (
    SecretsManagerPasswordProvider,
)
from adam.schema_provider import (
    admin_handler as admin,
    glue_data_catalog_schema_provider as schema_module,
    scan_providers,
    schema_ingestion_handler,
)
from adam.shared import scan_events
from shared import provider_client
from shared.network_target import validate_target as REAL_VALIDATE_TARGET
from tests.integration import seed
from tests.integration.conftest import validator_allowing_the_containers
from tests.integration.lambda_router import LambdaRouter

pytestmark = pytest.mark.integration

# Any password the database never issued: a fresh one each run.
WRONG_PASSWORD = secrets.token_urlsafe(24)

scan_targets = admin.scan_targets  # the module object the handler imported (flat, as its bundle does)

GROUP = "test-scan-schedules"
ROLE_ARN = "arn:aws:iam::123456789012:role/test-scan-scheduler"
INGESTION_ARN = "arn:aws:lambda:us-east-1:123456789012:function:ingest-fn"
SCANNERS = {"postgres": "arn:aws:lambda:us-east-1:123456789012:function:pg-scanner",
            "oracle": "arn:aws:lambda:us-east-1:123456789012:function:oracle-scanner"}
SCANNER_PATH = "/test/scanners/"
ADMIN, USER = "sub-admin", "sub-user"


def _event(user, groups, body=None):
    # As Cognito's access token carries them: the sub, the username (in this pool, a generated id),
    # and the email the pool's pre token generation trigger adds.
    claims = {"sub": user, "username": f"{user}-id", "email": f"{user}-name"}
    if groups is not None:
        claims["cognito:groups"] = groups
    return {"requestContext": {"authorizer": {"jwt": {"claims": claims}}}}


AS_ADMIN = _event(ADMIN, "[admin]")


def _der(pem: str) -> bytes:
    """A certificate's bytes, whatever the PEM's line wrapping."""
    import ssl
    return ssl.PEM_cert_to_DER_cert(pem.strip())


def _body(response):
    return json.loads(response["body"])


class _Index:
    """Records publishes, and how many entries this deployment made per database. Which
    entries the real index keeps is the Glue provider's, tested in test_schema_visibility."""

    def __init__(self):
        self.published = []
        self.ours = {}

    def publish_objects(self, *, data_source, engine, objects):
        objects = list(objects)
        self.published.append((data_source, engine, objects))
        removed = max(self.ours.get(data_source, 0) - len(objects), 0)
        self.ours[data_source] = len(objects)
        return {"created": len(objects), "updated": 0, "removed": removed,
                "kept": 0}


class _ScannerRoleSecrets:
    """The scanner role's Secrets Manager access, as its IAM policy allows it: logins
    under the scanner path and nothing else."""

    def __init__(self, client):
        self._client = client

    def get_secret_value(self, SecretId):
        name = SecretId.split(":secret:", 1)[-1]
        if not name.startswith(SCANNER_PATH):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDeniedException",
                           "Message": f"not authorized to perform: secretsmanager:GetSecretValue on {SecretId}"}},
                "GetSecretValue")
        return self._client.get_secret_value(SecretId=SecretId)


class _Bus:
    """Hands each scan bus event to the handler whose rule matches it."""

    def publish(self, _client, service, detail_type, detail):
        event = {"source": scan_events.source("test", service), "detail-type": detail_type,
                 "detail": json.loads(json.dumps(detail)), "account": "123456789012", "region": "us-east-1"}
        handler = scanner_handler.handler if detail_type == scan_events.SCAN_REQUESTED \
            else disabled_version_handler.handler if detail_type == scan_events.REGISTRATION_DISABLED \
            else schema_ingestion_handler.handler
        handler(event, None)


@pytest.fixture
def api(registry, scan_targets_table, postgres, monkeypatch, moto_endpoint):
    index = _Index()
    monkeypatch.setattr(schema_ingestion_handler, "provider_from_env", lambda: index)
    providers = f"scan-providers-{uuid.uuid4().hex[:8]}"
    boto3.client("dynamodb").create_table(
        TableName=providers, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "engine", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "engine", "KeyType": "HASH"}])
    bucket = f"scan-results-{uuid.uuid4().hex[:8]}"
    for name, value in {"STACK_NAME": "test", "SCAN_SCHEDULE_GROUP": GROUP,
                        "SCAN_SCHEDULER_ROLE_ARN": ROLE_ARN, "SCHEMA_INGESTION_FUNCTION_ARN": INGESTION_ARN,
                        "SCAN_TIMEOUT_SECONDS": "900", "SCAN_PROVIDERS_TABLE": providers,
                        "SCAN_BUS_NAME": "bus", "ENGINE": "postgres", "SCAN_RESULTS_BUCKET": bucket,
                        "SCANNER_ROLE_NAME": "test-scanner-role",
                        # As the stack sets it; the index is emulated, so counts are unknown unless a test makes one.
                        "SCHEMA_INDEX_DATABASE": "no_index_here", "AWS_ENDPOINT_URL_GLUE": moto_endpoint,
                        "AWS_ENDPOINT_URL_SCHEDULER": moto_endpoint, "AWS_ENDPOINT_URL_S3": moto_endpoint}.items():
        monkeypatch.setenv(name, value)
    boto3.client("s3").create_bucket(Bucket=bucket)
    for engine, arn in SCANNERS.items():
        scan_providers.register(engine, arn, bucket, account="123456789012", region="us-east-1")
    monkeypatch.setattr(admin, "validate_target", validator_allowing_the_containers(REAL_VALIDATE_TARGET))
    monkeypatch.setattr(admin, "_scheduler", None)
    monkeypatch.setattr(SecretsManagerPasswordProvider, "_secrets",
                        lambda self: _ScannerRoleSecrets(boto3.client("secretsmanager")))
    monkeypatch.setattr(scan_events, "publish", _Bus().publish)
    scheduler = boto3.client("scheduler")
    scheduler.create_schedule_group(Name=GROUP)
    monkeypatch.setattr(provider_client, "_function_names", {})
    monkeypatch.setattr(admin, "_lambda_client", LambdaRouter({
        INGESTION_ARN: schema_ingestion_handler.handler}))
    # Each engine's scanner is its own function, with its own ENGINE.
    monkeypatch.setattr(provider_client, "_lambda_client", LambdaRouter(
        dict.fromkeys(SCANNERS.values(), scanner_handler.handler),
        environments={arn: {"ENGINE": engine} for engine, arn in SCANNERS.items()}))
    yield index, postgres
    for page in scheduler.get_paginator("list_schedules").paginate(GroupName=GROUP):
        for schedule in page["Schedules"]:
            scheduler.delete_schedule(Name=schedule["Name"], GroupName=GROUP)
    scheduler.delete_schedule_group(Name=GROUP)
    boto3.client("dynamodb").delete_table(TableName=providers)


def _secrets():
    return boto3.client("secretsmanager")


def _byo_secret(postgres, password=None, path=SCANNER_PATH) -> str:
    """A scanner login an administrator's own process made, under the scanner path as the
    admin page says, unless `path` puts it elsewhere."""
    return _secrets().create_secret(
        Name=f"{path}dba-{uuid.uuid4()}",
        SecretString=json.dumps({"username": seed.OWNER,
                                 "password": password or postgres.owner_password}))["ARN"]


def _scanner_logins() -> set[str]:
    return {s["ARN"] for page in _secrets().get_paginator("list_secrets").paginate()
            for s in page["SecretList"] if s["Name"].startswith(SCANNER_PATH)}


def _exists(arn) -> bool:
    try:
        _secrets().describe_secret(SecretId=arn)
        return True
    except _secrets().exceptions.ResourceNotFoundException:
        return False


def _value(arn) -> dict:
    return json.loads(_secrets().get_secret_value(SecretId=arn)["SecretString"])


def _location(postgres, **overrides):
    return {"engine": "postgres", "host": postgres.host, "port": str(postgres.port),
            "database": postgres.database, **overrides}


def _identity(postgres, host=None):
    return f"postgres://{(host or postgres.host).lower()}:{postgres.port}/{postgres.database}"


def _targets():
    return scan_targets.read_all()


def _offered():
    """The registered databases as the schema service serves them to other services."""
    return schema_module.handler({"operation": "list_databases", "args": {}}, None)["response"]


def _offered_one(target_id):
    return schema_module.handler({"operation": "get_database", "args": {"targetId": target_id}}, None)["response"]


def _schedule(target_id):
    try:
        return boto3.client("scheduler").get_schedule(Name=f"scan-{target_id}", GroupName=GROUP)
    except boto3.client("scheduler").exceptions.ResourceNotFoundException:
        return None


def _add(body):
    # Every registration needs a name; tests about something else get a unique one.
    if isinstance(body, dict) and "name" not in body:
        body = {"name": f"db-{uuid.uuid4().hex[:8]}", **body}
    if isinstance(body, dict) and "statementTimeoutSeconds" not in body and "rowCap" not in body:
        body = {"statementTimeoutSeconds": 30, "rowCap": 1000, **body}
    return admin._add_scan_target(AS_ADMIN, {}, {}, body)


def _update(target_id, body):
    return admin._update_scan_target(AS_ADMIN, {"targetId": target_id}, {}, body)


def _disable(target_id):
    return admin._disable_scan_target(AS_ADMIN, {"targetId": target_id}, {}, None)


def _new_version(target_id, body):
    return admin._add_version(AS_ADMIN, {"targetId": target_id}, {}, body)


def _list():
    return _body(admin._list_scan_targets(AS_ADMIN, {}, {}, None))["targets"]


def _spy(monkeypatch) -> list[tuple[str, dict]]:
    """Every invocation the admin function makes, in order: (InvocationType, payload),
    whether of the ingestion function or of a scanner."""
    sent = []

    class _Spy:
        def __init__(self, real):
            self.real = real

        def invoke(self, **kwargs):
            sent.append((kwargs.get("InvocationType", "RequestResponse"), json.loads(kwargs["Payload"])))
            return self.real.invoke(**kwargs)
    monkeypatch.setattr(admin, "_lambda_client", _Spy(admin._get_lambda()))
    monkeypatch.setattr(provider_client, "_lambda_client", _Spy(provider_client._lambda_client))
    return sent


def _registered(postgres, **extra) -> dict:
    """Register the compose Postgres with a brought scanner login; return the listed target."""
    response = _add({**_location(postgres), "scannerSecretArn": _byo_secret(postgres), **extra})
    assert response["statusCode"] == 201, _body(response)
    return _body(response)


ROUTES = [
    (admin._list_scan_targets, {}, None),
    (admin._add_scan_target, {}, {"engine": "postgres", "host": "h", "port": "5432", "database": "d",
                                  "scannerSecretArn": "arn:aws:secretsmanager:us-east-1:1:secret:s"}),
    (admin._update_scan_target, {"targetId": "t"}, {"schedule": "rate(1 day)"}),
    (admin._add_version, {"targetId": "t"},
     {"host": "h", "scannerSecretArn": "arn:aws:secretsmanager:us-east-1:1:secret:s"}),
    (admin._disable_scan_target, {"targetId": "t"}, None),
    (admin._start_scan, {}, {"targetId": "t"}),
    (admin._admin_best_practices, {"engine": "postgres"}, None),
]


class TestOnlyAdminsMayCallAdminRoutes:

    @pytest.mark.parametrize("groups", [None, "[]", "[ops]", "[administrators]"])
    @pytest.mark.parametrize("route,path,body", ROUTES, ids=lambda v: getattr(v, "__name__", "args"))
    def test_everyone_else_is_forbidden_and_nothing_changes(self, api, route, path, body, groups):
        from shared.http_response import ForbiddenError
        before = _targets()
        with pytest.raises(ForbiddenError):
            route(_event(USER, groups), path, {}, body)
        assert _targets() == before

    def test_the_router_turns_it_into_a_403(self, api):
        from shared.http_response import route_request
        response = route_request(
            {"routeKey": "GET /api/admin/scan-targets", **_event(USER, "[ops]")},
            admin.ROUTES)
        assert response["statusCode"] == 403


class TestRegisteringWithAnExistingSecret:
    """The DBA's own secret, by ARN, created under the scanner path: the list says where,
    and saving checks the scanner can read it and log in."""

    def test_the_list_says_where_a_brought_secret_goes_and_which_engines_are_registered(self, api):
        listed = _body(admin._list_scan_targets(AS_ADMIN, {}, {}, None))
        assert listed["scannerSecretPath"] == SCANNER_PATH
        assert listed["engines"] == ["oracle", "postgres"]

    def test_a_secret_under_the_path_is_kept_with_the_target_and_left_alone(self, api):
        _, postgres = api
        arn = _byo_secret(postgres)
        before = _value(arn)
        response = _add({**_location(postgres), "scannerSecretArn": arn})
        assert response["statusCode"] == 201, _body(response)
        assert _value(arn) == before
        [listed] = _list()
        assert (listed["identity"], listed["scannerSecretArn"], listed["scannerSecretManaged"]) == \
            (_identity(postgres), arn, False)
        assert listed["targetId"] == _body(response)["targetId"]

    @pytest.mark.parametrize("path", ["/dba/", "/test/connections/u/", "/other/scanners/", "/test/scannersX/"])
    def test_a_secret_anywhere_else_is_refused_naming_the_path_and_nothing_is_stored(self, api, path):
        _, postgres = api
        arn = _byo_secret(postgres, path=path)
        response = _add({**_location(postgres), "scannerSecretArn": arn})
        assert response["statusCode"] == 400
        assert f"must be created under {SCANNER_PATH}" in _body(response)["error"]
        assert _targets() == []

    def test_a_secret_under_a_key_the_scanner_may_not_use_is_refused_naming_its_role(self, api, monkeypatch):
        _, postgres = api

        def kms_refuses(self, SecretId):
            raise botocore.exceptions.ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "Access to KMS is not allowed"}},
                "GetSecretValue")
        monkeypatch.setattr(_ScannerRoleSecrets, "get_secret_value", kms_refuses)
        response = _add({**_location(postgres), "scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 400
        assert "kms:Decrypt" in _body(response)["error"] and "test-scanner-role" in _body(response)["error"]
        assert _targets() == []

    def test_a_secret_that_does_not_exist_is_refused_and_nothing_is_stored(self, api):
        _, postgres = api
        response = _add({**_location(postgres), "scannerSecretArn":
                         f"arn:aws:secretsmanager:us-east-1:123456789012:secret:{SCANNER_PATH}none-AbCdEf"})
        assert response["statusCode"] == 400 and "No scanner login with that ARN" in _body(response)["error"]
        assert _targets() == []

    def test_a_users_connection_secret_is_refused(self, api, registry):
        _, postgres = api
        # A user's login, to a database no one registered: only its secret matters here.
        cid = registry.add("someone", "postgres", seed.OWNER, postgres.owner_password, targetId="not-registered")
        arn = _secrets().describe_secret(SecretId=registry.row("someone", cid)["secretName"])["ARN"]
        response = _add({**_location(postgres), "scannerSecretArn": arn})
        assert response["statusCode"] == 400 and f"must be created under {SCANNER_PATH}" in _body(response)["error"]
        assert _targets() == []

    def test_a_secret_the_scanner_cannot_log_in_with_is_refused(self, api):
        _, postgres = api
        arn = _byo_secret(postgres, password=WRONG_PASSWORD)
        response = _add({**_location(postgres), "scannerSecretArn": arn})
        assert response["statusCode"] == 400 and "could not log in" in _body(response)["error"]
        assert _targets() == []
        assert _exists(arn)

    @pytest.mark.parametrize("arn", ["not-an-arn", "arn:aws:s3:::bucket",
                                     "arn:aws:secretsmanager:us-east-1:123456789012:secret:",
                                     "arn:aws:ssm:us-east-1:123456789012:secret:/test/scanners/x"])
    def test_a_target_needs_a_real_secret_arn(self, api, arn):
        _, postgres = api
        response = _add({**_location(postgres), "scannerSecretArn": arn})
        assert _body(response)["error"] == "Give the ARN of the scanner secret (a Secrets Manager secret ARN)."
        assert _targets() == []

    def test_a_target_needs_a_secret_of_some_kind(self, api):
        _, postgres = api
        response = _add(_location(postgres))
        assert response["statusCode"] == 400 and "secret" in _body(response)["error"].lower()
        assert _targets() == []

    def test_a_database_registered_twice_is_refused_and_pointed_at_a_new_version(self, api):
        _, postgres = api
        _registered(postgres)
        response = _add({**_location(postgres, host=postgres.host.upper()),
                         "scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 400 and "new version" in _body(response)["error"]
        assert len(_targets()) == 1


class TestRegisteringWithCredentials:
    """The admin types the scanner's username and password; the scanner stores the login."""

    def _credentials(self, postgres, password=None):
        return {**_location(postgres), "scannerUsername": seed.OWNER,
                "scannerPassword": password or postgres.owner_password}

    def test_the_login_is_stored_under_the_scanner_path(self, api):
        _, postgres = api
        response = _add(self._credentials(postgres))
        assert response["statusCode"] == 201
        [target] = _targets()
        arn = target["scannerSecretArn"]
        assert target["scannerSecretManaged"] is True
        assert _secrets().describe_secret(SecretId=arn)["Name"].startswith(SCANNER_PATH)
        assert _value(arn) == {"username": seed.OWNER, "password": postgres.owner_password}

    def test_the_password_is_never_returned_stored_in_the_list_or_logged(self, api, caplog):
        _, postgres = api
        # INFO: the level both functions log at (botocore's DEBUG echoes every request body).
        caplog.set_level(logging.INFO)
        added = _add(self._credentials(postgres))
        listed = admin._list_scan_targets(AS_ADMIN, {}, {}, None)
        for text in (added["body"], listed["body"], json.dumps(_targets()), caplog.text):
            assert postgres.owner_password not in text

    def test_a_login_that_fails_stores_nothing_and_deletes_the_secret_it_made(self, api):
        _, postgres = api
        before = _scanner_logins()
        response = _add(self._credentials(postgres, password=WRONG_PASSWORD))
        assert response["statusCode"] == 400 and "could not log in" in _body(response)["error"]
        assert _targets() == []
        assert _scanner_logins() == before

    @pytest.mark.parametrize("given", [{"scannerUsername": "scanner"}, {"scannerPassword": "pw"}])
    def test_half_a_credential_is_refused(self, api, given):
        _, postgres = api
        response = _add({**_location(postgres), **given})
        assert response["statusCode"] == 400 and "both" in _body(response)["error"]
        assert _targets() == []

    def test_an_arn_and_credentials_together_are_refused(self, api):
        _, postgres = api
        response = _add({**self._credentials(postgres), "scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 400 and "not both" in _body(response)["error"]
        assert _targets() == []


class TestTheConnectionIsKeptAsTyped:

    def test_the_identity_is_the_one_a_users_connection_to_it_has(self, api, registry):
        """The index is keyed on it, and search matches it to each caller's connections, so
        a user's connection to the same database must have the same identity, however the
        host was typed."""
        from adam.data_source_provider.connection_opening import registered
        from adam.data_source_provider.connection_registry import get_connection
        from adam.schema_provider.source_identity import source_identity

        _, postgres = api
        _add({**_location(postgres, host=postgres.host.upper()), "scannerSecretArn": _byo_secret(postgres)})
        (target,) = _targets()
        connection = registry.add("someone", "postgres", seed.ANALYST, postgres.analyst_password,
                                  host=postgres.host, port=postgres.port, database=postgres.database)
        # As a provider opens the user's connection: the same database, however its host was typed.
        assert target["identity"] == source_identity(registered(get_connection("someone", connection)))
        assert target["host"] == postgres.host.upper()  # kept as typed

    def test_a_target_missing_a_field_is_rejected(self, api):
        _, postgres = api
        location = _location(postgres)
        del location["database"]
        response = _add({**location, "scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 400
        assert _targets() == []

    BLOCKED = "'169.254.169.254' is not an address a database can be registered at."

    def test_a_host_the_function_must_not_dial_is_rejected(self, api, monkeypatch):
        _, postgres = api
        sent = _spy(monkeypatch)
        response = _add({**_location(postgres, host="169.254.169.254"), "scannerSecretArn": _byo_secret(postgres)})
        assert json.loads(response["body"])["error"] == self.BLOCKED
        assert _targets() == [] and sent == []  # no scanner was asked to dial it

    def test_a_new_version_cannot_be_at_such_a_host(self, api, monkeypatch):
        _, postgres = api
        target = _registered(postgres)
        before = _targets()
        sent = _spy(monkeypatch)
        response = _new_version(target["targetId"], {"host": "169.254.169.254",
                                                     "scannerSecretArn": _byo_secret(postgres)})
        assert json.loads(response["body"])["error"] == self.BLOCKED
        assert _targets() == before and sent == []


class TestEditing:

    def test_changing_only_the_schedule_needs_no_secret(self, api):
        _, postgres = api
        target = _registered(postgres)
        response = _update(target["targetId"], {"schedule": "rate(6 hours)"})
        assert response["statusCode"] == 200, _body(response)
        assert _schedule(target["targetId"])["ScheduleExpression"] == "rate(6 hours)"
        assert _targets()[0]["scannerSecretArn"] == target["scannerSecretArn"]

    @pytest.mark.parametrize("change", [
        {"host": "127.0.0.1"}, {"host": "127.0.0.1", "scannerSecretArn": "SECRET"}, {"port": "1"},
        {"database": "other"}, {"Host": "127.0.0.1"}, {"tls": False}, {"identity": "postgres://evil:1/x"},
        {"name": "Renamed", "host": "127.0.0.1"}])
    def test_an_edit_that_would_change_where_it_is_is_refused_and_changes_nothing(self, api, monkeypatch, change):
        """Users' passwords for this version go where it is; only a new version can be elsewhere."""
        _, postgres = api
        target = _registered(postgres)
        change = {k: (_byo_secret(postgres) if v == "SECRET" else v) for k, v in change.items()}
        before = _targets()
        sent = _spy(monkeypatch)
        response = _update(target["targetId"], change)
        assert response["statusCode"] == 400 and "publish a new version" in _body(response)["error"]
        assert _targets() == before and sent == []

    def test_an_edit_that_does_not_move_it_rescans_it_and_purges_nothing(self, api):
        index, postgres = api
        target = _registered(postgres)
        index.published.clear()
        _update(target["targetId"], {"scannerSecretArn": _byo_secret(postgres)})
        assert index.published and all(identity == _identity(postgres) and tables
                                       for identity, _, tables in index.published)

    def test_blank_credentials_keep_the_stored_secret(self, api):
        _, postgres = api
        _add({**_location(postgres), "scannerUsername": seed.OWNER, "scannerPassword": postgres.owner_password})
        [before] = _targets()
        response = _update(before["targetId"], {"scannerUsername": "", "scannerPassword": "", "schedule": None})
        assert response["statusCode"] == 200
        assert _targets() == [before]
        assert _exists(before["scannerSecretArn"])

    def test_new_credentials_replace_the_secret_the_stack_made(self, api):
        _, postgres = api
        _add({**_location(postgres), "scannerUsername": seed.OWNER, "scannerPassword": postgres.owner_password})
        [before] = _targets()
        _update(before["targetId"], {"scannerUsername": seed.OWNER, "scannerPassword": postgres.owner_password})
        [after] = _targets()
        assert after["scannerSecretArn"] != before["scannerSecretArn"] and after["scannerSecretManaged"]
        assert not _exists(before["scannerSecretArn"])

    def test_switching_to_an_existing_secret_deletes_the_one_the_stack_made(self, api):
        _, postgres = api
        _add({**_location(postgres), "scannerUsername": seed.OWNER, "scannerPassword": postgres.owner_password})
        [before] = _targets()
        arn = _byo_secret(postgres)
        _update(before["targetId"], {"scannerSecretArn": arn})
        assert _targets()[0]["scannerSecretArn"] == arn and not _targets()[0]["scannerSecretManaged"]
        assert not _exists(before["scannerSecretArn"])

    def test_switching_away_from_a_brought_secret_leaves_it_alone(self, api):
        _, postgres = api
        target = _registered(postgres)
        _update(target["targetId"], {"scannerUsername": seed.OWNER, "scannerPassword": postgres.owner_password})
        assert _value(target["scannerSecretArn"]) == {"username": seed.OWNER, "password": postgres.owner_password}

    def test_a_failed_login_on_edit_restores_the_target(self, api):
        _, postgres = api
        target = _registered(postgres)
        before = _targets()
        bad = _byo_secret(postgres, password=WRONG_PASSWORD)
        response = _update(target["targetId"], {"scannerSecretArn": bad})
        assert response["statusCode"] == 400 and "could not log in" in _body(response)["error"]
        assert _targets() == before

    def test_switching_to_a_secret_outside_the_path_is_refused(self, api):
        _, postgres = api
        target = _registered(postgres)
        before = _targets()
        response = _update(target["targetId"], {"scannerSecretArn": _byo_secret(postgres, path="/dba/")})
        assert response["statusCode"] == 400 and SCANNER_PATH in _body(response)["error"]
        assert _targets() == before

    def test_switching_to_another_targets_secret_is_refused(self, api):
        """It would be deleted with the other target if the stack made it."""
        _, postgres = api
        first = _registered(postgres, host="localhost")
        second = _registered(postgres, host="127.0.0.1")
        response = _update(second["targetId"], {"scannerSecretArn": first["scannerSecretArn"]})
        assert response["statusCode"] == 400 and "Another target uses" in _body(response)["error"]

    def test_the_engine_cannot_be_changed(self, api):
        _, postgres = api
        target = _registered(postgres)
        response = _update(target["targetId"], {"engine": "oracle"})
        assert response["statusCode"] == 400 and "engine" in _body(response)["error"]

    def test_a_new_version_cannot_be_where_another_database_is(self, api):
        _, postgres = api
        first = _registered(postgres, host="localhost")
        _registered(postgres, host="127.0.0.1")
        response = _new_version(first["targetId"], {"host": "127.0.0.1", "scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 400 and "another database" in _body(response)["error"]

    def test_an_unknown_target_is_refused(self, api):
        response = _update("no-such-target", {"schedule": "rate(1 day)"})
        assert response["statusCode"] == 400 and "not a registered" in _body(response)["error"]


class TestSchedules:

    def test_a_target_is_on_demand_unless_given_a_schedule(self, api):
        _, postgres = api
        target = _registered(postgres)
        assert _schedule(target["targetId"]) is None
        assert _list()[0]["schedule"] is None

    def test_a_schedule_runs_the_ingestion_function_as_the_scheduler_role_for_that_target(self, api):
        _, postgres = api
        target = _registered(postgres, schedule="cron(0 2 * * ? *)")
        schedule = _schedule(target["targetId"])
        assert schedule["ScheduleExpression"] == "cron(0 2 * * ? *)"
        assert schedule["Target"]["Arn"] == INGESTION_ARN
        assert schedule["Target"]["RoleArn"] == ROLE_ARN
        assert json.loads(schedule["Target"]["Input"]) == {"targetId": target["targetId"]}
        assert _list()[0]["schedule"] == "cron(0 2 * * ? *)"

    def test_what_the_schedule_sends_scans_the_target(self, api):
        index, postgres = api
        target = _registered(postgres, schedule="rate(1 day)")
        index.published.clear()
        event = json.loads(_schedule(target["targetId"])["Target"]["Input"])
        schema_ingestion_handler.handler(event, None)
        [(identity, _, tables)] = index.published
        assert identity == _identity(postgres) and len(tables) == 4

    @pytest.mark.parametrize("expression", ["daily", "rate(0 days)", "rate(1 fortnight)", "cron(0 2 * *)",
                                            "at(2026-01-01T00:00:00)", 7])
    def test_something_that_is_not_a_rate_or_cron_is_refused_before_anything_is_stored(self, api, expression):
        _, postgres = api
        arn = _byo_secret(postgres)
        response = _add({**_location(postgres), "scannerSecretArn": arn, "schedule": expression})
        assert response["statusCode"] == 400 and "schedule" in _body(response)["error"]
        assert _targets() == []

    def test_schedulers_own_refusal_is_shown_and_the_registration_undone(self, api, monkeypatch):
        from botocore.stub import Stubber
        _, postgres = api
        client = boto3.client("scheduler")
        stub = Stubber(client)
        stub.add_client_error("create_schedule", service_error_code="ValidationException",
                              service_message="Invalid Schedule Expression cron(0 2 31 2 ? *).")
        stub.activate()
        monkeypatch.setattr(admin, "_scheduler", client)
        arn = _byo_secret(postgres)
        response = _add({**_location(postgres), "scannerSecretArn": arn, "schedule": "cron(0 2 31 2 ? *)"})
        assert response["statusCode"] == 400
        assert "Invalid Schedule Expression cron(0 2 31 2 ? *)." in _body(response)["error"]
        assert _targets() == []

    def test_clearing_the_schedule_deletes_it(self, api):
        _, postgres = api
        target = _registered(postgres, schedule="rate(1 day)")
        _update(target["targetId"], {"schedule": None})
        assert _schedule(target["targetId"]) is None

    def test_changing_the_schedule_updates_it(self, api):
        _, postgres = api
        target = _registered(postgres, schedule="rate(1 day)")
        _update(target["targetId"], {"schedule": "rate(12 hours)"})
        assert _schedule(target["targetId"])["ScheduleExpression"] == "rate(12 hours)"


class TestDisabling:
    """The version is refused to users at once and kept; then its schedule, the secret the
    stack made, its users' saved logins and its index entries go."""

    def test_it_is_kept_disabled_and_its_schedule_and_index_entries_go(self, api):
        index, postgres = api
        target = _registered(postgres, schedule="rate(1 day)")
        admin._start_scan(AS_ADMIN, {}, {}, {"targetId": target["targetId"]})
        response = _disable(target["targetId"])
        assert response["statusCode"] == 200, _body(response)
        [stored] = _targets()
        assert stored["disabled"] is True and stored["disabledBy"] == f"{ADMIN}-name"
        assert stored["disabledBySub"] == ADMIN and stored["disabledAt"]
        assert _schedule(target["targetId"]) is None
        assert index.published[-1] == (_identity(postgres), "postgres", [])
        assert _body(response)["indexEntriesRemoved"] == 4
        assert _offered() == []

    def test_its_users_saved_logins_are_deleted(self, api, registry):
        _, postgres = api
        target = _registered(postgres)
        cid = registry.add("someone", "postgres", seed.ANALYST, postgres.analyst_password,
                           targetId=target["targetId"])
        secret = registry.row("someone", cid)["secretName"]
        _disable(target["targetId"])
        assert "secretName" not in registry.row("someone", cid)
        assert not _exists(secret)

    def test_index_entries_another_version_at_the_same_place_uses_are_kept(self, api):
        index, postgres = api
        target = _registered(postgres)
        second = _body(_new_version(target["targetId"], {"scannerSecretArn": _byo_secret(postgres)}))
        index.published.clear()
        response = _disable(target["targetId"])
        assert _body(response)["sharedWithAnotherVersion"] is True and index.published == []
        assert _offered_one(second["targetId"])["identity"] == _identity(postgres)

    def test_a_brought_secret_is_left_exactly_as_it_was(self, api):
        _, postgres = api
        target = _registered(postgres)
        _disable(target["targetId"])
        assert _value(target["scannerSecretArn"]) == {"username": seed.OWNER, "password": postgres.owner_password}

    def test_it_deletes_a_secret_the_stack_made(self, api):
        _, postgres = api
        _add({**_location(postgres), "scannerUsername": seed.OWNER, "scannerPassword": postgres.owner_password})
        [target] = _targets()
        _disable(target["targetId"])
        assert not _exists(target["scannerSecretArn"])

    def test_a_disabling_that_fails_part_way_has_already_refused_it_and_can_be_run_again(self, api, monkeypatch):
        index, postgres = api
        _add({**_location(postgres), "scannerUsername": seed.OWNER, "scannerPassword": postgres.owner_password,
              "schedule": "rate(1 day)"})
        [target] = _targets()

        def index_unavailable(**_):
            raise RuntimeError("Glue is unavailable")
        with monkeypatch.context() as m:
            m.setattr(index, "publish_objects", index_unavailable)
            with pytest.raises(provider_client.ProviderError):
                _disable(target["targetId"])
        [left] = _targets()
        assert left["disabled"] is True and _list()[0]["disabled"] is True
        assert _offered() == []
        assert _schedule(target["targetId"]) is None and not _exists(target["scannerSecretArn"])
        assert "is disabled" in _body(admin._start_scan(AS_ADMIN, {}, {}, {"targetId": target["targetId"]}))["error"]
        with pytest.raises(schema_ingestion_handler.ScanRefused, match="is disabled"):
            schema_ingestion_handler.handler({"targetId": target["targetId"]}, None)

        again = _disable(target["targetId"])
        assert again["statusCode"] == 200, _body(again)
        [still] = _targets()
        assert still["disabled"] is True and still["disabledAt"] == left["disabledAt"]

    def test_a_disabled_version_cannot_be_edited(self, api):
        _, postgres = api
        target = _registered(postgres)
        _disable(target["targetId"])
        response = _update(target["targetId"], {"schedule": "rate(1 day)"})
        assert response["statusCode"] == 400 and "is disabled" in _body(response)["error"]


class TestScanTargetsAreNeverAUsersRows:
    """They are the schema service's own table; a user's list holds only their own rows."""

    def test_no_users_connection_list_includes_them(self, api, registry, monkeypatch):
        from adam.data_source_provider import (
            connection_registry,
            connections_api_handler as connections,
        )
        from adam.shared import provider_client as package_provider_client
        postgres = api[1]
        _registered(postgres)
        boto3.client("ssm").put_parameter(Name="/test/schema_provider_function_name", Value="schema-fn",
                                          Type="String", Overwrite=True)
        monkeypatch.setattr(package_provider_client, "_function_names", {})
        monkeypatch.setattr(package_provider_client, "_lambda_client",
                            LambdaRouter({"schema-fn": schema_module.handler}))
        registry.add(USER, "postgres", seed.OWNER, postgres.owner_password,
                     host=postgres.host, port=postgres.port, database=postgres.database)
        listed = _body(connections._list_connections(_event(USER, None), {}, {}, None))["connections"]
        assert len(listed) == 1 and "scannerSecretArn" not in json.dumps(listed)
        assert len(connection_registry.list_user_connections(USER)) == 1


class TestEachTargetShowsHowManyTablesAreIndexed:
    """So an administrator can see whether a registered database has been scanned yet."""

    INDEX = "test_schema_index"

    @pytest.fixture
    def glue(self, api, monkeypatch):
        import os
        monkeypatch.setenv("AWS_ENDPOINT_URL_GLUE", os.environ["AWS_ENDPOINT_URL_SSM"])  # the moto server
        monkeypatch.setenv("SCHEMA_INDEX_DATABASE", self.INDEX)
        monkeypatch.setattr(admin, "_glue", None)
        client = boto3.client("glue")
        try:
            client.delete_database(Name=self.INDEX)
        except client.exceptions.EntityNotFoundException:
            pass
        client.create_database(DatabaseInput={"Name": self.INDEX})

        def index(identity, names):
            for n in names:
                client.create_table(DatabaseName=self.INDEX, TableInput={
                    "Name": f"{abs(hash(identity)) % 10**6}__{n}",
                    "Parameters": {"source_database": identity, "source_engine": identity.split(":")[0]}})
        yield index
        client.delete_database(Name=self.INDEX)

    def _counts(self):
        return {t["identity"]: t["tableCount"] for t in _list()}

    def test_a_target_that_was_never_scanned_shows_zero(self, api, glue):
        _, postgres = api
        _registered(postgres)
        assert self._counts() == {_identity(postgres): 0}

    def test_it_counts_that_databases_tables_and_no_others(self, api, glue):
        _, postgres = api
        _registered(postgres, host="localhost")
        _registered(postgres, host="127.0.0.1")
        glue(_identity(postgres, "localhost"), ["a", "b", "c"])
        glue(_identity(postgres, "127.0.0.1"), ["x"])
        glue("postgres://unregistered:5432/d", ["y", "z"])
        assert self._counts() == {_identity(postgres, "localhost"): 3, _identity(postgres, "127.0.0.1"): 1}

    def test_a_lookup_that_fails_is_unknown_not_zero(self, api, glue, monkeypatch):
        _, postgres = api
        _registered(postgres)
        monkeypatch.setenv("SCHEMA_INDEX_DATABASE", "no_such_index")
        assert self._counts() == {_identity(postgres): None}


class TestScans:
    """A scan names a registered target and nothing else: the login check and the scan
    take its location and scanner login from the stored target, so a caller cannot
    point a scan at a host or secret of their choosing."""

    def test_an_admin_scans_a_registered_target_by_its_id_alone(self, api):
        index, postgres = api
        target = _registered(postgres)
        index.published.clear()
        response = admin._start_scan(AS_ADMIN, {}, {}, {"targetId": target["targetId"]})
        # Accepted at once: the scan runs in the background.
        assert response["statusCode"] == 202
        assert _body(response)["scanStatus"] == "queued"
        assert index.published[-1][0] == _identity(postgres)
        [stored] = _targets()
        assert (stored["scanStatus"], stored["scanMessage"]) == ("scanned", "Scanned 4 object(s).")

    def test_a_failed_scan_is_shown_on_the_target_with_its_reason(self, api, monkeypatch):
        _, postgres = api
        target = _registered(postgres)

        def refused(self):
            raise RuntimeError("permission denied for schema wealth")
        from adam.data_source_provider.postgresql_provider import PostgreSQLProvider
        monkeypatch.setattr(PostgreSQLProvider, "scan_schema", refused)
        admin._start_scan(AS_ADMIN, {}, {}, {"targetId": target["targetId"]})
        [stored] = _targets()
        assert (stored["scanStatus"], stored["scanMessage"]) == ("failed", "permission denied for schema wealth")
        listed = _body(admin._list_scan_targets(AS_ADMIN, {}, {}, None))["targets"][0]
        assert listed["scanStatus"] == "failed" and "permission denied" in listed["scanMessage"]

    def test_a_scan_the_timeout_ended_is_listed_as_failed(self, api):
        _, postgres = api
        _registered(postgres)
        stale = {"scanStatus": "scanning", "scanMessage": "Scanning.", "scanUpdatedAt": "2026-01-01T00:00:00+00:00"}
        scan_targets._put({**_targets()[0], **stale})
        listed = _body(admin._list_scan_targets(AS_ADMIN, {}, {}, None))["targets"][0]
        assert listed["scanStatus"] == "failed" and "did not finish within 15 minutes" in listed["scanMessage"]

    def test_nothing_in_the_request_but_the_target_id_reaches_the_scan(self, api, monkeypatch):
        _, postgres = api
        target = _registered(postgres)
        sent = _spy(monkeypatch)
        admin._start_scan(AS_ADMIN, {}, {}, {"targetId": target["targetId"],
                                             "scannerSecretArn": "arn:aws:secretsmanager:x:1:secret:evil",
                                             "location": {"host": "evil"}, "host": "evil"})
        # The login check, from the stored target, then the scan, named by its id alone.
        (check, scan) = [payload for _, payload in sent]
        assert check == {"operation": "probe", "args": {
            "location": {f: target[f] for f in ("host", "port", "database")},
            "scannerSecretArn": target["scannerSecretArn"]}}
        assert scan == {"targetId": target["targetId"]}

    def test_an_unregistered_target_is_refused(self, api):
        index, _ = api
        response = admin._start_scan(AS_ADMIN, {}, {}, {"targetId": "no-such-target"})
        assert response["statusCode"] == 400
        assert "not a registered scan target" in _body(response)["error"]
        assert index.published == []

    def test_a_disabled_version_is_not_scanned(self, api):
        index, postgres = api
        target = _registered(postgres)
        scan_targets.disable(target["targetId"], by="a", by_sub="s", at="2026-10-26T00:00:00+00:00")
        index.published.clear()
        response = admin._start_scan(AS_ADMIN, {}, {}, {"targetId": target["targetId"]})
        assert response["statusCode"] == 400 and "is disabled" in _body(response)["error"]
        assert index.published == []

    def test_the_scan_is_started_asynchronously_after_a_synchronous_login_check(self, api, monkeypatch):
        _, postgres = api
        target = _registered(postgres)
        sent = _spy(monkeypatch)
        admin._start_scan(AS_ADMIN, {}, {}, {"targetId": target["targetId"]})
        assert [kind for kind, _ in sent] == ["RequestResponse", "Event"]


class TestDbaGuidance:
    """Each engine's scanner says how to set up its accounts; the admin page shows it."""

    @pytest.fixture
    def providers(self, api):
        return api

    @pytest.mark.parametrize("engine,provider", [
        ("oracle", "adam.data_source_provider.oracle_provider:OracleProvider"),
        ("postgres", "adam.data_source_provider.postgresql_provider:PostgreSQLProvider")])
    def test_it_is_that_engines_own_text(self, providers, engine, provider):
        import importlib
        module, name = provider.split(":")
        expected = getattr(importlib.import_module(module), name).ADMIN_BEST_PRACTICES
        response = admin._admin_best_practices(AS_ADMIN, {"engine": engine.upper()}, {}, None)
        assert response["statusCode"] == 200
        assert _body(response) == {"engine": engine, "guidance": expected}

    def test_an_unregistered_engine_is_refused(self, providers):
        response = admin._admin_best_practices(AS_ADMIN, {"engine": "mysql"}, {}, None)
        assert response["statusCode"] == 400 and "No mysql database provider" in _body(response)["error"]


class TestNames:
    """Users choose a database by the name an administrator gives it."""

    def test_a_name_is_required(self, api):
        _, postgres = api
        response = _add({**_location(postgres), "name": " ", "scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 400 and "name" in _body(response)["error"]
        assert _targets() == []

    def test_two_databases_cannot_share_a_name_ignoring_case(self, api):
        _, postgres = api
        _registered(postgres, name="Wealth")
        response = _add({**_location(postgres, host="127.0.0.1"), "name": "wealth",
                         "scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 400 and "already named" in _body(response)["error"]

    def test_registering_lists_it_for_users_with_where_it_is_and_no_secret(self, api):
        _, postgres = api
        target = _registered(postgres, name="Wealth")
        [entry] = _offered()
        assert entry == {"targetId": target["targetId"], "lineageId": target["targetId"], "version": 1,
                         "latest": True, "name": "Wealth", "engine": "postgres",
                         "host": postgres.host, "port": str(postgres.port), "database": postgres.database,
                         "identity": f"postgres://{postgres.host}:{postgres.port}/{postgres.database}".lower(),
                         "statementTimeoutSeconds": 30, "rowCap": 1000}

    def test_a_rename_updates_what_users_see_without_a_new_login_or_scan(self, api, monkeypatch):
        index, postgres = api
        target = _registered(postgres, name="Wealth")
        index.published.clear()
        sent = _spy(monkeypatch)
        response = _update(target["targetId"], {"name": "Wealth platform"})
        assert response["statusCode"] == 200, _body(response)
        assert sent == [] and index.published == []
        assert _offered_one(target["targetId"])["name"] == "Wealth platform"

    def test_disabling_it_takes_it_off_users_list(self, api):
        _, postgres = api
        target = _registered(postgres)
        _disable(target["targetId"])
        assert _offered() == []

    def test_a_target_from_before_names_is_listed_unnamed_and_not_offered_to_users(self, api):
        _, postgres = api
        _registered(postgres)
        unnamed = {k: v for k, v in _targets()[0].items() if k != "name"}
        scan_targets._put(unnamed)
        assert _list()[0]["name"] is None
        assert _offered() == []


class TestStatementLimits:
    """Each database's statement timeout and row cap are the administrator's to set."""

    TIMEOUT_BOUNDS = "The statement timeout (seconds) must be between 1 and 240."

    @pytest.mark.parametrize("limits, refusal", [
        ({"rowCap": 1000}, "The statement timeout (seconds) is required."),
        ({"statementTimeoutSeconds": 30}, "The row cap is required."),
        ({"statementTimeoutSeconds": 0, "rowCap": 1000}, TIMEOUT_BOUNDS),
        ({"statementTimeoutSeconds": 241, "rowCap": 1000}, TIMEOUT_BOUNDS),
        ({"statementTimeoutSeconds": 30, "rowCap": 5001}, "The row cap must be between 1 and 5000.")])
    def test_both_are_required_and_bounded_with_no_default(self, api, limits, refusal):
        _, postgres = api
        response = _add({**_location(postgres), "scannerSecretArn": _byo_secret(postgres), **limits})
        assert response["statusCode"] == 400
        assert refusal in json.loads(response["body"])["error"]
        assert _targets() == []

    def test_they_reach_users_with_the_database(self, api):
        _, postgres = api
        target = _registered(postgres, statementTimeoutSeconds=12, rowCap=345)
        entry = _offered_one(target["targetId"])
        assert (entry["statementTimeoutSeconds"], entry["rowCap"]) == (12, 345)
        listed = _list()[0]
        assert (listed["statementTimeoutSeconds"], listed["rowCap"]) == (12, 345)

    def test_changing_them_needs_no_new_login_or_scan(self, api, monkeypatch):
        index, postgres = api
        target = _registered(postgres)
        index.published.clear()
        sent = _spy(monkeypatch)
        response = _update(target["targetId"], {"statementTimeoutSeconds": 60, "rowCap": 200})
        assert response["statusCode"] == 200, _body(response)
        assert sent == [] and index.published == []
        entry = _offered_one(target["targetId"])
        assert (entry["statementTimeoutSeconds"], entry["rowCap"]) == (60, 200)


class TestOracleTlsIsDecidedWhenTheDatabaseIsSaved:
    """The admin's save probes the database for TLS, keeps the answer on the target, and
    warns when there is none. Real Oracle on both sides of a real TLS handshake: the test
    listener (tls_proxy.py) offers TLS in front of the container, which has none."""

    @pytest.fixture
    def oracle_saved(self, api, oracle, monkeypatch, tmp_path):
        from tests.integration.tls_proxy import tls_listener

        host, rest = oracle.dsn.split(":", 1)
        port, service = rest.split("/", 1)
        # Verify against the test CA, as the deployed functions verify against theirs.
        monkeypatch.delenv("DB_TLS_DISABLED")
        monkeypatch.setenv("ENGINE", "oracle")  # the bus hands the scan to the Oracle scanner
        with tls_listener(host, int(port), tmp_path) as listener:
            monkeypatch.setenv("DB_TLS_CA_FILE", listener.ca_file)

            def save(at_port, at_host=host):
                secret = _secrets().create_secret(Name=f"{SCANNER_PATH}dba-{uuid.uuid4()}", SecretString=json.dumps(
                    {"username": seed.OWNER, "password": oracle.owner_password}))["ARN"]
                return _add({"engine": "oracle", "host": at_host, "port": str(at_port), "database": service,
                             "scannerSecretArn": secret})
            yield save, port, listener

    def test_a_database_without_tls_is_saved_without_it_and_the_admin_is_told(self, oracle_saved):
        save, plain_port, listener = oracle_saved
        response = save(plain_port)
        assert response["statusCode"] == 201, _body(response)
        saved = _body(response)
        assert saved["tls"] is False
        assert "TLS is not enabled on this database" in saved["tlsWarning"]
        assert "did not answer TLS on this port" in saved["tlsWarning"]
        # Kept for later: on the target, and on the admin page's list.
        stored = scan_targets.get(saved["targetId"])
        assert (stored["tls"], stored["tlsWarning"]) == (False, saved["tlsWarning"])
        (listed,) = _list()
        assert listed["tlsWarning"] == saved["tlsWarning"]
        assert stored["scanStatus"] == "scanned", stored.get("scanMessage")

    def test_a_database_with_tls_is_saved_with_it_and_scanned_over_it(self, oracle_saved):
        save, _, listener = oracle_saved
        response = save(listener.port, listener.host)
        assert response["statusCode"] == 201, _body(response)
        saved = _body(response)
        assert saved["tls"] is True and "tlsWarning" not in saved
        stored = scan_targets.get(saved["targetId"])
        assert stored["tls"] is True
        # The probe and the scan both came through the TLS listener, the only way to this port.
        assert stored["scanStatus"] == "scanned", stored.get("scanMessage")
        assert len(listener.handshakes) >= 2

    @pytest.fixture
    def own_certificate(self, oracle, monkeypatch, tmp_path):
        """A TLS listener presenting a certificate of its own, as dev's Oracle does on 2484."""
        from tests.integration.tls_proxy import tls_listener

        host, rest = oracle.dsn.split(":", 1)
        (tmp_path / "own").mkdir()
        with tls_listener(host, int(rest.split("/", 1)[0]), tmp_path / "own", self_signed=True) as listener:
            yield listener

    def test_a_database_with_its_own_certificate_is_saved_with_tls_trusting_only_it(self, oracle_saved,
                                                                                   own_certificate):
        """No CA in the bundle signed it, so the save trusts the one it presents, keeps it, and
        shows its fingerprint; the probe and the scan go over TLS trusting that certificate."""
        import hashlib
        import ssl

        save, _, _ = oracle_saved
        response = save(own_certificate.port, own_certificate.host)
        assert response["statusCode"] == 201, _body(response)
        saved = _body(response)
        digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(own_certificate.certificate)).hexdigest().upper()
        assert saved["tls"] is True and "tlsWarning" not in saved
        assert saved["tlsFingerprint"].replace(":", "") == digest
        stored = scan_targets.get(saved["targetId"])
        presented = ssl.PEM_cert_to_DER_cert(own_certificate.certificate)
        assert ssl.PEM_cert_to_DER_cert(stored["tlsCertificate"]) == presented
        assert stored["scanStatus"] == "scanned", stored.get("scanMessage")
        assert len(own_certificate.handshakes) >= 3  # the bundle's refusal, the pinned login, the scan

    def test_a_certificate_from_a_ca_the_service_does_not_carry_is_trusted_alone(self, oracle_saved, monkeypatch):
        """The test CA's certificate, with the bundle not holding that CA: the save trusts the
        database's own certificate, not the CA, so one certificate anchors the chain."""
        save, _, listener = oracle_saved
        monkeypatch.setenv("DB_TLS_CA_FILE", str(__import__("certifi").where()))
        response = save(listener.port, listener.host)
        assert response["statusCode"] == 201, _body(response)
        assert _body(response)["tls"] is True and _body(response)["tlsFingerprint"]
        (target,) = _targets()
        ca = Path(listener.ca_file).read_text()
        assert _der(target["tlsCertificate"]) == _der(listener.certificate) != _der(ca)

    def test_a_wrong_login_with_its_own_certificate_is_refused_and_nothing_is_saved(self, oracle_saved,
                                                                                  own_certificate, oracle):
        secret = _secrets().create_secret(Name=f"{SCANNER_PATH}dba-{uuid.uuid4()}", SecretString=json.dumps(
            {"username": seed.OWNER, "password": "not-the-password"}))["ARN"]
        response = _add({"engine": "oracle", "host": own_certificate.host, "port": str(own_certificate.port),
                         "database": oracle.dsn.rsplit("/", 1)[1], "scannerSecretArn": secret})
        assert response["statusCode"] == 400 and "ORA-01017" in _body(response)["error"]
        assert _targets() == []

    def test_scan_now_records_nothing_on_a_version_whose_tls_was_never_recorded(self, oracle_saved):
        """How it is reached is fixed with the version: one saved before TLS was checked gets
        a new version to record it, not a quiet write."""
        save, plain_port, _ = oracle_saved
        saved = _body(save(plain_port))
        unchecked = {k: v for k, v in scan_targets.get(saved["targetId"]).items() if k not in ("tls", "tlsWarning")}
        scan_targets._put(unchecked)
        response = admin._start_scan(AS_ADMIN, {}, {}, {"targetId": saved["targetId"]})
        assert response["statusCode"] == 400 and "publish a new version" in _body(response)["error"]
        assert scan_targets.get(saved["targetId"]) == unchecked

    def test_scan_now_against_a_database_presenting_another_certificate_changes_nothing(self, oracle_saved,
                                                                                        own_certificate):
        """The pinned certificate is part of the version: a database that now presents another
        is refused, and the pin stays as it was."""
        save, _, listener = oracle_saved
        saved = _body(save(own_certificate.port, own_certificate.host))
        stored = scan_targets.get(saved["targetId"])
        other = Path(listener.ca_file).read_text()   # a certificate the database does not present
        scan_targets._put({**stored, "tlsCertificate": other})
        before = scan_targets.get(saved["targetId"])
        response = admin._start_scan(AS_ADMIN, {}, {}, {"targetId": saved["targetId"]})
        assert response["statusCode"] == 400 and "different certificate" in _body(response)["error"]
        assert scan_targets.get(saved["targetId"]) == before

    def test_a_new_version_records_what_the_database_presents_now(self, oracle_saved, own_certificate):
        save, plain_port, _ = oracle_saved
        first = _body(save(plain_port))
        assert first["tls"] is False
        secret = _secrets().create_secret(Name=f"{SCANNER_PATH}dba-{uuid.uuid4()}", SecretString=json.dumps(
            {"username": seed.OWNER, "password": _value(first["scannerSecretArn"])["password"]}))["ARN"]
        response = _new_version(first["targetId"], {"host": own_certificate.host, "port": str(own_certificate.port),
                                                    "scannerSecretArn": secret})
        assert response["statusCode"] == 201, _body(response)
        second = scan_targets.get(_body(response)["targetId"])
        assert second["tls"] is True and _der(second["tlsCertificate"]) == _der(own_certificate.certificate)
        assert scan_targets.get(first["targetId"])["tls"] is False   # the first version is as it was

    def test_a_wrong_login_over_tls_is_refused_and_nothing_is_saved(self, oracle_saved, oracle):
        save, _, listener = oracle_saved
        secret = _secrets().create_secret(Name=f"{SCANNER_PATH}dba-{uuid.uuid4()}", SecretString=json.dumps(
            {"username": seed.OWNER, "password": "not-the-password"}))["ARN"]
        host, rest = oracle.dsn.split(":", 1)
        response = _add({"engine": "oracle", "host": listener.host, "port": str(listener.port),
                         "database": rest.split("/", 1)[1], "scannerSecretArn": secret})
        assert response["statusCode"] == 400 and "ORA-01017" in _body(response)["error"]
        assert _targets() == []


class TestVersions:
    """Where a database is and how it is reached are fixed for each version. A new version is
    the next of the same database, with its own scanner login; users stay where they are."""

    def test_a_new_version_is_the_next_of_the_same_database_and_the_first_stays_in_use(self, api):
        _, postgres = api
        first = _registered(postgres, host="localhost", name="Wealth", statementTimeoutSeconds=12, rowCap=34)
        response = _new_version(first["targetId"], {"host": "127.0.0.1", "scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 201, _body(response)
        second = _body(response)
        assert (second["lineageId"], second["version"], second["identity"]) == \
            (first["targetId"], 2, _identity(postgres, "127.0.0.1"))
        # The database's name and limits carry over; the first version is untouched and still offered to
        # its connections, but new connections go to the second.
        assert (second["name"], second["statementTimeoutSeconds"], second["rowCap"]) == ("Wealth", 12, 34)
        assert scan_targets.get(first["targetId"])["identity"] == _identity(postgres, "localhost")
        offered = {d["targetId"]: d["latest"] for d in _offered()}
        assert offered == {first["targetId"]: False, second["targetId"]: True}

    def test_a_new_version_at_the_same_place_is_allowed(self, api):
        """A rotated scanner login or a new certificate at the same host is still a new version."""
        _, postgres = api
        first = _registered(postgres)
        response = _new_version(first["targetId"], {"scannerSecretArn": _byo_secret(postgres)})
        assert response["statusCode"] == 201, _body(response)
        assert _body(response)["identity"] == first["identity"] and _body(response)["version"] == 2

    def test_a_new_version_needs_its_own_scanner_login(self, api):
        _, postgres = api
        first = _registered(postgres)
        for body in ({"host": "127.0.0.1"}, {"host": "127.0.0.1", "scannerSecretArn": first["scannerSecretArn"]}):
            response = _new_version(first["targetId"], body)
            assert response["statusCode"] == 400, body
        assert len(_targets()) == 1

    @pytest.mark.parametrize("body", [{"engine": "oracle"}, {"name": "Other"}, {"rowCap": 1}, {"tls": False},
                                      {"version": 9}, {"lineageId": "x"}])
    def test_a_new_version_takes_only_where_it_is_its_login_and_its_schedule(self, api, body):
        _, postgres = api
        first = _registered(postgres)
        response = _new_version(first["targetId"], {"scannerSecretArn": _byo_secret(postgres), **body})
        assert response["statusCode"] == 400 and len(_targets()) == 1

    def test_a_new_version_whose_scanner_cannot_log_in_is_not_stored(self, api):
        _, postgres = api
        first = _registered(postgres)
        bad = _byo_secret(postgres, password=WRONG_PASSWORD)
        response = _new_version(first["targetId"], {"scannerSecretArn": bad})
        assert response["statusCode"] == 400 and "could not log in" in _body(response)["error"]
        assert [t["targetId"] for t in _targets()] == [first["targetId"]]

    def test_a_name_or_limits_change_applies_to_every_version_and_a_schedule_to_one(self, api):
        _, postgres = api
        first = _registered(postgres, host="localhost", name="Wealth")
        second = _body(_new_version(first["targetId"], {"host": "127.0.0.1",
                                                        "scannerSecretArn": _byo_secret(postgres)}))
        response = _update(second["targetId"], {"name": "Wealth platform", "rowCap": 77, "schedule": "rate(1 day)"})
        assert response["statusCode"] == 200, _body(response)
        for version in (first, second):
            stored = scan_targets.get(version["targetId"])
            assert (stored["name"], stored["rowCap"]) == ("Wealth platform", 77)
        assert _schedule(second["targetId"])["ScheduleExpression"] == "rate(1 day)"
        assert _schedule(first["targetId"]) is None

    def test_another_databases_name_is_still_refused_but_its_own_versions_share_one(self, api):
        _, postgres = api
        _registered(postgres, host="localhost", name="Banking")
        wealth = _registered(postgres, host="127.0.0.1", name="Wealth")
        assert _update(wealth["targetId"], {"name": "banking"})["statusCode"] == 400
        assert _update(wealth["targetId"], {"name": "Wealth"})["statusCode"] == 200

    def test_a_scanner_login_rotation_is_checked_where_the_version_is_and_moves_nothing(self, api, monkeypatch):
        _, postgres = api
        target = _registered(postgres)
        sent = _spy(monkeypatch)
        response = _update(target["targetId"], {"scannerUsername": seed.OWNER,
                                                "scannerPassword": postgres.owner_password})
        assert response["statusCode"] == 200, _body(response)
        probes = [payload for _, payload in sent if payload.get("operation") == "probe"]
        assert [p["args"]["location"] for p in probes] == [{f: target[f] for f in ("host", "port", "database")}]
        assert scan_targets.get(target["targetId"])["identity"] == target["identity"]


class TestWhoChangedWhat:
    """Every creation, edit and disabling records who made it, from the verified token."""

    def test_a_version_records_who_created_it(self, api):
        _, postgres = api
        target = _registered(postgres)
        stored = scan_targets.get(target["targetId"])
        assert (stored["createdBy"], stored["createdBySub"]) == (f"{ADMIN}-name", ADMIN) and stored["createdAt"]
        assert _list()[0]["createdBy"] == f"{ADMIN}-name"

    def test_an_edit_records_who_made_it_on_every_version_it_changes(self, api):
        _, postgres = api
        first = _registered(postgres, host="localhost")
        second = _body(_new_version(first["targetId"], {"host": "127.0.0.1",
                                                        "scannerSecretArn": _byo_secret(postgres)}))
        other_admin = _event("sub-other-admin", "[admin]")
        admin._update_scan_target(other_admin, {"targetId": second["targetId"]}, {}, {"rowCap": 99})
        for version in (first, second):
            stored = scan_targets.get(version["targetId"])
            assert (stored["updatedBy"], stored["updatedBySub"]) == ("sub-other-admin-name", "sub-other-admin")
            assert stored["createdBySub"] == ADMIN

    def test_without_the_email_claim_the_username_is_recorded(self, api):
        """A token from before the trigger, or a pool without it: still the verified token's own."""
        _, postgres = api
        event = _event("sub-old-token", "[admin]")
        del event["requestContext"]["authorizer"]["jwt"]["claims"]["email"]
        body = {"name": "No email", "statementTimeoutSeconds": 30, "rowCap": 1000, **_location(postgres),
                "scannerSecretArn": _byo_secret(postgres)}
        response = admin._add_scan_target(event, {}, {}, body)
        assert scan_targets.get(_body(response)["targetId"])["createdBy"] == "sub-old-token-id"

    def test_a_request_cannot_say_who_made_it(self, api):
        _, postgres = api
        response = _add({**_location(postgres), "scannerSecretArn": _byo_secret(postgres),
                         "createdBy": "someone-else", "createdBySub": "sub-someone-else"})
        stored = scan_targets.get(_body(response)["targetId"])
        assert (stored["createdBy"], stored["createdBySub"]) == (f"{ADMIN}-name", ADMIN)

    def test_each_change_is_logged_with_who_and_no_password(self, api, caplog):
        _, postgres = api
        caplog.set_level(logging.INFO)
        _add({**_location(postgres), "scannerUsername": seed.OWNER, "scannerPassword": postgres.owner_password})
        [target] = _targets()
        _update(target["targetId"], {"rowCap": 50})
        _disable(target["targetId"])
        for verb in ("created by", "edited by", "disabled by"):
            assert any(verb in r.getMessage() and f"{ADMIN}-name" in r.getMessage() and ADMIN in r.getMessage()
                       for r in caplog.records), verb
        assert postgres.owner_password not in caplog.text
