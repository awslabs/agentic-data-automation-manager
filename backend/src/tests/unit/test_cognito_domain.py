#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The Cognito domain prefix is unique per stack name and account, and the same on every synth."""

import os

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from cdk_infra.auth_provider.auth_provider_stack import (
    DOMAIN_PREFIX_MAX,
    AuthProviderStack,
    cognito_domain_prefix,
)
from cdk_infra.shared.stage import Stage
from tests.unit.cdk_stacks import BACKEND, app_context


def _domain(account: str, stack_name: str = "AdamDev") -> str:
    previous = os.getcwd()
    os.chdir(BACKEND)
    try:
        app = cdk.App(context=app_context(**{"aws:cdk:bundling-stacks": []}))
        stack = AuthProviderStack(app, f"{stack_name}-Auth", parent_stack_name=stack_name, stage=Stage("dev"),
                                  website_url="https://d1.cloudfront.net",
                                  env=cdk.Environment(account=account, region="us-west-2"))
        domains = Template.from_stack(stack).find_resources("AWS::Cognito::UserPoolDomain")
    finally:
        os.chdir(previous)
    [domain] = domains.values()
    return domain["Properties"]["Domain"]


def test_the_same_stack_name_in_two_accounts_gets_two_domains():
    assert _domain("123456789012") == "adamdev-123456789012"
    assert _domain("111122223333") == "adamdev-111122223333"


def test_a_redeploy_keeps_the_same_domain():
    assert _domain("123456789012") == _domain("123456789012")


def test_an_unresolved_account_still_lands_in_the_domain():
    # app.py's env falls back to the deploy account when CDK_DEFAULT_ACCOUNT is unset.
    previous = os.getcwd()
    os.chdir(BACKEND)
    try:
        app = cdk.App(context=app_context(**{"aws:cdk:bundling-stacks": []}))
        stack = AuthProviderStack(app, "AdamDev-Auth", parent_stack_name="AdamDev", stage=Stage("dev"),
                                  website_url="https://d1.cloudfront.net")
        [domain] = Template.from_stack(stack).find_resources("AWS::Cognito::UserPoolDomain").values()
    finally:
        os.chdir(previous)
    assert domain["Properties"]["Domain"] == {"Fn::Join": ["", ["adamdev-", {"Ref": "AWS::AccountId"}]]}


def test_a_stack_name_too_long_for_cognito_fails_loudly():
    longest = "a" * (DOMAIN_PREFIX_MAX - 13)
    assert len(cognito_domain_prefix(longest, "123456789012")) == DOMAIN_PREFIX_MAX
    with pytest.raises(ValueError, match="too long"):
        cognito_domain_prefix(longest + "a", "123456789012")
