#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Tests that the identity path refuses to invent or accept a caller identity.

Every test here is an attack or a misconfiguration that must fail loudly. They
exist because the code they cover previously *looked* correct: the interceptor
had a step that stripped the inbound x-user-id header, a variable named
sanitized_headers, and a log line announcing it had injected a "trusted"
subject -- while still reading that subject from the attacker's own header when
the verified claim was absent. A docstring asserting a security property is
decoration; these tests are the property.

The rule each test enforces: identity has exactly one source, and its absence
raises. No default, no placeholder, no second-choice source.
"""

import json

import pytest

VICTIM = "victim-subject-456"
VERIFIED = "real-subject-123"


@pytest.fixture
def interceptor(monkeypatch):
    """The interceptor with token verification stubbed to succeed.

    Stubbing _verified_subject keeps these tests about what the handler does
    with a verified subject. The verification itself -- signature, issuer,
    client id -- is covered by TestInterceptorTokenVerification below, which
    uses the real function.
    """
    from adam.agent_provider import interceptor_handler

    monkeypatch.setattr(
        interceptor_handler, "_verified_subject", lambda headers: VERIFIED,
    )
    return interceptor_handler


def _event(body, headers=None):
    return {
        "interceptorInputVersion": "1.0",
        "mcp": {
            "rawGatewayRequest": {"body": ""},
            "gatewayRequest": {
                "path": "/mcp",
                "httpMethod": "POST",
                "headers": headers or {},
                "body": body,
            },
        },
    }


def _call_body(arguments):
    return {
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "execute_statement", "arguments": arguments},
    }


class TestInterceptorIdentity:
    """The interceptor is the only thing that may set the subject argument."""

    def test_injects_verified_subject_into_tool_arguments(self, interceptor):
        """The subject travels in the body, the only channel an MCP target has.

        transformedGatewayRequest carries `body` and nothing else for MCP
        targets, and a Lambda target's event is the map of its inputSchema
        properties -- so `params.arguments` is where the tool can read it.
        """
        result = interceptor.handler(_event(_call_body({"statement": "SELECT 1"})), None)
        arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
        assert arguments[interceptor.SUBJECT_ARGUMENT] == VERIFIED
        # The caller's real arguments are untouched.
        assert arguments["statement"] == "SELECT 1"

    def test_stuffed_subject_argument_is_replaced_on_a_tool_call(self, interceptor):
        """The takeover case: caller names a victim in the payload itself.

        Whoever the caller claims to be, the verified subject overwrites it.
        """
        result = interceptor.handler(
            _event(_call_body({"statement": "SELECT 1", "_verified_user_id": VICTIM})),
            None,
        )
        body = result["mcp"]["transformedGatewayRequest"]["body"]
        assert body["params"]["arguments"][interceptor.SUBJECT_ARGUMENT] == VERIFIED
        assert VICTIM not in json.dumps(result), (
            "The caller's claimed subject must not appear anywhere in the "
            "payload forwarded to the target."
        )

    def test_stuffed_subject_argument_is_stripped_on_a_discovery_call(self, interceptor):
        """The branch that adds no subject must still remove a stuffed one.

        tools/list needs no identity, so it takes the pass-through branch. An
        earlier version forwarded that body unchanged, which meant a stuffed
        argument rode through untouched on exactly the method that skips the
        injection step.
        """
        body = {
            "jsonrpc": "2.0", "id": 1, "method": "tools/list",
            "params": {"arguments": {"_verified_user_id": VICTIM}},
        }
        result = interceptor.handler(_event(body), None)
        forwarded = result["mcp"]["transformedGatewayRequest"]["body"]
        assert interceptor.SUBJECT_ARGUMENT not in forwarded["params"]["arguments"]
        assert VICTIM not in json.dumps(result)

    def test_discovery_methods_pass_without_a_subject(self, monkeypatch):
        """A client tool scan carries no user, and must still succeed.

        Requiring a subject here is what hung the Quick tool scan: the
        interceptor raised on tools/list, so the gateway never reached the
        dispatcher at all. The real handler, with no token and no identity
        provider configured: anything that tried to verify would raise.
        """
        from adam.agent_provider import interceptor_handler

        monkeypatch.setattr(interceptor_handler, "_identity_provider", None)
        monkeypatch.delenv("IDENTITY_PROVIDER", raising=False)
        for method in ("initialize", "tools/list", "ping"):
            body = {"jsonrpc": "2.0", "id": 1, "method": method}
            result = interceptor_handler.handler(_event(body), None)
            forwarded = result["mcp"]["transformedGatewayRequest"]["body"]
            assert forwarded == body, f"{method} must pass through unchanged"
            assert interceptor_handler.SUBJECT_ARGUMENT not in json.dumps(result)

    def test_output_carries_no_headers(self, interceptor):
        """MCP transformedGatewayRequest has no headers field -- so neither do we.

        Returning headers here was the original defect: the gateway dropped
        them, so the injected identity never reached the tool. Echoing them
        back would also hand the caller's bearer token to the target for no
        reason.
        """
        result = interceptor.handler(
            _event(
                _call_body({"statement": "SELECT 1"}),
                headers={"Authorization": "Bearer a.b.c", "Accept": "application/json"},
            ),
            None,
        )
        transformed = result["mcp"]["transformedGatewayRequest"]
        assert set(transformed) == {"body"}
        assert "Bearer a.b.c" not in json.dumps(result)


class TestInterceptorTokenVerification:
    """Verification refuses anything it cannot check, with no stubbing."""

    @pytest.mark.parametrize("headers", [
        {},
        {"Authorization": ""},
        {"Authorization": "Basic dXNlcjpwYXNz"},
        {"Authorization": "Bearer"},
        {"Authorization": "Bearer "},
        {"Authorization": "token-without-a-scheme"},
    ])
    def test_a_tool_call_without_a_bearer_token_raises(self, headers):
        """Defence in depth: the gateway's JWT authorizer refuses these first."""
        from adam.agent_provider.interceptor_handler import handler

        with pytest.raises(RuntimeError, match="No bearer token"):
            handler(_event(_call_body({}), headers=headers), None)

    @pytest.mark.parametrize("header", ["authorization", "Authorization"])
    def test_a_signed_token_injects_its_own_sub(self, monkeypatch, header):
        """Real verification (PyJWT, RS256, issuer, client) of a locally signed token;
        only the JWKS fetch is stubbed. `username` differs from `sub`, so injecting any
        other claim fails, and the header's case is the gateway's to choose."""
        import time

        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa

        from adam.agent_provider import interceptor_handler
        from adam.agent_provider.cognito_identity_provider import CognitoIdentityProvider

        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        issuer = "https://cognito-idp.us-east-1.amazonaws.com/us-east-1_test"
        jwks = type("Jwks", (), {"get_signing_key_from_jwt":
                                 lambda self, token: type("K", (), {"key": key.public_key()})()})()
        monkeypatch.setattr(interceptor_handler, "_identity_provider", CognitoIdentityProvider(
            issuer=issuer, allowed_client_ids=frozenset({"client-a"}), jwk_client=jwks))
        token = jwt.encode({"sub": VERIFIED, "username": "someone-else", "iss": issuer, "token_use": "access",
                            "client_id": "client-a", "exp": int(time.time()) + 300}, key, algorithm="RS256")

        result = interceptor_handler.handler(
            _event(_call_body({"statement": "SELECT 1", "_verified_user_id": VICTIM}),
                   headers={header: f"Bearer {token}"}), None)
        arguments = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
        assert arguments == {"statement": "SELECT 1", interceptor_handler.SUBJECT_ARGUMENT: VERIFIED}

    def test_required_config_absence_raises(self, monkeypatch):
        """No configured identity provider means no way to verify -- and so no default."""
        from adam.agent_provider import interceptor_handler

        monkeypatch.setattr(interceptor_handler, "_identity_provider", None)
        monkeypatch.delenv("IDENTITY_PROVIDER", raising=False)
        with pytest.raises(RuntimeError, match="IDENTITY_PROVIDER"):
            interceptor_handler.handler(
                _event(_call_body({}), headers={"Authorization": "Bearer a.b.c"}),
                None,
            )


