#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The Redshift test workgroup is a database, so it gets a database's exposure rules:
private, and reachable only by the functions that query it."""

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

from cdk_infra.test_targets.redshift_test_target_stack import REDSHIFT_PORT, RedshiftTestTargetStack
from tests.unit.cdk_stacks import ENV, PREFIX, VPC, app_context


@pytest.fixture(scope="module")
def template() -> Template:
    app = cdk.App(context=app_context())
    return Template.from_stack(RedshiftTestTargetStack(app, "T-RedshiftTestTarget", parent_stack_name=PREFIX,
                                                       **VPC, env=ENV))


def test_the_workgroup_is_private_and_in_the_apps_subnets(template):
    (workgroup,) = template.find_resources("AWS::RedshiftServerless::Workgroup").values()
    props = workgroup["Properties"]
    assert props["PubliclyAccessible"] is False
    assert props["SubnetIds"] == VPC["private_subnet_ids"].split(",")
    assert props["BaseCapacity"] <= 8  # a test target: the smallest sizes only


def test_only_the_app_security_group_may_connect_and_only_on_the_redshift_port(template):
    ((group_id, group),) = template.find_resources("AWS::EC2::SecurityGroup").items()
    ingress = group["Properties"].get("SecurityGroupIngress", [])
    assert ingress == [{"IpProtocol": "tcp", "FromPort": REDSHIFT_PORT, "ToPort": REDSHIFT_PORT,
                        "SourceSecurityGroupId": VPC["app_sg_id"], "Description": "Provider functions"}]
    # And it is the workgroup's only group: a rule on a group the workgroup does not use guards nothing.
    (workgroup,) = template.find_resources("AWS::RedshiftServerless::Workgroup").values()
    assert workgroup["Properties"]["SecurityGroupIds"] == [{"Fn::GetAtt": [group_id, "GroupId"]}]


def test_the_admin_password_is_never_in_the_template(template):
    (namespace,) = template.find_resources("AWS::RedshiftServerless::Namespace").values()
    assert "{{resolve:secretsmanager:" in str(namespace["Properties"]["AdminUserPassword"])
