#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""app.py decides what to build from the context it is given.

Run through `cdk ls`, which executes the real app and prints the stacks it
defines, so a wiring mistake in either mode fails here and not at deploy time.

The app deploys into an existing VPC only. Without all five network values it is
an error that names what is missing.

The app reads the VPC's existing endpoints with boto3; here it reads them from a
stub EC2 API on localhost (AWS_ENDPOINT_URL_EC2), so no AWS account is involved.
"""

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

import pytest

BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cdk_infra.network.vpc_endpoints_stack import (  # noqa: E402
    REQUIRED_INTERFACE_SERVICES,
    RUNTIME_INTERFACE_SERVICES,
)

NETWORK = {
    "vpcId": "vpc-0123456789abcdef0",
    "appSgId": "sg-0123456789abcdef0",
    "privateSubnetIds": "subnet-0aaaaaaaaaaaaaaaa,subnet-0bbbbbbbbbbbbbbbb",
    "availabilityZones": "us-east-1b,us-east-1c",
    "vpcCidrBlock": "10.0.0.0/16",
}
DEPLOYABLE = NETWORK
NO_CHAT = {**NETWORK, "enableChat": "false"}
REQUIRED = tuple(REQUIRED_INTERFACE_SERVICES)
RUNTIME = tuple(RUNTIME_INTERFACE_SERVICES)


@pytest.fixture(autouse=True)
def _cdk():
    if shutil.which("cdk") is None:
        pytest.fail("the cdk CLI is required", pytrace=False)


class _StubEc2(BaseHTTPRequestHandler):
    """Answers DescribeVpcEndpoints with `endpoints`, and records the VPC asked about."""

    endpoints: list[dict] = []
    asked_vpc_ids: list[str] = []

    def do_POST(self):
        form = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
        assert form["Action"] == ["DescribeVpcEndpoints"], form
        filters = {form[k][0]: form[k.replace("Name", "Value.1")][0]
                   for k in form if k.startswith("Filter.") and k.endswith(".Name")}
        _StubEc2.asked_vpc_ids.append(filters["vpc-id"])
        # The cdk CLI may replace CDK_DEFAULT_REGION with the caller's own, so
        # answer in the region the request was signed for.
        region = self.headers["Authorization"].split("Credential=")[1].split("/")[2]
        items = "".join(
            f"<item><vpcEndpointId>{e['id']}</vpcEndpointId><vpcEndpointType>Interface</vpcEndpointType>"
            f"<vpcId>{filters['vpc-id']}</vpcId><serviceName>com.amazonaws.{region}.{e['service']}</serviceName>"
            f"<state>{e.get('state', 'available')}</state>"
            f"<privateDnsEnabled>{str(e.get('private_dns', True)).lower()}</privateDnsEnabled>"
            "<tagSet>"
            + "".join(f"<item><key>{k}</key><value>{v}</value></item>" for k, v in e.get("tags", {}).items())
            + "</tagSet>"
            "</item>"
            for e in _StubEc2.endpoints)
        body = ('<DescribeVpcEndpointsResponse xmlns="http://ec2.amazonaws.com/doc/2016-11-15/">'
                f"<requestId>stub</requestId><vpcEndpointSet>{items}</vpcEndpointSet>"
                "</DescribeVpcEndpointsResponse>").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture(scope="module", autouse=True)
def stub_ec2():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StubEc2)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    os.environ["_TEST_EC2_URL"] = f"http://127.0.0.1:{server.server_port}"
    yield
    server.shutdown()
    os.environ.pop("_TEST_EC2_URL")


@pytest.fixture
def existing_endpoints():
    """Set to the endpoints the VPC already has; empty unless a test fills it."""
    _StubEc2.endpoints = []
    _StubEc2.asked_vpc_ids = []
    yield _StubEc2.endpoints


def _ls(context: dict[str, str] | None = None, *, region: str | None = "us-east-1"):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CDK_DEFAULT", "AWS_PROFILE", "AWS_SESSION_TOKEN"))}
    env.update(AWS_ENDPOINT_URL_EC2=os.environ["_TEST_EC2_URL"],
               AWS_ACCESS_KEY_ID=secrets.token_hex(10), AWS_SECRET_ACCESS_KEY=secrets.token_hex(20))
    if region:
        env["CDK_DEFAULT_REGION"] = region
    flags = [f for k, v in (context or {}).items() for f in ("-c", f"{k}={v}")]
    # Its own output directory: a developer's cdk.out grows with every deploy (5 GB made
    # one `cdk ls` take 218 s instead of 7), and a deploy running alongside holds its lock.
    with tempfile.TemporaryDirectory() as out:
        return subprocess.run(["cdk", "ls", "--app", f"{sys.executable} app.py", "-o", out, "-c", "stage=dev", *flags],
                              cwd=BACKEND, env=env, capture_output=True, text=True, timeout=180)


def _endpoint_services(context: dict[str, str]) -> set[str] | None:
    """The services the synthesized endpoint stack makes endpoints for; None with no such stack.
    Only that stack is bundled: it has no assets, and the others' would take minutes."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CDK_DEFAULT", "AWS_PROFILE", "AWS_SESSION_TOKEN"))}
    env.update(AWS_ENDPOINT_URL_EC2=os.environ["_TEST_EC2_URL"], AWS_ACCESS_KEY_ID=secrets.token_hex(10),
               AWS_SECRET_ACCESS_KEY=secrets.token_hex(20), CDK_DEFAULT_REGION="us-east-1")
    flags = [f for k, v in context.items() for f in ("-c", f"{k}={v}")]
    with tempfile.TemporaryDirectory() as out:
        stack = "AdamDev-VpcEndpoints"
        # Naming the stack, exclusively, bundles only it.
        result = subprocess.run(
            ["cdk", "synth", stack, "--exclusively", "--app", f"{sys.executable} app.py", "-o", out, "-q",
             "-c", "stage=dev", *flags], cwd=BACKEND, env=env, capture_output=True, text=True, timeout=300)
        if result.returncode != 0 and "No stacks match" in result.stderr:
            return None
        assert result.returncode == 0, result.stderr[-1500:]
        template = Path(out) / f"{stack}.template.json"
        resources = json.loads(template.read_text())["Resources"].values()
        return {r["Properties"]["ServiceName"].split(".", 3)[3]  # com.amazonaws.<region>.<service>
                for r in resources if r["Type"] == "AWS::EC2::VPCEndpoint"}


