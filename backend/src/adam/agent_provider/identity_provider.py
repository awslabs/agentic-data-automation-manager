#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Abstract base class for identity providers.

An identity provider turns the caller's bearer token into a verified Principal.
The gateway interceptor is the only caller: it is the one component that sees
the token, and it hands the verified subject to every tool.

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
