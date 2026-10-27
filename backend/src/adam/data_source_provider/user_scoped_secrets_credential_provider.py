#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Secrets Manager credentials read through a role that can read only one user's secrets.

The provider function holds no secret-read permission of its own. For each call it
assumes the stack's reader role, tagging the session with the userId of the row it
is opening. The role's policy allows `secretsmanager:GetSecretValue` only under
`/<stack>/connections/${aws:PrincipalTag/userId}/*`, so a row that names another
user's secret fails inside IAM, whatever this code does. That contains a bug here
(a wrong id, a path built from the wrong row); it does not defend against code
that is already running in the function and can tag its own session.

Nothing is kept between calls: the role is assumed afresh, and its short-lived
credentials are used for one read.
"""

import json

from adam.data_source_provider.credential_provider import Credential, CredentialProvider
from adam.shared.environment import require_env

# Long enough for one read; the minimum STS allows.
SESSION_SECONDS = 900


class UserScopedSecretsManagerPasswordProvider(CredentialProvider):

    def _sts(self):
        import boto3
        return boto3.client("sts")

    def _secrets_for(self, credentials: dict):
        import boto3
        return boto3.client(
            "secretsmanager",
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
        )

    def credential_for(self, connection: dict[str, str]) -> Credential:

        connection_id = connection.get("connectionId", "?")
        secret_name = connection.get("secretName", "")
        if not secret_name:
            raise ValueError(f"Connection {connection_id} has no secretName; it cannot be opened.")
        user_id = connection.get("userId", "")
        if not user_id:
            raise ValueError(
                f"Connection {connection_id} has no userId, so it cannot be said whose secret "
                "to read; refusing to read it."
            )
        # A user-scoped credential provider never reads a secret without the reader role.
        assumed = self._sts().assume_role(
            RoleArn=require_env("SECRET_READER_ROLE_ARN"),
            RoleSessionName=f"secret-reader-{connection_id}"[:64],
            DurationSeconds=SESSION_SECONDS,
            Tags=[{"Key": "userId", "Value": user_id}],
        )["Credentials"]
        payload = self._secrets_for(assumed).get_secret_value(SecretId=secret_name)["SecretString"]
        creds = json.loads(payload)
        if not creds.get("username") or not creds.get("password"):
            raise ValueError(f"Secret {secret_name} is missing a username or password.")
        return Credential(username=creds["username"], password=creds["password"])
