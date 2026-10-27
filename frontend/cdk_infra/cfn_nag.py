#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A cfn-nag rule a resource is exempt from, and why, written onto the resource itself."""

import jsii
from aws_cdk import Aspects, CfnResource, IAspect, Stack, aws_iam as iam
from constructs import IConstruct

# Every function's role gets one of these from CDK, and with it CloudWatch Logs.
WRITES_LOGS = ("Writes its logs under AWSLambdaBasicExecutionRole (or AWSLambdaVPCAccessExecutionRole, which "
               "includes it), the AWS managed policy CDK attaches; cfn-nag does not read managed policies.")


def skip(construct: IConstruct, rule: str, reason: str) -> None:
    resource = construct if isinstance(construct, CfnResource) else construct.node.default_child
    entry = resource.get_metadata("cfn_nag") or {"rules_to_suppress": []}
    entry["rules_to_suppress"].append({"id": rule, "reason": reason})
    resource.add_metadata("cfn_nag", entry)


def skip_role_policies(role: iam.IRole, rule: str, reason: str) -> None:
    """The same for the policy CDK writes for a role's grants, which has no handle of its own."""
    Aspects.of(Stack.of(role)).add(_RolePolicies(role, rule, reason))


@jsii.implements(IAspect)
class _RolePolicies:

    def __init__(self, role: iam.IRole, rule: str, reason: str):
        self._role, self._rule, self._reason = role, rule, reason

    def visit(self, node: IConstruct) -> None:
        if isinstance(node, iam.CfnPolicy):
            stack = Stack.of(node)
            if stack.resolve(self._role.role_name) in (stack.resolve(node.roles) or []):
                skip(node, self._rule, self._reason)
