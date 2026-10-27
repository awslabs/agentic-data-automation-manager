#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A Redshift Serverless workgroup for the Redshift provider's integration tests.

Deployed only with -c redshiftTestTarget=true; no part of the application uses
it. Private: reachable on 5439 from the application's security group only, which
is where the provider functions the tests invoke run. The tests seed it through
the Redshift Data API with the admin login in this stack's secret.
"""

from aws_cdk import (
    CfnOutput,
    Fn,
    RemovalPolicy,
    Stack,
    aws_ec2 as ec2,
    aws_kms as kms,
    aws_redshiftserverless as rss,
    aws_secretsmanager as secretsmanager,
)
from constructs import Construct

from cdk_infra.shared import cfn_nag, nag

REDSHIFT_PORT = 5439


class RedshiftTestTargetStack(Stack):

    def __init__(self, scope: Construct, construct_id: str, *,
                 parent_stack_name: str, vpc_id: str, app_sg_id: str, private_subnet_ids: str,
                 availability_zones: list[str], **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        vpc = ec2.Vpc.from_vpc_attributes(self, "Vpc", vpc_id=vpc_id, availability_zones=availability_zones)
        security_group = ec2.SecurityGroup(self, "WorkgroupSG", vpc=vpc, allow_all_outbound=False,
                                           description="Redshift test workgroup: the provider functions only")
        security_group.add_ingress_rule(ec2.Peer.security_group_id(app_sg_id), ec2.Port.tcp(REDSHIFT_PORT),
                                        "Provider functions")
        cfn_nag.skip(security_group, "W29", "A false positive: the only egress rule is CDK's placeholder for a "
                     "group allowed no outbound traffic (ICMP to 255.255.255.255/32), not a usable port range.")

        # Both names are required by the service; the stack's prefix keeps stages apart.
        name = f"{parent_stack_name.lower()}-redshift-test"
        # The Data API reads username and password from a secret; CloudFormation does
        # not return the ARN of the one Redshift can manage itself, so the stack makes its own.
        admin_key = kms.Key(self, "AdminSecretKey", enable_key_rotation=True, removal_policy=RemovalPolicy.DESTROY,
                            description=f"{name}: the admin secret")
        admin = secretsmanager.Secret(self, "AdminSecret",
            encryption_key=admin_key,
            generate_secret_string=secretsmanager.SecretStringGenerator(
                secret_string_template='{"username": "testadmin"}',  # noqa: S106 - a username; the password is generated
                generate_string_key="password",
                exclude_punctuation=True, require_each_included_type=True, password_length=32),
            removal_policy=RemovalPolicy.DESTROY,
        )
        nag.acknowledge(admin, "SMG4", "The admin login of a test target, deployed only with REDSHIFT_TEST_TARGET for "
                        "the deployed tests. Redshift rotates the secret it manages itself, but CloudFormation does "
                        "not return that secret's ARN, so the stack keeps this one.")
        namespace = rss.CfnNamespace(self, "Namespace",
            namespace_name=name,
            db_name="dev",
            admin_username="testadmin",
            admin_user_password=admin.secret_value_from_json("password").unsafe_unwrap(),
        )
        namespace.apply_removal_policy(RemovalPolicy.DESTROY)

        workgroup = rss.CfnWorkgroup(self, "Workgroup",
            workgroup_name=name,
            namespace_name=namespace.namespace_name,
            base_capacity=4,
            publicly_accessible=False,
            enhanced_vpc_routing=False,
            subnet_ids=Fn.split(",", private_subnet_ids),
            security_group_ids=[security_group.security_group_id],
            port=REDSHIFT_PORT,
        )
        workgroup.add_dependency(namespace)
        workgroup.apply_removal_policy(RemovalPolicy.DESTROY)

        CfnOutput(self, "WorkgroupName", value=workgroup.workgroup_name)
        CfnOutput(self, "Endpoint", value=workgroup.attr_workgroup_endpoint_address)
        CfnOutput(self, "AdminSecretArn", value=admin.secret_arn)
