#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Every token our code verifies itself is refused unless it is a genuine, current access token
from our user pool, for one of our clients.

Two entry points verify the caller's token with the same verifier: the Gateway's interceptor
(every tool call) and the agent runtime (every chat turn). Each runs here against a real JWKS
endpoint, served locally, with the real PyJWKClient fetching it: the keys, the key lookup by
`kid` and the signature check are PyJWT's and ours, not a stand-in.
"""

import base64
import hashlib
import hmac
import json
import time

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from agent_provider.identity_provider import TokenRefused, identity_provider_from_env
from tests.signed_tokens import CLIENT, KID, POOL_KEY, Jwks, claims, signed

OTHER_CLIENT = "someone-elses-client"
ATTACKER_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
PUBLIC_PEM = POOL_KEY.public_key().public_bytes(serialization.Encoding.PEM,
                                                serialization.PublicFormat.SubjectPublicKeyInfo)


@pytest.fixture(scope="module")
def jwks():
    server = Jwks()
    yield server
    server.close()


@pytest.fixture
def issuer(jwks, monkeypatch):
    jwks.configure(monkeypatch)
    return jwks.issuer


def _b64(data) -> str:
    raw = data if isinstance(data, bytes) else json.dumps(data).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _hs256_with_the_public_key(issuer):
    """The pool's public key, which anyone can fetch, used as an HMAC secret. Built by hand:
    PyJWT itself refuses to sign this way."""
    signing_input = f"{_b64({'alg': 'HS256', 'typ': 'JWT', 'kid': KID})}.{_b64(claims(issuer))}"
    return f"{signing_input}.{_b64(hmac.new(PUBLIC_PEM, signing_input.encode(), hashlib.sha256).digest())}"


def _another_subject_on_a_genuine_signature(issuer):
    header, _, signature = signed(claims(issuer)).split(".")
    return f"{header}.{_b64(claims(issuer, sub='sub-mallory'))}.{signature}"


# Tokens an attacker could send, each wrong in exactly one way.
FORGERIES = {
    "signed by another key": lambda i: signed(claims(i), key=ATTACKER_KEY),
    "signed by another key under a kid the pool lacks": lambda i: signed(claims(i), key=ATTACKER_KEY, kid="x"),
    "unsigned (alg none)": lambda i: f"{_b64({'alg': 'none', 'typ': 'JWT', 'kid': KID})}.{_b64(claims(i))}.",
    "HS256 with the pool's public key as the secret": _hs256_with_the_public_key,
    "another user's subject on a genuine signature": _another_subject_on_a_genuine_signature,
    "expired": lambda i: signed(claims(i, exp=int(time.time()) - 60)),
    "no expiry": lambda i: signed(claims(i, exp=None)),
    "not valid yet": lambda i: signed(claims(i, nbf=int(time.time()) + 3600)),
    "from another user pool": lambda i: signed(claims(i.replace("_pool", "_otherpool"))),
    "no issuer": lambda i: signed(claims(i, iss=None)),
    "for a client we do not accept": lambda i: signed(claims(i, client_id=OTHER_CLIENT)),
    "no client": lambda i: signed(claims(i, client_id=None)),
    "an ID token, not an access token": lambda i: signed(claims(i, token_use="id", client_id=None, aud=CLIENT)),
    "no token_use": lambda i: signed(claims(i, token_use=None)),
    "no subject": lambda i: signed(claims(i, sub=None)),
    "an empty subject": lambda i: signed(claims(i, sub="")),
    "not a JWT": lambda i: "not-a-jwt",
}


# ── The verifier both entry points use ───────────────────────────────────

def test_a_genuine_access_token_is_accepted(issuer):
    assert identity_provider_from_env().verify(signed(claims(issuer))).subject == "sub-alice"


@pytest.mark.parametrize("forgery", FORGERIES)
def test_the_verifier_refuses(issuer, forgery):
    with pytest.raises(TokenRefused):
        identity_provider_from_env().verify(FORGERIES[forgery](issuer))


def test_unreachable_keys_are_an_outage_not_a_refusal(issuer, monkeypatch):
    """A refusal tells the caller their token is bad; keys that cannot be fetched are ours to fix."""
    monkeypatch.setenv("OIDC_ISSUER", "http://127.0.0.1:9/us-east-1_pool")  # nothing listens
    with pytest.raises(jwt.PyJWKClientConnectionError):
        identity_provider_from_env().verify(signed(claims(issuer)))


# ── The Gateway's interceptor: every tool call ───────────────────────────

def _tool_call(token: str) -> dict:
    return {"mcp": {"gatewayRequest": {
        "headers": {"Authorization": f"Bearer {token}"},
        "body": {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                 "params": {"name": "t___execute_statement", "arguments": {"statement": "SELECT 1"}}}}}}


@pytest.fixture
def interceptor(issuer, monkeypatch):
    from agent_provider import interceptor_handler
    monkeypatch.setattr(interceptor_handler, "_identity_provider", None)
    return interceptor_handler


def test_the_interceptor_forwards_a_genuine_callers_subject(interceptor, issuer):
    from agent_provider.gateway_event import SUBJECT_ARGUMENT
    out = interceptor.handler(_tool_call(signed(claims(issuer))), None)
    assert out["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"][SUBJECT_ARGUMENT] == "sub-alice"


@pytest.mark.parametrize("forgery", FORGERIES)
def test_the_interceptor_forwards_no_tool_call_for(interceptor, issuer, forgery):
    """The raise is the Gateway's refusal: the tool call never reaches a tool."""
    with pytest.raises(TokenRefused):
        interceptor.handler(_tool_call(FORGERIES[forgery](issuer)), None)


