#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The connections routes, against real databases through the real provider handler.

A user connects to a registered database by choosing it from the list the schema
service serves and giving a login; where it is comes from that registration. A
connection is saved only if the database-provider function can log in with it.
The routes carry no database driver: they ask the provider, which is the one
component with network reach to the databases. Rows and scan targets live in
DynamoDB Local, credentials in moto Secrets Manager, and the "Lambdas" are the
real provider and schema handlers routed in-process.
"""

import json
import secrets

import boto3
import pytest

from adam.data_source_provider import connections_api_handler as connections
from adam.data_source_provider.connection_opening import registered
from adam.data_source_provider.provider_handler import handler as provider_handler
from adam.schema_provider import glue_data_catalog_schema_provider as schema_module, scan_targets
from adam.schema_provider.source_identity import source_identity
from adam.shared import provider_client
from adam.shared.network_target import (
    validate_target as REAL_VALIDATE_TARGET,  # before the api fixture replaces it
)
from tests.integration import seed
from tests.integration.conftest import register_database, validator_allowing_the_containers
from tests.integration.lambda_router import LambdaRouter

pytestmark = pytest.mark.integration

ALICE, BOB = "sub-alice", "sub-bob"
ORACLE_DB, PG_DB = "db-oracle", "db-postgres"
WRONG_PASSWORD = secrets.token_urlsafe(24)
# What a connection row keeps (beside its keys and creation time): which registered
# database, and where its login's secret is. Nothing about the database itself.
STORED = {"targetId", "secretName"}


def _event(user):
    return {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": user}}}}}


def _body(response):
    return json.loads(response["body"])


def _register(target_id, name, engine, host, port, database):
    """A scan target as the admin routes write one: named, so users may connect to it."""
    register_database(engine, host=host, port=port, database=database, target_id=target_id, name=name)


@pytest.fixture
def login_attempts_table(local_aws, monkeypatch):
    """The provider service's table of refused typed logins, as the stack declares it."""
    import uuid
    ddb = boto3.client("dynamodb")
    table = f"login-attempts-{uuid.uuid4().hex[:8]}"
    ddb.create_table(TableName=table, BillingMode="PAY_PER_REQUEST",
                     AttributeDefinitions=[{"AttributeName": "userId", "AttributeType": "S"},
                                           {"AttributeName": "window", "AttributeType": "N"}],
                     KeySchema=[{"AttributeName": "userId", "KeyType": "HASH"},
                                {"AttributeName": "window", "KeyType": "RANGE"}])
    monkeypatch.setenv("LOGIN_ATTEMPTS_TABLE_NAME", table)
    try:
        yield table
    finally:
        ddb.delete_table(TableName=table)


@pytest.fixture
def api(registry, scan_targets_table, login_attempts_table, oracle, postgres, oracle_account_mode, monkeypatch):
    # As ADMIN_BEST_PRACTICES advises; registration itself does not require it.
    oracle_account_mode(seed.ANALYST, True)
    monkeypatch.setenv("STACK_NAME", "test")
    monkeypatch.setattr(connections, "_secrets", None)
    monkeypatch.setattr(connections, "validate_target", validator_allowing_the_containers(REAL_VALIDATE_TARGET))

    ssm = boto3.client("ssm")
    for key, function in {"oracle_provider_function_name": "oracle-fn",
                          "postgresql_provider_function_name": "pg-fn",
                          "schema_provider_function_name": "schema-fn"}.items():
        ssm.put_parameter(Name=f"/test/{key}", Value=function, Type="String", Overwrite=True)
    monkeypatch.setattr(provider_client, "_function_names", {})
    monkeypatch.setattr(provider_client, "_lambda_client", LambdaRouter({
        "oracle-fn": provider_handler, "pg-fn": provider_handler, "schema-fn": schema_module.handler}))
    # What an administrator registered (the admin routes write these).
    host, rest = oracle.dsn.split(":", 1)
    port, service = rest.split("/", 1)
    _register(ORACLE_DB, "Banking", "oracle", host, port, service)
    _register(PG_DB, "Wealth", "postgres", postgres.host, postgres.port, postgres.database)
    return registry


def _oracle_body(oracle, password=None, **overrides):
    return {"databaseId": ORACLE_DB, "username": seed.ANALYST,
            "password": password or oracle.analyst_password, **overrides}


