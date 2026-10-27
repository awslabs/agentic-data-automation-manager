#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The project's structural rules, checked on every build.

- A table belongs to one service: only roles in its own stack have IAM on it,
  and no policy needs a Deny to keep another role out.
- No service package imports another service's package; services reach each
  other through their functions.
- shared/ holds only code two or more services use, and none of it reads or
  writes AWS data (tables, secrets, the schema index): that belongs to the
  service that owns the data.
- Stacks are wired by passing resources in app.py, never by named exports.
"""

import ast
import json
import re
from pathlib import Path

import pytest
from aws_cdk.assertions import Template

from tests.unit.cdk_stacks import BACKEND, build_stacks

PACKAGE = BACKEND / "src" / "adam"
SERVICES = ("agent_provider", "auth_provider", "data_source_provider", "schema_provider")


@pytest.fixture(scope="module")
def templates() -> dict[str, dict]:
    import os
    previous = os.getcwd()
    os.chdir(BACKEND)
    try:
        return {name: Template.from_stack(stack).to_json() for name, stack in build_stacks().items()}
    finally:
        os.chdir(previous)


def _dynamodb_statements(template: dict):
    for policy in (r for r in template["Resources"].values() if r["Type"] == "AWS::IAM::Policy"):
        for statement in policy["Properties"]["PolicyDocument"]["Statement"]:
            actions = statement["Action"] if isinstance(statement["Action"], list) else [statement["Action"]]
            if any(str(a).startswith("dynamodb:") for a in actions):
                yield statement


class TestEachTableBelongsToOneService:

    def test_a_role_reaches_only_tables_in_its_own_stack(self, templates):
        own = {name: {lid for lid, r in t["Resources"].items() if r["Type"] == "AWS::DynamoDB::Table"}
               for name, t in templates.items()}
        for name, template in templates.items():
            for statement in _dynamodb_statements(template):
                resources = json.dumps(statement.get("Resource"))
                if resources == '"*"':
                    assert statement["Action"] in ("dynamodb:ListStreams", ["dynamodb:ListStreams"]), statement
                    continue
                listed = statement["Resource"] if isinstance(statement["Resource"], list) else [statement["Resource"]]
                for resource in listed:
                    # Each one a table of this stack's, by reference (its ARN, or its ARN plus
                    # /index/* or /stream/*): never an ARN written out, which could be anyone's.
                    referenced = set(re.findall(r'"Fn::GetAtt": \["([A-Za-z0-9]+)"', json.dumps(resource)))
                    assert not isinstance(resource, str) and referenced and referenced <= own[name], (
                        f"{name}: a role reaches a table it does not own, or names one by string: {statement}")

    def test_no_policy_needs_a_deny_on_a_table(self, templates):
        for name, template in templates.items():
            denies = [s for s in _dynamodb_statements(template) if s["Effect"] == "Deny"]
            assert denies == [], f"{name}: a Deny means an Allow was too broad: {denies}"


def _imports(path: Path) -> set[str]:
    """Every module the file imports, absolute: relative imports are resolved against the
    file's package, and `from x import y` names x.y as well, since y may be a module."""
    package = path.relative_to(PACKAGE).parent.parts
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            base = ".".join(package[:len(package) - node.level + 1]) if node.level else ""
            module = ".".join(p for p in (base, node.module or "") if p)
            names.add(module)
            names |= {f"{module}.{a.name}" for a in node.names}
    return names


def _stems() -> dict[str, set[str]]:
    """Module name -> the services with a module of that name. A bundle lays its files out
    flat, so a handler imports a sibling by its bare name ("import conversations")."""
    owners: dict[str, set[str]] = {}
    for service in SERVICES:
        for path in (PACKAGE / service).rglob("*.py"):
            if path.name != "__init__.py":
                owners.setdefault(path.stem, set()).add(service)
    return owners


_OWNERS = _stems()


def _services_of(module: str, importer: str) -> set[str]:
    """The services a module belongs to: by its package, or for a bare bundled name, by
    which services have a module so named (the importer's own wins: it is a sibling)."""
    parts = module.split(".")
    if parts[0] == "adam" and len(parts) > 1:
        parts = parts[1:]
    if parts[0] in SERVICES:
        return {parts[0]}
    owners = _OWNERS.get(parts[0], set())
    return set() if importer in owners else owners


class TestServicesReachEachOtherThroughTheirFunctions:

    @pytest.mark.parametrize("service", SERVICES)
    def test_no_service_imports_another(self, service):
        crossings = set()
        for path in (PACKAGE / service).rglob("*.py"):
            for module in _imports(path):
                if _services_of(module, service) - {service}:
                    crossings.add((path.relative_to(PACKAGE).as_posix(), module))
        assert crossings == set()


class TestSharedIsOnlyWhatServicesShare:

    def _shared_modules(self):
        return sorted(p for p in (PACKAGE / "shared").glob("*.py") if p.name != "__init__.py")

    def test_every_shared_module_is_used_by_two_services(self):
        for shared in self._shared_modules():
            users = set()
            for service in SERVICES:
                for path in (PACKAGE / service).rglob("*.py"):
                    if any(m == f"shared.{shared.stem}" or m.endswith(f".shared.{shared.stem}")
                           for m in _imports(path)):
                        users.add(service)
            assert len(users) >= 2, f"shared/{shared.name} is used by {sorted(users) or 'no service'}"

    @pytest.mark.parametrize("client", ["dynamodb", "secretsmanager", "glue"])
    def test_shared_code_touches_no_service_data(self, client):
        """Any AWS client shared code makes (boto3.client, a Session's, by keyword) must name
        its service plainly, and not this one."""
        for shared in self._shared_modules():
            for node in ast.walk(ast.parse(shared.read_text())):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                        and node.func.attr in ("client", "resource")):
                    continue
                named = node.args[0] if node.args else next(
                    (k.value for k in node.keywords if k.arg == "service_name"), None)
                assert isinstance(named, ast.Constant), f"shared/{shared.name} makes a client it does not name"
                assert named.value != client, (
                    f"shared/{shared.name} calls {client}: that data belongs to one service, "
                    "reached through its function")


class TestStacksAreWiredByPassingResources:

    def test_no_named_exports_or_imports(self):
        sources = [BACKEND / "app.py", *(BACKEND / "cdk_infra").rglob("*.py"),
                   BACKEND.parent / "frontend" / "app.py", *(BACKEND.parent / "frontend" / "cdk_infra").rglob("*.py")]
        for path in sources:
            code = "\n".join(line.split("#", 1)[0] for line in path.read_text().splitlines())
            assert "export_name" not in code and "import_value" not in code, path
