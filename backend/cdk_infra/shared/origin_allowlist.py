#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The read every browser-facing function needs: the origin allowlist (shared/http_response.py)."""

from aws_cdk import Stack, aws_iam as iam


def origin_allowlist_read(stack: Stack, parent_stack_name: str) -> iam.PolicyStatement:
    parameters = f"arn:aws:ssm:{stack.region}:{stack.account}:parameter/{parent_stack_name}"
    return iam.PolicyStatement(
        actions=["ssm:GetParametersByPath"],
        resources=[parameters, f"{parameters}/*"],
    )