def _stacks(result) -> set[str]:
    assert result.returncode == 0, result.stderr[-1500:]
    return {line.strip() for line in result.stdout.splitlines() if line.strip().startswith("Adam")}


class TestVpcEndpoints:
    """Every interface endpoint the in-VPC functions need is checked on every synth."""

    def test_it_reads_the_given_vpc(self, existing_endpoints):
        _stacks(_ls(DEPLOYABLE))
        assert _StubEc2.asked_vpc_ids == [NETWORK["vpcId"]]

    def test_with_none_in_the_vpc_every_required_endpoint_is_created(self, existing_endpoints):
        assert _endpoint_services(NO_CHAT) == set(REQUIRED)

    def test_an_existing_endpoint_is_reused_and_only_the_missing_ones_created(self, existing_endpoints):
        existing_endpoints.append({"id": "vpce-glue", "service": "glue"})
        assert _endpoint_services(NO_CHAT) == set(REQUIRED) - {"glue"}

    def test_when_every_endpoint_exists_there_is_no_endpoint_stack(self, existing_endpoints):
        existing_endpoints += [{"id": f"vpce-{s}", "service": s} for s in (*REQUIRED, *RUNTIME)]
        stacks = _stacks(_ls(DEPLOYABLE))
        assert "AdamDev-VpcEndpoints" not in stacks
        assert "AdamDev-Agent" in stacks

    def test_endpoints_this_deployment_created_stay_in_its_stack(self, existing_endpoints):
        existing_endpoints += [{"id": f"vpce-{s}", "service": s} for s in (*REQUIRED, *RUNTIME) if s != "sts"]
        ours = {"createdby": "AdamDev"}
        existing_endpoints.append({"id": "vpce-sts", "service": "sts", "tags": ours})
        result = _ls(DEPLOYABLE)
        assert "AdamDev-VpcEndpoints" in _stacks(result)
        assert "keep   sts (vpce-sts)" in result.stderr

    def test_an_endpoint_with_private_dns_off_stops_the_deploy(self, existing_endpoints):
        existing_endpoints.append({"id": "vpce-nodns", "service": "glue", "private_dns": False})
        result = _ls(DEPLOYABLE)
        assert result.returncode != 0 and "vpce-nodns" in result.stderr


