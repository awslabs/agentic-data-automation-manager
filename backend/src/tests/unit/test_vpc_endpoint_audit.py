#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The stack creates an interface endpoint only for a service the VPC has none
for, keeps the ones it created, and stops on one that would deploy and not work."""

import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cdk_infra.network.vpc_endpoint_audit import CREATED_BY_TAG, classify, plan_endpoints  # noqa: E402

OURS = "AdamDev"
REGION = "us-east-1"


def endpoint(vpce="vpce-1", service="glue", state="available", private_dns=True, tags=None):
    return {"VpcEndpointId": vpce, "ServiceName": f"com.amazonaws.{REGION}.{service}",
            "State": state, "PrivateDnsEnabled": private_dns,
            "Tags": [{"Key": k, "Value": v} for k, v in (tags or {}).items()]}


class TestClassify:

    def test_none_is_created(self):
        assert classify("glue", [], OURS)[0] == "create"

    def test_a_deleted_one_counts_as_none(self):
        assert classify("glue", [endpoint(state="deleted")], OURS)[0] == "create"

    def test_a_healthy_existing_one_is_reused_not_duplicated(self):
        assert classify("glue", [endpoint(vpce="vpce-theirs")], OURS) == ("reuse", "vpce-theirs")

    def test_a_pending_one_is_reused(self):
        assert classify("glue", [endpoint(state="pending")], OURS)[0] == "reuse"

    def test_one_this_deployment_created_stays_in_the_stack(self):
        # Read as someone else's, it would drop out of the stack and CloudFormation would delete it.
        assert classify("glue", [endpoint(tags={CREATED_BY_TAG: OURS})], OURS)[0] == "create"

    def test_one_this_stack_created_before_it_tagged_createdby_stays_in_the_stack(self):
        tags = {"aws:cloudformation:stack-name": f"{OURS}-VpcEndpoints"}
        assert classify("glue", [endpoint(tags=tags)], OURS)[0] == "create"

    def test_one_another_stack_created_is_reused(self):
        tags = {"aws:cloudformation:stack-name": "OtherAppDev-VpcEndpoints"}
        assert classify("glue", [endpoint(tags=tags)], OURS)[0] == "reuse"

    def test_one_another_deployment_created_is_reused(self):
        assert classify("glue", [endpoint(tags={CREATED_BY_TAG: "AdamProd"})], OURS)[0] == "reuse"

    def test_private_dns_off_stops_and_names_the_endpoint(self):
        action, detail = classify("glue", [endpoint(vpce="vpce-nodns", private_dns=False)], OURS)
        assert action == "stop" and "vpce-nodns" in detail and "private DNS" in detail

    @pytest.mark.parametrize("state", ["failed", "rejected", "deleting", "pendingAcceptance"])
    def test_one_that_cannot_serve_stops(self, state):
        action, detail = classify("glue", [endpoint(state=state)], OURS)
        assert action == "stop" and state in detail

    def test_two_for_one_service_stop(self):
        action, detail = classify("glue", [endpoint(vpce="vpce-a"), endpoint(vpce="vpce-b")], OURS)
        assert action == "stop" and "vpce-a" in detail and "vpce-b" in detail


class TestPlan:

    def test_only_the_missing_services_are_created(self):
        plan = plan_endpoints(["glue", "sts", "ssm"],
                              [endpoint(service="glue"), endpoint(vpce="vpce-ssm", service="ssm")],
                              region=REGION, created_by=OURS)
        assert plan.create == ["sts"]
        assert plan.reuse == {"glue": "vpce-1", "ssm": "vpce-ssm"}

    def test_an_endpoint_in_another_region_or_for_another_service_does_not_count(self):
        other = {**endpoint(service="glue"), "ServiceName": "com.amazonaws.us-west-2.glue"}
        plan = plan_endpoints(["glue"], [other, endpoint(service="sts")], region=REGION, created_by=OURS)
        assert plan.create == ["glue"]

    def test_every_conflict_is_reported_at_once(self):
        with pytest.raises(ValueError) as error:
            plan_endpoints(["glue", "sts"],
                           [endpoint(vpce="vpce-g", service="glue", private_dns=False),
                            endpoint(vpce="vpce-s", service="sts", state="failed")],
                           region=REGION, created_by=OURS)
        assert "vpce-g" in str(error.value) and "vpce-s" in str(error.value)


class TestTheStackTagsWhatItCreates:

    def test_each_endpoint_is_tagged_createdby_the_parent_stack(self):
        import aws_cdk as cdk
        from aws_cdk.assertions import Match, Template

        from cdk_infra.network.vpc_endpoints_stack import VpcEndpointsStack
        from tests.unit.cdk_stacks import app_context

        app = cdk.App(context=app_context())
        stack = VpcEndpointsStack(app, "T-VpcEndpoints", parent_stack_name=OURS, vpc_id="vpc-1",
                                  vpc_cidr_block="10.0.0.0/16", private_subnet_ids="subnet-1,subnet-2",
                                  availability_zones=["us-east-1a", "us-east-1b"], services=["glue", "sts"],
                                  env=cdk.Environment(account="123456789012", region=REGION))
        template = Template.from_stack(stack)
        template.resource_count_is("AWS::EC2::VPCEndpoint", 2)
        template.all_resources_properties("AWS::EC2::VPCEndpoint", {
            "Tags": Match.array_with([{"Key": CREATED_BY_TAG, "Value": OURS}]),
        })
