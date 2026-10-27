#!/usr/bin/env python3
#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""CDK app for the website, deployed after the backend.

It builds the UI from the backend's stack outputs, which install_cdk.sh writes to
frontend/backend_outputs_<stage>.json, then deploys it to S3 and CloudFront:

    cdk deploy -c stage=dev
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import aws_cdk as cdk
from cdk_nag import AwsSolutionsChecks

from cdk_infra import nag
from cdk_infra.app_config import connect_sources, stack_prefix
from cdk_infra.frontend_stack import FrontendStack

HERE = Path(__file__).resolve().parent
UI_DIR = HERE / "src" / "adam_ui"

app = cdk.App()

# Required: install_cdk.sh passes it. A stage guessed here could build against another one.
STAGE = app.node.try_get_context("stage")
if not STAGE:
    raise ValueError("No stage given: deploy with install_cdk.sh, or pass -c stage=<stage>.")
PREFIX = stack_prefix(STAGE)

REGION = os.getenv("CDK_DEFAULT_REGION")
if not REGION:
    raise ValueError(
        "CDK_DEFAULT_REGION is not set. Run through the cdk CLI with a profile or "
        "AWS_REGION set (it exports CDK_DEFAULT_REGION), or export it yourself."
    )
env = cdk.Environment(account=os.getenv("CDK_DEFAULT_ACCOUNT"), region=REGION)

outputs_file = HERE / f"backend_outputs_{STAGE}.json"
# A file copied or renamed from another stage would build a UI pointed at that stage.
outputs = json.loads(outputs_file.read_text()) if outputs_file.is_file() else {}
if not any(stack.startswith(f"{PREFIX}-") for stack in outputs):
    raise ValueError(f"{outputs_file} has no outputs for {PREFIX}. "
                     f"Deploy the backend for stage {STAGE} first (install_cdk.sh -b -s {STAGE}).")

bash = shutil.which("bash")
if bash is None:
    raise RuntimeError("bash is not on PATH; it is needed to build the UI.")
build = subprocess.run(  # noqa: S603 - fixed argv: our own build script, no shell, no user input
    [bash, str(UI_DIR / "build_website_deployment.sh")],
    cwd=UI_DIR, env={**os.environ, "OUTPUTS_FILE": str(outputs_file)},
    capture_output=True, text=True, check=False,
)
# A failed build must not go on to deploy whatever stale dist/ is on disk.
if build.returncode != 0:
    raise RuntimeError(f"UI build failed (exit {build.returncode}):\n{build.stderr[-2000:]}")

FrontendStack(app, f"{PREFIX}-Frontend",
    parent_stack_name=PREFIX,
    production=STAGE == "prod",
    dist_path=str(UI_DIR / "dist"),
    connect_sources=connect_sources(outputs, REGION),
    env=env,
)

# Every stack is checked against the AWS Solutions rules on synth; an unresolved finding fails it.
cdk.Aspects.of(app).add(AwsSolutionsChecks())
nag.acknowledge_app_wide(app)

app.synth()
