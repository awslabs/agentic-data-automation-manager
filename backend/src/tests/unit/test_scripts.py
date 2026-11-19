#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""install_cdk.sh's dangerous step, run for real: it sources the deploy.env it writes."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]


def test_a_typed_deploy_setting_is_kept_as_data(tmp_path):
    """install_cdk.sh appends what it asked for to deploy.env and sources the file next run.
    A value with a space, a semicolon or a $(...) must come back as typed and run nothing."""
    line = next(ln.strip() for ln in (ROOT / "install_cdk.sh").read_text().splitlines()
                if ln.strip().startswith("printf '%s=%q") and "DEPLOY_ENV" in ln)
    env_file, marker = tmp_path / "deploy.env", tmp_path / "ran"
    typed = f"subnet-a, subnet-b; touch {marker} $(touch {marker})"
    script = f'DEPLOY_ENV={env_file}; v=PRIVATE_SUBNET_IDS; value="$1"; {line}\n' \
             f'set -a; source {env_file}; set +a; printf %s "$PRIVATE_SUBNET_IDS"'
    result = subprocess.run(["bash", "-c", script, "bash", typed], capture_output=True, text=True, check=True)
    assert result.stdout == typed
    assert not marker.exists()
