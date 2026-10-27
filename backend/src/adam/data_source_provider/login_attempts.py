#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""How many logins a user has typed that a registered database refused, per window.

The Connections page checks a typed username and password against a registered database
before it saves them. Unbounded, that is a way to guess any account's password on that
database. After MAX_FAILURES refusals in a WINDOW_SECONDS window a user's next check is
refused before anything is sent to the database, until the window ends.

The counts are this service's own table (LOGIN_ATTEMPTS_TABLE_NAME), one item per user per
window, which DynamoDB removes after it ends (expiresAt).
"""

import time

import boto3

from adam.shared.environment import require_env

MAX_FAILURES = 10
WINDOW_SECONDS = 15 * 60

_now = time.time


class TooManyFailures(Exception):
    """The user's typed logins were refused too often in this window."""


def _table():
    return boto3.resource("dynamodb").Table(require_env("LOGIN_ATTEMPTS_TABLE_NAME"))


def _window() -> int:
    return int(_now()) // WINDOW_SECONDS


def require_allowed(user_id: str) -> None:
    """Raise TooManyFailures when the user has used up this window's refusals."""
    item = _table().get_item(Key={"userId": user_id, "window": _window()}, ConsistentRead=True).get("Item")
    if item and int(item["failures"]) >= MAX_FAILURES:
        raise TooManyFailures(
            f"Too many logins were refused in the last {WINDOW_SECONDS // 60} minutes. Wait, then try again.")


def record_failure(user_id: str) -> None:
    window = _window()
    _table().update_item(
        Key={"userId": user_id, "window": window},
        UpdateExpression="ADD failures :one SET expiresAt = :end",
        ExpressionAttributeValues={":one": 1, ":end": (window + 1) * WINDOW_SECONDS + WINDOW_SECONDS})
