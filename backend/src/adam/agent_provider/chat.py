#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Chat initiation: keep the prompt, start the turn on the agent runtime, and return.

The answer is not returned here. The runtime runs the turn in the background and writes
its events to DynamoDB; a stream processor publishes them to AppSync Events, where the
client is subscribed. The runtime is called with the caller's own Authorization header,
so the token goes nowhere but the runtime, which forwards it to the Gateway.
"""

import json
import logging
import os
import urllib.parse
import uuid

import conversations
import urllib3

from shared.http_response import error, get_user_id, success

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

RUNTIME_ARN = os.environ.get("RUNTIME_ARN", "")
# Lambda always sets AWS_REGION; there is no region to guess.
DEPLOY_REGION = os.environ.get("DEPLOY_REGION") or os.environ["AWS_REGION"]
# Overridable so a test can point at a local stand-in for the runtime endpoint.
RUNTIME_ENDPOINT = (os.environ.get("RUNTIME_ENDPOINT", "")
                    or f"https://bedrock-agentcore.{DEPLOY_REGION}.amazonaws.com")
# The runtime answers once the turn has started. The HTTP API gives up on the route at 30 s.
START_TIMEOUT_SECONDS = 25


def _chat_models() -> list[dict]:
    """The models the chat page offers (cdk_infra/agent_provider/chat_models.py); the first is the default."""
    models = json.loads(os.environ.get("CHAT_MODELS") or "[]")
    if not models:
        raise RuntimeError("CHAT_MODELS is not set; this function is misconfigured.")
    return models


class _StartUnknown(Exception):
    """The runtime did not answer in time: the turn may still start."""


def _require_runtime_url(url: str) -> str:
    """https only; plain http only to the loopback stand-in a test runs."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")):
        return url
    raise ValueError(f"RUNTIME_ENDPOINT must be an https URL, got {url!r}.")


def _start_turn(authorization: str, payload: dict) -> bool:
    """Ask the runtime to start the turn. True once it has; False (logged) if it refused or
    could not be reached. Raises _StartUnknown when it did not answer in time."""
    escaped_arn = urllib.parse.quote(RUNTIME_ARN, safe="")
    url = _require_runtime_url(f"{RUNTIME_ENDPOINT}/runtimes/{escaped_arn}/invocations?qualifier=DEFAULT")
    headers = {"Content-Type": "application/json",
               "Authorization": authorization,
               "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": payload["session_id"]}
    # No retries and no redirects: the caller's token goes to this URL and nowhere else.
    try:
        with urllib3.PoolManager(retries=False) as http:
            response = http.request("POST", url, body=json.dumps(payload).encode("utf-8"), headers=headers,
                                    timeout=START_TIMEOUT_SECONDS)
    except urllib3.exceptions.ReadTimeoutError:
        # A cold runtime can start the turn after this; its own events, or the page's
        # inactivity reload, say how it ended.
        logger.warning("The runtime did not acknowledge conversation %s in %d s",
                       payload["session_id"], START_TIMEOUT_SECONDS)
        raise _StartUnknown from None
    except urllib3.exceptions.HTTPError:
        logger.exception("Starting a turn failed for conversation %s", payload["session_id"])
        return False
    if not 200 <= response.status < 300:
        logger.error("HTTP %d from the runtime for conversation %s: %s",
                     response.status, payload["session_id"], response.data.decode("utf-8", errors="replace")[:2000])
        return False
    try:
        answer = json.loads(response.data or b"{}")
    except json.JSONDecodeError:
        logger.exception("Starting a turn failed for conversation %s", payload["session_id"])
        return False
    # AgentCore answers 200 even when the entrypoint raises, with an error of its own.
    if not (isinstance(answer, dict) and answer.get("status") == "started"):
        logger.error("The runtime did not start the turn for conversation %s: %s",
                     payload["session_id"], str(answer)[:2000])
        return False
    return True


def _chat(event, path_params, query_params, body):
    if not body or not body.get("prompt"):
        return error("Missing prompt")

    user_id = get_user_id(event)
    prompt = body["prompt"]
    model_id = body.get("model")
    if not model_id:
        return error("Missing model")
    if model_id not in {m["id"] for m in _chat_models()}:
        return error("Not one of the models this deployment offers (GET /api/models)")
    if "history" in body:
        return error("History is kept by the server; send only the prompt")

    # Without the runtime nothing would ever answer, and the browser would wait forever.
    if not RUNTIME_ARN:
        return error("Chat is not enabled in this deployment (deploy with ENABLE_CHAT=true).", 503)

    # The HTTP API's JWT authorizer verified this header before the route ran.
    authorization = (event.get("headers") or {}).get("authorization", "")
    if not authorization.startswith("Bearer "):
        return error("Missing bearer token", 401)

    conversation_id = body.get("conversation_id") or str(uuid.uuid4())
    if not conversations.valid_conversation_id(conversation_id):
        return error("Not a conversation id")
    turn_id = conversations.new_turn_id()
    conversations.record_prompt(user_id, conversation_id, turn_id, prompt)

    payload = {
        "prompt": prompt,
        "session_id": conversation_id,
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "model_id": model_id,
    }
    try:
        started = _start_turn(authorization, payload)
    except _StartUnknown:
        return success({"conversation_id": conversation_id, "pending": True}, 202)
    if not started:
        conversations.record_failure(user_id, conversation_id, turn_id)
        return error(conversations.FAILURE_MESSAGE, 502)

    return success({"conversation_id": conversation_id}, 202)


def _models(event, path_params, query_params, body):
    get_user_id(event)
    return success({"models": _chat_models()})


ROUTES = {
    "POST /api/chat": _chat,
    "GET /api/models": _models,
    **conversations.ROUTES,
}
