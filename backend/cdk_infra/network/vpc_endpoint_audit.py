#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Which interface endpoints the VPC already has, and which this deployment creates.

The app reads the VPC's endpoints at synth time and creates one only for a
service that has none. Endpoints this deployment creates are tagged
`createdby=<parent stack name>`, so a later synth keeps them in the stack instead
of reading them as someone else's. Untagged endpoints were there before; they are
reused and never modified.

A conflict CloudFormation rejects loudly is left to fail. One that would deploy
cleanly and then route nothing stops synth, naming the endpoint and the fix.
"""

from dataclasses import dataclass, field

CREATED_BY_TAG = "createdby"
#: Endpoints stack name, given the parent stack name. CloudFormation tags what it
#: creates with aws:cloudformation:stack-name, which also marks endpoints this
#: stack created before it added the createdby tag.
ENDPOINTS_STACK_SUFFIX = "-VpcEndpoints"

#: An endpoint in one of these states serves, or is about to.
_USABLE_STATES = {"available", "pending"}
#: Kept in the describe output for a while after deletion; no longer there.
_GONE_STATES = {"deleted", "expired"}


@dataclass
class EndpointPlan:
    #: Every endpoint the stack declares: new ones, and ones it created before.
    create: list[str] = field(default_factory=list)
    #: service -> the id of an endpoint this deployment created before (in create too)
    kept: dict[str, str] = field(default_factory=dict)
    #: service -> the existing endpoint's id
    reuse: dict[str, str] = field(default_factory=dict)

    def summary(self) -> str:
        lines = [f"  keep   {s} ({self.kept[s]})" if s in self.kept else f"  create {s}" for s in self.create]
        lines += [f"  reuse  {s} ({vpce})" for s, vpce in self.reuse.items()]
        return "VPC interface endpoints:\n" + "\n".join(lines)


def classify(service: str, existing: list[dict], created_by: str) -> tuple[str, str]:
    """("create" | "reuse" | "stop", detail) for one service, given the VPC's
    endpoints for it as describe_vpc_endpoints returns them."""
    present = [e for e in existing if e["State"].lower() not in _GONE_STATES]
    if not present:
        return "create", ""
    if len(present) > 1:
        ids = ", ".join(e["VpcEndpointId"] for e in present)
        return "stop", (f"{service}: the VPC has {len(present)} endpoints for it ({ids}), so it is "
                        "unclear which one private DNS resolves to. Delete all but one.")
    endpoint = present[0]
    vpce = endpoint["VpcEndpointId"]
    tags = {t["Key"]: t["Value"] for t in endpoint.get("Tags", [])}
    if (tags.get(CREATED_BY_TAG) == created_by
            or tags.get("aws:cloudformation:stack-name") == created_by + ENDPOINTS_STACK_SUFFIX):
        return "create", vpce
    if endpoint["State"].lower() not in _USABLE_STATES:
        return "stop", (f"{service}: endpoint {vpce} is {endpoint['State']}. Delete it so "
                        "this deployment can create one, or repair it.")
    if not endpoint["PrivateDnsEnabled"]:
        return "stop", (f"{service}: endpoint {vpce} has private DNS off, so the regional hostname "
                        "still resolves to the public address and calls hang. Enable private DNS on "
                        "it, or delete it so this deployment can create one.")
    return "reuse", vpce


def plan_endpoints(services: list[str], endpoints: list[dict], *,
                   region: str, created_by: str) -> EndpointPlan:
    """The plan for every service, or ValueError naming every conflict at once."""
    plan, stops = EndpointPlan(), []
    for service in services:
        name = f"com.amazonaws.{region}.{service}"
        action, detail = classify(service, [e for e in endpoints if e["ServiceName"] == name], created_by)
        if action == "create":
            plan.create.append(service)
            if detail:
                plan.kept[service] = detail
        elif action == "reuse":
            plan.reuse[service] = detail
        else:
            stops.append(detail)
    if stops:
        raise ValueError("Existing VPC endpoints would deploy cleanly and not work:\n  "
                         + "\n  ".join(stops))
    return plan


def describe_interface_endpoints(vpc_id: str, region: str) -> list[dict]:
    """Every interface endpoint in the VPC. Uses the caller's AWS credentials."""
    import boto3

    pages = boto3.client("ec2", region_name=region).get_paginator("describe_vpc_endpoints").paginate(
        Filters=[{"Name": "vpc-id", "Values": [vpc_id]},
                 {"Name": "vpc-endpoint-type", "Values": ["Interface"]}])
    return [e for page in pages for e in page["VpcEndpoints"]]
