#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What the stage changes, read from the synthesized templates of both stages.

prod keeps users' data (point-in-time recovery, deletion protection, retained on stack
delete) and a year of logs, and accepts no sign-in from the local dev server. dev keeps
none of it and tears down clean. Every function writes to a log group whose retention is
set: none is left to the account's never-expire default.
"""

import os

import pytest
from aws_cdk.assertions import Template

from tests.unit.cdk_stacks import BACKEND, build_stacks

# Each holds data users or administrators put there: conversations, logins, registered
# databases, registered scanners.
USER_DATA_TABLES = {("agent", "SessionsTable"), ("external", "ConnectionsTable"),
                    ("glue", "ScanTargetsTable"), ("glue", "ScanProvidersTable")}
LOCAL_DEV_SERVER = "http://localhost:5173/"
CDK_AUTO_DELETE_HANDLER = "CustomS3AutoDeleteObjectsCustomResourceProviderHandler"


def _templates(stage: str) -> dict[str, Template]:
    previous = os.getcwd()
    os.chdir(BACKEND)
    try:
        return {name: Template.from_stack(stack) for name, stack in build_stacks(stage=stage).items()}
    finally:
        os.chdir(previous)


@pytest.fixture(scope="module")
def dev():
    return _templates("dev")


@pytest.fixture(scope="module")
def prod():
    return _templates("prod")


def _resource(templates, stack: str, prefix: str) -> dict:
    (found,) = [r for k, r in templates[stack].to_json()["Resources"].items() if k.startswith(prefix)
                and r["Type"] == "AWS::DynamoDB::Table"]
    return found


def _user_pool(templates) -> dict:
    (pool,) = templates["auth"].find_resources("AWS::Cognito::UserPool").values()
    return pool


def _web_client(templates) -> dict:
    return next(c["Properties"] for c in templates["auth"].find_resources("AWS::Cognito::UserPoolClient").values()
                if not c["Properties"].get("GenerateSecret"))


class TestProdKeepsUsersData:

    @pytest.mark.parametrize(("stack", "table"), sorted(USER_DATA_TABLES))
    def test_a_table_of_users_data_is_backed_up_protected_and_kept(self, prod, stack, table):
        found = _resource(prod, stack, table)
        assert found["Properties"]["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
        assert found["Properties"]["DeletionProtectionEnabled"] is True
        assert found["DeletionPolicy"] == "Retain"

    def test_the_user_pool_is_protected_and_kept(self, prod):
        pool = _user_pool(prod)
        assert pool["Properties"]["DeletionProtection"] == "ACTIVE"
        assert pool["DeletionPolicy"] == "Retain"

    def test_logs_are_kept_a_year_and_survive_the_stack(self, prod):
        for template in prod.values():
            for group in template.find_resources("AWS::Logs::LogGroup").values():
                assert group["Properties"]["RetentionInDays"] == 365
                assert group["DeletionPolicy"] == "Retain"

    def test_only_the_website_can_sign_in(self, prod):
        """Exactly the website's own URL: no local dev server, no other localhost, nothing else."""
        client = _web_client(prod)
        assert client["CallbackURLs"] == client["LogoutURLs"] == ["https://d1.cloudfront.net/"]


class TestDevTearsDownClean:

    @pytest.mark.parametrize(("stack", "table"), sorted(USER_DATA_TABLES))
    def test_a_table_goes_with_its_stack(self, dev, stack, table):
        found = _resource(dev, stack, table)
        # Point-in-time recovery stays on in every stage; it does not keep a table from going.
        assert found["Properties"]["PointInTimeRecoverySpecification"] == {"PointInTimeRecoveryEnabled": True}
        assert found["Properties"]["DeletionProtectionEnabled"] is False
        assert found["DeletionPolicy"] == "Delete"

    def test_the_user_pool_goes_with_its_stack(self, dev):
        pool = _user_pool(dev)
        assert pool["Properties"].get("DeletionProtection", "INACTIVE") == "INACTIVE"
        assert pool["DeletionPolicy"] == "Delete"

    def test_logs_are_kept_a_month(self, dev):
        for template in dev.values():
            for group in template.find_resources("AWS::Logs::LogGroup").values():
                assert group["Properties"]["RetentionInDays"] == 30
                assert group["DeletionPolicy"] == "Delete"

    def test_the_local_dev_server_can_sign_in(self, dev):
        client = _web_client(dev)
        assert LOCAL_DEV_SERVER in client["CallbackURLs"] and LOCAL_DEV_SERVER in client["LogoutURLs"]


@pytest.mark.parametrize("stage", ["dev", "prod"])
def test_every_function_writes_to_a_log_group_whose_retention_is_set(stage, dev, prod):
    """A function with no log group of its own writes to /aws/lambda/<name>, which Lambda
    creates on first use with no retention: kept forever."""
    templates = {"dev": dev, "prod": prod}[stage]
    for name, template in templates.items():
        groups = template.find_resources("AWS::Logs::LogGroup")
        for logical_id, function in template.find_resources("AWS::Lambda::Function").items():
            # CDK's own handler for a bucket's auto_delete_objects offers no log group
            # setting; it runs only when its stack is created or deleted.
            if logical_id.startswith(CDK_AUTO_DELETE_HANDLER):
                continue
            group = function["Properties"].get("LoggingConfig", {}).get("LogGroup", {}).get("Ref")
            assert group in groups, f"{name}/{logical_id} has no log group of its own"
            assert "RetentionInDays" in groups[group]["Properties"]


def test_no_optional_name_is_fixed(dev):
    """Names the service does not require are generated, so a resource can be replaced and
    two stages never collide. (The gateway, runtime, AppSync API, Glue database, Cognito
    domain and log delivery names are required, and carry the stage's prefix.)"""
    assert all("TableName" not in t["Properties"]
               for template in dev.values() for t in template.find_resources("AWS::DynamoDB::Table").values())
    assert "UserPoolName" not in _user_pool(dev)["Properties"]
    assert dev["auth"].find_resources("AWS::Cognito::IdentityPool") == {}


def test_every_given_private_subnet_is_used():
    """app.py accepts one subnet per zone; a third zone's subnet must not be dropped."""
    from cdk_infra.shared.imported_vpc import private_subnets
    assert private_subnets("subnet-a, subnet-b,subnet-c") == ["subnet-a", "subnet-b", "subnet-c"]
    with pytest.raises(ValueError, match="empty entry"):
        private_subnets("subnet-a,,subnet-c")
