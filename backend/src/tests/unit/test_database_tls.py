#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Postgres connections verify the server certificate, or TLS is switched off
explicitly. There is no third, silent option.

pg8000's `ssl_context=True` encrypts but sets CERT_NONE, so anyone on the path
can impersonate the database and collect the credential. These tests use the
real `ssl` module and the bundled RDS CA file.
"""

import ssl
from pathlib import Path

import pytest

from adam.data_source_provider import tls

BUNDLE = Path(tls.__file__).parent / "certs" / "rds-global-bundle.pem"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("DB_TLS_CA_FILE", raising=False)
    monkeypatch.delenv("DB_TLS_DISABLED", raising=False)


class TestPostgresSslContext:

    def test_nothing_configured_is_refused(self):
        with pytest.raises(RuntimeError, match="DB_TLS_CA_FILE"):
            tls.verified_ssl_context()

    def test_a_ca_file_gives_a_context_that_verifies_certificate_and_hostname(self, monkeypatch):
        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        context = tls.verified_ssl_context()
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True
        assert len(context.get_ca_certs()) > 100

    def test_a_missing_ca_file_is_refused_not_ignored(self, monkeypatch):
        monkeypatch.setenv("DB_TLS_CA_FILE", "/nonexistent/ca.pem")
        with pytest.raises(FileNotFoundError):
            tls.verified_ssl_context()

    def test_explicitly_disabling_tls_returns_no_context(self, monkeypatch):
        monkeypatch.setenv("DB_TLS_DISABLED", "true")
        assert tls.verified_ssl_context() is None

    @pytest.mark.parametrize("value", ["false", "1", "yes", "", "TRUE "])
    def test_only_the_exact_word_true_disables_tls(self, monkeypatch, value):
        monkeypatch.setenv("DB_TLS_DISABLED", value)
        with pytest.raises(RuntimeError):
            tls.verified_ssl_context()

    def test_disabled_and_a_ca_file_together_is_a_contradiction(self, monkeypatch):
        monkeypatch.setenv("DB_TLS_DISABLED", "true")
        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        with pytest.raises(RuntimeError, match="Both"):
            tls.verified_ssl_context()


class TestEachProviderConnectsWithVerification:
    """What each provider hands its driver. The driver is the boundary: it is
    replaced by a recorder, and everything before it is the provider's own code."""

    ROW = {"connectionId": "c1", "host": "db.example.com", "port": "5432", "database": "d"}

    def _connect_kwargs(self, monkeypatch, module, attribute, provider_class):
        from adam.data_source_provider.credential_provider import Credential

        calls = []
        monkeypatch.setattr(module, attribute, lambda **kwargs: calls.append(kwargs) or type("Session", (), {})())
        provider_class(dict(self.ROW), Credential("u", "p"), "subject").conn
        (kwargs,) = calls
        return kwargs

    def test_postgres_verifies_certificate_and_hostname(self, monkeypatch):
        import pg8000.native

        from adam.data_source_provider.postgresql_provider import PostgreSQLProvider

        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        context = self._connect_kwargs(monkeypatch, pg8000.native, "Connection", PostgreSQLProvider)["ssl_context"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True

    def test_mysql_verifies_certificate_and_hostname(self, monkeypatch):
        import pymysql

        from adam.data_source_provider.mysql_provider import MySQLProvider

        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        context = self._connect_kwargs(monkeypatch, pymysql, "connect", MySQLProvider)["ssl"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname is True

    def test_redshift_verifies_the_full_chain_and_hostname(self, monkeypatch):
        import redshift_connector

        from adam.data_source_provider.redshift_provider import RedshiftProvider

        kwargs = self._connect_kwargs(monkeypatch, redshift_connector, "connect", RedshiftProvider)
        assert kwargs["ssl"] is True and kwargs["sslmode"] == "verify-full"


class TestOracleUsesTheRegisteredDatabasesTlsSetting:
    """Oracle's TLS is per database: the admin's save decides it (see the probe), every
    connection follows it. The driver is replaced by a recorder; the provider is real."""

    class _Session:
        call_timeout = 0
        description = [("ONE",)]

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, statement):
            pass

        def fetchmany(self, n):
            return [(1,)]

        def rollback(self):
            pass

        def close(self):
            pass

    def _provider(self, monkeypatch, row, answers=None):
        """`answers`: per connect, an error message to raise, or None to connect."""
        import oracledb

        from adam.data_source_provider.credential_provider import Credential
        from adam.data_source_provider.oracle_provider import OracleProvider

        calls, answers = [], list(answers or [])

        def connect(**kwargs):
            calls.append(kwargs)
            error = answers.pop(0) if answers else None
            if error:
                raise oracledb.OperationalError(error)
            return self._Session()
        monkeypatch.setattr(oracledb, "connect", connect)
        return OracleProvider({"connectionId": "c1", "dsn": "db.example.com:2484/svc", **row},
                              Credential("u", "p"), "subject"), calls

    def test_a_tls_database_is_reached_over_verified_tcps(self, monkeypatch):
        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        provider, calls = self._provider(monkeypatch, {"tls": True})
        provider.conn
        (kwargs,) = calls
        assert kwargs["dsn"] == "tcps://db.example.com:2484/svc"
        assert kwargs["ssl_context"].verify_mode == ssl.CERT_REQUIRED and kwargs["ssl_context"].check_hostname

    def test_a_database_without_tls_is_reached_as_registered(self, monkeypatch):
        provider, calls = self._provider(monkeypatch, {"tls": False})
        provider.conn
        assert calls == [{"user": "u", "password": "p", "dsn": "db.example.com:2484/svc"}]

    @pytest.mark.parametrize("row", [{}, {"tls": "false"}, {"tls": None}])
    def test_a_connection_whose_database_was_never_checked_is_refused(self, monkeypatch, row):
        provider, calls = self._provider(monkeypatch, row)
        with pytest.raises(ValueError, match="no TLS setting"):
            provider.conn
        assert calls == []

    def test_tls_turned_off_for_the_function_does_not_become_plaintext(self, monkeypatch):
        monkeypatch.setenv("DB_TLS_DISABLED", "true")
        provider, calls = self._provider(monkeypatch, {"tls": True})
        with pytest.raises(RuntimeError, match="TLS is turned off"):
            provider.conn
        assert calls == []


class TestTheAdminsProbeDecidesOracleTls:

    _provider = TestOracleUsesTheRegisteredDatabasesTlsSetting._provider
    _Session = TestOracleUsesTheRegisteredDatabasesTlsSetting._Session

    def test_tls_is_used_when_the_database_answers_it(self, monkeypatch):
        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        provider, calls = self._provider(monkeypatch, {"discoverTls": True})
        assert provider.probe() == {"ok": True, "tls": True}
        assert [c["dsn"] for c in calls] == ["tcps://db.example.com:2484/svc"]

    @pytest.mark.parametrize("refusal, says", [
        ("DPY-6005: cannot connect to database. [SSL: UNEXPECTED_EOF_WHILE_READING]", "did not answer TLS"),
        ("DPY-6006: The name on the server certificate does not match", "does not name the host"),
    ])
    def test_without_tls_it_connects_as_registered_and_warns_why(self, monkeypatch, refusal, says):
        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        provider, calls = self._provider(monkeypatch, {"discoverTls": True}, answers=[refusal])
        result = provider.probe()
        assert (result["ok"], result["tls"]) == (True, False)
        assert "TLS is not enabled on this database" in result["tlsWarning"] and says in result["tlsWarning"]
        assert [c["dsn"] for c in calls] == ["tcps://db.example.com:2484/svc", "db.example.com:2484/svc"]

    def test_a_login_the_database_refused_over_tls_is_not_retried_without_it(self, monkeypatch):
        """The database answered over TLS, so the login is wrong; plaintext would only send the password again."""
        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        provider, calls = self._provider(monkeypatch, {"discoverTls": True},
                                         answers=["ORA-01017: invalid credential or not authorized"])
        with pytest.raises(RuntimeError, match="ORA-01017"):
            provider.probe()
        assert len(calls) == 1

    def test_a_users_connection_test_decides_nothing(self, monkeypatch):
        """Only the admin's save probes for TLS; a user's test uses the database's setting."""
        provider, calls = self._provider(monkeypatch, {"tls": False})
        assert provider.probe() == {"ok": True}
        assert [c["dsn"] for c in calls] == ["db.example.com:2484/svc"]


class TestADatabaseWithItsOwnCertificate:
    """No CA in the bundle signed it: the admin's save trusts the one the database presents,
    and every connection after trusts that one only."""

    _provider = TestOracleUsesTheRegisteredDatabasesTlsSetting._provider
    _Session = TestOracleUsesTheRegisteredDatabasesTlsSetting._Session
    PEM = "-----BEGIN CERTIFICATE-----" + BUNDLE.read_text().split("-----BEGIN CERTIFICATE-----")[1]

    def test_a_pinned_certificate_is_the_only_one_trusted(self, monkeypatch):
        import hashlib

        from adam.data_source_provider import oracle_provider

        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))  # the bundle's other CAs must not count
        provider, calls = self._provider(monkeypatch, {"tls": True, "tlsCertificate": self.PEM})
        assert provider.conn is not None
        (kwargs,) = calls
        context = kwargs["ssl_context"]
        assert kwargs["dsn"].startswith("tcps://") and context.check_hostname
        trusted = [hashlib.sha256(der).hexdigest() for der in context.get_ca_certs(binary_form=True)]
        assert trusted == [hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.PEM)).hexdigest()]
        # The server must name what the pinned certificate names, wherever a SCAN listener hands on to.
        assert kwargs["ssl_server_dn_match"] is True
        assert kwargs["ssl_server_cert_dn"] == oracle_provider._subject(self.PEM)

    def test_a_certificate_that_cannot_be_read_leaves_the_database_without_tls_and_says_why(self, monkeypatch):
        from adam.data_source_provider import oracle_provider

        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))

        def unreadable(address, timeout):
            raise ConnectionResetError("reset")
        monkeypatch.setattr(oracle_provider.ssl, "get_server_certificate", unreadable)
        provider, calls = self._provider(monkeypatch, {"discoverTls": True}, answers=[
            "DPY-6005: [SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate"])
        result = provider.probe()
        assert result["tls"] is False and "could not be read" in result["tlsWarning"]
        assert [c["dsn"] for c in calls] == ["tcps://db.example.com:2484/svc", "db.example.com:2484/svc"]

    def test_the_save_pins_what_the_database_presents_when_the_bundle_refuses_it(self, monkeypatch):
        from adam.data_source_provider import oracle_provider

        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        asked = []
        monkeypatch.setattr(oracle_provider.ssl, "get_server_certificate",
                            lambda address, timeout: asked.append(address) or self.PEM)
        provider, calls = self._provider(monkeypatch, {"discoverTls": True}, answers=[
            "DPY-6005: cannot connect. [SSL: CERTIFICATE_VERIFY_FAILED] self-signed certificate"])
        result = provider.probe()
        assert asked == [("db.example.com", 2484)]
        assert (result["tls"], result["tlsCertificate"]) == (True, self.PEM)
        assert len(result["tlsFingerprint"].split(":")) == 32
        # Retried trusting exactly what it presented, and nothing from the bundle.
        import hashlib
        assert len(calls) == 2
        trusted = [hashlib.sha256(der).hexdigest() for der in calls[1]["ssl_context"].get_ca_certs(binary_form=True)]
        assert trusted == [hashlib.sha256(ssl.PEM_cert_to_DER_cert(self.PEM)).hexdigest()]

    def test_a_login_refused_over_the_pinned_certificate_is_not_retried_without_tls(self, monkeypatch):
        from adam.data_source_provider import oracle_provider

        monkeypatch.setenv("DB_TLS_CA_FILE", str(BUNDLE))
        monkeypatch.setattr(oracle_provider.ssl, "get_server_certificate", lambda address, timeout: self.PEM)
        provider, calls = self._provider(monkeypatch, {"discoverTls": True}, answers=[
            "DPY-6005: [SSL: CERTIFICATE_VERIFY_FAILED]", "ORA-01017: invalid credential or not authorized"])
        with pytest.raises(RuntimeError, match="ORA-01017"):
            provider.probe()
        assert len(calls) == 2
