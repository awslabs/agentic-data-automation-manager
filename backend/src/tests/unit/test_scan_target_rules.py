#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The rules for scan targets that need no service: how an item is read, which
schedules and scanner secrets a request may give, and the schedule names.

The flows themselves (creating secrets, schedules, rollback, removal) are in
tests/integration/test_admin_api.py against DynamoDB Local, moto and the compose
databases.
"""

import re

import pytest

from adam.schema_provider import admin_handler as admin
from schema_provider import scan_targets
from schema_provider.source_identity import source_identity
from shared.http_response import Rejected, UnauthenticatedError, get_user_id

ITEM = {"targetId": "t", "lineageId": "t", "version": 1, "identity": "postgres://h:5432/d",
        "engine": "postgres", "host": "H", "port": "5432", "database": "d", "scannerSecretArn": "arn:x",
        "scannerSecretManaged": True, "disabled": False, "createdBy": "admin@example.com"}


class TestTheItems:

    def test_an_item_reads_back_as_the_target(self):
        assert scan_targets._entry(ITEM) == ITEM

    @pytest.mark.parametrize("change", [
        {"identity": None}, {"engine": ""}, {"scannerSecretArn": ""}, {"host": 5},
        {"scannerSecretManaged": "false"}, {"disabled": None}, {"lineageId": ""}, {"createdBy": None}])
    def test_anything_else_is_an_error_not_a_target(self, change):
        with pytest.raises(RuntimeError, match="not a scan target"):
            scan_targets._entry({**ITEM, **change})

    @pytest.mark.parametrize("version", [0, -1, True, "one", None])
    def test_a_version_that_is_not_a_positive_number_is_an_error(self, version):
        with pytest.raises(RuntimeError, match="version"):
            scan_targets._entry({**ITEM, "version": version})

    def test_an_item_from_before_versions_is_version_1_of_its_own_database(self):
        legacy = {k: v for k, v in ITEM.items() if k not in ("lineageId", "version", "createdBy", "disabled")}
        entry = scan_targets._entry({**legacy, "removing": False})
        assert (entry["lineageId"], entry["version"], entry["createdBy"], entry["disabled"]) == \
            ("t", 1, scan_targets.BEFORE_VERSIONING, False)
        assert "removing" not in entry

    def test_a_removal_it_had_begun_is_a_disabling(self):
        legacy = {k: v for k, v in ITEM.items() if k not in ("lineageId", "version", "createdBy", "disabled")}
        assert scan_targets._entry({**legacy, "removing": True})["disabled"] is True

    def test_the_connection_it_gives_has_the_identity_it_is_registered_under(self):
        entry = {"targetId": "t", "engine": "postgres", "host": "H", "port": "5432", "database": "D"}
        assert source_identity(scan_targets.connection(entry)) == "postgres://h:5432/d"


class TestAVersionsSecurityFieldsAreWrittenOnce:
    """Where a user's password goes is fixed when the version is created. The storage layer
    refuses to change it before reaching the table, whatever a route sends."""

    @pytest.fixture(autouse=True)
    def no_table(self, monkeypatch):
        # A refusal that reached the table would fail differently: there is none to reach.
        monkeypatch.delenv("SCAN_TARGETS_TABLE", raising=False)

    @pytest.mark.parametrize("field", [*scan_targets.SECURITY_FIELDS, "lineageId", "version", "targetId",
                                       "createdBy", "disabled", "Host", "HOST", "tls_certificate"])
    def test_update_refuses_it_by_name(self, field):
        with pytest.raises(ValueError, match=field):
            scan_targets.update("t", {"name": "New name", field: "elsewhere"})

    @pytest.mark.parametrize("body", [{"host": "evil"}, {"Host": "evil"}, {"tls": False},
                                      {"tlsCertificate": "-----BEGIN CERTIFICATE-----"}, {"engine": "oracle"},
                                      {"name": "n", "port": "1"}, {"location": {"host": "evil"}}])
    def test_an_edit_request_naming_it_is_refused_by_name(self, body):
        with pytest.raises(Rejected, match="publish a new version"):
            admin._only(body, admin._SETTINGS_FIELDS, "An edit")

    def test_an_edit_request_of_settings_alone_is_accepted(self):
        body = {"name": "n", "statementTimeoutSeconds": 30, "rowCap": 10, "schedule": None,
                "scannerSecretArn": "", "scannerUsername": "", "scannerPassword": ""}
        assert admin._only(body, admin._SETTINGS_FIELDS, "An edit") == body

    @pytest.mark.parametrize("body", [{"engine": "oracle"}, {"name": "n"}, {"rowCap": 1}, {"tls": True},
                                      {"targetId": "x"}, {"lineageId": "x"}])
    def test_a_new_version_takes_only_where_it_is_its_login_and_its_schedule(self, body):
        with pytest.raises(Rejected):
            admin._only(body, admin._NEW_VERSION_FIELDS, "A new version")


class TestALoginCheckChangesNothingAboutHowAVersionIsReached:
    """A scan or a scanner-login rotation logs in where the version is. If the database now
    presents other TLS, nothing is written: a new version is the admin's to publish."""

    PEM_A = "-----BEGIN CERTIFICATE-----\nAAAA\n-----END CERTIFICATE-----"
    VERSION = {"identity": "oracle://h:2484/s", "version": 3}

    def test_the_same_tls_and_certificate_pass(self):
        admin._require_same_transport({**self.VERSION, "tls": True, "tlsCertificate": self.PEM_A},
                                      {"tls": True, "tlsCertificate": self.PEM_A})

    def test_an_engine_without_a_tls_decision_passes(self):
        admin._require_same_transport(dict(self.VERSION), {})

    @pytest.mark.parametrize("stored,found", [
        ({"tls": True, "tlsCertificate": PEM_A}, {"tls": True, "tlsCertificate": PEM_A.replace("AAAA", "BBBB")}),
        ({"tls": True, "tlsCertificate": PEM_A}, {"tls": True}),
        ({"tls": True}, {"tls": True, "tlsCertificate": PEM_A}),
        ({"tls": True}, {"tls": False}),
        ({"tls": False}, {"tls": True}),
        ({}, {"tls": False}),
    ], ids=["another-certificate", "pin-dropped", "pin-added", "tls-dropped", "tls-added", "never-recorded"])
    def test_anything_else_is_refused_naming_a_new_version(self, stored, found):
        with pytest.raises(Rejected, match="publish a new version"):
            admin._require_same_transport({**self.VERSION, **stored}, found)


