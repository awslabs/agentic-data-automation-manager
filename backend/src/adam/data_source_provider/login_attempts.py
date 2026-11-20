#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""How many logins a user has typed that a registered database refused, per window.

The Connections page checks a typed username and password against a registered database
before it saves them. Unbounded, that is a way to guess any account's password on that
database. After MAX_FAILURES refusals in a WINDOW_SECONDS window a user's next check is
refused before anything is sent to the database, until the window ends. Each check is counted
as refused before it is sent and taken back once the database accepts it, so checks sent together
count against the limit at once.

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


def reserve(user_id: str) -> int:
    """Count a check as refused before it is sent, in one conditional write, so checks sent
    together cannot all pass a count none of them has added to yet. Raises TooManyFailures when
    the user has used up this window's refusals. Returns the window, for release()."""
    window, table = _window(), _table()
    try:
        table.update_item(
            Key={"userId": user_id, "window": window},
            UpdateExpression="ADD failures :one SET expiresAt = :end",
            ConditionExpression="attribute_not_exists(failures) OR failures < :max",
            ExpressionAttributeValues={":one": 1, ":max": MAX_FAILURES,
                                       ":end": (window + 1) * WINDOW_SECONDS + WINDOW_SECONDS})
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        raise TooManyFailures(
            f"Too many logins were refused in the last {WINDOW_SECONDS // 60} minutes. Wait, then try again.") from None
    return window


def release(user_id: str, window: int) -> None:
    """Take back the count reserve() added for a check the database did not refuse, from the
    window it was added to."""
    table = _table()
    try:
        table.update_item(Key={"userId": user_id, "window": window}, UpdateExpression="ADD failures :minus",
                          ConditionExpression="failures > :zero",
                          ExpressionAttributeValues={":minus": -1, ":zero": 0})
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        # Its window has ended and DynamoDB removed the item: there is nothing left to take back.
        pass
