#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A provider reads a connection's secret through a role that can read only that
user's secrets.

The role's policy is keyed on a session tag, so what stops a call from reading
someone else's secret is IAM, not the provider's code. These tests cover the part
that is the provider's: assuming the role tagged with the row's owner, and
refusing to read when it cannot say whose the row is. IAM's refusal itself is not
tested here: test_cdk_security.py checks the role's policy, and IAM policy
simulation against a deployed role shows the refusal.
"""

import json

import pytest

from adam.data_source_provider import credential_provider as cp
from adam.data_source_provider.credential_provider import Credential
from adam.data_source_provider.user_scoped_secrets_credential_provider import (
    UserScopedSecretsManagerPasswordProvider,
)

ROLE = "arn:aws:iam::123456789012:role/T-SecretReader"
ROW = {"connectionId": "c1", "userId": "user-a", "secretName": "/T/connections/user-a/c1"}


class _Sts:
    def __init__(self):
        self.calls = []

    def assume_role(self, **kwargs):
        self.calls.append(kwargs)
        return {"Credentials": {"AccessKeyId": "AKIAEXAMPLEEXAMPLE1", "SecretAccessKey": "s" * 40,
                                "SessionToken": "t" * 20}}


class _Secrets:
    def __init__(self, store):
        self.store, self.reads = store, []

    def get_secret_value(self, SecretId):
        self.reads.append(SecretId)
        return {"SecretString": json.dumps(self.store[SecretId])}


@pytest.fixture
def provider(monkeypatch):
    """The provider, with AWS replaced at boto3.client: `made` records the arguments
    each Secrets Manager client is built with, which is the step that decides whose
    credentials read the secret."""
    import boto3

    monkeypatch.setenv("SECRET_READER_ROLE_ARN", ROLE)
    sts, made = _Sts(), []
    secrets = _Secrets({ROW["secretName"]: {"username": "alice", "password": "pw"}})

    def client(service, **kwargs):
        if service == "sts":
            return sts
        assert service == "secretsmanager", service
        made.append(kwargs)
        return secrets
    monkeypatch.setattr(boto3, "client", client)
    return UserScopedSecretsManagerPasswordProvider(), sts, secrets, made


class TestAssumingTheReaderRole:

    def test_the_role_is_assumed_tagged_with_the_rows_owner(self, provider):
        p, sts, _, _ = provider
        p.credential_for(ROW)
        assert len(sts.calls) == 1
        call = sts.calls[0]
        assert call["RoleArn"] == ROLE
        assert call["Tags"] == [{"Key": "userId", "Value": "user-a"}]

    def test_the_secret_is_read_with_the_assumed_credentials_only(self, provider):
        p, _, secrets, made = provider
        assert p.credential_for(ROW) == Credential("alice", "pw")
        # Built with the assumed role's credentials and nothing else: not the function's own role.
        assert made == [{"aws_access_key_id": "AKIAEXAMPLEEXAMPLE1", "aws_secret_access_key": "s" * 40,
                         "aws_session_token": "t" * 20}]
        assert secrets.reads == [ROW["secretName"]]

    def test_every_call_assumes_afresh_nothing_is_kept_between_calls(self, provider):
        p, sts, _, _ = provider
        p.credential_for(ROW)
        p.credential_for({**ROW, "userId": "user-b", "secretName": ROW["secretName"]})
        assert [c["Tags"][0]["Value"] for c in sts.calls] == ["user-a", "user-b"]

    def test_the_session_is_short(self, provider):
        p, sts, _, _ = provider
        p.credential_for(ROW)
        assert sts.calls[0]["DurationSeconds"] <= 900


class TestItRefusesWhenItCannotSayWhoseRowItIs:

    @pytest.mark.parametrize("row", [
        {"connectionId": "c1", "secretName": "/T/connections/x/c1"},
        {"connectionId": "c1", "userId": "", "secretName": "/T/connections/x/c1"},
    ])
    def test_a_row_without_an_owner_is_not_read(self, provider, row):
        p, sts, secrets, _ = provider
        with pytest.raises(ValueError, match="userId"):
            p.credential_for(row)
        assert sts.calls == [] and secrets.reads == []

    def test_a_row_without_a_secret_name_is_not_read(self, provider):
        p, sts, _, _ = provider
        with pytest.raises(ValueError, match="secretName"):
            p.credential_for({"connectionId": "c1", "userId": "user-a"})
        assert sts.calls == []

    def test_no_reader_role_configured_is_an_error_not_a_direct_read(self, provider, monkeypatch):
        p, sts, secrets, made = provider
        monkeypatch.delenv("SECRET_READER_ROLE_ARN")
        with pytest.raises(RuntimeError, match="SECRET_READER_ROLE_ARN"):
            p.credential_for(ROW)
        assert sts.calls == [] and made == [] and secrets.reads == []


class TestItIsSelectableByName:

    def test_the_stack_selects_it_with_the_environment_variable(self, monkeypatch):
        monkeypatch.setenv("CREDENTIAL_PROVIDER", "secrets_manager_user_scoped")
        assert isinstance(cp.credential_provider_from_env(), UserScopedSecretsManagerPasswordProvider)
