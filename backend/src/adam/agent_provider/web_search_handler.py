#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The web_search MCP tool: its own function behind AgentCore Gateway.

Separate from the database tools' dispatcher so that only this function holds
Bedrock permissions, and the dispatcher only routes database tool calls.
"""

import logging
from typing import Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from agent_provider import gateway_event
from shared.environment import require_env

logger = logging.getLogger()
logger.setLevel(logging.INFO)

_bedrock_client = None


def _get_bedrock():
    global _bedrock_client
    if _bedrock_client is None:
        # One attempt beyond the first: a grounded answer runs several searches
        # and a retry storm would outlast the gateway's own timeout.
        _bedrock_client = boto3.client("bedrock-runtime", config=Config(
            read_timeout=120, retries={"max_attempts": 2, "mode": "standard"},
        ))
    return _bedrock_client


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    name = gateway_event.tool_name(context)
    # Raises outside any try: an unattributable call is a wiring fault and should page us.
    user = gateway_event.user_id(event)
    logger.info("Tool: %s, user: %s", name, user)
    if name != "web_search":
        return gateway_event.error(f"Unknown tool: {name}")
    return search(event.get("query", ""), event.get("num_results", 5))


def search(query: str, num_results: int) -> dict:
    """Answer a question from the live web, with the sources it came from.

    Backed by Amazon Nova web grounding, a Bedrock system tool, so queries stay inside AWS.

    Citations are returned alongside the answer, not folded into it: the web
    grounding terms require that they be shown to the end user.
    """
    if not query.strip():
        return gateway_event.error("No search query provided.")
    try:
        model_id = require_env("WEB_SEARCH_MODEL_ID")
    except RuntimeError as e:
        logger.error("web_search is not configured: %s", e)
        return gateway_event.error(f"Web search failed: {e}")
    try:
        response = _get_bedrock().converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": query}]}],
            toolConfig={"tools": [{"systemTool": {"name": "nova_grounding"}}]},
        )
    except (ClientError, BotoCoreError) as e:
        logger.exception("web_search failed")
        return gateway_event.error(f"Web search failed: {e}")

    answer_parts: list[str] = []
    sources: list[dict[str, str]] = []
    seen: set[str] = set()
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            answer_parts.append(block["text"])
        for citation in block.get("citationsContent", {}).get("citations", []):
            web = citation.get("location", {}).get("web", {})
            url = web.get("url", "")
            if url and url not in seen:
                seen.add(url)
                sources.append({"url": url, "domain": web.get("domain", "")})

    answer = "".join(answer_parts).strip()
    logger.info("web_search answer_chars=%d sources=%d", len(answer), len(sources))
    if not sources:
        # An answer with no citation is the model speaking from its training
        # data, not the web. Said plainly so it is not presented as live.
        return gateway_event.result({
            "query": query, "answer": answer, "sources": [],
            "note": "No web sources were cited; treat this answer as ungrounded.",
        })
    return gateway_event.result({"query": query, "answer": answer,
                  "sources": sources[:max(1, min(num_results, 10))]})
