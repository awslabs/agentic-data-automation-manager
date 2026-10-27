#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""DynamoDB Streams -> AppSync Events publisher for agent session messages.

Triggered by INSERT events on the sessions table. Filters for msg# records
and publishes each message to the AppSync Events channel
/agent/{user sub}/{session id}. The channel carries the owner's sub so the
namespace's onSubscribe handler can let a caller subscribe only to their own.

A channel whose publish fails is reported back as a batch item failure, so the
stream retries it; the event source mapping bounds the retries and sends what still
fails to a dead-letter queue. Nothing that failed is reported as published.
"""

import json
import logging
import os
from typing import Any

import botocore.auth
import botocore.awsrequest
import botocore.exceptions
import botocore.httpsession
import botocore.session

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

EVENTS_API_HTTP_ENDPOINT = os.environ.get("EVENTS_API_HTTP_ENDPOINT", "")
# Lambda always sets AWS_REGION; there is no region to guess.
REGION = os.environ["AWS_REGION"]


def _sign_and_post(url: str, body: bytes) -> tuple[int, str]:
    session = botocore.session.get_session()
    credentials = session.get_credentials().get_frozen_credentials()
    signer = botocore.auth.SigV4Auth(credentials, "appsync", REGION)

    request = botocore.awsrequest.AWSRequest(
        method="POST",
        url=url,
        data=body,
        headers={"Content-Type": "application/json"},
    )
    signer.add_auth(request)

    response = botocore.httpsession.URLLib3Session(timeout=10).send(request.prepare())
    return response.status_code, response.content.decode()


def _extract_message(record: dict[str, Any]) -> dict[str, Any] | None:
    if record.get("eventName") != "INSERT":
        return None

    new_image = record.get("dynamodb", {}).get("NewImage", {})
    if not new_image:
        return None

    sort_key = new_image.get("sort_key", {}).get("S", "")
    if not sort_key.startswith("msg#"):
        return None

    parts = sort_key.split("#", 2)
    if len(parts) < 3:
        return None

    session_id = parts[1]
    user_id = new_image.get("user_id", {}).get("S", "")
    # Both become path segments. A slash in either would let a value reshape the
    # channel into one that belongs to someone else.
    if not user_id or "/" in user_id or not session_id or "/" in session_id:
        logger.error("Dropping a session message with an unusable user or session id")
        return None

    return {
        "user_id": user_id,
        "session_id": session_id,
        "message_type": new_image.get("message_type", {}).get("S", ""),
        "content": new_image.get("content", {}).get("S", ""),
    }


def _build_appsync_event(message: dict[str, Any]) -> dict[str, Any]:
    msg_type = message["message_type"]
    content_str = message["content"]

    try:
        data = json.loads(content_str)
    except (json.JSONDecodeError, TypeError):
        data = {"content": content_str}

    return {"event": msg_type, "data": data}


class PublishError(RuntimeError):
    """AppSync Events did not accept every event of a publish."""


def _publish_to_channel(channel: str, events: list[dict[str, Any]]) -> None:
    url = f"https://{EVENTS_API_HTTP_ENDPOINT}/event"
    payload = json.dumps({
        "channel": channel,
        "events": [json.dumps(e) for e in events],
    }).encode()

    status, body = _sign_and_post(url, payload)
    # A 200 can still carry per-event failures.
    failed = json.loads(body).get("failed") if status == 200 and body else None
    if status != 200 or failed:
        raise PublishError(f"publish to {channel} failed: {status} {(failed or body)!s:.200}")
    logger.info("Published %d events to %s", len(events), channel)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    if not EVENTS_API_HTTP_ENDPOINT:
        raise RuntimeError("EVENTS_API_HTTP_ENDPOINT is not set; there is nowhere to publish agent events.")

    by_channel: dict[str, list[dict[str, Any]]] = {}
    first_record: dict[str, str] = {}
    for record in event.get("Records", []):
        message = _extract_message(record)
        if message is None:
            continue
        channel = f"/agent/{message['user_id']}/{message['session_id']}"
        by_channel.setdefault(channel, []).append(_build_appsync_event(message))
        first_record.setdefault(channel, record["dynamodb"]["SequenceNumber"])

    # Reported per channel: the stream resumes from the earliest failed record, so a
    # retry can re-send events that did publish. The page applies a repeat as a no-op.
    failures = []
    for channel, events_list in by_channel.items():
        try:
            _publish_to_channel(channel, events_list)
        except (PublishError, botocore.exceptions.HTTPClientError, botocore.exceptions.ConnectionError, ValueError):
            logger.exception("Agent events for %s will be retried", channel)
            failures.append({"itemIdentifier": first_record[channel]})
    return {"batchItemFailures": failures}
