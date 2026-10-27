#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""A chat event that AppSync did not accept is retried, never reported as published.

The browser waits on the event stream for a turn's answer. If a publish fails and
the stream processor treats it as done, the record is checkpointed and the user waits
forever. So a failed channel comes back as a batch item failure, and only that one:
the stream retries from it, and channels that did publish are not resent for nothing.

Drives the real handler. Only the signed HTTP call to AppSync is replaced, with the
responses AppSync gives: a non-200, or a 200 whose body lists failed events.
"""

import json

import pytest

from adam.agent_provider import sessions_stream_handler as stream


def _record(user_id: str, session_id: str, seq: int) -> dict:
    return {"eventName": "INSERT", "dynamodb": {"SequenceNumber": f"{seq:021d}", "NewImage": {
        "user_id": {"S": user_id},
        "sort_key": {"S": f"msg#{session_id}#20261019T000000000000Z#{seq:06d}"},
        "message_type": {"S": "output"},
        "content": {"S": json.dumps({"content": "answer"})},
    }}}


@pytest.fixture
def appsync(monkeypatch):
    """Answers each channel's publish with the response set for it (default: accepted)."""
    responses, published = {}, []

    def post(url, body):
        channel = json.loads(body)["channel"]
        status, payload = responses.get(channel, (200, '{"failed": []}'))
        if status == 200 and payload == '{"failed": []}':
            published.append(channel)
        return status, payload

    monkeypatch.setattr(stream, "EVENTS_API_HTTP_ENDPOINT", "events.example.com")
    monkeypatch.setattr(stream, "_sign_and_post", post)
    return responses, published


BATCH = [_record("sub-alice", "s1", 1), _record("sub-alice", "s1", 2), _record("sub-bob", "s2", 3)]


def test_a_refused_publish_is_reported_as_a_batch_item_failure_and_the_other_channel_is_not(appsync):
    responses, published = appsync
    responses["/agent/sub-alice/s1"] = (500, "internal error")
    result = stream.handler({"Records": BATCH}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": f"{1:021d}"}]}
    assert published == ["/agent/sub-bob/s2"]


def test_events_appsync_lists_as_failed_inside_a_200_are_retried(appsync):
    responses, _ = appsync
    responses["/agent/sub-bob/s2"] = (200, json.dumps({"failed": [{"index": 0, "code": "Throttled"}]}))
    result = stream.handler({"Records": BATCH}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": f"{3:021d}"}]}


def test_a_fully_accepted_batch_reports_no_failures(appsync):
    _, published = appsync
    assert stream.handler({"Records": BATCH}, None) == {"batchItemFailures": []}
    assert sorted(published) == ["/agent/sub-alice/s1", "/agent/sub-bob/s2"]


def test_without_an_endpoint_the_invocation_fails_instead_of_dropping_events(monkeypatch):
    monkeypatch.setattr(stream, "EVENTS_API_HTTP_ENDPOINT", "")
    with pytest.raises(RuntimeError, match="EVENTS_API_HTTP_ENDPOINT"):
        stream.handler({"Records": BATCH}, None)


@pytest.fixture
def appsync_server(monkeypatch):
    """A local HTTP server in AppSync's place: answers each POST with the status and body set."""
    import secrets
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    monkeypatch.setenv("AWS_ACCESS_KEY_ID", secrets.token_hex(10))
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", secrets.token_hex(20))
    monkeypatch.delenv("AWS_SESSION_TOKEN", raising=False)
    answer, received = {"status": 200, "body": '{"failed": []}'}, []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append((dict(self.headers), self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(answer["status"])
            self.end_headers()
            self.wfile.write(answer["body"].encode())

        def log_message(self, format, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/event", answer, received
    finally:
        server.shutdown()
        server.server_close()


def test_the_publish_is_sent_signed_and_its_answer_returned(appsync_server):
    url, _, received = appsync_server
    assert stream._sign_and_post(url, b'{"channel": "/agent/a/s"}') == (200, '{"failed": []}')
    headers, body = received[0]
    signature = headers["Authorization"]
    assert signature.startswith("AWS4-HMAC-SHA256 ") and "/appsync/aws4_request" in signature
    assert body == b'{"channel": "/agent/a/s"}'


@pytest.mark.parametrize("status", [400, 401, 500])
def test_a_refusal_comes_back_as_its_status_and_body_not_an_exception(appsync_server, status):
    url, answer, _ = appsync_server
    answer.update(status=status, body="refused")
    assert stream._sign_and_post(url, b"{}") == (status, "refused")


def test_an_unreachable_endpoint_is_retried_per_channel_not_a_failed_invocation(appsync_server, monkeypatch):
    monkeypatch.setattr(stream, "EVENTS_API_HTTP_ENDPOINT", "127.0.0.1:9")  # nothing listens
    result = stream.handler({"Records": BATCH}, None)
    assert result == {"batchItemFailures": [{"itemIdentifier": f"{1:021d}"}, {"itemIdentifier": f"{3:021d}"}]}
