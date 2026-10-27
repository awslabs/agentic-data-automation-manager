#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""cdk-nag's AWS Solutions findings that do not apply, acknowledged with the reason on what they cover."""

import aws_cdk as cdk
from cdk_nag import NagPackSuppression, NagSuppressions, RegexAppliesTo
from constructs import IConstruct


def acknowledge(scope: IConstruct, rule: str, reason: str, *, findings: list | None = None) -> None:
    """Acknowledge AwsSolutions-<rule> on scope and everything in it. findings narrows it to those
    findings of the rule, as cdk-nag names them, or a RegexAppliesTo."""
    NagSuppressions.add_resource_suppressions(
        scope, [NagPackSuppression(id=f"AwsSolutions-{rule}", reason=reason, applies_to=findings)],
        apply_to_children=True)


def acknowledge_app_wide(app: cdk.App) -> None:
    """What holds for every stack of the app: here, CDK's BucketDeployment handler."""
    acknowledge(app, "L1", "CDK's BucketDeployment handler runs the runtime CDK sets; the stack has no function "
                "of its own.")
    acknowledge(app, "IAM4", "AWSLambdaBasicExecutionRole is AWS's standard managed policy for a Lambda "
                "function's role, which CDK attaches to its BucketDeployment handler by default.",
                findings=["Policy::arn:<AWS::Partition>:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"])
    acknowledge(app, "IAM5", "CDK's BucketDeployment grants its handler these S3 actions by default, on CDK's "
                "asset bucket (to read the built UI) and on the website bucket (to copy and prune it).",
                findings=[*(f"Action::s3:{a}"
                            for a in ("Abort*", "DeleteObject*", "GetBucket*", "GetObject*", "List*")),
                          RegexAppliesTo(regex=r"/^Resource::<WebsiteBucket[A-Za-z0-9]+\.Arn>\/\*$/"),
                          RegexAppliesTo(regex=r"/^Resource::arn:(<AWS::Partition>|aws[a-z-]*):s3:::"
                                               r"cdk-[a-z0-9]+-assets-[0-9]+-[a-z0-9-]+\/\*$/")])
