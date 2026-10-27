#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""Agent events reach only the user they belong to.

Two halves, tested together because they must agree on one path shape,
/agent/{user sub}/{session id}:

- the stream processor publishes each user's events to that user's channel;
- the AppSync onSubscribe handler lets a caller subscribe only under their sub.

The handler is the real JavaScript file the stack deploys, run with node. The
AppSync runtime supplies `util`; the harness supplies a `util` whose
unauthorized() throws, which is what AppSync turns into a rejected subscription.
"""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from adam.agent_provider import sessions_stream_handler as stream

HANDLER = Path(__file__).resolve().parents[3] / "cdk_infra" / "agent_provider" / "on_subscribe.js"


def _record(user_id: str, session_id: str, seq: int = 0, kind: str = "status") -> dict:
    return {"eventName": "INSERT", "dynamodb": {"SequenceNumber": f"{seq:021d}", "NewImage": {
        "user_id": {"S": user_id},
        "sort_key": {"S": f"msg#{session_id}#{seq:06d}"},
        "message_type": {"S": kind},
        "content": {"S": json.dumps({"content": "x"})},
    }}}


@pytest.fixture
def published(monkeypatch):
    posted = []
    monkeypatch.setattr(stream, "EVENTS_API_HTTP_ENDPOINT", "events.example.com")
    monkeypatch.setattr(stream, "_sign_and_post",
                        lambda url, body: posted.append(json.loads(body)) or (200, '{"failed": []}'))
    return posted


class TestPublisherPutsTheUserInTheChannel:

    def test_events_go_to_the_owners_channel(self, published):
        stream.handler({"Records": [_record("sub-alice", "s1")]}, None)
        assert [p["channel"] for p in published] == ["/agent/sub-alice/s1"]

    def test_two_users_with_the_same_session_id_get_separate_channels(self, published):
        """Session ids are chosen by the client, so they can collide or be squatted."""
        stream.handler({"Records": [_record("sub-alice", "s1"), _record("sub-mallory", "s1")]}, None)
        assert sorted(p["channel"] for p in published) == ["/agent/sub-alice/s1", "/agent/sub-mallory/s1"]

    def test_events_for_one_session_are_batched_per_channel(self, published):
        stream.handler({"Records": [_record("sub-alice", "s1", 0), _record("sub-alice", "s1", 1)]}, None)
        assert len(published) == 1 and len(published[0]["events"]) == 2

    def test_a_record_without_a_user_is_not_published(self, published):
        record = _record("sub-alice", "s1")
        del record["dynamodb"]["NewImage"]["user_id"]
        stream.handler({"Records": [record]}, None)
        assert published == []

    def test_a_user_id_containing_a_slash_cannot_reshape_the_channel(self, published):
        stream.handler({"Records": [_record("sub-alice/other", "s1")]}, None)
        assert published == []


class TestOnSubscribeHandler:

    @pytest.fixture(autouse=True)
    def _node(self):
        if shutil.which("node") is None:
            pytest.fail("node is required to run the AppSync handler", pytrace=False)

    def _run(self, path: str, sub: str | None) -> bool:
        """True when the subscription is allowed."""
        source = HANDLER.read_text().replace('import { util } from "@aws-appsync/utils";', "")
        script = (
            "const util = { unauthorized() { throw new Error('UNAUTHORIZED'); } };\n"
            + source.replace("export function", "function")
            + f"\ntry {{ onSubscribe({{ info: {{ channel: {{ path: {json.dumps(path)} }} }}, "
              f"identity: {json.dumps({'sub': sub} if sub is not None else None)} }}); console.log('ALLOWED'); }}"
              " catch (e) { console.log(e.message); }"
        )
        out = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout.strip()
        assert out in ("ALLOWED", "UNAUTHORIZED"), out
        return out == "ALLOWED"

    def test_a_caller_may_subscribe_under_their_own_sub(self):
        assert self._run("/agent/sub-alice/s1", "sub-alice")

    def test_a_caller_may_not_subscribe_under_another_sub(self):
        assert not self._run("/agent/sub-alice/s1", "sub-mallory")

    def test_a_wildcard_inside_the_callers_own_namespace_is_allowed(self):
        """It can only ever match the caller's own events."""
        assert self._run("/agent/sub-alice/*", "sub-alice")

    @pytest.mark.parametrize("path", ["/agent/*", "/agent/sub-alice", "/agent",
                                      "/agent/sub-alice/s1/extra", "/other/sub-alice/s1", "/"])
    def test_wildcards_and_other_shapes_are_refused(self, path):
        assert not self._run(path, "sub-alice")

    def test_no_identity_is_refused(self):
        assert not self._run("/agent/sub-alice/s1", None)

    def test_an_identity_with_an_empty_sub_is_refused(self):
        assert not self._run("/agent/x/s1", "")
