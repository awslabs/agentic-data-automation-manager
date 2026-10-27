#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Building a Lambda's code from an explicit list of files.

Every function is assembled by copying named files, never a directory or a
wildcard: a file added to a source package must not ship in a function until
someone adds it to that function's list. tests/unit/test_bundles.py builds each
bundle from these same commands and checks that exactly the intended files are
in it.
"""

import shlex

from aws_cdk import BundlingOptions, aws_lambda as lambda_

from cdk_infra.shared import database_tls
from cdk_infra.shared.data_source_engines import ENGINES

# What a function's source asset never needs: the tests, and Python's bytecode caches.
SOURCE_EXCLUDES = ["tests", "**/__pycache__", "*.pyc"]

# Exact versions, so every deploy builds the same function. The Lambda runtime's own boto3
# predates Converse systemTool (Nova web grounding) and the Glue asset APIs, so those
# functions bring their own.
BOTO3 = "boto3==1.43.103"
PYJWT = ("pyjwt[crypto]==2.15.0", "cryptography==50.0.1")

_DATABASE_PROVIDER = [
    "__init__.py",
    "data_source_provider.py",
    "connection_opening.py",
    "connection_registry.py",
    "engines.py",
    "credential_provider.py",
    "user_scoped_secrets_credential_provider.py",
    "provider_handler.py",
]


def copy_files(src_dir: str, files: list[str], dest_dir: str) -> str:
    """One bundling step: make `dest_dir` and copy exactly `files` from `src_dir` into it."""
    sources = " ".join(f"/asset-input/{src_dir}/{name}" for name in files)
    return f"mkdir -p /asset-output/{dest_dir} && cp {sources} /asset-output/{dest_dir}/"


def bundling_options(steps: list[str], pip: tuple[str, ...] = (), pip_alone: tuple[str, ...] = ()) -> BundlingOptions:
    """Docker bundling: install the `pip` requirements (if any) into the output, then the `pip_alone` ones
    without the dependencies they declare, then run `steps`."""
    commands = []
    if pip:
        commands.append(f"pip install --no-cache-dir {' '.join(shlex.quote(r) for r in pip)} -t /asset-output")
    if pip_alone:
        commands.append(f"pip install --no-cache-dir --no-deps {' '.join(shlex.quote(r) for r in pip_alone)} "
                        "-t /asset-output")
    commands += steps
    return BundlingOptions(
        image=lambda_.Runtime.PYTHON_3_13.bundling_image,
        command=["bash", "-c", " && ".join(commands)],
    )


def provider_steps(engine: str) -> list[str]:
    """What a database-provider function carries: its own engine and nothing of the others.

    The assets of these functions are rooted at src/, so paths start at adam/.
    """
    build = ENGINES[engine]
    steps = [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/data_source_provider", _DATABASE_PROVIDER + list(build.files),
                   "adam/data_source_provider"),
        copy_files("adam/shared", ["__init__.py", "environment.py", "provider_client.py",
                                                     "schema_types.py", "statement_limits.py"],
                   "adam/shared"),
    ]
    return steps + _ca_bundle_steps(engine)


def _ca_bundle_steps(engine: str) -> list[str]:
    if not ENGINES[engine].rds_tls:
        return []
    return [copy_files("adam/data_source_provider/certs", [database_tls.BUNDLE_FILE],
                       "adam/data_source_provider/certs")]


def ingestion_steps() -> list[str]:
    """The ingestion function: the scan-target and provider lists, the schema index
    writer and the scan bus events. No database driver and no provider code: the
    providers scan."""
    return [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/schema_provider",
                   ["__init__.py", "schema_provider.py", "glue_data_catalog_schema_provider.py",
                    "schema_ingestion_handler.py", "scan_targets.py", "scan_providers.py"],
                   "adam/schema_provider"),
        copy_files("adam/shared", ["__init__.py", "environment.py", "scan_events.py",
                                                     "schema_types.py"],
                   "adam/shared"),
    ]


def scanner_steps(engine: str) -> list[str]:
    """One engine's scanner function: that engine, the plain credential provider (a scan
    has no user) and the scan bus events, and nothing of the other engines or of the
    connection registry. Rooted at src/, like the providers."""
    steps = [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/data_source_provider",
                   ["__init__.py", "data_source_provider.py", "credential_provider.py",
                    "secrets_manager_credential_provider.py", "engines.py", "scanner_handler.py"]
                   + list(ENGINES[engine].files),
                   "adam/data_source_provider"),
        copy_files("adam/shared", ["__init__.py", "environment.py", "scan_events.py",
                                                     "schema_types.py", "statement_limits.py"],
                   "adam/shared"),
    ]
    return steps + _ca_bundle_steps(engine)


def search_steps() -> list[str]:
    """The schema provider function: it reads the index and the registered databases
    and writes nothing, so it carries the readers and the value types and not the
    ingestion handler."""
    return [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/schema_provider",
                   ["__init__.py", "schema_provider.py", "glue_data_catalog_schema_provider.py", "scan_targets.py"],
                   "adam/schema_provider"),
        copy_files("adam/shared", ["__init__.py", "environment.py", "schema_types.py"],
                   "adam/shared"),
    ]


def connections_api_steps() -> list[str]:
    """The Connections page's API: the connection registry and the HTTP helpers, no driver."""
    return [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/data_source_provider",
                   ["__init__.py", "connection_registry.py", "connections_api_handler.py", "engines.py",
                    "login_attempts.py"],
                   "adam/data_source_provider"),
        copy_files("adam/shared",
                   ["__init__.py", "environment.py", "http_response.py", "network_target.py", "provider_client.py"],
                   "adam/shared"),
    ]