class TestSubjectArgumentIsWiredEndToEnd:
    """The three places that must agree on the argument name.

    The interceptor writes it, the tool Lambda reads it, and the gateway only
    forwards arguments a tool's inputSchema declares. A rename in one place
    silently drops identity rather than failing, so pin all three together.
    """

    def test_the_tool_functions_read_the_subject_the_interceptor_writes(self, interceptor):
        """The interceptor's output arguments are the tool function's event."""
        from adam.agent_provider import gateway_event

        result = interceptor.handler(_event(_call_body({"statement": "SELECT 1"})), None)
        event = result["mcp"]["transformedGatewayRequest"]["body"]["params"]["arguments"]
        assert gateway_event.user_id(event) == VERIFIED

    @pytest.mark.parametrize("module, tool", [("mcp_handler", "execute_statement"),
                                              ("web_search_handler", "web_search")])
    def test_every_tool_function_refuses_a_call_with_no_subject(self, module, tool):
        """Called as the gateway calls it. A fallback subject would run the tool instead."""
        import importlib

        handler = importlib.import_module(f"adam.agent_provider.{module}").handler
        context = type("Context", (), {"client_context": type("ClientContext", (), {
            "custom": {"bedrockAgentCoreToolName": f"target___{tool}"}})()})()
        with pytest.raises(RuntimeError, match="cannot be attributed"):
            handler({"data_source": "x", "statement": "SELECT 1", "query": "q"}, context)

    def test_every_tool_declares_the_argument(self):
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
        from adam.agent_provider import interceptor_handler
        from cdk_infra.agent_provider.gateway_tool_schema import TOOLS

        name = interceptor_handler.SUBJECT_ARGUMENT
        for tool in TOOLS:
            schema = tool["inputSchema"]
            assert name in schema["properties"], (
                f"{tool['name']} does not declare {name}, so the gateway will "
                "drop it and the tool will receive no identity."
            )
            assert name not in schema["required"], (
                f"{tool['name']} marks {name} required, which would make the "
                "model responsible for supplying identity."
            )


