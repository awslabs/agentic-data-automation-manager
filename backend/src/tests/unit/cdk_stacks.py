#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Builds every service stack for tests, with bundling off so no container runs.

Shared by the template tests (test_cdk_security.py) and the bundle contents tests
(test_bundles.py), so both look at the same stacks.
"""

import json
import sys
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cdk_infra.agent_provider.agent_provider_stack import AgentProviderStack  # noqa: E402
from cdk_infra.auth_provider.auth_provider_stack import AuthProviderStack  # noqa: E402
from cdk_infra.data_source_provider.data_source_provider_stack import DataSourceProviderStack  # noqa: E402
from cdk_infra.glue_catalog_provider.glue_catalog_provider_stack import GlueCatalogProviderStack  # noqa: E402
from cdk_infra.shared import nag  # noqa: E402
from cdk_infra.shared.stage import Stage  # noqa: E402

PREFIX = "T"


def app_context(**extra) -> dict:
    """backend/cdk.json's context, its feature flags above all, as the cdk CLI gives app.py:
    without it a test synthesizes with different defaults than a deploy does."""
    cdk_json = Path(__file__).resolve().parents[3] / "cdk.json"
    return {**json.loads(cdk_json.read_text())["context"], **extra}

ENV = cdk.Environment(account="123456789012", region="us-west-2")
VPC = {"vpc_id": "vpc-1", "app_sg_id": "sg-0123456789abcdef0", "private_subnet_ids": "subnet-1,subnet-2",
           "availability_zones": ["us-west-2a", "us-west-2b"]}


def build_stacks(*, enable_chat: bool = True, stage: str = "dev", web_search: bool = True) -> dict[str, cdk.Stack]:
    """The stacks, keyed auth / agent / external / glue, as a default install builds
    them (chat included, stage dev). Call with the backend directory as the working
    directory: assets are relative to it."""
    from cdk_infra.network.vpc_endpoints_stack import (
        REQUIRED_INTERFACE_SERVICES,
        RUNTIME_INTERFACE_SERVICES,
        VpcEndpointsStack,
    )
    from cdk_infra.test_targets.redshift_test_target_stack import RedshiftTestTargetStack
    settings = Stage(stage)
    app = cdk.App(context=app_context(**{"aws:cdk:bundling-stacks": []}))
    # Wired as in app.py: the agent stack routes to functions the other two own, and the
    # providers' scanners serve the schema service's bus and functions.
    glue = GlueCatalogProviderStack(app, "T-Glue", parent_stack_name=PREFIX, stage=settings, **VPC, env=ENV)
    external = DataSourceProviderStack(app, "T-External", parent_stack_name=PREFIX, stage=settings, **VPC,
                                             dynamodb_endpoint_url="", scan_bus=glue.scan_bus,
                                             schema_admin_function=glue.admin_function,
                                             schema_ingestion_function=glue.ingestion_function,
                                             schema_function=glue.function, user_pool_id="us-west-2_x", env=ENV)
    stacks = {
        "auth": AuthProviderStack(app, "T-Auth", parent_stack_name=PREFIX, stage=settings,
                                  website_url="https://d1.cloudfront.net", env=ENV),
        "agent": AgentProviderStack(
            app, "T-Agent", parent_stack_name=PREFIX, stage=settings, **VPC,
            identity_provider="cognito",
            oidc_issuer_url="https://cognito-idp.us-west-2.amazonaws.com/us-west-2_x",
            allowed_client_ids=["c1", "c2"], frontend_client_id="c1", user_pool_id="us-west-2_x",
            web_search_model_id="us.amazon.nova-2-lite-v1:0" if web_search else None,
            connections_api_function=external.connections_api_function,
            admin_function=glue.admin_function, enable_chat=enable_chat, env=ENV),
        "external": external,
        "glue": glue,
        # Built by app.py with REDSHIFT_TEST_TARGET.
        "redshift": RedshiftTestTargetStack(app, "T-RedshiftTestTarget", parent_stack_name=PREFIX, **VPC, env=ENV),
        # Built by app.py when the VPC lacks an endpoint the functions need: here, all of them.
        "endpoints": VpcEndpointsStack(app, "T-VpcEndpoints", parent_stack_name=PREFIX, vpc_id=VPC["vpc_id"],
                                       vpc_cidr_block="10.0.0.0/16", private_subnet_ids=VPC["private_subnet_ids"],
                                       availability_zones=VPC["availability_zones"],
                                       services=[*REQUIRED_INTERFACE_SERVICES, *RUNTIME_INTERFACE_SERVICES],
                                       env=ENV),
    }
    # Checked as app.py checks every stack, once they all exist.
    cdk.Aspects.of(app).add(AwsSolutionsChecks())
    nag.acknowledge_app_wide(app)
    return stacks
