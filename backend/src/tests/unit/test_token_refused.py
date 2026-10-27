#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The runtime tells the website when the gateway refused the caller's token, so the website,
which knows the scope its sign-in requests, can say whether signing in again fixes it."""

import pytest


@pytest.fixture(scope="module")
def agent():
    import os
    os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")  # read when the module loads
    from adam.agent_provider.agent_runtime import agent
    return agent


def _as_the_mcp_client_raises(gateway_error: Exception) -> Exception:
    """The gateway's answer, wrapped the way strands' MCP client wraps it."""
    from strands.types.exceptions import MCPClientInitializationError
    group = ExceptionGroup("unhandled errors in a TaskGroup", [ExceptionGroup("inner", [gateway_error])])
    try:
        raise MCPClientInitializationError(f"the client initialization failed: {group}") from group
    except MCPClientInitializationError as e:
        return e


@pytest.mark.parametrize("reason", ["insufficient_scope", "invalid_token"])
def test_a_refused_token_is_found_however_deeply_it_is_wrapped(agent, reason):
    from mcp.shared.exceptions import MCPError
    gateway = MCPError(code=-32001, message=f"{reason} - The request requires higher privileges", data=None)
    error = _as_the_mcp_client_raises(gateway)
    assert agent._token_refused(error) == reason


def test_any_other_failure_is_not_a_refused_token(agent):
    assert agent._token_refused(_as_the_mcp_client_raises(ConnectionError("gateway unreachable"))) == ""
