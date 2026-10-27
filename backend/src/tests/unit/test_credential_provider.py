#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Credentials come only from the configured CredentialProvider, and engines
build themselves from the connection row without the registry branching on
engine names.
"""

import json

import pytest

from adam.data_source_provider import (
    connection_opening,
    connection_registry,
    credential_provider as cp_module,
)
from adam.data_source_provider.credential_provider import Credential
from adam.data_source_provider.oracle_provider import OracleProvider
from adam.data_source_provider.postgresql_provider import PostgreSQLProvider
from adam.data_source_provider.secrets_manager_credential_provider import (
    SecretsManagerPasswordProvider,
)


class _FakeSecrets:
    def __init__(self, secrets: dict[str, dict]):
        self.secrets = secrets
        self.requested: list[str] = []

    def get_secret_value(self, SecretId):
        self.requested.append(SecretId)
        return {"SecretString": json.dumps(self.secrets[SecretId])}


ORACLE_ROW = {"connectionId": "c-ora", "engine": "oracle", "dsn": "h:1521/svc",
              "secretName": "/stack/connections/u1/c-ora"}
PG_ROW = {"connectionId": "c-pg", "engine": "postgres", "host": "h", "port": "5432",
          "database": "dev", "secretName": "/stack/connections/u1/c-pg"}


class TestSecretsManagerPasswordProvider:
    def test_reads_the_secret_the_row_names(self):
        secrets = _FakeSecrets({ORACLE_ROW["secretName"]: {"username": "alice", "password": "pw"}})
        credential = SecretsManagerPasswordProvider(secrets).credential_for(ORACLE_ROW)
        assert credential == Credential("alice", "pw")
        assert secrets.requested == [ORACLE_ROW["secretName"]]

    def test_row_without_secret_name_is_refused(self):
        with pytest.raises(ValueError, match="secretName"):
            SecretsManagerPasswordProvider(_FakeSecrets({})).credential_for(
                {"connectionId": "c"})

    @pytest.mark.parametrize("payload", [{"username": "alice"}, {"password": "pw"}, {}])
    def test_incomplete_secret_is_refused(self, payload):
        secrets = _FakeSecrets({ORACLE_ROW["secretName"]: payload})
        with pytest.raises(ValueError, match="username or password"):
            SecretsManagerPasswordProvider(secrets).credential_for(ORACLE_ROW)

    def test_password_never_appears_in_repr(self):
        assert "pw" not in repr(Credential("alice", "pw"))


class TestCredentialProviderFromEnv:
    def test_unset_is_refused(self, monkeypatch):
        monkeypatch.delenv("CREDENTIAL_PROVIDER", raising=False)
        with pytest.raises(RuntimeError, match="CREDENTIAL_PROVIDER"):
            cp_module.credential_provider_from_env()

    def test_unknown_is_refused(self, monkeypatch):
        monkeypatch.setenv("CREDENTIAL_PROVIDER", "nope")
        with pytest.raises(RuntimeError, match="no implementation"):
            cp_module.credential_provider_from_env()

    def test_the_unscoped_reader_cannot_be_selected(self, monkeypatch):
        """It reads any secret its role can; a user's provider must read only that user's."""
        monkeypatch.setenv("CREDENTIAL_PROVIDER", "secrets_manager_password")
        with pytest.raises(RuntimeError, match="no implementation"):
            cp_module.credential_provider_from_env()


@pytest.fixture(autouse=True)
def tls_off(monkeypatch):
    """These tests are about credentials, not TLS (see test_database_tls.py)."""
    monkeypatch.setenv("DB_TLS_DISABLED", "true")


class TestRegistryBuildsThroughTheProviders:
    """The registry asks the credential provider, then the engine class. Nothing else."""

    @pytest.fixture
    def credentials(self, monkeypatch):
        issued = []

        class _Recording(cp_module.CredentialProvider):
            def credential_for(self, connection):
                issued.append(connection["connectionId"])
                return Credential(f"user-of-{connection['connectionId']}", "pw")

        monkeypatch.setattr(cp_module, "credential_provider_from_env", lambda: _Recording())
        return issued

    @pytest.mark.parametrize("row,cls", [(ORACLE_ROW, OracleProvider),
                                         (PG_ROW, PostgreSQLProvider)])
    def test_each_engine_is_built_with_the_issued_credential(self, credentials, row, cls):
        provider = connection_opening.build_provider(dict(row), "sub-1")
        assert isinstance(provider, cls)
        assert provider._credential.username == f"user-of-{row['connectionId']}"
        assert credentials == [row["connectionId"]]

    def test_unknown_engine_never_asks_for_a_credential(self, credentials):
        with pytest.raises(connection_registry.ConnectionError_, match="no provider"):
            connection_opening.build_provider({"connectionId": "c", "engine": "db2"}, "sub-1")
        assert credentials == []
