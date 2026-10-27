#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""CognitoIdentityProvider accepts only tokens signed by its issuer, for its clients.

Tokens are signed with a locally generated RSA key; the JWKS fetch is replaced
by a stub that returns that key, so every check below runs the real PyJWT
verification.
"""

import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_provider import identity_provider as idp_module
from agent_provider.cognito_identity_provider import CognitoIdentityProvider
from agent_provider.identity_provider import TokenRefused

ISSUER = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"
CLIENT = "client-a"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


SIGNING_KEY = _key()
OTHER_KEY = _key()


class _StubJwkClient:
    """Returns the trusted public key for any token, as PyJWKClient would."""

    def get_signing_key_from_jwt(self, token):
        return type("K", (), {"key": SIGNING_KEY.public_key()})()


def _token(*, key=SIGNING_KEY, **overrides):
    claims = {
        "sub": "user-1",
        "iss": ISSUER,
        "exp": int(time.time()) + 300,
        "token_use": "access",
        "client_id": CLIENT,
        **overrides,
    }
    claims = {k: v for k, v in claims.items() if v is not None}
    return jwt.encode(claims, key, algorithm="RS256")


@pytest.fixture
def provider():
    return CognitoIdentityProvider(
        issuer=ISSUER, allowed_client_ids=frozenset({CLIENT}), jwk_client=_StubJwkClient(),
    )


class TestVerify:
    def test_valid_token_yields_its_subject(self, provider):
        principal = provider.verify(_token())
        assert principal.subject == "user-1"
        assert principal.issuer == ISSUER

    def test_signature_from_another_key_is_refused(self, provider):
        with pytest.raises(TokenRefused) as refused:
            provider.verify(_token(key=OTHER_KEY))
        assert isinstance(refused.value.__cause__, jwt.InvalidSignatureError)

    def test_other_issuer_is_refused(self, provider):
        with pytest.raises(TokenRefused) as refused:
            provider.verify(_token(iss="https://evil.example.com"))
        assert isinstance(refused.value.__cause__, jwt.InvalidIssuerError)

    def test_expired_token_is_refused(self, provider):
        with pytest.raises(TokenRefused) as refused:
            provider.verify(_token(exp=int(time.time()) - 60))
        assert isinstance(refused.value.__cause__, jwt.ExpiredSignatureError)

    def test_client_not_allowed_is_refused(self, provider):
        with pytest.raises(TokenRefused, match="client"):
            provider.verify(_token(client_id="client-b"))

    def test_unexpected_token_use_is_refused(self, provider):
        with pytest.raises(TokenRefused, match="token_use"):
            provider.verify(_token(token_use="refresh"))

    def test_missing_sub_is_refused(self, provider):
        with pytest.raises(TokenRefused) as refused:
            provider.verify(_token(sub=None))
        assert isinstance(refused.value.__cause__, jwt.MissingRequiredClaimError)


class TestConstruction:
    def test_empty_client_set_is_refused(self):
        with pytest.raises(ValueError, match="allowed_client_ids"):
            CognitoIdentityProvider(issuer=ISSUER, allowed_client_ids=frozenset(),
                                    jwk_client=_StubJwkClient())


class TestFromEnv:
    def test_builds_the_named_provider(self, monkeypatch):
        monkeypatch.setenv("IDENTITY_PROVIDER", "cognito")
        monkeypatch.setenv("OIDC_ISSUER", ISSUER)
        monkeypatch.setenv("ALLOWED_CLIENT_IDS", "client-a, client-b")
        provider = idp_module.identity_provider_from_env()
        assert isinstance(provider, CognitoIdentityProvider)
        assert provider._allowed_client_ids == frozenset({"client-a", "client-b"})

    @pytest.mark.parametrize("missing", ["IDENTITY_PROVIDER", "OIDC_ISSUER", "ALLOWED_CLIENT_IDS"])
    def test_each_setting_is_required(self, monkeypatch, missing):
        for name, value in (("IDENTITY_PROVIDER", "cognito"), ("OIDC_ISSUER", ISSUER),
                            ("ALLOWED_CLIENT_IDS", CLIENT)):
            monkeypatch.setenv(name, value)
        monkeypatch.delenv(missing)
        with pytest.raises(RuntimeError, match=missing):
            idp_module.identity_provider_from_env()

    def test_unknown_provider_is_refused(self, monkeypatch):
        monkeypatch.setenv("IDENTITY_PROVIDER", "nope")
        monkeypatch.setenv("OIDC_ISSUER", ISSUER)
        monkeypatch.setenv("ALLOWED_CLIENT_IDS", CLIENT)
        with pytest.raises(RuntimeError, match="no implementation"):
            idp_module.identity_provider_from_env()
