#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Abstract base class for database credential providers.

A credential provider answers one question: what does this connection log in
with? Database providers receive the answer and never fetch it themselves, so
how a credential is stored (a Secrets Manager password today; a short-lived
token later) changes without touching any engine.

Secrets Manager passwords are the first implementation. Another is added by
implementing `credential_for` and registering it in `_PROVIDERS`.
"""

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from importlib import import_module


@dataclass(frozen=True)
class Credential:
    """What one database session logs in with. Never logged or repr'd."""

    username: str
    password: str = field(repr=False)


class CredentialProvider(ABC):

    @abstractmethod
    def credential_for(self, connection: dict[str, str]) -> Credential:
        """Return the credential for one registered connection, or raise."""


# What a user-facing provider may be configured with. The plain Secrets Manager reader
# is not selectable: only the scanner, which has no user, builds it, directly.
_PROVIDERS: dict[str, str] = {
    # Reads through a role limited to the row owner's secrets.
    "secrets_manager_user_scoped": (
        "adam.data_source_provider.user_scoped_secrets_credential_provider"
        ".UserScopedSecretsManagerPasswordProvider"
    ),
}


def credential_provider_from_env() -> CredentialProvider:
    """Build the credential provider the stack configured in CREDENTIAL_PROVIDER."""
    name = os.environ.get("CREDENTIAL_PROVIDER", "").lower()
    if not name:
        raise RuntimeError(
            "Required environment variable CREDENTIAL_PROVIDER is not set. No "
            "database connection can be opened without knowing where its "
            "credential comes from."
        )
    if name not in _PROVIDERS:
        raise RuntimeError(
            f"CREDENTIAL_PROVIDER={name!r} has no implementation. Known: {sorted(_PROVIDERS)}"
        )
    module_path, class_name = _PROVIDERS[name].rsplit(".", 1)
    return getattr(import_module(module_path), class_name)()
