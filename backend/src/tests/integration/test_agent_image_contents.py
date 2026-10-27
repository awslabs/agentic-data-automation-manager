#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The built agent image, opened up and listed.

test_agent_image.py checks what the image is built from; this builds it and looks at
what a process inside it can actually see.
"""

import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

pytestmark = pytest.mark.integration

PACKAGE = Path(__file__).resolve().parents[2] / "adam"
BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from cdk_infra.shared.bundling import AGENT_IMAGE_FILES  # noqa: E402

TAG = "agent-image-contents-test"

RUNTIME = shutil.which("finch") or shutil.which("docker")


def _run(*args, check=True):
    return subprocess.run([RUNTIME, *args], capture_output=True, text=True, check=check, timeout=1200)


def _inside(*command) -> str:
    return _run("run", "--rm", "--entrypoint", command[0], TAG, *command[1:]).stdout


@pytest.fixture(scope="module")
def image():
    if RUNTIME is None:
        pytest.fail("no container runtime (finch or docker) to build the agent image", pytrace=False)
    # The build context CDK stages: the package, cut down to AGENT_IMAGE_FILES.
    with tempfile.TemporaryDirectory() as context:
        for name in AGENT_IMAGE_FILES:
            (Path(context) / name).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(PACKAGE / name, Path(context) / name)
        _run("build", "-t", TAG, "-f", str(Path(context) / "agent_provider/agent_runtime/Dockerfile"), context)
    yield TAG
    _run("rmi", "-f", TAG, check=False)


def test_the_app_directory_holds_only_the_agent_and_its_token_verifier(image):
    listed = _inside("find", "/app", "-type", "f").split()
    assert sorted(listed) == ["/app/agent.py", "/app/agent_provider/__init__.py",
                              "/app/agent_provider/cognito_identity_provider.py",
                              "/app/agent_provider/identity_provider.py",
                              "/app/shared/__init__.py", "/app/shared/environment.py"]


def test_it_runs_as_uid_1000(image):
    assert _inside("id", "-u").strip() == "1000"


def test_there_is_no_package_installer(image):
    result = _run("run", "--rm", "--entrypoint", "python", image, "-m", "pip", "--version", check=False)
    assert result.returncode != 0 and "No module named pip" in result.stderr


# Run in the image: every installed distribution, and those reachable from the agent's
# requirements through each distribution's own declared requirements (extras only when asked
# for; other markers count as met, so the reachable set can only be larger than pip's).
_CLOSURE = r"""
import json, re, sys
import importlib.metadata as m

def key(name):
    return re.sub(r"[-_.]+", "-", name).lower()

installed = {key(d.metadata["Name"]) for d in m.distributions()}
todo, reached = [], set()
for line in sys.argv[1:]:
    name, extras = re.match(r"([A-Za-z0-9_.-]+)(?:\[([^\]]*)\])?", line).groups()
    todo.append((key(name), {key(e) for e in (extras or "").split(",") if e}))
while todo:
    name, extras = todo.pop()
    if (name, frozenset(extras)) in reached or name not in installed:
        continue
    reached.add((name, frozenset(extras)))
    for req in m.distribution(name).requires or []:
        dep, dep_extras, marker = re.match(r"([A-Za-z0-9_.-]+)(?:\[([^\]]*)\])?[^;]*(?:;(.*))?", req).groups()
        wanted = re.search(r"extra\s*==\s*[\"']([^\"']+)", marker or "")
        if wanted and key(wanted.group(1)) not in extras:
            continue
        todo.append((key(dep), {key(e) for e in (dep_extras or "").split(",") if e}))
print(json.dumps({"installed": sorted(installed), "reached": sorted({n for n, _ in reached})}))
"""


def test_the_installed_distributions_are_the_agents_and_their_dependencies(image):
    listed = (PACKAGE / "agent_provider/agent_runtime/requirements.txt").read_text().splitlines()
    requirements = [r.strip() for r in listed
                    if r.strip() and not r.startswith("#")]
    result = json.loads(_inside("python", "-c", _CLOSURE, *requirements))
    extra = set(result["installed"]) - set(result["reached"])
    # Nothing the agent's requirements do not need: no installer, no database driver.
    assert extra == set(), sorted(extra)


def test_the_agent_imports_in_the_image(image):
    # AgentCore Runtime sets AWS_REGION in the container; the agent has no default of its own.
    out = _run("run", "--rm", "-e", "AWS_REGION=us-east-1", "--entrypoint", "python", image,
               "-c", "import agent; print('imported')")
    assert out.stdout.strip().endswith("imported")