def registry_steps() -> list[str]:
    """The connection registry's API for other services: the registry and the client it
    reaches the schema service with."""
    return [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/data_source_provider",
                   ["__init__.py", "connection_registry.py", "engines.py", "registry_handler.py"],
                   "adam/data_source_provider"),
        copy_files("adam/shared", ["__init__.py", "environment.py", "provider_client.py"],
                   "adam/shared"),
    ]


def admin_steps() -> list[str]:
    """The admin routes, laid out flat (handler at the root) because they import their
    service's scan_targets and scan_providers and the shared helpers by top-level package name."""
    return [
        "cp /asset-input/adam/schema_provider/admin_handler.py /asset-output/",
        copy_files("adam/schema_provider",
                   ["__init__.py", "scan_providers.py", "scan_targets.py", "source_identity.py"], "schema_provider"),
        copy_files("adam/shared",
                   ["__init__.py", "environment.py", "http_response.py", "network_target.py", "provider_client.py",
                    "statement_limits.py"],
                   "shared"),
    ]


def deleted_users_steps() -> list[str]:
    """Deletes the saved logins of users the pool no longer has: its handler and the environment helper."""
    return [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/data_source_provider", ["__init__.py", "deleted_users_handler.py"],
                   "adam/data_source_provider"),
        copy_files("adam/shared", ["__init__.py", "environment.py"], "adam/shared"),
    ]


def disabled_version_steps() -> list[str]:
    """Deletes users' saved logins for a disabled database version: the registry and the event names."""
    return [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/data_source_provider",
                   ["__init__.py", "connection_registry.py", "disabled_version_handler.py"],
                   "adam/data_source_provider"),
        copy_files("adam/shared", ["__init__.py", "environment.py", "provider_client.py", "scan_events.py"],
                   "adam/shared"),
    ]


def registration_steps() -> list[str]:
    """The custom resource that registers a stack's scanners on the scan bus."""
    return [
        copy_files("adam", ["__init__.py"], "adam"),
        copy_files("adam/data_source_provider", ["__init__.py", "registration_handler.py"],
                   "adam/data_source_provider"),
        copy_files("adam/shared", ["__init__.py", "scan_events.py"],
                   "adam/shared"),
    ]


# The agent service's functions. Their assets are rooted at the package, and each handler
# sits at the bundle's root.

def interceptor_steps() -> list[str]:
    """The gateway interceptor: the identity provider it verifies callers' tokens with."""
    return [
        copy_files("agent_provider", ["interceptor_handler.py"], ""),
        copy_files("agent_provider", ["__init__.py", "gateway_event.py", "identity_provider.py",
                                      "cognito_identity_provider.py"], "agent_provider"),
        copy_files("shared", ["__init__.py", "environment.py"], "shared"),
    ]


def dispatcher_steps() -> list[str]:
    """The MCP dispatcher: it reaches the registry and the providers through their functions."""
    return [
        copy_files("agent_provider", ["mcp_handler.py"], ""),
        copy_files("agent_provider", ["__init__.py", "gateway_event.py"], "agent_provider"),
        copy_files("shared", ["__init__.py", "environment.py", "provider_client.py", "statement_limits.py"],
                   "shared"),
    ]


def web_search_steps() -> list[str]:
    return [
        copy_files("agent_provider", ["web_search_handler.py"], ""),
        copy_files("agent_provider", ["__init__.py", "gateway_event.py"], "agent_provider"),
        copy_files("shared", ["__init__.py", "environment.py"], "shared"),
    ]


def chat_api_steps() -> list[str]:
    """The chat routes and conversation history."""
    return [
        copy_files("agent_provider", ["api_handler.py", "chat.py", "conversations.py"], ""),
        copy_files("shared", ["__init__.py", "http_response.py"], "shared"),
    ]


# The agent runtime's image, built from the package: the agent, the verifier it checks each
# caller's token with (the Gateway interceptor's), and nothing else. Paths are from the package.
AGENT_IMAGE_FILES = [
    "agent_provider/agent_runtime/Dockerfile",
    "agent_provider/agent_runtime/requirements.txt",
    "agent_provider/agent_runtime/agent.py",
    "agent_provider/__init__.py",
    "agent_provider/identity_provider.py",
    "agent_provider/cognito_identity_provider.py",
    "shared/__init__.py",
    "shared/environment.py",
]