def _pg_body(postgres, password=None, **overrides):
    return {"databaseId": PG_DB, "username": seed.OWNER,
            "password": password or postgres.owner_password, **overrides}


def _rows(api, user):
    return [i["connectionId"]["S"] for i in boto3.client("dynamodb").scan(
        TableName=api.table)["Items"] if i["userId"]["S"] == user]


def _secrets(user=ALICE):
    """Names of the user's connection secrets. Moto outlives a test, so tests compare
    before and after rather than against empty."""
    pages = boto3.client("secretsmanager").get_paginator("list_secrets").paginate(
        Filters=[{"Key": "name", "Values": [f"/test/connections/{user}/"]}])
    return {s["Name"] for page in pages for s in page["SecretList"]}


@pytest.mark.parametrize("engine", ["oracle", "postgres"])
class TestCreate:
    def _body(self, engine, oracle, postgres, **kw):
        return _oracle_body(oracle, **kw) if engine == "oracle" else _pg_body(postgres, **kw)

    def test_a_working_connection_is_saved_and_listed(self, api, oracle, postgres, engine):
        response = connections._create_connection(
            _event(ALICE), {}, {}, self._body(engine, oracle, postgres))
        assert response["statusCode"] == 201
        created = _body(response)
        listed = _body(connections._list_connections(_event(ALICE), {}, {}, None))["connections"]
        assert [c["connectionId"] for c in listed] == [created["connectionId"]]
        assert "password" not in json.dumps(listed)

    def test_a_connection_that_cannot_log_in_is_not_saved_and_leaves_no_secret(
            self, api, oracle, postgres, engine):
        before = _secrets()
        response = connections._create_connection(
            _event(ALICE), {}, {}, self._body(engine, oracle, postgres, password=WRONG_PASSWORD))
        assert response["statusCode"] == 400
        assert _rows(api, ALICE) == []
        assert _secrets() == before

    def test_the_failure_does_not_echo_driver_detail(self, api, oracle, postgres, engine):
        response = connections._create_connection(
            _event(ALICE), {}, {}, self._body(engine, oracle, postgres, password=WRONG_PASSWORD))
        text = response["body"]
        assert WRONG_PASSWORD not in text
        assert "ORA-" not in text and "28P01" not in text and "pg8000" not in text

    def test_testing_before_saving_saves_nothing(self, api, oracle, postgres, engine):
        before = _secrets()
        ok = _body(connections._test_connection(
            _event(ALICE), {}, {}, self._body(engine, oracle, postgres)))
        bad = _body(connections._test_connection(
            _event(ALICE), {}, {}, self._body(engine, oracle, postgres, password=WRONG_PASSWORD)))
        assert ok["success"] is True and bad["success"] is False
        assert _rows(api, ALICE) == [] and _secrets() == before
        assert WRONG_PASSWORD not in json.dumps(bad)


class TestRegistrationOnlyChecksTheLogin:
    """Read-only account settings differ by release (Oracle before 23.26 has none),
    so registering checks the login only; ADMIN_BEST_PRACTICES carries the advice."""

    def test_an_oracle_account_that_can_write_is_accepted(self, api, oracle):
        body = _oracle_body(oracle, username=seed.OWNER, password=oracle.owner_password)
        assert connections._create_connection(_event(ALICE), {}, {}, body)["statusCode"] == 201

    def test_testing_it_before_saving_succeeds(self, api, oracle):
        body = _oracle_body(oracle, username=seed.OWNER, password=oracle.owner_password)
        assert _body(connections._test_connection(_event(ALICE), {}, {}, body))["success"] is True

    def test_a_wrong_password_is_still_a_generic_message(self, api, oracle):
        body = _oracle_body(oracle, password=WRONG_PASSWORD)
        result = _body(connections._test_connection(_event(ALICE), {}, {}, body))
        assert result["success"] is False and result["message"] == connections._LOGIN_FAILED