class TestTheStageNamesTheStacks:

    def test_the_app_and_install_cdk_sh_name_the_same_stacks(self, existing_endpoints):
        """install_cdk.sh finds and deploys the stacks by the prefix it builds itself; a stage
        with a capital inside it once named different stacks in the two."""
        line = next(ln for ln in (BACKEND.parent / "install_cdk.sh").read_text().splitlines()
                    if ln.startswith('prefix="Adam'))
        script_prefix = subprocess.run(["bash", "-c", f'stage=devAlice; {line}; printf %s "$prefix"'],
                                       capture_output=True, text=True, check=True).stdout
        stacks = _stacks(_ls({**DEPLOYABLE, "stage": "devAlice"}))
        assert script_prefix == "AdamDevAlice"
        assert stacks and all(s.startswith(f"{script_prefix}-") for s in stacks), stacks


class TestEnableChat:
    """The chat runtime pulls its image and ships its logs from inside the VPC."""

    def test_without_it_the_runtimes_endpoints_are_not_made(self, existing_endpoints):
        existing_endpoints += [{"id": f"vpce-{s}", "service": s} for s in REQUIRED]
        assert _endpoint_services(NO_CHAT) is None

    def test_by_default_the_runtimes_endpoints_are_made(self, existing_endpoints):
        existing_endpoints += [{"id": f"vpce-{s}", "service": s} for s in REQUIRED]
        assert _endpoint_services(DEPLOYABLE) == set(RUNTIME)

    def test_a_value_other_than_true_or_false_is_refused(self, existing_endpoints):
        result = _ls({**DEPLOYABLE, "enableChat": "yes"})
        assert result.returncode != 0 and "enableChat" in result.stderr


class TestNoNetworkIsRefused:

    def test_with_no_network_nothing_is_built_and_every_value_is_named(self):
        result = _ls()
        assert result.returncode != 0
        assert all(k in result.stderr for k in NETWORK)


class TestAnIncompleteNetworkIsRefused:

    @pytest.mark.parametrize("missing", sorted(NETWORK))
    def test_leaving_out_one_value_names_it(self, missing):
        given = {k: v for k, v in NETWORK.items() if k != missing}
        result = _ls(given)
        assert result.returncode != 0
        assert f"missing {missing}." in result.stderr  # this key named alone, not just the list of all five

    def test_subnets_and_zones_must_pair_up(self):
        result = _ls({**NETWORK, "availabilityZones": "us-east-1b"})
        assert result.returncode != 0 and "availabilityZones" in result.stderr


