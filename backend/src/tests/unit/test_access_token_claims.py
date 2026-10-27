#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A user's access token carries their email, so the admin routes' records name a person.

The pool signs users in by email, so their username is a generated id. The trigger adds the
email to the access token alone; the stack wires it as a V2 trigger, which needs Essentials.
"""

import copy

import pytest
from aws_cdk.assertions import Template

from adam.auth_provider import access_token_claims_trigger as trigger
from tests.unit.cdk_stacks import build_stacks


def _event(attributes):
    return {"version": "2", "triggerSource": "TokenGeneration_HostedAuth",
            "request": {"userAttributes": attributes, "groupConfiguration": {}, "scopes": ["openid"]},
            "response": {"claimsAndScopeOverrideDetails": None}}


def test_the_email_is_added_to_the_access_token_and_nothing_else():
    event = trigger.handler(_event({"sub": "s", "email": "pat@example.com"}), None)
    assert event["response"]["claimsAndScopeOverrideDetails"] == {
        "accessTokenGeneration": {"claimsToAddOrOverride": {"email": "pat@example.com"}}}


def test_a_user_without_an_email_gets_the_token_cognito_would_issue():
    event = _event({"sub": "s"})
    assert trigger.handler(copy.deepcopy(event), None) == event


@pytest.fixture(scope="module")
def pool():
    resources = Template.from_stack(build_stacks()["auth"]).find_resources("AWS::Cognito::UserPool")
    (props,) = [r["Properties"] for r in resources.values()]
    return props


def test_the_stack_runs_it_as_the_pools_v2_pre_token_generation_trigger(pool):
    config = pool["LambdaConfig"]["PreTokenGenerationConfig"]
    assert config["LambdaVersion"] == "V2_0"
    assert "AccessTokenClaimsTrigger" in config["LambdaArn"]["Fn::GetAtt"][0]


def test_the_pool_is_on_a_plan_that_runs_v2_triggers(pool):
    assert pool["UserPoolTier"] == "ESSENTIALS"