class TestExisting:
    def test_testing_a_saved_connection_uses_its_stored_credentials(self, api, postgres):
        cid = _body(connections._create_connection(
            _event(ALICE), {}, {}, _pg_body(postgres)))["connectionId"]
        result = _body(connections._test_existing_connection(
            _event(ALICE), {"connectionId": cid}, {}, None))
        assert result["success"] is True
        api.rotate(ALICE, cid, seed.OWNER, WRONG_PASSWORD)
        result = _body(connections._test_existing_connection(
            _event(ALICE), {"connectionId": cid}, {}, None))
        assert result["success"] is False

    def test_another_user_cannot_test_update_or_delete_it(self, api, postgres):
        cid = _body(connections._create_connection(
            _event(ALICE), {}, {}, _pg_body(postgres)))["connectionId"]
        params = {"connectionId": cid}
        for call, body in ((connections._test_existing_connection, None),
                           (connections._update_connection, {"username": "u", "password": "p"}),
                           (connections._delete_connection, None)):
            response = call(_event(BOB), params, {}, body)
            assert response["statusCode"] == 400 and _body(response)["error"] == "Connection not found."
        assert _rows(api, ALICE) == [cid]

    def test_delete_removes_the_row_and_the_secret(self, api, postgres):
        before = _secrets()
        cid = _body(connections._create_connection(
            _event(ALICE), {}, {}, _pg_body(postgres)))["connectionId"]
        assert len(_secrets() - before) == 1
        connections._delete_connection(_event(ALICE), {"connectionId": cid}, {}, None)
        assert _rows(api, ALICE) == [] and _secrets() == before


class TestUpdate:
    def _create(self, postgres):
        return _body(connections._create_connection(
            _event(ALICE), {}, {}, _pg_body(postgres)))["connectionId"]

    def test_a_request_cannot_point_a_connection_elsewhere(self, api, postgres):
        """Otherwise the stored password could be sent to a host of the caller's choosing."""
        cid = self._create(postgres)
        response = connections._update_connection(
            _event(ALICE), {"connectionId": cid}, {},
            {"host": "db.other.example", "username": seed.OWNER, "password": postgres.owner_password})
        assert response["statusCode"] == 200
        assert set(api.row(ALICE, cid)) == STORED  # nothing about where the database is
        assert registered({**api.row(ALICE, cid), "connectionId": cid})["host"] == postgres.host

    def test_a_new_login_for_a_disabled_version_is_refused_and_changes_nothing(self, api, postgres):
        cid = self._create(postgres)
        before = api.row(ALICE, cid)
        scan_targets.disable(PG_DB, by="admin", by_sub="sub-admin", at="2026-10-26T00:00:00+00:00")
        response = connections._update_connection(
            _event(ALICE), {"connectionId": cid}, {},
            {"username": seed.OWNER, "password": postgres.owner_password})
        assert response["statusCode"] == 400 and "disabled" in _body(response)["error"]
        assert api.row(ALICE, cid) == before

    def test_new_credentials_that_work_replace_the_stored_ones(self, api, postgres):
        cid = self._create(postgres)  # as the schema owner
        response = connections._update_connection(
            _event(ALICE), {"connectionId": cid}, {},
            {"username": seed.ANALYST, "password": postgres.analyst_password})
        assert response["statusCode"] == 200
        who = provider_handler({"operation": "execute_statement", "args": {
            "user_id": ALICE, "connection_id": cid, "statement": "SELECT current_user AS who",
            "timeout_seconds": 30, "row_cap": 10}}, None)
        assert who["response"]["rows"] == [{"who": seed.ANALYST}]

    def test_new_credentials_that_do_not_work_are_not_stored(self, api, postgres):
        cid = self._create(postgres)
        response = connections._update_connection(
            _event(ALICE), {"connectionId": cid}, {},
            {"username": seed.OWNER, "password": WRONG_PASSWORD})
        assert response["statusCode"] == 400
        assert _body(connections._test_existing_connection(
            _event(ALICE), {"connectionId": cid}, {}, None))["success"] is True

    def test_a_connection_from_before_registered_databases_must_be_added_again(self, api, postgres):
        cid = self._create(postgres)
        boto3.client("dynamodb").update_item(
            TableName=api.table, Key={"userId": {"S": ALICE}, "connectionId": {"S": cid}},
            UpdateExpression="REMOVE targetId")
        response = connections._update_connection(
            _event(ALICE), {"connectionId": cid}, {},
            {"username": seed.OWNER, "password": postgres.owner_password})
        assert response["statusCode"] == 400 and "add it again" in _body(response)["error"]


