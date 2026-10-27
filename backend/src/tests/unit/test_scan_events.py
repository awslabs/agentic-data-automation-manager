#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""The scan bus's event names, as the CDK rules match them and the functions publish them.

A rule that matches a name no function publishes delivers nothing and says nothing,
so the two lists are compared here rather than trusted to agree.
"""

import json
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[3]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from adam.shared import scan_events  # noqa: E402
from cdk_infra.shared import scan_bus  # noqa: E402


def test_the_sources_agree():
    assert scan_bus.SCHEMA_SERVICE_SOURCE == scan_events.SCHEMA_SERVICE
    assert scan_bus.DATABASE_PROVIDER_SOURCE == scan_events.DATABASE_PROVIDER


def test_the_rules_match_every_event_each_side_publishes():
    assert scan_bus.SCAN_REQUESTED == scan_events.SCAN_REQUESTED
    assert scan_bus.REGISTRATION_DISABLED == scan_events.REGISTRATION_DISABLED
    assert scan_bus.PROVIDER_EVENTS == [scan_events.PROVIDER_REGISTERED, scan_events.PROVIDER_DEREGISTERED,
                                        scan_events.SCAN_COMPLETED, scan_events.SCAN_FAILED]


class _Events:
    def __init__(self, failed: int = 0):
        self.entries, self.failed = [], failed

    def put_events(self, Entries):
        self.entries.extend(Entries)
        return {"FailedEntryCount": self.failed,
                "Entries": [{"ErrorCode": "AccessDenied", "ErrorMessage": "no"}] if self.failed else [{}]}


class TestPublish:

    @pytest.fixture(autouse=True)
    def _env(self, monkeypatch):
        monkeypatch.setenv("SCAN_BUS_NAME", "bus")
        monkeypatch.setenv("STACK_NAME", "Stack")

    def test_it_publishes_under_the_stacks_source_for_that_service(self):
        events = _Events()
        scan_events.publish(events, scan_events.SCHEMA_SERVICE, scan_events.SCAN_REQUESTED, {"a": 1})
        assert events.entries == [{"EventBusName": "bus", "Source": "Stack.schema-service",
                                   "DetailType": "ScanRequested", "Detail": json.dumps({"a": 1})}]

    def test_an_entry_eventbridge_refused_raises(self):
        """PutEvents reports a refused entry in its response, not as an exception."""
        with pytest.raises(RuntimeError, match="refused the ScanFailed event: AccessDenied"):
            scan_events.publish(_Events(failed=1), scan_events.DATABASE_PROVIDER, scan_events.SCAN_FAILED, {})

    def test_without_the_bus_it_raises(self, monkeypatch):
        monkeypatch.delenv("SCAN_BUS_NAME")
        with pytest.raises(RuntimeError, match="SCAN_BUS_NAME"):
            scan_events.publish(_Events(), scan_events.SCHEMA_SERVICE, scan_events.SCAN_REQUESTED, {})


def test_a_scan_result_key_is_per_target_and_request():
    """Two scans never share a file, and every file is under scans/, the only prefix the
    scanners may write and ingestion may read (test_cdk_security checks the grants)."""
    keys = {scan_events.scan_result_key(t, r) for t in ("t1", "t2") for r in ("r1", "r2")}
    assert len(keys) == 4 and all(k.startswith("scans/") for k in keys)