class TestToolFunctionIdentity:
    """The tool functions read identity from the reserved argument only."""

    def test_reads_subject_from_the_reserved_argument(self):
        from adam.agent_provider.gateway_event import SUBJECT_ARGUMENT, user_id

        assert user_id({SUBJECT_ARGUMENT: VERIFIED}) == VERIFIED

    @pytest.mark.parametrize("event", [{}, {"_verified_user_id": ""}, {"statement": "SELECT 1"}])
    def test_missing_subject_raises(self, event):
        from adam.agent_provider.gateway_event import user_id

        with pytest.raises(RuntimeError, match="cannot be attributed"):
            user_id(event)

class TestApiHandlerIdentity:
    """The HTTP API reads the subject the JWT authorizer verified, or refuses."""

    def test_returns_verified_sub(self):
        from shared.http_response import get_user_id

        event = {
            "requestContext": {
                "authorizer": {"jwt": {"claims": {"sub": "real-subject-123"}}},
            },
        }
        assert get_user_id(event) == "real-subject-123"

    @pytest.mark.parametrize("event", [
        {},
        {"requestContext": {}},
        {"requestContext": {"authorizer": {}}},
        {"requestContext": {"authorizer": {"jwt": {"claims": {}}}}},
    ])
    def test_missing_claim_raises_instead_of_returning_anonymous(self, event):
        """This returned the literal string "anonymous" before.

        Five connections routes partition on this value, so every caller whose
        claims were missing shared one "anonymous" partition -- reading and
        writing each other's database connection records.
        """
        # Imported by the flat name deliberately. api_handler does
        # "from shared.http_response import ..." because the CDK bundling
        # flattens the tree, so that is the module object holding the class it
        # actually raises. Importing adam.shared.http_response here
        # instead loads the same file a second time under a second name,
        # yielding a *different* class object that pytest.raises will not
        # match -- the test fails while the code is correct.
        from shared.http_response import UnauthenticatedError, get_user_id

        with pytest.raises(UnauthenticatedError):
            get_user_id(event)

class TestOriginAllowlist:
    """An unresolvable CORS allowlist must fail, not become allow-all."""

    def test_missing_stack_name_raises(self, monkeypatch):
        from adam.shared import http_response

        monkeypatch.setattr(http_response, "_stack_name", "")
        monkeypatch.setattr(http_response, "_allowed_origins", None)
        with pytest.raises(RuntimeError, match="STACK_NAME"):
            http_response.get_allowed_origins()

    def test_empty_allowlist_raises(self, monkeypatch):
        """No origin_* params must not degrade into reflecting any Origin."""
        from adam.shared import http_response

        class _Ssm:
            def get_parameters_by_path(self, **_):
                return {"Parameters": []}

        monkeypatch.setattr(http_response, "_stack_name", "TestStack")
        monkeypatch.setattr(http_response, "_allowed_origins", None)
        monkeypatch.setattr(http_response, "_ssm_client", _Ssm())
        with pytest.raises(RuntimeError, match="no CORS allowlist"):
            http_response.get_allowed_origins()

    def test_unknown_origin_is_not_reflected(self, monkeypatch):
        from adam.shared import http_response

        monkeypatch.setattr(http_response, "_allowed_origins", {
            "origin_frontend": "https://good.example",
        })
        monkeypatch.setattr(http_response, "_allowed_origins_read_at", http_response._now())
        event = {"headers": {"origin": "https://evil.example"}}
        assert http_response._validate_origin(event) == ""

    def test_known_origin_is_reflected(self, monkeypatch):
        from adam.shared import http_response

        monkeypatch.setattr(http_response, "_allowed_origins", {
            "origin_frontend": "https://good.example",
        })
        monkeypatch.setattr(http_response, "_allowed_origins_read_at", http_response._now())
        event = {"headers": {"origin": "https://good.example"}}
        assert http_response._validate_origin(event) == "https://good.example"


def test_the_search_limit_the_tool_advertises_is_the_one_the_dispatcher_applies():
    import sys
    from pathlib import Path

    import mcp_handler
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from cdk_infra.agent_provider.gateway_tool_schema import TOOLS

    (search,) = [t for t in TOOLS if t["name"] == "search_schema"]
    described = search["inputSchema"]["properties"]["limit"]["description"]
    assert f"(default {mcp_handler._SEARCH_SCHEMA_DEFAULT_LIMIT})" in described
