#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Each tool event the runtime streams names the call it belongs to.

The agent runs tool calls in parallel, so their results come back in any order. The
chat page puts each result under the call it names; an event without the call's id
can only be guessed onto the last call, which shows a result under the wrong statement.

Drives the runtime's real callback handler with the event sequence Strands produces
for two parallel calls whose results come back in reverse order.
"""

import json


def _run_two_parallel_calls():
    from adam.agent_provider.agent_runtime import agent

    emitted = []
    handler = agent.StreamingCallbackHandler("sub-alice", "conv-1",
                                             lambda kind, content: emitted.append((kind, json.loads(content))))
    for tool_use_id, statement in (("call-a", "SELECT 1 FROM a"), ("call-b", "SELECT 1 FROM b")):
        handler(event={"contentBlockStart": {"start": {"toolUse": {
            "name": "adam___execute_statement", "toolUseId": tool_use_id}}}})
        handler(event={}, current_tool_use={"input": {"statement": statement}})
        handler(event={"contentBlockStop": {}})
    handler(event={}, message={"role": "user", "content": [
        {"toolResult": {"toolUseId": "call-b", "content": [{"text": "rows from b"}]}},
        {"toolResult": {"toolUseId": "call-a", "content": [{"text": "rows from a"}]}},
    ]})
    return emitted


def test_each_result_names_the_call_it_answers():
    results = [data for kind, data in _run_two_parallel_calls() if kind == "tool_result"]
    assert [(r["toolUseId"], r["output"]) for r in results] == [("call-b", "rows from b"), ("call-a", "rows from a")]


def test_each_call_and_its_statement_carry_the_same_id():
    emitted = _run_two_parallel_calls()
    uses = [d["toolUseId"] for k, d in emitted if k == "tool_use"]
    inputs = [(d["toolUseId"], d["input"]["statement"]) for k, d in emitted if k == "tool_input"]
    assert uses == ["call-a", "call-b"]
    assert inputs == [("call-a", "SELECT 1 FROM a"), ("call-b", "SELECT 1 FROM b")]
