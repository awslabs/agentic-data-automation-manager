#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A checkov check a resource is exempt from, and why, written onto the resource itself."""

import jsii
from aws_cdk import CfnResource, IAspect, aws_lambda as lambda_
from constructs import IConstruct

from cdk_infra.shared import cfn_nag

# The handlers CDK adds for custom resources, bucket emptying and website deployment run only
# while CloudFormation deploys; our own functions state their reasons (stage.python_function).
DEPLOY_HANDLER_REASONS = {
    "CKV_AWS_116": ("A CDK-provided handler CloudFormation invokes synchronously during a deploy: the deploy "
                    "receives any failure, and a dead-letter queue applies only to asynchronous invocations."),
    "CKV_AWS_117": "A CDK-provided deploy-time handler: calls only AWS APIs and reaches nothing in the VPC.",
}
# Only where CDK's construct gives no way to set the key: BucketDeployment's handler.
UNKEYED_HANDLER_SETTINGS = ("CDK's BucketDeployment creates this handler and its environment, and exposes no "
                            "setting for the key that encrypts them; the handler is not ours to configure.")


def skip(construct: IConstruct, check_id: str, reason: str) -> None:
    resource = construct if isinstance(construct, CfnResource) else construct.node.default_child
    entry = resource.get_metadata("checkov") or {"skip": []}
    entry["skip"].append({"id": check_id, "comment": reason})
    resource.add_metadata("checkov", entry)


@jsii.implements(IAspect)
class DeployHandlerReasons:
    """Gives every Lambda function without reasons of its own the deploy-handler reasons.
    tests/unit/test_cdk_security.py pins which functions those are, so a new function of ours
    created without stage.python_function fails there instead of being exempted."""

    def visit(self, node: IConstruct) -> None:
        if (isinstance(node, CfnResource) and node.cfn_resource_type == "AWS::Lambda::Function"
                and not node.get_metadata("checkov")):
            for check_id, reason in DEPLOY_HANDLER_REASONS.items():
                skip(node, check_id, reason)
            cfn_nag.skip(node, "W58", cfn_nag.WRITES_LOGS)
            cfn_nag.skip(node, "W89", DEPLOY_HANDLER_REASONS["CKV_AWS_117"])
            if isinstance(node, lambda_.CfnFunction) and node.environment is not None and node.kms_key_arn is None:
                skip(node, "CKV_AWS_173", UNKEYED_HANDLER_SETTINGS)
