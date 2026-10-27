#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""What the agent's container image is built from, and what it holds.

The agent runs the model's tool calls on behalf of users, so whatever sits in its
image is whatever a compromised agent can read. The image holds the agent and the
packages it imports, and nothing else.

Same method as test_bundles.py: list what is there, remove what we want, and
nothing may be left.
"""

import ast
import re
import sys
from pathlib import Path

import pytest

PACKAGE = Path(__file__).resolve().parents[2] / "adam"
AGENT_DIR = PACKAGE / "agent_provider" / "agent_runtime"
BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cdk_infra.shared.bundling import AGENT_IMAGE_FILES  # noqa: E402

# The image's own Python: the agent and the verifier it checks callers' tokens with.
IMAGE_SOURCES = [PACKAGE / f for f in AGENT_IMAGE_FILES if f.endswith(".py")]
# Packages the image carries as files rather than as requirements.
IN_THE_IMAGE = {"agent_provider", "shared"}


def _name(requirement: str) -> str:
    return re.split(r"[<>=!~\[; ]", requirement.strip(), maxsplit=1)[0].lower().replace("_", "-")


def _requirements() -> list[str]:
    return [line.strip() for line in (AGENT_DIR / "requirements.txt").read_text().splitlines()
            if line.strip() and not line.startswith("#")]


def test_every_requirement_is_bounded_below():
    assert [r for r in _requirements() if ">=" not in r and "==" not in r] == []


def _third_party_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    top = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            top.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            top.add(node.module.split(".")[0])
    return {m for m in top if m not in sys.stdlib_module_names}


# Which requirement provides each third-party module the agent imports. Written out,
# not read from the installed packages, so the test means the same in every environment.
PROVIDED_BY = {
    "strands": "strands-agents",
    "bedrock_agentcore": "bedrock-agentcore",
    "boto3": "boto3",
    "botocore": "boto3",
    "jwt": "pyjwt",
}


def _image_imports() -> set[str]:
    return set().union(*(_third_party_imports(p) for p in IMAGE_SOURCES)) - IN_THE_IMAGE


def test_every_import_in_the_image_comes_from_a_requirement():
    imported = _image_imports()
    unknown = imported - set(PROVIDED_BY)
    assert not unknown, f"imported, and no requirement is known to provide it: {sorted(unknown)}"
    assert {PROVIDED_BY[m] for m in imported} <= {_name(r) for r in _requirements()}


def test_every_requirement_is_used():
    used = {PROVIDED_BY[m] for m in _image_imports()}
    # Not imported by name: the OpenTelemetry wrapper starts the process, and cryptography is
    # pyjwt[crypto]'s RSA backend, pinned beside it.
    unexplained = {_name(r) for r in _requirements()} - used - {"aws-opentelemetry-distro", "cryptography"}
    assert not unexplained, f"requested but never imported: {sorted(unexplained)}"


class TestDockerfile:
    @pytest.fixture(scope="class")
    def lines(self):
        text = (AGENT_DIR / "Dockerfile").read_text()
        return [ln.strip() for ln in text.replace("\\\n", " ").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]

    def test_it_copies_only_the_files_we_want(self, lines):
        copied = set()
        for line in lines:
            if line.startswith("COPY ") and "--from=" not in line:
                parts = line.split()[1:]
                copied.update(parts[:-1])
        # Exactly the files the build context is cut down to, but the Dockerfile itself.
        assert copied == set(AGENT_IMAGE_FILES) - {"agent_provider/agent_runtime/Dockerfile"}

    def test_it_never_uses_add(self, lines):
        assert [ln for ln in lines if ln.startswith("ADD ")] == []

    def test_it_runs_as_an_unprivileged_user(self, lines):
        users = [ln for ln in lines if ln.startswith("USER ")]
        assert users and users[-1].split()[1].split(":")[0] not in {"root", "0"}

    def test_it_installs_nothing_after_the_user_switch(self, lines):
        after = lines[max(i for i, ln in enumerate(lines) if ln.startswith("USER ")):]
        assert [ln for ln in after if "pip " in ln or "apt" in ln] == []


def test_the_agent_serves_on_the_port_agentcore_calls():
    """AgentCore Runtime sends requests to 8080; app.run() serves there unless told otherwise."""
    runs = [n for n in ast.walk(ast.parse((AGENT_DIR / "agent.py").read_text()))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "run"
            and isinstance(n.func.value, ast.Name) and n.func.value.id == "app"]
    assert len(runs) == 1
    port = next((k.value for k in runs[0].keywords if k.arg == "port"), None)
    assert port is None or (isinstance(port, ast.Constant) and port.value == 8080)


def test_the_agent_prompt_names_only_tools_the_gateway_serves():
    from cdk_infra.agent_provider import gateway_tool_schema
    served = {t["name"] for t in gateway_tool_schema.TOOLS} if hasattr(gateway_tool_schema, "TOOLS") else None
    assert served, "cannot read the tool list from gateway_tool_schema"
    from adam.agent_provider.agent_runtime import agent
    # The prompt as built when the Gateway serves every tool it can, web_search included.
    prompt = agent.system_prompt([f"target___{name}" for name in served])
    promised = set(re.findall(r"^- (\w+):", prompt, flags=re.M))
    assert promised == served
