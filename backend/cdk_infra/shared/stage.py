#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What the stage changes: prod keeps its data and its logs; any other stage tears down clean.

prod turns on deletion protection on the tables that hold users' data and on the user pool,
keeps those, the logs and each stack's key when a stack is deleted, and keeps logs for a year.
Any other stage keeps logs a month, deletes everything with its stack, and accepts sign-in from
the local frontend dev server. Every stage keeps point-in-time recovery on its tables, and each
stack encrypts what it stores (tables, logs, function settings) with a key of its own.
"""

from dataclasses import dataclass

from aws_cdk import (
    Duration,
    RemovalPolicy,
    Stack,
    aws_dynamodb as dynamodb,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_logs as logs,
    aws_sqs as sqs,
)
from constructs import Construct

from cdk_infra.shared import cfn_nag, checkov, nag

PRODUCTION_STAGE = "prod"

# Why a function has no dead-letter queue of its own (checkov CKV_AWS_116).
SYNCHRONOUS = ("Invoked synchronously by {}: the caller receives any failure, and a dead-letter queue "
               "applies only to asynchronous invocations.")
ASYNC_DESTINATION = ("Asynchronous: failures go to an SQS on-failure destination and the rule's dead-letter "
                     "queue, which supersede DeadLetterConfig, the only setting checkov looks for.")
STREAM_DLQ = "Invoked by its DynamoDB stream, whose failed batches go to the event source's SQS dead-letter queue."
# Sign-in callback for a frontend dev server on this machine; never a prod callback.
LOCAL_FRONTEND_URL = "http://localhost:5173/"


@dataclass(frozen=True)
class Stage:
    name: str

    @property
    def prefix(self) -> str:
        """Adam<Stage>: the stacks' names and the start of every SSM path and
        resource name. Only the first letter is raised, as install_cdk.sh does, so "devAlice"
        names the same stacks in both."""
        return f"Adam{self.name[:1].upper()}{self.name[1:]}"

    @property
    def production(self) -> bool:
        return self.name == PRODUCTION_STAGE

    @property
    def data_removal_policy(self) -> RemovalPolicy:
        """For what holds users' data: tables, the user pool, logs."""
        return RemovalPolicy.RETAIN if self.production else RemovalPolicy.DESTROY

    @property
    def log_retention(self) -> logs.RetentionDays:
        return logs.RetentionDays.ONE_YEAR if self.production else logs.RetentionDays.ONE_MONTH

    @property
    def log_retention_days(self) -> int:
        """The same period in days, for logs kept outside CloudWatch (an S3 lifecycle rule)."""
        return 365 if self.production else 30

    @property
    def local_frontend_urls(self) -> list[str]:
        return [] if self.production else [LOCAL_FRONTEND_URL]

    def data_key(self, scope: Construct) -> kms.Key:
        """The stack's own key for what it stores: one per stack, so each service holds the key to
        its data. CloudWatch Logs may use it only for this account's log groups in this region."""
        stack = Stack.of(scope)
        existing = stack.node.try_find_child("DataKey")
        if existing is not None:
            return existing
        key = kms.Key(stack, "DataKey", enable_key_rotation=True, removal_policy=self.data_removal_policy,
                      description=f"{stack.stack_name}: tables, logs and function settings")
        key.add_to_resource_policy(iam.PolicyStatement(
            principals=[iam.ServicePrincipal(f"logs.{stack.region}.amazonaws.com")],
            actions=["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"],
            resources=["*"],
            conditions={"ArnLike": {"kms:EncryptionContext:aws:logs:arn":
                                    f"arn:aws:logs:{stack.region}:{stack.account}:log-group:*"}}))
        return key

    def failure_queue(self, scope: Construct, construct_id: str, *, from_event_rules: bool = False) -> sqs.Queue:
        """A dead-letter queue under the stack's key: what lands there is users' requests and events.
        EventBridge writes a rule's dead letters itself, so from_event_rules lets it use the key."""
        key = self.data_key(scope)
        if from_event_rules:
            stack = Stack.of(scope)
            key.add_to_resource_policy(iam.PolicyStatement(
                principals=[iam.ServicePrincipal("events.amazonaws.com")],
                actions=["kms:GenerateDataKey", "kms:Decrypt"], resources=["*"],
                conditions={"StringEquals": {"aws:SourceAccount": stack.account}}))
        queue = sqs.Queue(scope, construct_id, encryption=sqs.QueueEncryption.KMS, encryption_master_key=key,
                          enforce_ssl=True, retention_period=Duration.days(14))
        nag.acknowledge(queue, "SQS3", "A false positive: this queue is itself the dead-letter queue.")
        return queue

    def table_settings(self, scope: Construct) -> dict:
        """Keyword arguments for a dynamodb.Table that holds users' data."""
        return {
            "removal_policy": self.data_removal_policy,
            "deletion_protection": self.production,
            "point_in_time_recovery_specification": dynamodb.PointInTimeRecoverySpecification(
                point_in_time_recovery_enabled=True),
            "encryption": dynamodb.TableEncryption.CUSTOMER_MANAGED,
            "encryption_key": self.data_key(scope),
        }

    def log_group(self, scope: Construct, construct_id: str) -> logs.LogGroup:
        return logs.LogGroup(scope, construct_id, encryption_key=self.data_key(scope),
                             retention=self.log_retention, removal_policy=self.data_removal_policy)

    def python_function(self, scope: Construct, construct_id: str, *, failures: str,
                        outside_vpc: str | None = None, **kwargs) -> lambda_.Function:
        """A Python 3.13 ARM function writing to a log group of its own, kept as the stage says,
        with its settings encrypted under the stack's key.

        failures: how it is invoked and where a failure goes, which is why it has no
        dead-letter queue of its own. outside_vpc: why it needs no VPC, required for a
        function not placed in one."""
        if ("vpc" in kwargs) == (outside_vpc is not None):
            raise ValueError(f"{construct_id}: give a VPC or the reason it needs none, not both.")
        key = self.data_key(scope)
        fn = lambda_.Function(scope, construct_id,
                              runtime=lambda_.Runtime.PYTHON_3_13,
                              architecture=lambda_.Architecture.ARM_64,
                              log_group=self.log_group(scope, f"{construct_id}Logs"),
                              environment_encryption=key,
                              **kwargs)
        key.grant_decrypt(fn)
        checkov.skip(fn, "CKV_AWS_116", failures)
        cfn_nag.skip(fn, "W58", cfn_nag.WRITES_LOGS)
        if outside_vpc:
            checkov.skip(fn, "CKV_AWS_117", outside_vpc)
            cfn_nag.skip(fn, "W89", outside_vpc)
        return fn
