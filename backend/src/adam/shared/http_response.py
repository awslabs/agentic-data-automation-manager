#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Shared HTTP API Gateway response helpers with SSM-based origin validation."""

import functools
import json
import logging
import os
import re
import time
from decimal import Decimal
from typing import Any

import boto3

logger = logging.getLogger(__name__)

_ssm_client = None
_allowed_origins = None
_allowed_origins_read_at = 0.0
_stack_name = os.environ.get("STACK_NAME", "")

# How long a container trusts the origin list it read. Short, so an origin added or removed
# by hand (origin_frontend_local for a developer's local server) takes effect in about a
# minute without a redeploy or waiting for a cold start.
ORIGIN_CACHE_SECONDS = 60
_now = time.monotonic


class UnauthenticatedError(Exception):
    """The request carried no verifiable caller identity.

    Raised rather than substituting a placeholder subject. Every connection and
    every conversation is partitioned by that subject, so a stand-in like
    "anonymous" would not fail — it would
    succeed against a partition shared by every caller whose claims were
    missing. Surfaced as 401 by route_request.
    """


class ForbiddenError(Exception):
    """The caller is who they say they are but may not do this. Surfaced as 403."""


class Rejected(Exception):
    """The request is wrong in a way the caller can fix; its message is safe to show."""


def bad_request(fn):
    """Turn a Rejected into a 400 and leave every other failure to the router."""
    @functools.wraps(fn)
    def wrapper(event, path_params, query_params, body):
        try:
            return fn(event, path_params, query_params, body)
        except Rejected as e:
            return error(str(e), 400)
    return wrapper


def get_user_id(event: dict) -> str:
    """Return the verified Cognito subject, or refuse the request.

    The JWT authorizer on the HTTP API has already validated the token by the
    time we see this event, so a missing `sub` means the route was reached
    without one — never that the caller is a legitimate guest. Returning a
    placeholder here would file that caller's database connections under an id
    shared with every other caller in the same state.
    """
    try:
        sub = event["requestContext"]["authorizer"]["jwt"]["claims"]["sub"]
    except (KeyError, TypeError):
        raise UnauthenticatedError(
            "Request carried no verified Cognito subject; sign in again."
        ) from None
    if not isinstance(sub, str) or not sub.strip():
        raise UnauthenticatedError("Request carried a subject that is not a user id.")
    return sub


def caller_groups(event: dict) -> set[str]:
    """The Cognito groups in the verified token, as a set of exact names.

    HTTP API delivers a list claim to the Lambda as a string -- "[admin ops]" --
    not as an array, so this accepts that form, a JSON array, and a real list.
    Group names cannot contain whitespace, so splitting on it is exact.
    """
    try:
        raw = event["requestContext"]["authorizer"]["jwt"]["claims"].get("cognito:groups", "")
    except (KeyError, TypeError, AttributeError):
        return set()
    if isinstance(raw, list):
        return {str(g) for g in raw}
    text = str(raw).strip()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return {str(g) for g in parsed}
        except json.JSONDecodeError:
            pass
        text = text[1:-1]
    return set(text.split())


def require_group(event: dict, group: str) -> None:
    if group not in caller_groups(event):
        raise ForbiddenError(f"This action requires membership of the {group!r} group.")


class _Encoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:
        if isinstance(o, Decimal):
            return float(o) if o % 1 else int(o)
        return super().default(o)


def get_allowed_origins() -> dict:
    """Fetch all /{stack_name}/origin_* SSM params. Cached for ORIGIN_CACHE_SECONDS.

    Raises on every failure path. An empty allowlist is not a degraded mode of
    this function, it is an unusable one: the only caller treats "no allowlist"
    as "reflect whatever Origin the request asked for", so silently returning
    {} here turns a missing env var or a throttled SSM call into CORS
    allow-all. Failing the request is recoverable; quietly serving every
    origin with credentials is not.
    """
    global _allowed_origins, _allowed_origins_read_at, _ssm_client
    if _allowed_origins is not None and _now() - _allowed_origins_read_at < ORIGIN_CACHE_SECONDS:
        return _allowed_origins

    if not _stack_name:
        raise RuntimeError(
            "STACK_NAME is not set; cannot resolve the CORS origin allowlist. "
            "This function is misconfigured -- check its CDK environment."
        )

    if not _ssm_client:
        _ssm_client = boto3.client("ssm")

    origins = {}
    path = f"/{_stack_name}"
    next_token = ""
    while next_token is not None:
        args = {"Path": path, "Recursive": True, "MaxResults": 10}
        if next_token:
            args["NextToken"] = next_token
        response = _ssm_client.get_parameters_by_path(**args)
        for p in response["Parameters"]:
            name = p["Name"].replace(f"/{_stack_name}/", "")
            if name.startswith("origin_"):
                value = p["Value"]
                if name == "origin_frontend" and not value.startswith("http"):
                    value = "https://" + value
                origins[name] = value
        next_token = response.get("NextToken")

    if not origins:
        raise RuntimeError(
            f"No origin_* parameters found under /{_stack_name}; refusing to "
            "serve requests with no CORS allowlist to check them against."
        )

    # Assigned only once fully built, so a mid-pagination failure cannot leave
    # a partial allowlist cached for the life of the container.
    _allowed_origins = origins
    _allowed_origins_read_at = _now()
    logger.info("Allowed origins: %s", list(_allowed_origins.values()))
    return _allowed_origins


