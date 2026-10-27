#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Shared test fixtures for unit and integration tests."""

import os
import sys

# Add src to path so adam package is importable
_SRC = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, _SRC)
# The demo dataset tools live beside the runtime package, in backend/, because they never ship.
sys.path.insert(0, os.path.join(_SRC, ".."))

# The CDK bundling steps flatten each Lambda's sources, so handler modules
# import their siblings as top-level names ("from shared.http_response import
# ...", "from agent_provider import gateway_event"). Those names only resolve
# against the bundle layout, not the repo tree. Putting the same directories on
# the path here lets tests import the handlers exactly as Lambda does, instead
# of rewriting production imports to suit the tests.
for _bundle_dir in ("adam", "adam/agent_provider"):
    sys.path.insert(0, os.path.join(_SRC, _bundle_dir))
