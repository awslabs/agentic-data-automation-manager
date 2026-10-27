#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The chat page's history: each caller sees, reopens and deletes only their own
conversations, and a conversation reads back with both sides of every turn.

Real pieces: the chat route and the conversation routes as the HTTP API invokes
them, and DynamoDB Local for the sessions table. The runtime's answer is written
the way the runtime writes it (message_key, seq from 1).
"""

import json
import secrets
import uuid

import boto3
import pytest

from adam.agent_provider.agent_runtime import agent as runtime_agent
from tests.integration.runtime_stand_in import RuntimeStandIn

pytestmark = pytest.mark.integration

ALICE, MALLORY = "sub-alice", "sub-mallory"


@pytest.fixture
def history(monkeypatch, dynamodb_endpoint):
    import chat
    import conversations

    client = boto3.client("dynamodb", region_name="us-east-1", endpoint_url=dynamodb_endpoint,
                          aws_access_key_id=secrets.token_hex(10), aws_secret_access_key=secrets.token_hex(20))
    table = f"sessions-{uuid.uuid4().hex[:8]}"
    client.create_table(
        TableName=table, BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": "user_id", "AttributeType": "S"},
                              {"AttributeName": "sort_key", "AttributeType": "S"}],
        KeySchema=[{"AttributeName": "user_id", "KeyType": "HASH"},
                   {"AttributeName": "sort_key", "KeyType": "RANGE"}])
    monkeypatch.setenv("SESSIONS_TABLE", table)
    monkeypatch.setattr(chat, "RUNTIME_ARN", "arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/r")
    monkeypatch.setenv("CHAT_MODELS", json.dumps([{"id": "m", "label": "M"}]))
    monkeypatch.setattr(conversations, "_ddb_client", client)
    monkeypatch.setattr(runtime_agent, "SESSIONS_TABLE", table)
    monkeypatch.setattr(runtime_agent, "_ddb_client", client)
    # The runtime endpoint, starting every turn it is asked to.
    runtime = RuntimeStandIn(200, json.dumps({"status": "started"}))
    monkeypatch.setattr(chat, "RUNTIME_ENDPOINT", runtime.url)
    try:
        yield chat, conversations, client, table, runtime
    finally:
        runtime.close()
        client.delete_table(TableName=table)


def _event(user_id, route, path_params=None):
    method, path = route.split(" ", 1)
    return {"routeKey": route, "pathParameters": path_params or {}, "headers": {"authorization": "Bearer t"},
            "requestContext": {"http": {"method": method, "path": path},
                               "authorizer": {"jwt": {"claims": {"sub": user_id}}}}}


def _call(module, user_id, route, path_params=None, body=None):
    response = module.ROUTES[route](_event(user_id, route, path_params), path_params or {}, {}, body)
    return response["statusCode"], json.loads(response["body"])


def _ask(history, user_id, prompt, conversation_id):
    chat, conversations, client, table, runtime = history
    status, body = _call(chat, user_id, "POST /api/chat",
                         body={"prompt": prompt, "model": "m", "conversation_id": conversation_id})
    assert status == 202, body
    turn = json.loads(runtime.requests[-1][3])["turn_id"]
    # The runtime's answer, written by the runtime's own writer: the history reads and
    # deletes what the runtime wrote, so the two must agree on its key.
    runtime_agent._write_session_message(user_id, conversation_id, turn, 1, "output",
                                         json.dumps({"content": f"answer to {prompt}"}))


MESSAGES = "GET /api/conversations/{conversation_id}/messages"
DELETE = "DELETE /api/conversations/{conversation_id}"


class TestAConversationReadsBackWhole:

    def test_every_turn_keeps_its_prompt_and_answer_in_order(self, history):
        conversations = history[1]
        conversation = str(uuid.uuid4())
        _ask(history, ALICE, "first", conversation)
        _ask(history, ALICE, "second", conversation)
        status, body = _call(conversations, ALICE, MESSAGES, {"conversation_id": conversation})
        assert status == 200
        assert [(m["event"], m["data"]["content"]) for m in body["messages"]] == [
            ("user", "first"), ("output", "answer to first"),
            ("user", "second"), ("output", "answer to second")]

    def test_the_first_prompt_names_the_conversation(self, history):
        conversations = history[1]
        conversation = str(uuid.uuid4())
        _ask(history, ALICE, "first", conversation)
        _ask(history, ALICE, "second", conversation)
        _, body = _call(conversations, ALICE, "GET /api/conversations")
        assert [c["title"] for c in body["conversations"]] == ["first"]


class TestOneCallersHistoryIsInvisibleToAnother:

    @pytest.fixture
    def alices(self, history):
        conversation = str(uuid.uuid4())
        _ask(history, ALICE, "private", conversation)
        return conversation

    def test_it_is_not_listed(self, history, alices):
        _, body = _call(history[1], MALLORY, "GET /api/conversations")
        assert body["conversations"] == []

    def test_it_cannot_be_read_by_id(self, history, alices):
        status, _ = _call(history[1], MALLORY, MESSAGES, {"conversation_id": alices})
        assert status == 404

    def test_it_cannot_be_deleted_by_id(self, history, alices):
        conversations = history[1]
        status, _ = _call(conversations, MALLORY, DELETE, {"conversation_id": alices})
        assert status == 404
        _, body = _call(conversations, ALICE, MESSAGES, {"conversation_id": alices})
        assert len(body["messages"]) == 2


class TestDeleting:

    def test_removes_the_conversation_and_every_event_and_nothing_else(self, history):
        _, conversations, client, table, _ = history
        doomed, kept = str(uuid.uuid4()), str(uuid.uuid4())
        for _ in range(15):  # more items than one BatchWriteItem call takes
            _ask(history, ALICE, "q", doomed)
        _ask(history, ALICE, "keep", kept)

        def alices():
            return {i["sort_key"]["S"] for i in client.query(
                TableName=table, KeyConditionExpression="user_id = :u",
                ExpressionAttributeValues={":u": {"S": ALICE}})["Items"]}
        keeping = {k for k in alices() if kept in k}
        assert f"conv#{kept}" in keeping and len(keeping) == 3  # the conversation, its prompt and answer
        status, _ = _call(conversations, ALICE, DELETE, {"conversation_id": doomed})
        assert status == 200
        assert alices() == keeping


class TestARequestThatCannotBeServedIsRefused:

    @pytest.mark.parametrize("body, refusal", [
        ({"model": "m"}, "Missing prompt"),
        ({"prompt": "hi"}, "Missing model"),
        ({"prompt": "hi", "model": "m", "conversation_id": "../conv#x"}, "Not a conversation id"),
    ])
    def test_a_chat_without_what_it_needs_hands_nothing_to_the_runtime(self, history, body, refusal):
        chat, _, client, table, runtime = history
        status, answer = _call(chat, ALICE, "POST /api/chat", body=body)
        assert (status, answer["error"]) == (400, refusal)
        assert runtime.requests == [] and client.scan(TableName=table)["Count"] == 0

    @pytest.mark.parametrize("route", [MESSAGES, DELETE])
    def test_a_path_that_is_not_a_conversation_id_is_refused(self, history, route):
        _, conversations, _, _, _ = history
        status, answer = _call(conversations, ALICE, route, {"conversation_id": "conv#x"})
        assert (status, answer["error"]) == (400, "Not a conversation id")

    def test_a_delete_dynamodb_keeps_leaving_unfinished_is_an_error(self, history, monkeypatch):
        """Reporting success would show the conversation gone while some of it remains."""
        chat, conversations, client, table, _ = history
        conversation = str(uuid.uuid4())
        _ask(history, ALICE, "hello", conversation)
        monkeypatch.setattr(client, "batch_write_item", lambda RequestItems: {"UnprocessedItems": RequestItems})
        with pytest.raises(RuntimeError, match="undeleted"):
            _call(conversations, ALICE, DELETE, {"conversation_id": conversation})