class TestRegisteredDatabases:
    """Users connect only to databases an administrator registered, chosen by name."""

    def test_users_see_each_registered_database_by_name_and_nothing_about_where_it_is(self, api):
        listed = _body(connections._list_databases(_event(ALICE), {}, {}, None))["databases"]
        assert listed == [{"databaseId": ORACLE_DB, "name": "Banking", "engine": "oracle", "version": 1},
                          {"databaseId": PG_DB, "name": "Wealth", "engine": "postgres", "version": 1}]

    def test_a_database_that_is_not_registered_is_refused_and_nothing_stored(self, api, postgres):
        before = _secrets()
        response = connections._create_connection(
            _event(ALICE), {}, {}, _pg_body(postgres, databaseId="not-registered"))
        assert response["statusCode"] == 400
        assert _rows(api, ALICE) == [] and _secrets() == before

    def test_a_host_in_the_request_is_ignored(self, api, postgres):
        response = connections._create_connection(
            _event(ALICE), {}, {}, _pg_body(postgres, host="db.other.example"))
        assert response["statusCode"] == 201
        cid = _body(response)["connectionId"]
        assert set(api.row(ALICE, cid)) == STORED
        assert registered({**api.row(ALICE, cid), "connectionId": cid})["host"] == postgres.host

    def test_the_list_names_each_connection_and_notices_when_its_database_is_gone(self, api, postgres):
        connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres))
        [listed] = _body(connections._list_connections(_event(ALICE), {}, {}, None))["connections"]
        assert (listed["databaseId"], listed["name"]) == (PG_DB, "Wealth")
        scan_targets._delete(PG_DB)   # no route deletes a version; a table emptied by hand still says so
        [listed] = _body(connections._list_connections(_event(ALICE), {}, {}, None))["connections"]
        assert listed["name"] is None


class TestTargetsAreValidated:

    def _registered_at(self, host):
        _register("db-bad", "Bad", "postgres", host, "5432", "d")
        return {"databaseId": "db-bad", "username": "u", "password": "p"}

    @pytest.mark.parametrize("host", [
        "127.0.0.2", "localhost.", "169.254.169.254", "0.0.0.0", "::1", "224.0.0.1"])  # noqa: S104  # nosec B104 - hosts the validator must refuse; nothing binds
    def test_hosts_the_function_must_not_dial_are_refused_before_it_is_asked_to(self, api, host):
        before = _secrets()
        response = connections._create_connection(_event(ALICE), {}, {}, self._registered_at(host))
        # The validator's refusal, not a login that failed after the function dialed the host.
        assert _body(response)["error"] == f"{host!r} is not an address a database can be registered at."
        assert "pg-fn" not in provider_client._lambda_client.invoked  # the provider was never asked to dial it
        assert _rows(api, ALICE) == [] and _secrets() == before

    def test_the_same_check_guards_test_before_save(self, api):
        result = connections._test_connection(_event(ALICE), {}, {}, self._registered_at("169.254.169.254"))
        assert _body(result)["error"] == "'169.254.169.254' is not an address a database can be registered at."
        assert "pg-fn" not in provider_client._lambda_client.invoked

    def test_a_private_address_is_allowed(self):
        REAL_VALIDATE_TARGET("10.0.12.34")


class _Forwarder:
    """A TCP relay to the Postgres container on a port of its own: a database an
    administrator has moved. It counts the sessions that come through it."""

    def __init__(self, host: str, port: int):
        import socket
        import threading

        self.sessions = 0
        self._listener = socket.create_server(("127.0.0.1", 0))
        self.port = self._listener.getsockname()[1]

        def pipe(a, b):
            try:
                while data := a.recv(65536):
                    b.sendall(data)
            except OSError:
                pass
            finally:
                for s in (a, b):
                    try:
                        s.shutdown(socket.SHUT_RDWR)
                    except OSError:
                        pass

        def serve():
            while True:
                try:
                    client, _ = self._listener.accept()
                except OSError:
                    return
                self.sessions += 1
                upstream = socket.create_connection((host, port))
                threading.Thread(target=pipe, args=(client, upstream), daemon=True).start()
                threading.Thread(target=pipe, args=(upstream, client), daemon=True).start()

        threading.Thread(target=serve, daemon=True).start()

    def close(self):
        self._listener.close()


def _whoami(user, cid, **extra_args):
    """What the dispatcher sends a provider for a statement; extra_args are anything else
    a caller might add."""
    return provider_handler({"operation": "execute_statement", "args": {
        "user_id": user, "connection_id": cid, "statement": "SELECT current_user AS who",
        "timeout_seconds": 30, "row_cap": 10, **extra_args}}, None)