class TestWhatUsersAreToldAboutAVersion:

    def _version(self, n, disabled=False):
        return {**ITEM, "targetId": f"t{n}", "lineageId": "t1", "version": n, "name": "Banking", "disabled": disabled}

    def test_new_connections_go_to_the_newest_version_not_disabled(self):
        versions = [self._version(1), self._version(2), self._version(3, disabled=True)]
        assert scan_targets.latest_active(versions)["targetId"] == "t2"

    def test_no_version_in_use_means_none(self):
        assert scan_targets.latest_active([self._version(1, disabled=True)]) is None

    def test_a_disabled_version_with_a_newer_one_says_to_connect_again(self):
        versions = [self._version(1, disabled=True), self._version(2)]
        assert "was updated" in scan_targets.refusal(versions[0], versions)

    def test_a_disabled_version_with_none_newer_says_nothing_replaces_it(self):
        versions = [self._version(1), self._version(2, disabled=True)]
        assert "nothing replaces it" in scan_targets.refusal(versions[1], versions)

    def test_an_unknown_version_is_not_registered(self):
        assert scan_targets.refusal(None, []) == "That database is not registered."


class TestSubjects:

    def test_an_ordinary_subject_is_unchanged(self):
        sub = "0b6f1f0e-3c1a-4c55-9a39-1f0a3c1d2e4f"
        assert get_user_id({"requestContext": {"authorizer": {"jwt": {"claims": {"sub": sub}}}}}) == sub

    @pytest.mark.parametrize("claims", [{}, {"sub": ""}, {"sub": "  "}, {"sub": 5}])
    def test_no_usable_subject_is_refused(self, claims):
        with pytest.raises(UnauthenticatedError):
            get_user_id({"requestContext": {"authorizer": {"jwt": {"claims": claims}}}})


