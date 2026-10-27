#!/usr/bin/env python3
#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""CDK app: every stack for one stage, deployed into an existing VPC with your
own databases. install_cdk.sh supplies the network from deploy.env:

    cdk deploy --all -c stage=dev \
        -c vpcId=vpc-... -c appSgId=sg-... -c privateSubnetIds=subnet-a,subnet-b \
        -c availabilityZones=us-east-1b,us-east-1c -c vpcCidrBlock=10.0.0.0/16

It builds no VPC and no database. It adds the interface endpoints the functions
need to get out of a VPC without a NAT, for the services the VPC has no endpoint
for (it reads the VPC's endpoints with your credentials), and one database provider stack for
whichever databases your users register. All five network values are required,
and availabilityZones must list one zone per subnet, in the same order, because
the two lists are paired by position.

The region comes from CDK_DEFAULT_REGION (the cdk CLI sets it from your profile).
Optional: -c dynamodbEndpointUrl=<VPC endpoint host> for a VPC whose DynamoDB
traffic goes through an interface endpoint, -c enableChat=false to leave out the
agent runtime behind the web console's chat page (on by default; Quick doesn't
need it), and
-c redshiftTestTarget=true for the Redshift Serverless workgroup the Redshift
provider's integration tests run against (the application never uses it).
"""

import os
import sys

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from cdk_infra.agent_provider.agent_provider_stack import AgentProviderStack
from cdk_infra.auth_provider.auth_provider_stack import AuthProviderStack
from cdk_infra.data_source_provider.data_source_provider_stack import DataSourceProviderStack
from cdk_infra.glue_catalog_provider.glue_catalog_provider_stack import GlueCatalogProviderStack
from cdk_infra.network.vpc_endpoint_audit import ENDPOINTS_STACK_SUFFIX, describe_interface_endpoints, plan_endpoints
from cdk_infra.network.vpc_endpoints_stack import (
    REQUIRED_INTERFACE_SERVICES,
    RUNTIME_INTERFACE_SERVICES,
    VpcEndpointsStack,
)
from cdk_infra.shared import nag
from cdk_infra.shared.stage import Stage
from cdk_infra.test_targets.redshift_test_target_stack import RedshiftTestTargetStack

app = cdk.App()

# install_cdk.sh passes the stage (STAGE there). "prod" keeps data and logs; see cdk_infra/shared/stage.py.
# Required: install_cdk.sh passes it. A stage guessed here could build against another one.
STAGE = app.node.try_get_context("stage")
if not STAGE:
    raise ValueError("No stage given: deploy with install_cdk.sh, or pass -c stage=<stage>.")
stage = Stage(STAGE)
PREFIX = stage.prefix

REGION = os.getenv("CDK_DEFAULT_REGION")
if not REGION:
    raise ValueError(
        "CDK_DEFAULT_REGION is not set. Run through the cdk CLI with a profile or "
        "AWS_REGION set (it exports CDK_DEFAULT_REGION), or export it yourself."
    )
env = cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"), region=REGION)

DYNAMODB_ENDPOINT_URL = app.node.try_get_context("dynamodbEndpointUrl") or ""


def _flag(name: str, default: str = "false") -> bool:
    """A true/false context value. Anything else is refused, so a typo cannot quietly mean false."""
    value = str(app.node.try_get_context(name) or default).lower()
    if value not in ("true", "false"):
        raise ValueError(f"{name} must be true or false, got {value!r}.")
    return value == "true"


# On unless turned off: the website's Chat page needs the agent runtime; Quick does not.
ENABLE_CHAT = _flag("enableChat", default="true")
REDSHIFT_TEST_TARGET = _flag("redshiftTestTarget")
# Off unless turned on: web_search sends the agent's query to a web search outside the VPC.
ENABLE_WEB_SEARCH = _flag("enableWebSearch")

# ── Network: an existing VPC, described in context ───────────────────────
NETWORK_KEYS = ("vpcId", "appSgId", "privateSubnetIds", "availabilityZones", "vpcCidrBlock")
given = {k: app.node.try_get_context(k) for k in NETWORK_KEYS if app.node.try_get_context(k)}
missing = [k for k in NETWORK_KEYS if k not in given]
if missing:
    raise ValueError(
        f"The stack deploys into an existing VPC and needs all of {', '.join(NETWORK_KEYS)}; "
        f"missing {', '.join(missing)}. install_cdk.sh reads them from deploy.env."
    )
AVAILABILITY_ZONES = [az.strip() for az in given["availabilityZones"].split(",")]
subnets = given["privateSubnetIds"].split(",")
if len(subnets) != len(AVAILABILITY_ZONES):
    raise ValueError(
        f"availabilityZones lists {len(AVAILABILITY_ZONES)} zone(s) for "
        f"{len(subnets)} subnet(s) in privateSubnetIds. They are paired by position, "
        "so list one zone per subnet, in the same order."
    )
# Subnet and zone lists are positional partners; a wrong pairing places network
# interfaces in the wrong zone while synthesising cleanly, so neither is guessed.
network_args = {
    "vpc_id": given["vpcId"],
    "app_sg_id": given["appSgId"],
    "private_subnet_ids": given["privateSubnetIds"],
    "availability_zones": AVAILABILITY_ZONES,
}
# Every interface endpoint the functions in the VPC need: each existing one is
# reused, each missing one is created, and one that cannot serve stops synth.
endpoint_plan = plan_endpoints([*REQUIRED_INTERFACE_SERVICES, *(RUNTIME_INTERFACE_SERVICES if ENABLE_CHAT else ())],
                               describe_interface_endpoints(given["vpcId"], REGION),
                               region=REGION, created_by=PREFIX)
print(endpoint_plan.summary(), file=sys.stderr)
if endpoint_plan.create:
    VpcEndpointsStack(app, f"{PREFIX}{ENDPOINTS_STACK_SUFFIX}",
        parent_stack_name=PREFIX,
        vpc_id=given["vpcId"],
        vpc_cidr_block=given["vpcCidrBlock"],
        private_subnet_ids=given["privateSubnetIds"],
        availability_zones=AVAILABILITY_ZONES,
        services=endpoint_plan.create,
        env=env,
    )

_vpc_args = dict(parent_stack_name=PREFIX, env=env, **network_args)

# Each stack owns its own data. Resources one stack needs from another are passed
# in here; services reach each other at run time through SSM-named functions.

# ── Auth (Cognito) ───────────────────────────────────────────────────────
# The website's URL, once it is deployed: the web client's sign-in callback. install_cdk.sh
# passes it from the website stack's outputs; a first install has no website yet.
WEBSITE_URL = app.node.try_get_context("websiteUrl") or ""
auth_stack = AuthProviderStack(app, f"{PREFIX}-Auth", parent_stack_name=PREFIX, stage=stage,
                               website_url=WEBSITE_URL, env=env)

# ── Schema service: the index, scan targets, ingestion and the admin routes ──
glue_stack = GlueCatalogProviderStack(app, f"{PREFIX}-GlueCatalog", **_vpc_args, stage=stage,
    dynamodb_endpoint_url=DYNAMODB_ENDPOINT_URL,
)

# ── Database provider service: engine providers, the connection registry and ──
# ── the schema scanners, which register on the schema service's scan bus ─────
database_stack = DataSourceProviderStack(app, f"{PREFIX}-DataSourceProviders",
    **_vpc_args,
    stage=stage,
    dynamodb_endpoint_url=DYNAMODB_ENDPOINT_URL,
    scan_bus=glue_stack.scan_bus,
    schema_admin_function=glue_stack.admin_function,
    schema_ingestion_function=glue_stack.ingestion_function,
    schema_function=glue_stack.function,
    user_pool_id=auth_stack.user_pool.user_pool_id,
)

# ── Agent (Runtime + Gateway + MCP + HTTP API + AppSync) ─────────────────
# web_search is Amazon Nova web grounding, which is only offered through US
# cross-region inference profiles.
WEB_SEARCH_MODEL_ID = "us.amazon.nova-2-lite-v1:0"

AgentProviderStack(app, f"{PREFIX}-Agent",
    **_vpc_args,
    stage=stage,
    dynamodb_endpoint_url=DYNAMODB_ENDPOINT_URL,
    identity_provider="cognito",
    oidc_issuer_url=f"https://cognito-idp.{REGION}.amazonaws.com/{auth_stack.user_pool.user_pool_id}",
    allowed_client_ids=[auth_stack.user_pool_client.user_pool_client_id,
                        auth_stack.quick_client.user_pool_client_id],
    frontend_client_id=auth_stack.user_pool_client.user_pool_client_id,
    user_pool_id=auth_stack.user_pool.user_pool_id,
    web_search_model_id=WEB_SEARCH_MODEL_ID if ENABLE_WEB_SEARCH else None,
    connections_api_function=database_stack.connections_api_function,
    admin_function=glue_stack.admin_function,
    enable_chat=ENABLE_CHAT,
)

# ── Test targets, deployed only when asked for ───────────────────────────
if REDSHIFT_TEST_TARGET:
    RedshiftTestTargetStack(app, f"{PREFIX}-RedshiftTestTarget", **_vpc_args)

# The website is its own CDK app, in frontend/, deployed from these stacks' outputs.

# Every stack is checked against the AWS Solutions rules on synth; an unresolved finding fails it.
cdk.Aspects.of(app).add(AwsSolutionsChecks())
nag.acknowledge_app_wide(app)

app.synth()
