#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Secrets Manager implementation of CredentialProvider.

Reads the username and password from the secret named on the connection row.
The name is taken from the row, never derived from a naming convention: a
guessed path either misses or points at someone else's secret.
"""

import json

from adam.data_source_provider.credential_provider import (
    Credential,
    CredentialProvider,
)


class SecretsManagerPasswordProvider(CredentialProvider):

    def __init__(self, secrets_client=None):
        self._secrets_client = secrets_client

    def _secrets(self):
        # Created per provider instance, and providers are built per invocation.
        if self._secrets_client is None:
            import boto3
            self._secrets_client = boto3.client("secretsmanager")
        return self._secrets_client

    def credential_for(self, connection: dict[str, str]) -> Credential:
        secret_name = connection.get("secretName", "")
        if not secret_name:
            raise ValueError(
                f"Connection {connection.get('connectionId', '?')} has no "
                "secretName; it cannot be opened."
            )
        payload = self._secrets().get_secret_value(SecretId=secret_name)["SecretString"]
        creds = json.loads(payload)
        if not creds.get("username") or not creds.get("password"):
            raise ValueError(f"Secret {secret_name} is missing a username or password.")
        return Credential(username=creds["username"], password=creds["password"])