def _validate_origin(event: dict) -> str:
    """Return the request Origin if it's in the allowed list, else empty string."""
    headers = event.get("headers") or {}
    origin = headers.get("origin", "")
    if not origin:
        return ""

    if origin in get_allowed_origins().values():
        return origin
    return ""


def _cors_headers(origin: str) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Access-Control-Allow-Headers": "Authorization,Content-Type",
        "Access-Control-Allow-Methods": "GET,POST,PATCH,DELETE,OPTIONS",
    }
    if origin:
        # Echoed only for an origin on the allowlist, never "*". No Allow-Credentials:
        # the token travels in the Authorization header, which does not need it.
        headers["Access-Control-Allow-Origin"] = origin
        headers["Access-Control-Max-Age"] = "600"      # how long a browser may reuse a preflight
        headers["Vary"] = "Origin"
    return headers


def success(body: Any, status_code: int = 200, *, origin: str = "") -> dict:
    return {
        "statusCode": status_code,
        "headers": _cors_headers(origin),
        "body": json.dumps(body, cls=_Encoder, default=str),
    }


_ARN_RE = re.compile(r"arn:aws[a-z\-]*:[^:\s]+:[^:\s]*:\d{12}:[^\s,\"']+")
_ACCOUNT_RE = re.compile(r"\b\d{12}\b")


def sanitize_error(message: str) -> str:
    """Strip ARNs, account numbers, and resource identifiers from error messages."""
    sanitized = _ARN_RE.sub("[REDACTED]", message)
    sanitized = _ACCOUNT_RE.sub("[REDACTED]", sanitized)
    return sanitized


def error(message: str, status_code: int = 400, *, origin: str = "") -> dict:
    return {
        "statusCode": status_code,
        "headers": _cors_headers(origin),
        "body": json.dumps({"error": sanitize_error(message)}),
    }


def route_request(event: dict, routes: dict) -> dict:
    """Dispatch an HTTP API Gateway v2 event to the matching handler.

    routes is a dict like {"GET /api/connections": handler_fn, ...}.
    Each handler_fn receives (event, path_params, query_params, body).
    """
    origin = _validate_origin(event)
    route_key = event.get("routeKey", "")

    if route_key.startswith("OPTIONS "):
        return {"statusCode": 204, "headers": _cors_headers(origin), "body": ""}

    handler = routes.get(route_key)

    if handler is None and "{proxy+}" in route_key:
        http_ctx = event.get("requestContext", {}).get("http", {})
        method = http_ctx.get("method", "GET")
        path = http_ctx.get("path", "")
        actual_key = f"{method} {path}"
        handler = routes.get(actual_key)
        if handler is None:
            for pattern, fn in routes.items():
                pat_method, pat_path = pattern.split(" ", 1)
                if pat_method != method:
                    continue
                parts = pat_path.split("/")
                path_parts = path.split("/")
                if len(parts) != len(path_parts):
                    continue
                match = True
                extracted = {}
                for pp, tp in zip(path_parts, parts):
                    if tp.startswith("{") and tp.endswith("}"):
                        extracted[tp[1:-1]] = pp
                    elif pp != tp:
                        match = False
                        break
                if match:
                    handler = fn
                    event.setdefault("pathParameters", {}).update(extracted)
                    break

    if handler is None:
        return error(f"Unknown route: {route_key}", 404, origin=origin)

    path_params = event.get("pathParameters") or {}
    query_params = event.get("queryStringParameters") or {}
    body = None
    if event.get("body"):
        try:
            body = json.loads(event["body"])
        except (json.JSONDecodeError, TypeError):
            body = event["body"]

    try:
        result = handler(event, path_params, query_params, body)
        if isinstance(result, dict) and "headers" in result:
            result["headers"].update(_cors_headers(origin))
        return result
    except ForbiddenError as e:
        logger.warning("Forbidden request for %s: %s", route_key, e)
        return error(str(e), 403, origin=origin)
    except UnauthenticatedError as e:
        # 401, not 500: the request is well-formed, it just isn't attributable
        # to anyone. A 500 would read as our bug and invite a retry.
        logger.warning("Unauthenticated request for %s: %s", route_key, e)
        return error(str(e), 401, origin=origin)
    except Exception:
        # What failed, and why, is ours to read in the log; the caller gets a reference to it.
        reference = (event.get("requestContext") or {}).get("requestId", "")
        logger.exception("Handler error for %s (request %s)", route_key, reference)
        return error(f"Internal error. Reference: {reference}", 500, origin=origin)
