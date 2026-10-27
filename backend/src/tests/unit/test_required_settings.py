#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A setting the stack always gives a function is required where it is read: its absence is
a broken deployment, said loudly, never a quiet fallback that looks like data."""

import pytest


def test_the_admin_routes_do_not_report_unknown_counts_when_the_index_is_not_configured(monkeypatch):
    """Without the index name the page would show "count unknown" as if the index were unreadable."""
    from schema_provider import admin_handler

    monkeypatch.delenv("SCHEMA_INDEX_DATABASE", raising=False)
    with pytest.raises(RuntimeError, match="SCHEMA_INDEX_DATABASE"):
        admin_handler._table_counts()


def test_the_chat_route_offers_no_model_when_none_is_configured(monkeypatch):
    import chat

    monkeypatch.delenv("CHAT_MODELS", raising=False)
    with pytest.raises(RuntimeError, match="CHAT_MODELS"):
        chat._models({"requestContext": {"authorizer": {"jwt": {"claims": {"sub": "s"}}}}}, {}, {}, None)


def test_the_runtime_needs_the_model_in_the_request(monkeypatch):
    """There is no default model to fall back to: the chat route always names one."""
    from adam.agent_provider.agent_runtime import agent

    monkeypatch.setattr(agent, "_verified_subject", lambda authorization: "sub-alice")
    monkeypatch.setattr(agent.BedrockAgentCoreContext, "get_request_headers",
                        staticmethod(lambda: {"Authorization": "Bearer t"}))
    conversation = "0b5a8f0e-3a0e-4c1e-9d3a-1f2e3d4c5b6a"
    with pytest.raises(agent.InvalidInvocation, match="model_id"):
        agent.invoke({"prompt": "hi", "session_id": conversation, "conversation_id": conversation,
                      "turn_id": "20261021T120000000000Z"})
