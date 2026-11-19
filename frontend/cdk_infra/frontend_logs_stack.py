#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The website's access logs: Adam<Stage>-FrontendLogs, always in us-east-1.

CloudFront's log delivery is set up through CloudWatch Logs in us-east-1 whatever Region the
distribution's stack is in, so this stack sits there, beside the website stack. install_cdk.sh
deploys it after the website stack, with that stack's distribution id.
"""

from aws_cdk import (
    RemovalPolicy,
    Stack,
    aws_iam as iam,
    aws_kms as kms,
    aws_logs as logs,
)
from constructs import Construct

#: The only Region CloudFront's log delivery source can be created in.
LOG_DELIVERY_REGION = "us-east-1"


class FrontendLogsStack(Stack):

    def __init__(
        self, scope: Construct, construct_id: str, *,
        parent_stack_name: str,
        # prod keeps its access logs a year, and with the stack; other stages a month.
        production: bool,
        distribution_id: str,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        if self.region != LOG_DELIVERY_REGION:
            raise ValueError(f"{construct_id} must be in {LOG_DELIVERY_REGION}, where CloudFront's log delivery "
                             f"is set up, not {self.region}.")
        if not distribution_id:
            raise ValueError(f"{construct_id} needs the website stack's distribution id (-c distributionId=...).")
        kept = RemovalPolicy.RETAIN if production else RemovalPolicy.DESTROY

        # Its own key, kept whenever the logs it encrypts are kept, or they could no longer be read.
        key = kms.Key(self, "DataKey", enable_key_rotation=True, removal_policy=kept,
                      description=f"{parent_stack_name}-FrontendLogs: the website's access logs")
        key.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ServicePrincipal(f"logs.{self.region}.amazonaws.com")],
            actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
            resources=["*"],
            conditions={"ArnLike": {"kms:EncryptionContext:aws:logs:arn":
                                    f"arn:aws:logs:{self.region}:{self.account}:log-group:*"}}))

        # Who loaded the site, from where: kept in a log group under this key rather than in a
        # bucket that would need ACLs enabled.
        access_logs = logs.LogGroup(self, "DistributionAccessLogs", encryption_key=key,
            retention=logs.RetentionDays.ONE_YEAR if production else logs.RetentionDays.ONE_MONTH,
            removal_policy=kept)
        log_source = logs.CfnDeliverySource(self, "DistributionLogSource",
            name=f"{parent_stack_name}-cloudfront", log_type="ACCESS_LOGS",
            resource_arn=f"arn:aws:cloudfront::{self.account}:distribution/{distribution_id}")
        log_destination = logs.CfnDeliveryDestination(self, "DistributionLogDestination",
            name=f"{parent_stack_name}-cloudfront-logs", destination_resource_arn=access_logs.log_group_arn,
            output_format="json")
        delivery = logs.CfnDelivery(self, "DistributionLogDelivery",
            delivery_source_name=log_source.name, delivery_destination_arn=log_destination.attr_arn)
        delivery.add_resource_dependency(log_source)
