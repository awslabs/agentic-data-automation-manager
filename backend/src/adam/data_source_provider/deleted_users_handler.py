#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Deletes the saved logins of users who are no longer in the user pool.

Run daily by its schedule. Every connection is keyed by its user's sub; a user the pool no
longer has (an administrator deleted them) cannot sign in to use them, so their secrets and
rows are deleted. A disabled user is still in the pool and keeps theirs: they may be enabled
again.

It deletes nothing when it cannot be sure. Any error from Cognito stops the run, and when
every user with connections looks absent -- the sign of a wrong pool, not of everyone leaving
at once -- it logs an error and stops.
"""

import logging

import boto3

from adam.shared.environment import require_env

logger = logging.getLogger()
logger.setLevel(logging.INFO)


class Refused(RuntimeError):
    """The run would delete every user's logins; nothing was deleted."""


def _connections() -> dict[str, list[dict]]:
    """Every connection, by its user's sub."""
    table, by_user, start = boto3.resource("dynamodb").Table(require_env("CONNECTIONS_TABLE_NAME")), {}, None
    while True:
        args = {"ProjectionExpression": "userId, connectionId, secretName"}
        if start:
            args["ExclusiveStartKey"] = start
        page = table.scan(**args)
        for item in page.get("Items", []):
            by_user.setdefault(item["userId"], []).append(item)
        start = page.get("LastEvaluatedKey")
        if not start:
            return by_user


def _in_pool(cognito, pool_id: str, sub: str) -> bool:
    if '"' in sub or "\\" in sub:
        raise ValueError(f"A connection is keyed by {sub!r}, which is not a Cognito sub.")
    return bool(cognito.list_users(UserPoolId=pool_id, Filter=f'sub = "{sub}"', Limit=1)["Users"])


def handler(event, context):
    pool_id = require_env("USER_POOL_ID")
    cognito = boto3.client("cognito-idp")
    by_user = _connections()
    gone = [sub for sub in by_user if not _in_pool(cognito, pool_id, sub)]
    if by_user and len(gone) == len(by_user):
        logger.error("Every one of the %d users with connections is absent from pool %s; deleting nothing",
                     len(by_user), pool_id)
        raise Refused(f"Every user with connections is absent from {pool_id}: a wrong pool, not deleted users.")

    table = boto3.resource("dynamodb").Table(require_env("CONNECTIONS_TABLE_NAME"))
    secrets = boto3.client("secretsmanager")
    deleted = 0
    for sub in gone:
        for row in by_user[sub]:
            if row.get("secretName"):
                try:
                    secrets.delete_secret(SecretId=row["secretName"], ForceDeleteWithoutRecovery=True)
                except secrets.exceptions.ResourceNotFoundException:
                    pass
            table.delete_item(Key={"userId": sub, "connectionId": row["connectionId"]})
            deleted += 1
        logger.info("User %s is no longer in the pool: deleted their %d connection(s)", sub, len(by_user[sub]))
    return {"usersChecked": len(by_user), "usersGone": len(gone), "connectionsDeleted": deleted}
