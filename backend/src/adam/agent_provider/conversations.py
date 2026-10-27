#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Conversation history for the web console's chat page, kept in the sessions table.

Every item is under the caller's verified sub, the table's partition key, so a caller
can list, read and delete only their own conversations:

    conv#{conversation id}                    title, created_at, updated_at
    msg#{conversation id}#{turn}#{seq:06d}    one event of one turn

A turn is one prompt and its answer. The chat route writes the prompt as seq 0, the
runtime writes the answer's events from seq 1, and a runtime call that fails writes
FAILURE_SEQUENCE. A turn id is a UTC timestamp, so a conversation reads back in order
and no turn overwrites another.
"""

import json
import os
import re
from datetime import UTC, datetime

import boto3

from shared.http_response import error, get_user_id, success

FAILURE_SEQUENCE = 999_999
TITLE_LENGTH = 80
# The browser makes conversation ids with crypto.randomUUID(). Anything else could
# carry a "#" and reach into another conversation's keys.
_CONVERSATION_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
# Progress lines while the agent works; nothing to show when a conversation is reopened.
_NOT_KEPT = {"status"}
_BATCH = 25
_ATTEMPTS = 5

_ddb_client = None


def _ddb():
    global _ddb_client
    if _ddb_client is None:
        _ddb_client = boto3.client("dynamodb")
    return _ddb_client


def _table() -> str:
    table = os.environ.get("SESSIONS_TABLE", "")
    if not table:
        raise RuntimeError("SESSIONS_TABLE is not set; there is nowhere to keep conversations.")
    return table


def valid_conversation_id(value) -> bool:
    return isinstance(value, str) and bool(_CONVERSATION_ID.match(value))


def new_turn_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


def message_key(conversation_id: str, turn_id: str, seq: int) -> str:
    return f"msg#{conversation_id}#{turn_id}#{seq:06d}"


def record_prompt(user_id: str, conversation_id: str, turn_id: str, prompt: str) -> None:
    """Create or touch the conversation, and keep the prompt as the turn's first event."""
    now = datetime.now(UTC).isoformat()
    _ddb().update_item(
        TableName=_table(),
        Key={"user_id": {"S": user_id}, "sort_key": {"S": f"conv#{conversation_id}"}},
        # The first prompt names the conversation; later ones only move it up the list.
        UpdateExpression="SET title = if_not_exists(title, :title), "
                         "created_at = if_not_exists(created_at, :now), updated_at = :now",
        ExpressionAttributeValues={":title": {"S": prompt[:TITLE_LENGTH]}, ":now": {"S": now}},
    )
    _ddb().put_item(
        TableName=_table(),
        Item={
            "user_id": {"S": user_id},
            "sort_key": {"S": message_key(conversation_id, turn_id, 0)},
            "session_id": {"S": conversation_id},
            "message_type": {"S": "user"},
            "content": {"S": json.dumps({"content": prompt})},
            "timestamp": {"S": now},
        },
    )


FAILURE_MESSAGE = "The agent could not be reached. Try again in a moment."


def record_failure(user_id: str, conversation_id: str, turn_id: str) -> None:
    """Tell the caller the turn could not start: the browser is subscribed to the turn's
    channel, and the event stays in the conversation's history.

    Generic on purpose: what the runtime answered can carry ARNs and account ids.
    """
    _ddb().put_item(
        TableName=_table(),
        Item={
            "user_id": {"S": user_id},
            "sort_key": {"S": message_key(conversation_id, turn_id, FAILURE_SEQUENCE)},
            "session_id": {"S": conversation_id},
            "message_type": {"S": "error"},
            "content": {"S": json.dumps({"content": FAILURE_MESSAGE})},
            "timestamp": {"S": datetime.now(UTC).isoformat()},
        },
    )


def _query(user_id: str, prefix: str) -> list[dict]:
    items, start = [], None
    while True:
        page = _ddb().query(
            TableName=_table(),
            KeyConditionExpression="user_id = :u AND begins_with(sort_key, :p)",
            ExpressionAttributeValues={":u": {"S": user_id}, ":p": {"S": prefix}},
            **({"ExclusiveStartKey": start} if start else {}),
        )
        items.extend(page["Items"])
        start = page.get("LastEvaluatedKey")
        if not start:
            return items


def _content(raw: str):
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"content": raw}


def _list(event, path_params, query_params, body):
    user_id = get_user_id(event)
    conversations = [{
        "conversation_id": item["sort_key"]["S"].removeprefix("conv#"),
        "title": item["title"]["S"],
        "created_at": item["created_at"]["S"],
        "updated_at": item["updated_at"]["S"],
    } for item in _query(user_id, "conv#")]
    conversations.sort(key=lambda c: c["updated_at"], reverse=True)
    return success({"conversations": conversations})


def _messages(event, path_params, query_params, body):
    user_id = get_user_id(event)
    conversation_id = path_params.get("conversation_id")
    if not valid_conversation_id(conversation_id):
        return error("Not a conversation id", 400)
    items = _query(user_id, f"msg#{conversation_id}#")
    if not items:
        return error("No such conversation", 404)
    messages = []
    for item in items:
        if item["message_type"]["S"] in _NOT_KEPT:
            continue
        _, _, turn_id, seq = item["sort_key"]["S"].split("#")
        messages.append({"turn": turn_id, "seq": int(seq), "event": item["message_type"]["S"],
                         "data": _content(item["content"]["S"])})
    return success({"conversation_id": conversation_id, "messages": messages})


def _delete(event, path_params, query_params, body):
    user_id = get_user_id(event)
    conversation_id = path_params.get("conversation_id")
    if not valid_conversation_id(conversation_id):
        return error("Not a conversation id", 400)
    keys = [item["sort_key"]["S"] for item in _query(user_id, f"msg#{conversation_id}#")]
    keys += [item["sort_key"]["S"] for item in _query(user_id, f"conv#{conversation_id}")
             if item["sort_key"]["S"] == f"conv#{conversation_id}"]
    if not keys:
        return error("No such conversation", 404)
    for i in range(0, len(keys), _BATCH):
        requests = [{"DeleteRequest": {"Key": {"user_id": {"S": user_id}, "sort_key": {"S": k}}}}
                    for k in keys[i:i + _BATCH]]
        for _ in range(_ATTEMPTS):
            unprocessed = _ddb().batch_write_item(RequestItems={_table(): requests}).get("UnprocessedItems", {})
            requests = unprocessed.get(_table(), [])
            if not requests:
                break
        else:
            raise RuntimeError(f"DynamoDB left {len(requests)} item(s) of conversation {conversation_id} undeleted.")
    return success({"deleted": conversation_id})


ROUTES = {
    "GET /api/conversations": _list,
    "GET /api/conversations/{conversation_id}/messages": _messages,
    "DELETE /api/conversations/{conversation_id}": _delete,
}
