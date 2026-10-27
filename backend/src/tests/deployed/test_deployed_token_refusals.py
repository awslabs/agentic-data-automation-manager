#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Every way into a deployed stage refuses a token it did not issue.

The HTTP API (the web console), the AgentCore Gateway (Quick and the chat agent's tools) and
the agent runtime (the chat page) each have a JWT authorizer, configured by our stacks. Each is
sent tokens an attacker can make without the user pool's key: signed by another key (under the
pool's own kid), unsigned, from another user pool, or with no bearer token at all. A genuinely
signed but expired or wrong-client token needs the pool's key; our own verifier's handling of
those is tests/unit/test_forged_tokens.py.

    TEST_DEPLOYED_PREFIX=AdamDev AWS_PROFILE=<deploy profile> \\
        ../../.venv/bin/python -m pytest tests/deployed -m deployed
"""

import base64
import json
import os
import time
import urllib.parse

import jwt
import pytest
import urllib3
from cryptography.hazmat.primitives.asymmetric import rsa

pytestmark = pytest.mark.deployed

PREFIX = os.environ.get("TEST_DEPLOYED_PREFIX", "")
ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="module")
def stage() -> dict:
    if not PREFIX:
        pytest.fail("TEST_DEPLOYED_PREFIX is not set; these tests run against a deployed stage.", pytrace=False)
    import boto3
    outputs = {}
    for stack in ("Auth", "Agent"):
        described = boto3.client("cloudformation").describe_stacks(StackName=f"{PREFIX}-{stack}")["Stacks"][0]
        outputs.update({o["OutputKey"]: o["OutputValue"] for o in described["Outputs"]})
    region = outputs["UserPoolId"].split("_", 1)[0]
    issuer = f"https://cognito-idp.{region}.amazonaws.com/{outputs['UserPoolId']}"
    jwks = urllib3.request("GET", f"{issuer}/.well-known/jwks.json", timeout=10, retries=False)
    kid = json.loads(jwks.data)["keys"][0]["kid"]
    return {**outputs, "region": region, "issuer": issuer, "kid": kid}


def _b64(data: dict) -> str:
    return base64.urlsafe_b64encode(json.dumps(data).encode()).decode().rstrip("=")


def _claims(stage: dict, **overrides) -> dict:
    now = int(time.time())
    return {"sub": "sub-forged", "iss": stage["issuer"], "exp": now + 300, "iat": now, "token_use": "access",
            "client_id": stage["UserPoolClientId"], "scope": f"{PREFIX}-gateway/invoke", **overrides}


FORGERIES = {
    "signed by another key under the pool's kid": lambda s: jwt.encode(
        _claims(s), ATTACKER_KEY, algorithm="RS256", headers={"kid": s["kid"]}),
    "unsigned (alg none)": lambda s: f"{_b64({'alg': 'none', 'typ': 'JWT', 'kid': s['kid']})}.{_b64(_claims(s))}.",
    "from another user pool": lambda s: jwt.encode(
        _claims(s, iss=s["issuer"].rsplit("_", 1)[0] + "_NotOurPool"), ATTACKER_KEY, algorithm="RS256",
        headers={"kid": s["kid"]}),
    "no bearer token": lambda s: None,
}


def _status(url: str, token: str | None, *, body: dict | None = None, headers: dict | None = None) -> int:
    return urllib3.request("POST" if body is not None else "GET", url,
                           body=json.dumps(body).encode() if body is not None else None,
                           headers={"Content-Type": "application/json", **(headers or {}),
                                    **({"Authorization": f"Bearer {token}"} if token else {})},
                           timeout=30, retries=False).status


@pytest.mark.parametrize("forgery", FORGERIES)
def test_the_web_consoles_api_refuses(stage, forgery):
    assert _status(stage["HttpApiUrl"].rstrip("/") + "/api/models", FORGERIES[forgery](stage)) == 401


@pytest.mark.parametrize("forgery", FORGERIES)
def test_the_gateway_refuses(stage, forgery):
    status = _status(stage["QuickMcpServerUrl"], FORGERIES[forgery](stage),
                     body={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                     headers={"Accept": "application/json, text/event-stream"})
    assert status in (401, 403)  # it answers 403 for a well-formed token it cannot verify


@pytest.mark.parametrize("forgery", FORGERIES)
def test_the_agent_runtime_refuses(stage, forgery):
    if "RuntimeArn" not in stage:
        pytest.fail("This stage has no agent runtime (chat is off); deploy with chat to test it.", pytrace=False)
    arn = urllib.parse.quote(stage["RuntimeArn"], safe="")
    url = f"https://bedrock-agentcore.{stage['region']}.amazonaws.com/runtimes/{arn}/invocations?qualifier=DEFAULT"
    status = _status(url, FORGERIES[forgery](stage), body={"prompt": "hi"},
                     headers={"X-Amzn-Bedrock-AgentCore-Runtime-Session-Id": "0" * 36})
    assert status in (401, 403)
