#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Settings a function cannot run without: their absence is a deployment fault, said loudly."""

import os


def require_env(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"Required environment variable {name} is not set; this function is misconfigured.")
    return value
