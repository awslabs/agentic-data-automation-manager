#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Calling another microservice through its fronting Lambda.

Every provider publishes its function name in SSM at /{STACK_NAME}/<key>. A
caller looks the name up and invokes it; it never imports the provider's code.
This is the one place that lookup and invoke are written down.
"""

import json
from typing import Any

import boto3

from .environment import require_env

# SSM keys the provider stacks publish their function names under.
ENGINE_PROVIDER_SSM = {
    "oracle": "oracle_provider_function_name",
    "postgres": "postgresql_provider_function_name",
    "mysql": "mysql_provider_function_name",
    "mariadb": "mariadb_provider_function_name",
    "redshift": "redshift_provider_function_name",
}
SCHEMA_PROVIDER_SSM = "schema_provider_function_name"
CONNECTION_REGISTRY_SSM = "connection_registry_function_name"

_lambda_client = None
_function_names: dict[str, str] = {}


class ProviderError(RuntimeError):
    """A provider Lambda answered with an error, or failed outright."""


class ProviderRefused(ProviderError):
    """The provider answered with an error it wrote for the caller to show, rather than failing."""


def function_name(ssm_key: str) -> str:
    """The deployed name of the function a stack published under this SSM key."""
    # Function names do not change for the life of a deployment, so they are
    # looked up once per container. Nothing here is a credential or a session.
    if ssm_key not in _function_names:
        resp = boto3.client("ssm").get_parameter(Name=f"/{require_env('STACK_NAME')}/{ssm_key}")
        _function_names[ssm_key] = resp["Parameter"]["Value"]
    return _function_names[ssm_key]


def call_provider(ssm_key: str, operation: str, args: dict) -> Any:
    """Invoke one operation of the function a stack published under `ssm_key`."""
    return call_function(function_name(ssm_key), operation, args)


def call_function(name: str, operation: str, args: dict) -> Any:
    """Invoke one provider operation and return its response, or raise ProviderError
    (ProviderRefused when the provider answered with an error of its own)."""
    global _lambda_client
    if _lambda_client is None:
        _lambda_client = boto3.client("lambda")
    resp = _lambda_client.invoke(
        FunctionName=name,
        InvocationType="RequestResponse",
        Payload=json.dumps({"operation": operation, "args": args}),
    )
    payload = json.loads(resp["Payload"].read())
    # An unhandled exception in the provider arrives as a payload with
    # errorMessage and the FunctionError header, not as "error".
    if resp.get("FunctionError"):
        raise ProviderError(payload.get("errorMessage") or "provider failed")
    if "error" in payload:
        raise ProviderRefused(payload["error"])
    return payload.get("response", payload)
