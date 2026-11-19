#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Abstract base class for identity providers.

An identity provider turns the caller's bearer token into a verified Principal.
The gateway interceptor verifies every tool call's token with it, signature included,
and hands the verified subject to every tool. The agent runtime, in a VPC with no route
to the issuer, takes the principal of the token its CUSTOM_JWT authorizer verified.

Cognito is the first implementation. Another IdP is added by implementing
`verify` and registering it in `_PROVIDERS`.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib import import_module

from shared.environment import require_env


@dataclass(frozen=True)
class Principal:
    """A caller whose token signature, issuer, expiry and client were verified."""

    subject: str
    issuer: str
    claims: dict = field(default_factory=dict, repr=False, compare=False)


class TokenRefused(Exception):
    """The token is not one this deployment accepts: forged, expired, another issuer's, another
    client's, or not an access token. Anything else that fails (the issuer's keys cannot be
    fetched) is an outage, raised as itself."""


class IdentityProvider(ABC):

    @abstractmethod
    def verify(self, token: str) -> Principal:
        """Verify a bearer token and return its principal.

        Raises TokenRefused for any token that does not pass. There is no partially-verified result.
        """

    @abstractmethod
    def principal_of_authorized(self, token: str) -> Principal:
        """The principal of a token whose signature an authorizer in front of the caller has
        already verified against this issuer's keys: every check `verify` makes except the
        signature, so no keys are fetched.

        Only for an entry point that nothing reaches without passing that authorizer: the
        agent runtime, behind its CUSTOM_JWT authorizer, which has no route to the issuer.
        Raises TokenRefused as `verify` does.
        """


_PROVIDERS: dict[str, str] = {
    "cognito": "cognito_identity_provider.CognitoIdentityProvider",
}


def identity_provider_from_env() -> IdentityProvider:
    """Build the identity provider the stack configured.

    IDENTITY_PROVIDER names the implementation; OIDC_ISSUER and
    ALLOWED_CLIENT_IDS configure it. All three are required.
    """
    name = require_env("IDENTITY_PROVIDER").lower()
    if name not in _PROVIDERS:
        raise RuntimeError(
            f"IDENTITY_PROVIDER={name!r} has no implementation. Known: {sorted(_PROVIDERS)}"
        )
    module_path, class_name = _PROVIDERS[name].rsplit(".", 1)
    # Relative to this module's package, so it resolves the same way however it is
    # imported (as `agent_provider` in the bundle, or `adam.agent_provider`).
    provider_class = getattr(import_module(f"{__package__}.{module_path}"), class_name)
    allowed = frozenset(
        c.strip() for c in require_env("ALLOWED_CLIENT_IDS").split(",") if c.strip()
    )
    return provider_class(issuer=require_env("OIDC_ISSUER"), allowed_client_ids=allowed)
