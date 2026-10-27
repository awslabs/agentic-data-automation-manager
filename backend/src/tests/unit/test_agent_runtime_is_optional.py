#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The AgentCore Runtime behind the web console's chat is deployed only with enableChat.

Quick is the agent in a default install. With the flag the runtime, the chat API
function's RUNTIME_ARN and the RuntimeArn output the website looks for all appear
together; without it none of them do, and the chat route says so instead of
accepting a prompt nothing will ever answer.
"""

import json
import os

import pytest
from aws_cdk.assertions import Match, Template

from tests.unit.cdk_stacks import BACKEND, build_stacks


def _agent_template(enable_chat: bool) -> Template:
    previous = os.getcwd()
    os.chdir(BACKEND)
    try:
        return Template.from_stack(build_stacks(enable_chat=enable_chat)["agent"])
    finally:
        os.chdir(previous)


def _api_function_env(template: Template) -> dict:
    functions = template.find_resources("AWS::Lambda::Function", {
        "Properties": {"Handler": "api_handler.handler"}})
    (function,) = functions.values()
    return function["Properties"]["Environment"]["Variables"]


class TestWithoutTheFlag:

    @pytest.fixture(scope="class")
    def template(self):
        return _agent_template(enable_chat=False)

    def test_there_is_no_runtime_and_nothing_points_at_one(self, template):
        template.resource_count_is("AWS::BedrockAgentCore::Runtime", 0)
        assert "RuntimeArn" not in template.find_outputs("*")
        assert "RUNTIME_ARN" not in _api_function_env(template)


class TestWithTheFlag:

    @pytest.fixture(scope="class")
    def template(self):
        return _agent_template(enable_chat=True)

    def test_the_runtime_is_deployed_and_wired_to_the_chat_route_and_the_website(self, template):
        (runtime_id,) = template.find_resources("AWS::BedrockAgentCore::Runtime")
        arn = {"Fn::GetAtt": [runtime_id, "AgentRuntimeArn"]}
        assert _api_function_env(template)["RUNTIME_ARN"] == arn
        template.has_output("RuntimeArn", {"Value": arn})

    def test_the_runtime_accepts_only_the_websites_tokens(self, template):
        template.has_resource_properties("AWS::BedrockAgentCore::Runtime", {
            "AuthorizerConfiguration": {"CustomJWTAuthorizer": Match.object_like({"AllowedClients": ["c1"]})}})


def test_without_a_runtime_the_chat_route_refuses_and_keeps_nothing(monkeypatch):
    import chat

    monkeypatch.setattr(chat, "RUNTIME_ARN", "")
    monkeypatch.setenv("CHAT_MODELS", json.dumps([{"id": "m", "label": "M"}]))

    def no_storage(*args, **kwargs):
        raise AssertionError("a refused chat must not be kept")

    monkeypatch.setattr(chat.conversations, "record_prompt", no_storage)
    event = {"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "sub-alice"}}}}, "headers": {}}
    response = chat._chat(event, {}, {}, {"prompt": "hi", "model": "m"})
    assert response["statusCode"] == 503
    assert "not enabled" in json.loads(response["body"])["error"]