def _version(of: str, version: int, **location) -> str:
    """Publish the next version of a registered database, as the admin routes write one."""
    current = scan_targets.get(of)
    target_id = f"{of}-v{version}"
    where = {**{f: current[f] for f in ("engine", *scan_targets.FIELDS)}, **{k: str(v) for k, v in location.items()}}
    scan_targets.create({**{f: current[f] for f in ("lineageId", "name", *scan_targets.LIMIT_FIELDS)},
                         "targetId": target_id, "version": version, **where,
                         "identity": source_identity(scan_targets.connection({"targetId": target_id, **where})),
                         "scannerSecretArn": f"arn:aws:secretsmanager:us-east-1:1:secret:scanner-{version}",
                         "scannerSecretManaged": False, "disabled": False, "createdBy": "test-admin"})
    return target_id


def _disable(target_id: str) -> None:
    scan_targets.disable(target_id, by="test-admin", by_sub="sub-admin", at="2026-10-26T00:00:00+00:00")


class TestAConnectionStaysOnItsVersion:
    """A connection is a login to one version of a database. A version's location never
    changes, so the password goes only where the user entered it for; a new version is the
    user's to connect to, with their login."""

    def _connect(self, postgres, database_id=PG_DB):
        response = connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres, databaseId=database_id))
        assert response["statusCode"] == 201, _body(response)
        return _body(response)["connectionId"]

    def test_a_new_version_elsewhere_takes_no_connection_with_it(self, api, postgres):
        cid = self._connect(postgres)
        row = api.row(ALICE, cid)
        relay = _Forwarder(postgres.host, int(postgres.port))
        try:
            _version(PG_DB, 2, host="127.0.0.1", port=relay.port)
            assert _whoami(ALICE, cid)["response"]["rows"] == [{"who": seed.OWNER}]
            assert relay.sessions == 0  # nothing went to the new version's location
            assert api.row(ALICE, cid) == row
        finally:
            relay.close()

    def test_publishing_a_version_saves_no_login_for_anyone(self, api, postgres):
        self._connect(postgres)
        before = _secrets()
        _version(PG_DB, 2, host="127.0.0.1")
        assert _secrets() == before and len(_rows(api, ALICE)) == 1

    def test_new_connections_go_to_the_newest_version_and_not_an_older_one(self, api, postgres):
        relay = _Forwarder(postgres.host, int(postgres.port))
        try:
            newest = _version(PG_DB, 2, host="127.0.0.1", port=relay.port)
            offered = _body(connections._list_databases(_event(ALICE), {}, {}, None))["databases"]
            assert [d["databaseId"] for d in offered if d["name"] == "Wealth"] == [newest]
            refused = connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres))
            assert refused["statusCode"] == 400 and "older version" in _body(refused)["error"]
            cid = self._connect(postgres, newest)
            assert api.row(ALICE, cid)["targetId"] == newest
            assert relay.sessions >= 1  # the login was checked where the new version is
        finally:
            relay.close()

    def test_an_older_version_still_in_use_keeps_serving_its_connections(self, api, postgres):
        cid = self._connect(postgres)
        _version(PG_DB, 2, host="127.0.0.1")
        assert _whoami(ALICE, cid)["response"]["rows"] == [{"who": seed.OWNER}]
        assert _body(connections._test_existing_connection(
            _event(ALICE), {"connectionId": cid}, {}, None))["success"] is True

    def test_a_disabled_version_dials_nothing_even_before_its_logins_are_deleted(self, api, postgres):
        relay = _Forwarder(postgres.host, int(postgres.port))
        try:
            at_relay = _version(PG_DB, 2, host="127.0.0.1", port=relay.port)
            cid = self._connect(postgres, at_relay)
            sessions = relay.sessions
            _disable(at_relay)
            assert api.row(ALICE, cid)["secretName"]  # the clean-up has not run yet
            assert _whoami(ALICE, cid) == {"error": "Wealth was disabled by an administrator, and nothing replaces "
                                                    "it. Remove this connection."}
            assert relay.sessions == sessions
        finally:
            relay.close()

    def test_a_disabled_version_with_a_newer_one_tells_its_users_to_connect_again(self, api, postgres):
        cid = self._connect(postgres)
        _version(PG_DB, 2, host="127.0.0.1")
        _disable(PG_DB)
        assert "was updated by an administrator" in _whoami(ALICE, cid)["error"]
        (listed,) = _body(connections._list_connections(_event(ALICE), {}, {}, None))["connections"]
        assert listed["name"] == "Wealth" and "connect to Wealth again" in listed["disabled"]
        assert listed["host"] == "" and listed["dsn"] == ""  # nothing about where it was

    def test_the_list_shows_each_connections_own_version(self, api, postgres):
        cid = self._connect(postgres)
        _version(PG_DB, 2, host="127.0.0.1")
        (listed,) = _body(connections._list_connections(_event(ALICE), {}, {}, None))["connections"]
        assert (listed["connectionId"], listed["version"], listed["host"]) == (cid, 1, postgres.host)


