#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Cognito user pool implementation of IdentityProvider."""

import jwt

from .identity_provider import IdentityProvider, Principal, TokenRefused


class CognitoIdentityProvider(IdentityProvider):

    def __init__(self, *, issuer: str, allowed_client_ids: frozenset[str],
                 jwk_client: jwt.PyJWKClient | None = None):
        if not issuer:
            raise ValueError("issuer is required.")
        if not allowed_client_ids:
            raise ValueError("allowed_client_ids is required; an empty set would accept no one.")
        self._issuer = issuer
        self._allowed_client_ids = allowed_client_ids
        self._jwk_client = jwk_client or jwt.PyJWKClient(f"{issuer}/.well-known/jwks.json")

    def verify(self, token: str) -> Principal:
        try:
            signing_key = self._jwk_client.get_signing_key_from_jwt(token)
            claims = jwt.decode(
                token,
                signing_key.key,
                algorithms=["RS256"],
                issuer=self._issuer,
                # Cognito access tokens carry `client_id`, not `aud`, so the client
                # is checked explicitly below.
                options={"verify_aud": False, "require": ["exp", "iss", "sub"]},
            )
        except jwt.PyJWKClientConnectionError:
            raise  # the issuer's keys could not be fetched: an outage, not a refusal
        except jwt.PyJWTError as e:
            raise TokenRefused(str(e)) from e

        # An access token: what the website and Quick send, and the only kind that carries the
        # gateway's scope. An ID token is for the client that asked for it, not for an API.
        token_use = claims.get("token_use", "")
        if token_use != "access":  # noqa: S105 - a claim value, not a password
            raise TokenRefused(f"Not an access token: token_use is {token_use!r}")

        client_id = claims.get("client_id", "") or claims.get("aud", "")
        if client_id not in self._allowed_client_ids:
            raise TokenRefused(f"Token was issued to client {client_id!r}, which this deployment does not accept.")

        subject = claims.get("sub", "")
        if not subject:
            raise TokenRefused("Verified token carried no sub claim.")
        return Principal(subject=subject, issuer=self._issuer, claims=claims)
