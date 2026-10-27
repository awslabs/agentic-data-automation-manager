#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The role a database provider assumes to read one user's connection secret.

The provider functions hold no `secretsmanager:GetSecretValue` of their own. Each
call assumes this role with a `userId` session tag, and the role's policy allows a
read only under `/<stack>/connections/<that userId>/`. IAM, not the provider's
code, then refuses another user's secret. See
data_source_provider/user_scoped_secrets_credential_provider.py.
"""

from aws_cdk import aws_iam as iam, aws_lambda as lambda_
from constructs import Construct

from cdk_infra.shared import nag

# Resolved by IAM from the session tag; deliberately not a CloudFormation substitution.
_USER = "${aws:PrincipalTag/userId}"


def add_secret_reader(scope: Construct, parent_stack_name: str, *,
                      region: str, account: str,
                      functions: list[lambda_.Function]) -> iam.Role:
    """Create the reader role, let `functions` assume it, and point them at it."""
    reader = iam.Role(scope, "SecretReader",
        assumed_by=iam.CompositePrincipal(*[iam.ArnPrincipal(fn.role.role_arn) for fn in functions]),
        description="Reads one user's connection secret, keyed on the userId session tag",
    )
    # A trust policy that names a role must also allow tagging the session, or the
    # tag that carries the user is refused.
    reader.assume_role_policy.add_statements(iam.PolicyStatement(
        actions=["sts:TagSession"],
        principals=[iam.ArnPrincipal(fn.role.role_arn) for fn in functions],
    ))
    reader.add_to_policy(iam.PolicyStatement(
        actions=["secretsmanager:GetSecretValue"],
        resources=[f"arn:aws:secretsmanager:{region}:{account}"
                   f":secret:/{parent_stack_name}/connections/{_USER}/*"],
    ))
    nag.acknowledge(reader, "IAM5", "Reads only the secrets under its session's userId tag, which the provider "
                    "sets to the caller's verified sub: one user's connections, and no one else's.",
                    findings=[nag.arn("secretsmanager",
                                      f"secret:/{parent_stack_name}/connections/<aws:PrincipalTag/userId>/*")])
    for fn in functions:
        fn.add_to_role_policy(iam.PolicyStatement(
            actions=["sts:AssumeRole", "sts:TagSession"],
            resources=[reader.role_arn],
        ))
        fn.add_environment("SECRET_READER_ROLE_ARN", reader.role_arn)
    return reader
