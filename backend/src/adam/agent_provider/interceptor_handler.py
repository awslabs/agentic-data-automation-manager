#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Gateway REQUEST interceptor: puts the caller's verified subject on every tool call.

Runs before every MCP request reaches a Lambda tool target. The interceptor's payload carries
the request and its headers but no claims, so the only identity is the bearer token. The
Gateway validates it first; this verifies it again (signature against the issuer's keys,
issuer, expiry, client, an access token) through the configured IdentityProvider, so the
subject is proven, not caller-supplied.

Discovery (`initialize`, `tools/list`, ...) is the same for every caller and needs no
subject; `tools/call` acts on one user's data sources and requires one.

For an MCP target the body is the only channel to the tool: `transformedGatewayRequest`
carries `body` alone, and a Lambda target's event is its input-schema arguments. So the
subject goes into `params.arguments` under SUBJECT_ARGUMENT, which every tool declares, and
any value a caller put there is discarded first, on every request.
"""

import logging

from agent_provider.gateway_event import SUBJECT_ARGUMENT
from agent_provider.identity_provider import (
    IdentityProvider,
    identity_provider_from_env,
)

logger = logging.getLogger()
logger.setLevel(logging.INFO)

# Discovery and lifecycle methods. These return the same thing for every
# caller and touch no user-owned resource, so they carry no subject.
METHODS_WITHOUT_IDENTITY = frozenset({
    "initialize",
    "notifications/initialized",
    "notifications/cancelled",
    "ping",
    "tools/list",
    "resources/list",
    "resources/templates/list",
    "prompts/list",
    "completion/complete",
})

_identity_provider: IdentityProvider | None = None


def _get_identity_provider() -> IdentityProvider:
    """Built once per container. Holds only the issuer's public signing keys."""
    global _identity_provider
    if _identity_provider is None:
        _identity_provider = identity_provider_from_env()
    return _identity_provider


def _verified_subject(headers: dict) -> str:
    """Verify the caller's bearer token and return its subject."""
    authorization = ""
    for key, value in headers.items():
        if key.lower() == "authorization":
            authorization = value or ""
            break

    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise RuntimeError(
            "No bearer token on an MCP request that requires user identity."
        )

    return _get_identity_provider().verify(token).subject


def _method_of(body) -> str:
    """Pull the JSON-RPC method out of the request body."""
    if isinstance(body, dict):
        return body.get("method", "") or ""
    # MCP targets receive a parsed body. Anything else means the contract
    # changed, and guessing the method would mean guessing whether this request
    # needs a user. Refuse instead.
    raise RuntimeError(
        f"Expected a parsed JSON-RPC body, got {type(body).__name__}. Cannot "
        "determine whether this request requires user identity."
    )


def _without_subject(body: dict) -> dict:
    """Drop any caller-supplied subject argument and discard its value.

    Runs on every method, including the ones that need no identity: a
    `tools/list` whose body was stuffed with SUBJECT_ARGUMENT must not carry it
    onward just because that branch adds nothing of its own.

    The value goes nowhere. It is not returned, not logged, and not held in a
    variable that the injection step can reach -- the whole point is that a
    string the caller chose can never be the string the target reads.

    Only `params.arguments` is scrubbed because that is the only part of the
    body a Lambda target ever sees: its `event` is the map of the tool's
    inputSchema properties. Anything stuffed elsewhere in the body cannot reach
    the tool to begin with.
    """
    params = body.get("params")
    if not isinstance(params, dict):
        return body
    arguments = params.get("arguments")
    if not isinstance(arguments, dict) or SUBJECT_ARGUMENT not in arguments:
        return body

    logger.warning(
        "Request arrived with a caller-supplied %s argument. Discarding it.",
        SUBJECT_ARGUMENT,
    )
    stripped = {k: v for k, v in arguments.items() if k != SUBJECT_ARGUMENT}
    return {**body, "params": {**params, "arguments": stripped}}


def _with_subject(body: dict, subject: str) -> dict:
    """Set the verified subject on a body that `_without_subject` already cleaned. It reads
    nothing from the body's arguments, so the value just discarded cannot come back."""
    params = dict(body.get("params", {}))
    arguments = dict(params.get("arguments", {}))
    arguments[SUBJECT_ARGUMENT] = subject
    params["arguments"] = arguments
    return {**body, "params": params}


def handler(event, context):
    mcp = event.get("mcp", {})
    request = mcp.get("gatewayRequest", {})
    headers = request.get("headers", {})
    body = request.get("body", "")

    method = _method_of(body)

    # Unconditionally, before the method is even considered: no caller-supplied
    # subject argument survives this line on any request.
    body = _without_subject(body)

    if method in METHODS_WITHOUT_IDENTITY:
        logger.info("Method %s needs no subject; forwarding without one.", method)
        transformed_body = body
    else:
        user_id = _verified_subject(headers)
        transformed_body = _with_subject(body, user_id)
        logger.info("Method %s: injected verified subject %s", method, user_id)

    # Only `body`. The MCP transformedGatewayRequest has no other field, so
    # there is nowhere here to leak the caller's bearer token even by accident.
    return {
        "interceptorOutputVersion": "1.0",
        "mcp": {
            "transformedGatewayRequest": {
                "body": transformed_body,
            }
        }
    }