class TestScheduleNames:

    def test_a_schedule_name_is_one_scheduler_accepts_and_one_per_target(self):
        """Two targets sharing a name would share, and overwrite, one schedule."""
        a, b = "0b6f1f0e-3c1a-4c55-9a39-1f0a3c1d2e4f", "7c1e2d3f-0000-4c55-9a39-1f0a3c1d2e4f"
        name = scan_targets.schedule_name(a)
        assert re.fullmatch(r"[0-9a-zA-Z_.-]{1,64}", name), name
        assert scan_targets.schedule_name(a) == name and scan_targets.schedule_name(b) != name


class TestScheduleExpressions:

    @pytest.mark.parametrize("given,expected", [
        ("rate(1 day)", "rate(1 day)"), (" rate(15 minutes) ", "rate(15 minutes)"),
        ("rate(12 hours)", "rate(12 hours)"), ("cron(0 2 * * ? *)", "cron(0 2 * * ? *)"),
        ("cron(0/30 8-17 ? * MON-FRI *)", "cron(0/30 8-17 ? * MON-FRI *)"),
        (None, None), ("", None), ("  ", None),
    ])
    def test_rate_and_cron_are_accepted_and_nothing_means_on_demand(self, given, expected):
        assert admin._schedule_expression(given) == expected

    @pytest.mark.parametrize("given", ["daily", "rate(0 days)", "rate(1 week)", "rate(day)", "cron(0 2 * * ?)",
                                       "at(2026-01-01T00:00:00)", 3, ["rate(1 day)"]])
    def test_anything_else_is_refused_with_an_example(self, given):
        with pytest.raises(Rejected, match="rate|schedule"):
            admin._schedule_expression(given)


class TestTheScannerSecretARequestGives:

    @pytest.fixture(autouse=True)
    def stack(self, monkeypatch):
        monkeypatch.setenv("STACK_NAME", "T")

    SECRET = "arn:aws:secretsmanager:us-east-1:123456789012:secret:"

    def test_an_arn_under_the_scanner_path(self):
        arn = f"{self.SECRET}/T/scanners/dba-scanner-AbCdEf"
        assert admin._secret_choice({"scannerSecretArn": f" {arn} "}, []) == {"arn": arn}

    @pytest.mark.parametrize("name", ["/dba/scanner-AbCdEf", "/T/connections/u/c-AbCdEf",
                                      "/Other/scanners/x-AbCdEf", "/T/scannersX/x-AbCdEf", "T/scanners/x"])
    def test_a_secret_anywhere_else_is_refused(self, name):
        """The scanners may read nothing outside the path, so it would only fail later."""
        with pytest.raises(Rejected, match="must be created under /T/scanners/"):
            admin._secret_choice({"scannerSecretArn": f"{self.SECRET}{name}"}, [])

    def test_a_secret_another_target_uses_is_refused(self):
        """A secret the stack made is deleted with its own target, which would leave this one without one."""
        arn = f"{self.SECRET}/T/scanners/x-AbCdEf"
        with pytest.raises(Rejected, match="Another target uses"):
            admin._secret_choice({"scannerSecretArn": arn}, [{"scannerSecretArn": arn}])

    def test_a_username_and_password(self):
        assert admin._secret_choice({"scannerUsername": " scanner ", "scannerPassword": " p w "}, []) == \
            {"username": "scanner", "password": " p w "}

    @pytest.mark.parametrize("body", [{"scannerUsername": "u"}, {"scannerPassword": "p"},
                                      {"scannerUsername": "u", "scannerPassword": 5}])
    def test_half_a_credential_is_refused(self, body):
        with pytest.raises(Rejected, match="both"):
            admin._secret_choice(body, [])

    def test_both_kinds_at_once_are_refused(self):
        with pytest.raises(Rejected, match="not both"):
            admin._secret_choice({"scannerSecretArn": "arn:aws:secretsmanager:us-east-1:123456789012:secret:s",
                                  "scannerPassword": "p"}, [])

    def test_none_is_none(self):
        assert admin._secret_choice({"scannerUsername": "", "scannerPassword": ""}, []) is None
