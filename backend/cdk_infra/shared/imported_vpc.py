#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The existing VPC every stack deploys into, rebuilt from the values app.py passes in."""

from aws_cdk import aws_ec2 as ec2
from constructs import Construct


def private_subnets(private_subnet_ids: str) -> list[str]:
    """Every private subnet, from the comma-separated list app.py passes in (one per zone)."""
    subnets = [s.strip() for s in private_subnet_ids.split(",")]
    if not all(subnets):
        raise ValueError(f"privateSubnetIds has an empty entry: {private_subnet_ids!r}")
    return subnets


def import_vpc(scope: Construct, *, vpc_id: str, availability_zones: list[str],
               private_subnet_ids: str, vpc_cidr_block: str | None = None) -> ec2.IVpc:
    """from_vpc_attributes pairs zones to subnets by index, so the zones are the subnets'
    own (app.py checks them against the subnets), never a guess."""
    return ec2.Vpc.from_vpc_attributes(scope, "Vpc",
        vpc_id=vpc_id,
        availability_zones=availability_zones,
        private_subnet_ids=private_subnets(private_subnet_ids),
        vpc_cidr_block=vpc_cidr_block,
    )
