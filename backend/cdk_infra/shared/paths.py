#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Absolute paths to the Lambda sources, so a stack builds from any working directory."""

from pathlib import Path

SRC = str(Path(__file__).resolve().parents[2] / "src")
PACKAGE = f"{SRC}/adam"
AUTH_PROVIDER = f"{PACKAGE}/auth_provider"
AGENT_PROVIDER = f"{PACKAGE}/agent_provider"
AGENT_RUNTIME = f"{AGENT_PROVIDER}/agent_runtime"
