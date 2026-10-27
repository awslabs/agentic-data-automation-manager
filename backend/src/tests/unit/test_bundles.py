#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Each Lambda's bundle holds exactly the files it is meant to, and can import itself.

The bundling commands only run in a container at deploy time. Here the real commands
are captured while the stacks are built, and run locally, skipping only `pip`, into an
empty directory. That directory is then listed; the files we want are removed from the
list, and nothing may be left. Nothing is named as forbidden: a file nobody listed here
fails the test whatever it is, so adding one to a bundle is a decision made in this file.

`pip` is checked the same way, as a set of packages. And each bundle's handler is
imported from the bundle directory alone, which catches a file a function needs but
does not have before it fails as an ImportError in AWS.
"""

import json
import os
import subprocess
import sys
from unittest import mock

import aws_cdk as cdk
import pytest
from aws_cdk import aws_lambda as lambda_

from tests.unit.cdk_stacks import BACKEND, build_stacks

DB = "adam/data_source_provider/"
SP = "adam/schema_provider/"
SH = "adam/shared/"
CA_BUNDLE = DB + "certs/rds-global-bundle.pem"

# What every database-provider function carries, whichever engine it serves.
_PROVIDER_COMMON = {
    "adam/__init__.py",
    DB + "__init__.py", DB + "connection_opening.py", DB + "credential_provider.py",
    DB + "data_source_provider.py", DB + "provider_handler.py", DB + "connection_registry.py",
    DB + "engines.py", DB + "user_scoped_secrets_credential_provider.py",
    SH + "__init__.py", SH + "environment.py", SH + "provider_client.py", SH + "schema_types.py",
    SH + "statement_limits.py",
}
ORACLE_PROVIDER = _PROVIDER_COMMON | {DB + "oracle_provider.py", DB + "tls.py", CA_BUNDLE}
POSTGRES_PROVIDER = _PROVIDER_COMMON | {DB + "postgresql_provider.py", DB + "tls.py", CA_BUNDLE}
MYSQL_FILES = {DB + "mysql_provider.py", DB + "tls.py", CA_BUNDLE}
MARIADB_FILES = MYSQL_FILES | {DB + "mariadb_provider.py"}
REDSHIFT_FILES = {DB + "redshift_provider.py"}
REDSHIFT_PIP = {"redshift_connector==2.2.0", "scramp==1.4.17", "packaging==26.3", "boto3==1.43.103"}

# A scanner: its engine and the plain credential provider (a scan has no user), and
# nothing of the connection registry, the user-scoped secret reader or the other engine.
_SCANNER_COMMON = {
    "adam/__init__.py",
    DB + "__init__.py", DB + "credential_provider.py", DB + "data_source_provider.py", DB + "engines.py",
    DB + "scanner_handler.py", DB + "secrets_manager_credential_provider.py",
    SH + "__init__.py", SH + "environment.py", SH + "scan_events.py", SH + "schema_types.py",
    SH + "statement_limits.py",
}

# function -> (pip packages, every file in its bundle)
EXPECTED = {
    "T-Auth/InvitationMessageTrigger": (set(), {"invitation_message_trigger.py"}),
    "T-Auth/AccessTokenClaimsTrigger": (set(), {"access_token_claims_trigger.py"}),
    "T-Agent/SessionsStreamProcessor": (set(), {"sessions_stream_handler.py"}),
    "T-Agent/InterceptorFunction": ({"pyjwt[crypto]==2.15.0", "cryptography==50.0.1"}, {
        "interceptor_handler.py",
        "agent_provider/__init__.py", "agent_provider/gateway_event.py", "agent_provider/identity_provider.py",
        "agent_provider/cognito_identity_provider.py", "shared/__init__.py", "shared/environment.py",
    }),
    # Reaches the registry and the providers through their functions; no table, no secret.
    "T-Agent/McpDispatcherFunction": (set(), {
        "mcp_handler.py", "agent_provider/__init__.py", "agent_provider/gateway_event.py",
        "shared/__init__.py", "shared/environment.py", "shared/provider_client.py",
        "shared/statement_limits.py",
    }),
    # Nova web grounding needs a newer boto3 than the runtime's.
    "T-Agent/WebSearchFunction": ({"boto3==1.43.103"}, {
        "web_search_handler.py", "agent_provider/__init__.py", "agent_provider/gateway_event.py",
        "shared/__init__.py", "shared/environment.py",
    }),
    "T-Agent/ApiFunction": (set(), {
        "api_handler.py", "chat.py", "conversations.py", "shared/__init__.py", "shared/http_response.py",
    }),
    "T-External/OracleProviderFunction": ({"oracledb==26.0.1"}, ORACLE_PROVIDER),
    "T-External/PostgreSQLProviderFunction": ({"pg8000==1.31.5", "scramp==1.4.17"}, POSTGRES_PROVIDER),
    "T-External/MySQLProviderFunction": ({"PyMySQL==1.2.3"}, _PROVIDER_COMMON | MYSQL_FILES),
    "T-External/MariaDBProviderFunction": ({"PyMySQL==1.2.3"}, _PROVIDER_COMMON | MARIADB_FILES),
    "T-External/RedshiftProviderFunction": (REDSHIFT_PIP, _PROVIDER_COMMON | REDSHIFT_FILES),
    "T-External/ConnectionsApiFunction": (set(), {
        "adam/__init__.py",
        DB + "__init__.py", DB + "connection_registry.py", DB + "connections_api_handler.py", DB + "engines.py",
        DB + "login_attempts.py",
        SH + "__init__.py", SH + "environment.py", SH + "http_response.py", SH + "network_target.py",
        SH + "provider_client.py",
    }),
    "T-External/ConnectionRegistryFunction": (set(), {
        "adam/__init__.py",
        DB + "__init__.py", DB + "connection_registry.py", DB + "engines.py", DB + "registry_handler.py",
        SH + "__init__.py", SH + "environment.py", SH + "provider_client.py",
    }),
    "T-External/OracleScannerFunction": ({"oracledb==26.0.1"}, _SCANNER_COMMON | {
        DB + "oracle_provider.py", DB + "tls.py", CA_BUNDLE}),
    "T-External/PostgresScannerFunction": ({"pg8000==1.31.5", "scramp==1.4.17"}, _SCANNER_COMMON | {
        DB + "postgresql_provider.py", DB + "tls.py", CA_BUNDLE}),
    "T-External/MySQLScannerFunction": ({"PyMySQL==1.2.3"}, _SCANNER_COMMON | MYSQL_FILES),
    "T-External/MariaDBScannerFunction": ({"PyMySQL==1.2.3"}, _SCANNER_COMMON | MARIADB_FILES),
    "T-External/RedshiftScannerFunction": (REDSHIFT_PIP, _SCANNER_COMMON | REDSHIFT_FILES),
    # Deletes the saved logins of users the pool no longer has.
    "T-External/DeletedUsersFunction": (set(), {
        "adam/__init__.py", DB + "__init__.py", DB + "deleted_users_handler.py",
        SH + "__init__.py", SH + "environment.py",
    }),
    # Deletes the saved logins of a disabled version's connections: the registry, no driver, no HTTP helpers.
    "T-External/DisabledVersionFunction": (set(), {
        "adam/__init__.py", DB + "__init__.py", DB + "connection_registry.py", DB + "disabled_version_handler.py",
        SH + "__init__.py", SH + "environment.py", SH + "provider_client.py", SH + "scan_events.py",
    }),
    "T-External/ScannerRegistrationFunction": (set(), {
        "adam/__init__.py", DB + "__init__.py", DB + "registration_handler.py",
        SH + "__init__.py", SH + "scan_events.py",
    }),
    # Reads the index and the registered databases; carries no writer, no drivers, no credentials.
    "T-Glue/GlueCatalogProviderFunction": ({"boto3==1.43.103"}, {
        "adam/__init__.py",
        SP + "__init__.py", SP + "schema_provider.py", SP + "glue_data_catalog_schema_provider.py",
        SP + "scan_targets.py", SH + "__init__.py", SH + "environment.py", SH + "schema_types.py",
    }),
    # Requests scans and indexes the providers' results: no driver, no provider code, no credential.
    "T-Glue/SchemaIngestionFunction": ({"boto3==1.43.103"}, {
        "adam/__init__.py",
        SP + "__init__.py", SP + "schema_provider.py", SP + "glue_data_catalog_schema_provider.py",
        SP + "schema_ingestion_handler.py", SP + "scan_targets.py", SP + "scan_providers.py",
        SH + "__init__.py", SH + "environment.py", SH + "scan_events.py", SH + "schema_types.py",
    }),
    "T-Glue/AdminApiFunction": (set(), {
        "admin_handler.py", "schema_provider/__init__.py", "schema_provider/scan_providers.py",
        "schema_provider/scan_targets.py", "schema_provider/source_identity.py",
        "shared/__init__.py", "shared/environment.py", "shared/http_response.py", "shared/network_target.py",
        "shared/provider_client.py",
        "shared/statement_limits.py",
    }),
}


def _provided_by(requirements: set[str]) -> set[str]:
    """Top-level import names the given pip packages provide, with their dependencies.

    boto3 is in the Lambda runtime, so every function has it whether or not it installs it.
    """
    from importlib import metadata

    from packaging.requirements import Requirement

    provides: dict[str, set[str]] = {}
    for module, dists in metadata.packages_distributions().items():
        for dist in dists:
            provides.setdefault(dist.lower().replace("_", "-"), set()).add(module)

    names, seen = set(), set()
    todo = [(Requirement(r).name, set(Requirement(r).extras)) for r in requirements | {"boto3"}]
    while todo:
        dist, extras = todo.pop()
        dist = dist.lower().replace("_", "-")
        if (dist, frozenset(extras)) in seen:
            continue
        seen.add((dist, frozenset(extras)))
        names |= provides.get(dist, set())
        try:
            for req in metadata.distribution(dist).requires or []:
                parsed = Requirement(req)
                # An extra such as pyjwt[crypto] switches on the requirements marked for it.
                if parsed.marker is None or any(parsed.marker.evaluate({"extra": e}) for e in extras | {""}):
                    todo.append((parsed.name, set(parsed.extras)))
        except metadata.PackageNotFoundError:
            pass
    return names


def _packages(commands: list[str]) -> set[str]:
    found = set()
    for command in commands:
        words = command.split()[2:]          # after "pip install"
        skip = False
        for word in words:
            if skip:
                skip = False
            elif word == "-t":
                skip = True
            elif not word.startswith("-"):
                found.add(word.strip("'\""))
    return found


@pytest.fixture(scope="module")
def bundles(tmp_path_factory):
    """name -> {handler, pip, files, dir, bundled}, by running each function's real commands."""
    records, by_code = [], {}
    original_from_asset, original_init = lambda_.Code.from_asset, lambda_.Function.__init__

    def capture_asset(path, **kwargs):
        code = original_from_asset(path, **kwargs)
        by_code[id(code)] = {"root": path, "bundling": kwargs.get("bundling")}
        return code

    def capture_function(self, scope, construct_id, **kwargs):
        asset = by_code.get(id(kwargs.get("code")), {"root": None, "bundling": None})
        records.append({"name": f"{cdk.Stack.of(scope).stack_name}/{construct_id}",
                        "handler": kwargs.get("handler"), "in_vpc": kwargs.get("vpc") is not None, **asset})
        return original_init(self, scope, construct_id, **kwargs)

    previous = os.getcwd()
    os.chdir(BACKEND)
    try:
        with mock.patch.object(lambda_.Code, "from_asset", staticmethod(capture_asset)), \
             mock.patch.object(lambda_.Function, "__init__", capture_function):
            build_stacks()
    finally:
        os.chdir(previous)

    built = {}
    for record in records:
        entry = {"handler": record["handler"], "bundled": record["bundling"] is not None,
                 "in_vpc": record["in_vpc"]}
        if record["bundling"] is not None:
            steps = record["bundling"].command[-1].split(" && ")
            pip = [s for s in steps if s.startswith("pip install")]
            rest = [s for s in steps if not s.startswith("pip install")]
            out = tmp_path_factory.mktemp(record["name"].replace("/", "_"))
            script = " && ".join(rest).replace("/asset-input", record["root"]).replace("/asset-output", str(out))
            result = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
            assert result.returncode == 0, f"{record['name']}: {result.stderr}"
            entry.update(pip=_packages(pip), pip_commands=pip, dir=out,
                         files={str(p.relative_to(out)) for p in out.rglob("*") if p.is_file()})
        built[record["name"]] = entry
    return built