class TestADisabledVersionsLoginsAreDeleted:
    """The schema service's RegistrationDisabled reaches the database provider's clean-up,
    which deletes the version's saved logins and keeps each connection as a marker."""

    def _event(self, target_id, source="test.schema-service", detail_type="RegistrationDisabled"):
        return {"source": source, "detail-type": detail_type, "detail": {"targetId": target_id}}

    def test_every_users_login_for_that_version_is_deleted(self, api, postgres):
        from adam.data_source_provider import disabled_version_handler
        alice = connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres))
        bob = connections._create_connection(_event(BOB), {}, {}, _pg_body(postgres))
        alice_cid, bob_cid = _body(alice)["connectionId"], _body(bob)["connectionId"]
        saved = {api.row(ALICE, alice_cid)["secretName"], api.row(BOB, bob_cid)["secretName"]}
        _disable(PG_DB)
        result = disabled_version_handler.handler(self._event(PG_DB), None)
        assert result == {"targetId": PG_DB, "connections": 2}
        for user, cid in ((ALICE, alice_cid), (BOB, bob_cid)):
            row = api.row(user, cid)
            assert "secretName" not in row and row["targetId"] == PG_DB and row["versionDisabledAt"]
        assert not (_secrets(ALICE) | _secrets(BOB)) & saved

    def test_another_versions_logins_are_kept(self, api, postgres, oracle):
        from adam.data_source_provider import disabled_version_handler
        pg = _body(connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres)))["connectionId"]
        ora = _body(connections._create_connection(_event(ALICE), {}, {}, _oracle_body(oracle)))["connectionId"]
        _disable(PG_DB)
        disabled_version_handler.handler(self._event(PG_DB), None)
        assert "secretName" not in api.row(ALICE, pg)
        assert _body(connections._test_existing_connection(
            _event(ALICE), {"connectionId": ora}, {}, None))["success"] is True

    def test_running_it_again_is_safe_and_the_marker_can_be_removed(self, api, postgres):
        from adam.data_source_provider import disabled_version_handler
        cid = _body(connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres)))["connectionId"]
        _disable(PG_DB)
        disabled_version_handler.handler(self._event(PG_DB), None)
        assert disabled_version_handler.handler(self._event(PG_DB), None) == {"targetId": PG_DB, "connections": 1}
        response = connections._delete_connection(_event(ALICE), {"connectionId": cid}, {}, None)
        assert response["statusCode"] == 200 and _rows(api, ALICE) == []

    @pytest.mark.parametrize("source,detail_type", [("test.database-provider", "RegistrationDisabled"),
                                                    ("other.schema-service", "RegistrationDisabled"),
                                                    ("test.schema-service", "ScanRequested")])
    def test_an_event_from_anyone_else_or_of_another_kind_deletes_nothing(self, api, postgres, source, detail_type):
        from adam.data_source_provider import disabled_version_handler
        cid = _body(connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres)))["connectionId"]
        before = api.row(ALICE, cid)
        with pytest.raises(ValueError):
            disabled_version_handler.handler(self._event(PG_DB, source, detail_type), None)
        assert api.row(ALICE, cid) == before


