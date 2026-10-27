#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A user the pool no longer has loses their saved logins; nobody else does, and when the
answer cannot be trusted nothing is deleted.

The real handler, with connections in DynamoDB Local and the user pool and secrets in moto.
"""

import uuid

import boto3
import pytest

from adam.data_source_provider import deleted_users_handler as cleanup

pytestmark = pytest.mark.integration


@pytest.fixture
def pool(registry, moto_endpoint, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL_COGNITO_IDENTITY_PROVIDER", moto_endpoint)
    cognito = boto3.client("cognito-idp")
    pool_id = cognito.create_user_pool(PoolName=f"users-{uuid.uuid4().hex[:8]}")["UserPool"]["Id"]
    monkeypatch.setenv("USER_POOL_ID", pool_id)

    def user(email):
        cognito.admin_create_user(UserPoolId=pool_id, Username=email, MessageAction="SUPPRESS",
                                  UserAttributes=[{"Name": "email", "Value": email}])
        return next(a["Value"] for a in cognito.admin_get_user(UserPoolId=pool_id, Username=email)["UserAttributes"]
                    if a["Name"] == "sub")
    try:
        yield cognito, pool_id, user
    finally:
        cognito.delete_user_pool(UserPoolId=pool_id)


def _connect(registry, sub) -> tuple[str, str]:
    cid = registry.add(sub, "postgres", "login", "pw", targetId="t-any")
    return cid, registry.row(sub, cid)["secretName"]


def _secret_exists(name) -> bool:
    try:
        boto3.client("secretsmanager").describe_secret(SecretId=name)
        return True
    except boto3.client("secretsmanager").exceptions.ResourceNotFoundException:
        return False


def _rows(registry, sub) -> list:
    return boto3.client("dynamodb").query(TableName=registry.table, KeyConditionExpression="userId = :u",
                                          ExpressionAttributeValues={":u": {"S": sub}})["Items"]


def test_a_deleted_users_logins_go_and_everyone_elses_stay(registry, pool):
    cognito, pool_id, user = pool
    staying, leaving = user("stays@example.com"), user("leaves@example.com")
    kept, (_, gone_secret) = _connect(registry, staying), _connect(registry, leaving)
    _connect(registry, leaving)
    cognito.admin_delete_user(UserPoolId=pool_id, Username="leaves@example.com")

    result = cleanup.handler({}, None)

    assert result == {"usersChecked": 2, "usersGone": 1, "connectionsDeleted": 2}
    assert _rows(registry, leaving) == [] and not _secret_exists(gone_secret)
    assert len(_rows(registry, staying)) == 1 and _secret_exists(kept[1])


def test_a_disabled_user_keeps_theirs(registry, pool):
    cognito, pool_id, user = pool
    active, disabled = user("active@example.com"), user("disabled@example.com")
    _connect(registry, active)
    _, secret = _connect(registry, disabled)
    cognito.admin_disable_user(UserPoolId=pool_id, Username="disabled@example.com")
    assert cleanup.handler({}, None)["connectionsDeleted"] == 0
    assert _secret_exists(secret)


def test_when_every_user_looks_absent_nothing_is_deleted(registry, pool, monkeypatch):
    """The sign of a wrong pool: the run stops instead of deleting everyone's logins."""
    cognito, _, user = pool
    one = user("one@example.com")
    _, secret = _connect(registry, one)
    other = cognito.create_user_pool(PoolName=f"other-{uuid.uuid4().hex[:8]}")["UserPool"]["Id"]
    try:
        monkeypatch.setenv("USER_POOL_ID", other)
        with pytest.raises(cleanup.Refused):
            cleanup.handler({}, None)
    finally:
        cognito.delete_user_pool(UserPoolId=other)
    assert _secret_exists(secret) and len(_rows(registry, one)) == 1


def test_a_cognito_error_stops_the_run_and_deletes_nothing(registry, pool, monkeypatch):
    _, _, user = pool
    keep = user("keep@example.com")
    _, secret = _connect(registry, keep)
    monkeypatch.setenv("USER_POOL_ID", "us-east-1_doesnotexist")
    with pytest.raises(Exception, match="(?i)not ?found|does not exist"):
        cleanup.handler({}, None)
    assert _secret_exists(secret) and len(_rows(registry, keep)) == 1


def test_with_no_connections_there_is_nothing_to_do(registry, pool):
    assert cleanup.handler({}, None) == {"usersChecked": 0, "usersGone": 0, "connectionsDeleted": 0}