def test_every_function_in_the_app_has_a_stated_bundle(bundles):
    assert set(bundles) == set(EXPECTED), "a function was added or removed; state its bundle in EXPECTED"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_bundle_holds_exactly_the_files_we_want(bundles, name):
    remaining = set(bundles[name]["files"])
    missing = []
    for wanted in EXPECTED[name][1]:
        if wanted in remaining:
            remaining.remove(wanted)
        else:
            missing.append(wanted)
    assert not missing, f"{name} is missing {sorted(missing)}"
    assert not remaining, f"{name} also contains {sorted(remaining)}"


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_bundle_installs_exactly_the_packages_we_want(bundles, name):
    assert bundles[name]["pip"] == EXPECTED[name][0]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_handler_is_a_function_in_the_bundle(bundles, name):
    """Loaded from the bundle alone, as Lambda loads it: a handler string naming a function
    its module lacks fails in AWS with Runtime.HandlerNotFound."""
    module, function = bundles[name]["handler"].rsplit(".", 1)
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(bundles[name]["dir"])
    check = "import importlib, sys; m = importlib.import_module(sys.argv[1]); assert callable(getattr(m, sys.argv[2]))"
    result = subprocess.run([sys.executable, "-c", check, module, function], cwd=bundles[name]["dir"], env=env,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr[-800:]


_IMPORT_ALL = r"""
import importlib, json, sys
from pathlib import Path

bundle = Path(sys.argv[1]).resolve()
allowed = set(json.loads(sys.argv[2])) | set(sys.stdlib_module_names)
first_party = {p.name.removesuffix(".py") for p in bundle.iterdir()}


class OnlyWhatTheFunctionHas:
    # Refuses any third-party import the function's declared packages do not provide.
    def find_spec(self, name, path=None, target=None):
        top = name.split(".")[0]
        if top in allowed or top in first_party:
            return None
        raise ImportError(f"{top!r} is not provided to this function (imported as {name!r})")


sys.meta_path.insert(0, OnlyWhatTheFunctionHas())
modules = sorted(str(p.relative_to(bundle).with_suffix("")).replace("/", ".")
                 for p in bundle.rglob("*.py"))
failures = []
for module in modules:
    name = module.removesuffix(".__init__")
    try:
        loaded = importlib.import_module(name)
    except Exception as e:
        failures.append(f"{name}: {type(e).__name__}: {e}")
        continue
    if not str(loaded.__file__).startswith(str(bundle)):
        failures.append(f"{name} came from {loaded.__file__}, not the bundle")
print(json.dumps(failures))
sys.exit(1 if failures else 0)
"""


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_every_module_in_the_bundle_imports_using_only_the_bundle_and_its_packages(bundles, name):
    """Not just the handler: an engine module is loaded only when a scan or call names its
    engine, so a file it needs must be in the bundle even though nothing imports it at start.
    Third-party imports are limited to what the function's declared packages provide."""
    bundle = bundles[name]
    allowed = sorted(_provided_by(EXPECTED[name][0]))
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = str(bundle["dir"])
    result = subprocess.run([sys.executable, "-c", _IMPORT_ALL, str(bundle["dir"]), json.dumps(allowed)],
                            cwd=bundle["dir"], env=env, capture_output=True, text=True)
    assert result.returncode == 0, (result.stdout.strip() or result.stderr)[-1200:]


def test_every_aws_service_an_in_vpc_function_calls_has_an_endpoint(bundles):
    """A function in the VPC reaches AWS only through an endpoint: one it calls that is
    not in the required list hangs at run time (the scan-target list did, for Scheduler)."""
    import re

    from cdk_infra.network.vpc_endpoints_stack import NOT_INTERFACE_ENDPOINTS, REQUIRED_INTERFACE_SERVICES

    client = re.compile(r"boto3\.(?:client|resource)\(\s*[\"']([a-z0-9-]+)[\"']")
    in_vpc = {name: b for name, b in bundles.items() if b["in_vpc"]}
    assert in_vpc, "no function in the VPC was found"
    for name, bundle in in_vpc.items():
        called = {service for path in bundle["files"] if path.endswith(".py")
                  for service in client.findall((bundle["dir"] / path).read_text())}
        missing = called - set(REQUIRED_INTERFACE_SERVICES) - set(NOT_INTERFACE_ENDPOINTS)
        assert not missing, f"{name} calls {sorted(missing)}, which no required endpoint covers"


def test_every_engine_is_deployed_routed_and_identified_alike():
    """An engine is listed in three places (cdk_infra/shared/data_source_engines.py says
    which). One missing from any of them deploys a function nothing calls, or a
    connection type nothing can serve."""
    from adam.data_source_provider import engines
    from adam.schema_provider.glue_data_catalog_schema_provider import _CLASSIFICATION_FROM_ENGINE
    from adam.schema_provider.source_identity import source_identity
    from adam.shared.provider_client import ENGINE_PROVIDER_SSM
    from cdk_infra.shared.data_source_engines import ENGINES

    assert set(ENGINES) == set(engines.known_engines()) == set(ENGINE_PROVIDER_SSM)
    assert {engine: build.ssm_key for engine, build in ENGINES.items()} == ENGINE_PROVIDER_SSM
    assert set(_CLASSIFICATION_FROM_ENGINE) == set(ENGINES)
    for engine in ENGINES:
        dsn = engines.build_dsn(engine, "h", "1", "d")
        assert source_identity({"engine": engine, "dsn": dsn, "host": "h", "port": "1", "database": "d"})


@pytest.mark.parametrize("name", ["T-External/RedshiftProviderFunction", "T-External/RedshiftScannerFunction"])
def test_redshift_connector_comes_without_the_packages_only_its_sign_in_plugins_use(bundles, name):
    """requests would bring certifi (MPL-2.0); it, beautifulsoup4 and lxml serve only the SAML, Okta,
    ADFS and Azure plugins. A password login imports none of them."""
    commands = bundles[name]["pip_commands"]
    alone = [c for c in commands if " --no-deps " in c]
    assert len(alone) == 1 and _packages(alone) == {"redshift_connector==2.2.0"}
    named = {p.split("==")[0].lower() for p in _packages(commands)}
    assert not named & {"requests", "certifi", "beautifulsoup4", "lxml"}
