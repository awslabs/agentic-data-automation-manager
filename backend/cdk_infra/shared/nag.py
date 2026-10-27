#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""cdk-nag's AWS Solutions findings that do not apply, acknowledged with the reason on what they cover."""

import re

import aws_cdk as cdk
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo
from constructs import IConstruct


def acknowledge(scope: IConstruct, rule: str, reason: str, *, findings: list | None = None) -> None:
    """Acknowledge AwsSolutions-<rule> on scope and everything in it. findings narrows it to those
    findings of the rule, as cdk-nag names them (e.g. "Resource::*"), or a RegexAppliesTo."""
    NagSuppressions.add_resource_suppressions(
        scope, [NagPackSuppression(id=f"AwsSolutions-{rule}", reason=reason, applies_to=findings)],
        apply_to_children=True)


def acknowledge_app_wide(app: cdk.App) -> None:
    """What holds for every stack of the app."""
    acknowledge(app, "L1", "A false positive: our functions run Python 3.13, a current Lambda runtime, not an "
                "old one; cdk-nag flags every runtime but the newest. CDK's own handlers run the runtime CDK sets.")
    acknowledge(app, "IAM4", "AWSLambdaBasicExecutionRole and AWSLambdaVPCAccessExecutionRole are AWS's standard "
                "managed policies for a Lambda function's role, which CDK attaches by default.",
                findings=[f"Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/{policy}"
                          for policy in ("AWSLambdaBasicExecutionRole", "AWSLambdaVPCAccessExecutionRole")])
    acknowledge(app, "IAM5", "CDK's key and queue grants write these action wildcards by default; the resource "
                "they apply to is the stack's own key.",
                findings=["Action::kms:GenerateDataKey*", "Action::kms:ReEncrypt*"])


def arn(service: str, resource: str) -> RegexAppliesTo:
    """The finding for a wildcard ARN of this service and resource, in this stack's Region and account as
    cdk-nag shows them: the values when the stack's environment is set, <AWS::Region> and
    <AWS::AccountId> when it is not."""
    escaped = re.sub(r"([\\^$.|?*+()\[\]{}/])", r"\\\1", resource)
    return RegexAppliesTo(regex=f"/^Resource::arn:aws:{service}:([a-z0-9-]+|<AWS::Region>):"
                                f"([0-9]{{12}}|<AWS::AccountId>):{escaped}$/")


def function_versions(scope: IConstruct) -> None:
    """CDK's grant_invoke covers a function and its versions: "<function ARN>:*", or the same with
    the ARN read from another stack's output."""
    acknowledge(scope, "IAM5", "CDK's grant_invoke grants the function and its versions (:*) by default.",
                findings=[RegexAppliesTo(
                    regex=r'/^Resource::(<[A-Za-z0-9]+\.Arn>|\{"Fn::GetStackOutput":.*\}):\*$/')])


def own_parameters(scope: IConstruct, prefix: str) -> None:
    acknowledge(scope, "IAM5", f"Reads only this deployment's own SSM parameters, under /{prefix}/: non-secret "
                "wiring such as function names and allowed origins.",
                findings=[arn("ssm", f"parameter/{prefix}/*")])


def tagged_providers(scope: IConstruct, prefix: str) -> None:
    acknowledge(scope, "IAM5", f"May invoke only functions tagged mcp-service-provider={prefix}, which the "
                "statement's condition enforces; an ARN cannot name functions by tag.",
                findings=[arn("lambda", "function:*")])
