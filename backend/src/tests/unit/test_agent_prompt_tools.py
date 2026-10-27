#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The chat agent is told only of the tools the Gateway lists: web_search is off unless deployed."""

import pytest

from adam.agent_provider.agent_runtime import agent

DATA_TOOLS = ["adam___search_schema", "adam___execute_statement"]


def test_without_web_search_the_prompt_never_mentions_it():
    prompt = agent.system_prompt(DATA_TOOLS)
    assert "web_search" not in prompt and "web page" in prompt   # the untrusted-data rule stays
    assert "- search_schema:" in prompt and "- execute_statement:" in prompt
    assert "{" not in prompt


def test_with_web_search_served_the_prompt_describes_it():
    prompt = agent.system_prompt([*DATA_TOOLS, "web-search___web_search"])
    assert "- web_search: Search the web." in prompt


def test_a_tool_merely_ending_in_the_name_is_not_web_search():
    assert "web_search" not in agent.system_prompt([*DATA_TOOLS, "adam___not_web_search"])


class _Tool:
    def __init__(self, name):
        self.tool_name = name


@pytest.mark.parametrize("served,mentioned", [(DATA_TOOLS, False), ([*DATA_TOOLS, "web-search___web_search"], True)])
def test_the_agent_is_built_with_the_prompt_for_the_tools_it_was_given(monkeypatch, served, mentioned):
    built = {}
    monkeypatch.setattr(agent, "BedrockModel", lambda **kwargs: object())
    monkeypatch.setattr(agent, "Agent", lambda **kwargs: built.update(kwargs))
    agent._create_agent([_Tool(n) for n in served], callback_handler=None, model_id="m")
    assert ("web_search" in built["system_prompt"]) is mentioned