class TestTheRegionIsRequired:

    def test_no_region_is_an_error_not_a_guess(self):
        # Run the app itself: the cdk CLI fills CDK_DEFAULT_REGION in from the
        # caller's AWS configuration, which would hide the case under test.
        env = {**{k: v for k, v in os.environ.items() if not k.startswith("CDK_DEFAULT")},
               "CDK_CONTEXT_JSON": '{"stage": "dev"}'}
        result = subprocess.run([sys.executable, "app.py"], cwd=BACKEND, env=env,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode != 0
        assert "CDK_DEFAULT_REGION" in result.stderr


class TestTheStageIsRequired:

    def test_no_stage_is_an_error_not_dev(self):
        env = {**os.environ, "CDK_DEFAULT_REGION": "us-west-2", "CDK_CONTEXT_JSON": "{}"}
        result = subprocess.run([sys.executable, "app.py"], cwd=BACKEND, env=env,
                                capture_output=True, text=True, timeout=120)
        assert result.returncode != 0
        assert "No stage given" in result.stderr


def _agent_template(context: dict[str, str]) -> dict:
    """The synthesized Agent stack, with no asset bundled (its functions' code is not what is checked)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("CDK_DEFAULT", "AWS_PROFILE", "AWS_SESSION_TOKEN"))}
    env.update(AWS_ENDPOINT_URL_EC2=os.environ["_TEST_EC2_URL"], AWS_ACCESS_KEY_ID=secrets.token_hex(10),
               AWS_SECRET_ACCESS_KEY=secrets.token_hex(20), CDK_DEFAULT_REGION="us-east-1")
    with tempfile.TemporaryDirectory() as out:
        # The app run as the CLI runs it, with its context in CDK_CONTEXT_JSON; that is the only way
        # to ask for no bundling (the CLI refuses aws: keys from -c).
        cdk_json = json.loads((BACKEND / "cdk.json").read_text()).get("context", {})
        env.update(CDK_OUTDIR=out, CDK_CONTEXT_JSON=json.dumps(
            {**cdk_json, "stage": "dev", **context, "aws:cdk:bundling-stacks": []}))
        result = subprocess.run([sys.executable, "app.py"], cwd=BACKEND, env=env,
                                capture_output=True, text=True, timeout=300)
        assert result.returncode == 0, result.stderr[-1500:]
        return json.loads((Path(out) / "AdamDev-Agent.template.json").read_text())


def _web_search_parts(template: dict) -> set[str]:
    resources = template["Resources"]
    parts = {k for k, r in resources.items() if r["Type"] == "AWS::BedrockAgentCore::GatewayTarget"
             and r["Properties"]["Name"] == "web-search"}
    parts |= {k for k, r in resources.items()
              if r["Type"] == "AWS::Lambda::Function" and k.startswith("WebSearchFunction")}
    parts |= {k for k, r in resources.items() if r["Type"] == "AWS::IAM::Policy"
              and "nova_grounding" in json.dumps(r["Properties"]["PolicyDocument"])}
    return parts


class TestEnableWebSearch:
    """web_search sends the agent's query to a web search outside the VPC, so it is off unless asked for."""

    def test_by_default_there_is_no_web_search_function_target_or_grant(self, existing_endpoints):
        existing_endpoints += [{"id": f"vpce-{s}", "service": s} for s in REQUIRED + RUNTIME]
        assert _web_search_parts(_agent_template(DEPLOYABLE)) == set()

    def test_false_is_the_same_as_leaving_it_out(self, existing_endpoints):
        existing_endpoints += [{"id": f"vpce-{s}", "service": s} for s in REQUIRED + RUNTIME]
        assert _web_search_parts(_agent_template({**DEPLOYABLE, "enableWebSearch": "false"})) == set()

    def test_true_deploys_its_function_gateway_target_and_grounding_grant(self, existing_endpoints):
        existing_endpoints += [{"id": f"vpce-{s}", "service": s} for s in REQUIRED + RUNTIME]
        parts = _web_search_parts(_agent_template({**DEPLOYABLE, "enableWebSearch": "true"}))
        assert any(p.startswith("WebSearchFunction") for p in parts) and len(parts) >= 3, parts

    def test_a_value_other_than_true_or_false_is_refused(self, existing_endpoints):
        result = _ls({**DEPLOYABLE, "enableWebSearch": "yes"})
        assert result.returncode != 0 and "enableWebSearch" in result.stderr
