#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The stack outputs are everything Amazon Quick's MCP integration form needs,
except the client secret, which no output may carry."""

import json
import os

import pytest
from aws_cdk.assertions import Template

from tests.unit.cdk_stacks import BACKEND, build_stacks


@pytest.fixture(scope="module")
def outputs():
    cwd = os.getcwd()
    os.chdir(BACKEND)
    try:
        stacks = build_stacks()
        return {name: Template.from_stack(stacks[name]).to_json().get("Outputs", {})
                for name in ("auth", "agent")}
    finally:
        os.chdir(cwd)


class TestQuickOutputs:

    def test_the_gateway_endpoint_is_an_output(self, outputs):
        value = outputs["agent"]["QuickMcpServerUrl"]["Value"]
        assert value["Fn::GetAtt"][1] == "GatewayUrl"

    @pytest.mark.parametrize("name, path", [("QuickTokenUrl", "/oauth2/token"),
                                            ("QuickAuthorizationUrl", "/oauth2/authorize")])
    def test_the_oauth_urls_are_on_the_user_pool_domain(self, outputs, name, path):
        value = json.dumps(outputs["auth"][name]["Value"])
        assert "CognitoDomain" in value and value.endswith(path + '"]]}')

    def test_the_client_id_and_the_command_for_its_secret_are_outputs(self, outputs):
        assert "QuickClientId" in outputs["auth"]
        command = json.dumps(outputs["auth"]["QuickClientSecretCommand"]["Value"])
        assert "describe-user-pool-client" in command and "QuickClient" in command

    def test_no_output_carries_a_client_secret(self, outputs):
        """An output's value is resolved at deploy time; the secret would be in it if any
        part of the value is an attribute of a resource (GetAtt) rather than text or an id
        (Ref). The secret command names the secret's attribute only as text, for the CLI."""
        def attributes(value):
            if isinstance(value, dict):
                for key, inner in value.items():
                    if key == "Fn::GetAtt":
                        yield inner
                    yield from attributes(inner)
            elif isinstance(value, list):
                for inner in value:
                    yield from attributes(inner)

        for stack in outputs.values():
            for name, output in stack.items():
                assert not [a for a in attributes(output["Value"]) if "Secret" in json.dumps(a)], name