# ── The agent runtime: every chat turn ───────────────────────────────────

CONVERSATION, TURN = "0b5a8f0e-3a0e-4c1e-9d3a-1f2e3d4c5b6a", "20261021T120000000000Z"


@pytest.fixture
def runtime(issuer, monkeypatch):
    """The real entry point; only what it would store and the thread it would start are captured."""
    from adam.agent_provider.agent_runtime import agent
    monkeypatch.setattr(agent, "_identity_provider", None)
    written, started = [], []
    monkeypatch.setattr(agent, "_write_session_message", lambda *a: written.append(a))

    class _Thread:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        def start(self):
            started.append(self.kwargs)
    monkeypatch.setattr(agent.threading, "Thread", _Thread)

    def invoke(token: str):
        monkeypatch.setattr(agent.BedrockAgentCoreContext, "get_request_headers",
                            staticmethod(lambda: {"Authorization": f"Bearer {token}"}))
        return agent.invoke({"prompt": "hi", "session_id": CONVERSATION, "conversation_id": CONVERSATION,
                             "turn_id": TURN, "model_id": "m"})
    return invoke, written, started


def test_the_runtime_starts_a_genuine_callers_turn_under_their_subject(runtime, issuer):
    invoke, written, started = runtime
    assert invoke(signed(claims(issuer)))["status"] == "started"
    assert [w[0] for w in written] == ["sub-alice"]
    assert started[0]["args"][1] == "sub-alice"


@pytest.mark.parametrize("forgery", FORGERIES)
def test_the_runtime_stores_and_starts_nothing_for(runtime, issuer, forgery):
    from adam.agent_provider.agent_runtime.agent import CallerIdentityError
    invoke, written, started = runtime
    with pytest.raises(CallerIdentityError):
        invoke(FORGERIES[forgery](issuer))
    assert written == [] and started == []


def test_a_verified_callers_subject_is_the_one_the_dispatcher_acts_for(interceptor, issuer, monkeypatch):
    """End to end through our own code: the token's sub, written by the interceptor, is the
    user the dispatcher asks the registry about. Only the provider functions are stood in for."""
    import mcp_handler

    asked = []

    def registry(key, operation, args):
        asked.append(args["user_id"])
        raise mcp_handler.ProviderError("enough: the caller is known")
    monkeypatch.setattr(mcp_handler, "call_provider", registry)
    forwarded = interceptor.handler(_tool_call(signed(claims(issuer, sub="sub-bob"))), None)
    arguments = forwarded["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
    context = type("Context", (), {"client_context": type("ClientContext", (), {
        "custom": {"bedrockAgentCoreToolName": "target___execute_statement"}})()})()
    mcp_handler.handler({**arguments, "data_source": "Wealth"}, context)
    assert asked == ["sub-bob"]


def test_a_genuine_token_with_another_users_id_in_the_body_starts_nothing(runtime, issuer, monkeypatch):
    from adam.agent_provider.agent_runtime import agent
    _, written, started = runtime
    monkeypatch.setattr(agent.BedrockAgentCoreContext, "get_request_headers",
                        staticmethod(lambda: {"Authorization": f"Bearer {signed(claims(issuer))}"}))
    with pytest.raises(agent.InvalidInvocation, match="does not match"):
        agent.invoke({"prompt": "hi", "session_id": CONVERSATION, "conversation_id": CONVERSATION,
                      "turn_id": TURN, "model_id": "m", "user_id": "sub-mallory"})
    assert written == [] and started == []


def test_a_turns_stored_error_carries_no_arn_or_account(runtime, issuer, monkeypatch):
    """The browser shows the stored error; what failed inside AWS stays in the log."""
    from adam.agent_provider.agent_runtime import agent
    _, written, _ = runtime

    def failing():
        raise RuntimeError("AccessDenied on arn:aws:iam::123456789012:role/runtime-role in 123456789012")
    monkeypatch.setattr(agent, "_get_gateway_mcp_url", failing)
    agent.run_turn("sub-alice", "Bearer t", CONVERSATION, TURN, "hi", "m")
    (_, _, _, _, kind, content), = written
    assert kind == "error" and "123456789012" not in content and "arn:aws" not in content
    # Not even the error's name: the user gets the fixed message and the reference the log has.
    assert json.loads(content) == {"content": f"{agent.AGENT_FAILURE} Reference: {CONVERSATION}/{TURN}."}
