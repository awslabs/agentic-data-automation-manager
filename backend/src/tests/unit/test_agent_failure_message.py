#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A failed chat turn tells the user a fixed message and a reference, never the exception's text.

The real run_turn, with only its boundaries replaced: the Gateway's MCP client, and the
sessions table the turn's events are written to.
"""

import json
import logging

import pytest

from adam.agent_provider.agent_runtime import agent

CONVERSATION, TURN = "0b6f1f0e-3c1a-4c55-9a39-1f0a3c1d2e4f", "20261026T120000000000Z"
INTERNALS = ("connection to db-internal.example.net:5432 failed for arn:aws:iam::123456789012:role/Secret "
             "at /var/task/adam/handler.py")


@pytest.fixture
def written(monkeypatch):
    events = []
    monkeypatch.setattr(agent, "_write_session_message",
                        lambda user, conversation, turn, seq, kind, content: events.append((kind, json.loads(content))))
    monkeypatch.setattr(agent, "GATEWAY_URL", "https://gw.example/mcp")
    return events


def _failing_client(error):
    class _Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            raise error

        def __exit__(self, *exc):
            return False
    return _Client


def test_the_user_sees_the_fixed_message_and_its_reference_and_nothing_of_the_error(monkeypatch, written, caplog):
    monkeypatch.setattr(agent, "MCPClient", _failing_client(RuntimeError(INTERNALS)))
    caplog.set_level(logging.ERROR)
    agent.run_turn("sub-user", "Bearer t", CONVERSATION, TURN, "hi", "model")
    ((kind, event),) = written
    reference = f"{CONVERSATION}/{TURN}"
    assert kind == "error" and event == {"content": f"{agent.AGENT_FAILURE} Reference: {reference}."}
    # The details are the operator's, in the log, under the same reference.
    assert reference in caplog.text and INTERNALS in caplog.text


def test_a_refused_token_is_still_reported_as_one(monkeypatch, written):
    monkeypatch.setattr(agent, "MCPClient", _failing_client(RuntimeError("401 invalid_token")))
    agent.run_turn("sub-user", "Bearer t", CONVERSATION, TURN, "hi", "model")
    ((kind, event),) = written
    assert kind == "error" and event["code"] == agent.GATEWAY_REFUSAL and event["reason"] == "invalid_token"