class TestAnOldRowIsReadOnlyForItsLogin:

    def test_a_location_still_on_an_old_row_is_never_dialled(self, api, postgres):
        """Rows from before connections followed their registration carried their own host and
        port. A relay stands where such a row points: nothing may reach it."""
        response = connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres))
        cid = _body(response)["connectionId"]
        relay = _Forwarder(postgres.host, int(postgres.port))
        try:
            boto3.client("dynamodb").update_item(
                TableName=api.table, Key={"userId": {"S": ALICE}, "connectionId": {"S": cid}},
                UpdateExpression="SET host = :h, port = :p, dsn = :d, engine = :e",
                ExpressionAttributeValues={":h": {"S": "127.0.0.1"}, ":p": {"S": str(relay.port)},
                                           ":d": {"S": f"postgresql://127.0.0.1:{relay.port}/x"},
                                           ":e": {"S": "postgres"}})
            assert _whoami(ALICE, cid)["response"]["rows"] == [{"who": seed.OWNER}]
            assert relay.sessions == 0
        finally:
            relay.close()


class TestASchemaServiceOutageIsNotTheUsersMistake:

    def test_it_is_an_error_not_a_request_to_choose_another_database(self, api, postgres, monkeypatch):
        def failing(event, context):
            raise RuntimeError("schema service down")
        monkeypatch.setattr(provider_client, "_lambda_client", LambdaRouter({"schema-fn": failing}))
        with pytest.raises(provider_client.ProviderError):
            connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres))
        assert _rows(api, ALICE) == []


class TestGuessingPasswordsIsBounded:
    """Checking a typed login is a way to try passwords against a registered database; a user's
    refused logins are counted, and past the limit the next check never reaches the database."""

    def _probes(self, monkeypatch):
        sent = []
        real = provider_client._lambda_client

        class _Spy:
            def invoke(self, **kwargs):
                if json.loads(kwargs["Payload"]).get("operation") == "probe":
                    sent.append(kwargs["FunctionName"])
                return real.invoke(**kwargs)
        monkeypatch.setattr(provider_client, "_lambda_client", _Spy())
        return sent

    def _test(self, user, postgres, password):
        return _body(connections._test_connection(_event(user), {}, {}, _pg_body(postgres, password=password)))

    def test_past_the_limit_even_the_right_password_is_refused_and_nothing_is_sent(self, api, postgres, monkeypatch):
        from adam.data_source_provider import login_attempts
        for _ in range(login_attempts.MAX_FAILURES):
            assert self._test(ALICE, postgres, WRONG_PASSWORD)["success"] is False
        sent = self._probes(monkeypatch)
        refused = connections._test_connection(_event(ALICE), {}, {}, _pg_body(postgres))
        assert refused["statusCode"] == 400 and "Too many logins were refused" in _body(refused)["error"]
        created = connections._create_connection(_event(ALICE), {}, {}, _pg_body(postgres))
        assert created["statusCode"] == 400 and _rows(api, ALICE) == []
        assert sent == []

    def test_one_users_refusals_do_not_limit_another(self, api, postgres):
        from adam.data_source_provider import login_attempts
        for _ in range(login_attempts.MAX_FAILURES):
            self._test(ALICE, postgres, WRONG_PASSWORD)
        assert self._test(BOB, postgres, postgres.owner_password)["success"] is True

    def test_logins_that_work_are_not_counted(self, api, postgres):
        from adam.data_source_provider import login_attempts
        for _ in range(login_attempts.MAX_FAILURES + 2):
            assert self._test(ALICE, postgres, postgres.owner_password)["success"] is True

    def test_a_new_window_starts_counting_again(self, api, postgres, monkeypatch):
        from adam.data_source_provider import login_attempts
        start = 1_800_000_000
        monkeypatch.setattr(login_attempts, "_now", lambda: start)
        for _ in range(login_attempts.MAX_FAILURES):
            self._test(ALICE, postgres, WRONG_PASSWORD)
        assert "error" in _body(connections._test_connection(_event(ALICE), {}, {}, _pg_body(postgres)))
        monkeypatch.setattr(login_attempts, "_now", lambda: start + login_attempts.WINDOW_SECONDS)
        assert self._test(ALICE, postgres, postgres.owner_password)["success"] is True

    def test_each_refused_login_is_logged_with_its_user(self, api, postgres, caplog):
        import logging
        caplog.set_level(logging.ERROR)
        self._test(ALICE, postgres, WRONG_PASSWORD)
        assert any("failed" in r.getMessage() and ALICE in r.getMessage() for r in caplog.records)
        assert WRONG_PASSWORD not in caplog.text
