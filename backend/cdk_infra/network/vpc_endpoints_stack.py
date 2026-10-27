#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Interface VPC endpoints the functions in the VPC need, for a VPC without internet egress.

Independent stack. A Lambda in a VPC has no public IP, so only a NAT gateway or an interface
endpoint gives it a way to AWS services. Each endpoint the functions need is declared here
(reused when the VPC already has one), so a new account needs nothing made by hand.

Not included: cognito-idp. With private DNS it captures the Cognito API, which refuses
PrivateLink for any user pool with a hosted-UI domain ("PrivateLink access is disabled for
the user pool that has ManagedLogin configured"). Nothing in the VPC calls Cognito; a
function that needed to would need a NAT gateway.
"""

from aws_cdk import (
    Stack,
    Tags,
    aws_ec2 as ec2,
)
from constructs import Construct

from cdk_infra.network.vpc_endpoint_audit import CREATED_BY_TAG
from cdk_infra.shared import cfn_nag
from cdk_infra.shared.imported_vpc import import_vpc

#: Every interface endpoint the functions in the VPC need: short name → construct id label.
#: Each deploy checks the VPC for each one: an existing endpoint is reused, a missing
#: one is created, and one that cannot serve stops the deploy (vpc_endpoint_audit.py).
#: A test checks that every AWS client an in-VPC function makes is listed here.
#: InterfaceVpcEndpointAwsService expands a short name to
#: "com.amazonaws.<region>.<name>"; the plain InterfaceVpcEndpointService class
#: does not, so a short name there creates an endpoint for a service that does not exist.
REQUIRED_INTERFACE_SERVICES = {
    # AgentCore data plane.
    "bedrock-agentcore": "AgentCoreDataPlane",
    # AgentCore Gateway ingress -- a separate endpoint service from the data
    # plane, per the AgentCore PrivateLink guide.
    "bedrock-agentcore.gateway": "AgentCoreGateway",
    # Model invocation.
    "bedrock-runtime": "BedrockRuntime",
    # The admin routes count each database's tables in the schema index.
    "glue": "Glue",
    # The scanner functions answer scan requests on the scan bus.
    "events": "EventBridge",
    # Provider functions read users' secrets through an assumed role.
    "secretsmanager": "SecretsManager",  # pragma: allowlist secret
    "sts": "Sts",
    # Functions read their configuration from Parameter Store.
    "ssm": "Ssm",
    # The admin routes and the API function invoke other functions.
    "lambda": "Lambda",
    # The admin routes create, read and delete the scan schedules.
    "scheduler": "Scheduler",
}

#: Added only when the chat runtime is deployed (enableChat): AgentCore pulls the
#: runtime's image from ECR and ships its logs from inside the VPC. Its image layers
#: come from S3, through the gateway endpoint that is a prerequisite anyway.
RUNTIME_INTERFACE_SERVICES = {
    "ecr.api": "EcrApi",
    "ecr.dkr": "EcrDocker",
    "logs": "Logs",
}
_LABELS = {**REQUIRED_INTERFACE_SERVICES, **RUNTIME_INTERFACE_SERVICES}

#: Not interface endpoints, and why: what the functions in the VPC need from each.
#: DynamoDB and S3 use gateway endpoints on the subnets' route table (a prerequisite);
#: DynamoDB's interface endpoint has no private DNS name, so DYNAMODB_ENDPOINT_URL names
#: one when the VPC uses it. cognito-idp is harmful: with private DNS it captures the
#: regional hostname, and Cognito refuses PrivateLink for pools with a hosted-UI domain.
NOT_INTERFACE_ENDPOINTS = ("dynamodb", "s3", "cognito-idp")


# CDK's default group for an interface endpoint: 443 in from the VPC's range, all traffic out.
SHARED_ENDPOINT = ("The endpoint is in the customer's VPC and may be shared by every deployment and workload "
                   "there, so its security group admits the VPC's range rather than one deployment's group.")

class VpcEndpointsStack(Stack):
    """Endpoint ENIs added to an imported, externally-owned VPC, for the services
    it has no endpoint for. Each is tagged createdby=<parent stack name>, which
    tells the ones this deployment added from the ones that were there before.
    """

    def __init__(
        self, scope: Construct, construct_id: str, *,
        parent_stack_name: str,
        vpc_id: str,
        vpc_cidr_block: str,
        private_subnet_ids: str,
        availability_zones: list[str],
        services: list[str],
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        unknown = [s for s in services if s not in _LABELS]
        if unknown:
            raise ValueError(f"Not endpoints this stack creates: {', '.join(unknown)}.")

        # The CIDR lets the endpoints' default security group allow the VPC: a fact
        # about the imported VPC, not a choice.
        vpc = import_vpc(self, vpc_id=vpc_id, availability_zones=availability_zones,
                         private_subnet_ids=private_subnet_ids, vpc_cidr_block=vpc_cidr_block)

        self.endpoints: dict[str, ec2.InterfaceVpcEndpoint] = {}
        for service in services:
            label = _LABELS[service]
            self.endpoints[service] = ec2.InterfaceVpcEndpoint(self, f"{label}Endpoint",
                vpc=vpc,
                service=ec2.InterfaceVpcEndpointAwsService(service),
                # By type, never by id: an interface in every private subnet the functions use.
                subnets=ec2.SubnetSelection(
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                ),
                # Private DNS is on by default: without it the regional hostname still resolves
                # to the public address, and the endpoint would change nothing.
            )
            Tags.of(self.endpoints[service]).add(CREATED_BY_TAG, parent_stack_name)
            for security_group in self.endpoints[service].connections.security_groups:
                for rule in ("W5", "W40", "W9"):
                    cfn_nag.skip(security_group, rule, SHARED_ENDPOINT)

