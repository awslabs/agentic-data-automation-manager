#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Tests against a deployed stage, named by TEST_DEPLOYED_PREFIX (run with -m deployed)."""

import pytest


def pytest_collection_modifyitems(items):
    for item in items:
        if "tests/deployed" in str(item.fspath):
            item.add_marker(pytest.mark.deployed)
