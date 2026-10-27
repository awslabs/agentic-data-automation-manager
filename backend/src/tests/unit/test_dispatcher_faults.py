#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What the dispatcher tells the agent, and what it does not.

A refusal the agent can act on (a data source it named that the caller has none of, a
limit) comes back as a tool error. A fault in our own wiring raises, so the gateway fails
the call and the log shows it: handed to the model as a tool error, it would be retried,
apologised for, or answered around. The real handler, with only the provider functions
(the Lambda boundary) stood in for.
"""

import mcp_handler
import pytest

from agent_provider.gateway_event import SUBJECT_ARGUMENT
from shared import provider_client

CONTEXT = type("Context", (), {"client_context": type("ClientContext", (), {
    "custom": {"bedrockAgentCoreToolName": "target___execute_statement"}})()})()
EVENT = {"data_source": "Wealth", "statement": "SELECT 1", SUBJECT_ARGUMENT: "sub-alice"}


@pytest.fixture
def providers(monkeypatch):
    def answering(error: Exception):
        def call(*args, **kwargs):
            raise error
        monkeypatch.setattr(mcp_handler, "call_provider", call)
    return answering


def test_a_refusal_from_a_provider_is_a_tool_error_the_agent_reads(providers):
    providers(provider_client.ProviderRefused("No connection named 'Wealth'."))
    result = mcp_handler.handler(EVENT, CONTEXT)
    assert "No connection named 'Wealth'." in result["content"]


def test_a_fault_in_our_wiring_is_raised_not_handed_to_the_model(providers):
    providers(KeyError("connectionId"))
    with pytest.raises(KeyError):
        mcp_handler.handler(EVENT, CONTEXT)
