#  Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#  SPDX-License-Identifier: Apache-2.0

"""web_search must return live, cited context -- not an empty success.

It was backed by DuckDuckGo's Instant Answer API, which only knows encyclopedic
entities. Measured against the deployed tool: "Oracle Corporation" returned 4
results, "current US 10-year treasury yield outlook" returned 0 -- as an HTTP 200
with an empty list, which the agent cannot tell apart from "nothing relevant
exists". Market and product context is what the demo's web step is for, so the
step that is meant to show the agent reaching beyond the databases produced
nothing.

It is now Amazon Nova web grounding. These tests pin the response contract:
answer text, deduplicated source URLs (the grounding terms require they be shown
to the user), and an explicit flag when nothing was cited.
"""

import json

import pytest

from adam.agent_provider import web_search_handler


class _FakeBedrock:
    def __init__(self, content):
        self.content = content
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": self.content}}}


def _cite(url, domain):
    return {"citationsContent": {"citations": [
        {"location": {"web": {"url": url, "domain": domain}}}]}}


@pytest.fixture
def bedrock(monkeypatch):
    monkeypatch.setenv("WEB_SEARCH_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
    fake = _FakeBedrock([
        {"toolUse": {"name": "nova_grounding"}},
        {"text": "The 10-year Treasury yield is 5.17%."},
        _cite("https://www.investing.com/rates-bonds/", "investing.com"),
        _cite("https://www.investing.com/rates-bonds/", "investing.com"),
        _cite("https://www.cnbc.com/bonds/", "cnbc.com"),
        {"text": " It is a benchmark for mortgages."},
    ])
    monkeypatch.setattr(web_search_handler, "_get_bedrock", lambda: fake)
    return fake


def _run(query="What is the current US 10-year Treasury yield?", n=5):
    return json.loads(web_search_handler.search(query, n)["content"])


class TestGroundedAnswer:

    def test_grounding_is_requested_as_a_system_tool(self, bedrock):
        _run()
        cfg = bedrock.calls[0]["toolConfig"]
        assert cfg == {"tools": [{"systemTool": {"name": "nova_grounding"}}]}
        assert bedrock.calls[0]["modelId"] == "us.amazon.nova-2-lite-v1:0"

    def test_sources_are_deduplicated_in_order(self, bedrock):
        assert [s["domain"] for s in _run()["sources"]] == ["investing.com", "cnbc.com"]

    def test_answer_text_is_joined_across_blocks(self, bedrock):
        assert _run()["answer"].endswith("benchmark for mortgages.")

    def test_the_answer_says_it_is_data_from_the_web(self, bedrock):
        """Web pages are the text most likely to be written as instructions to an agent."""
        from adam.agent_provider.gateway_event import UNTRUSTED_NOTICE

        assert _run()["notice"] == UNTRUSTED_NOTICE

    @pytest.mark.parametrize("asked, given", [(0, 1), (3, 3), (50, 10)])
    def test_num_results_is_kept_between_one_and_ten(self, monkeypatch, asked, given):
        monkeypatch.setenv("WEB_SEARCH_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
        cited = [_cite(f"https://site{i}.example/", f"site{i}.example") for i in range(12)]
        monkeypatch.setattr(web_search_handler, "_get_bedrock",
                            lambda: _FakeBedrock([{"text": "An answer."}, *cited]))
        assert len(_run(n=asked)["sources"]) == given

    def test_an_uncited_answer_is_flagged_not_passed_off_as_live(self, monkeypatch):
        monkeypatch.setenv("WEB_SEARCH_MODEL_ID", "us.amazon.nova-2-lite-v1:0")
        monkeypatch.setattr(web_search_handler, "_get_bedrock",
                            lambda: _FakeBedrock([{"text": "From memory."}]))
        out = _run()
        assert out["sources"] == []
        assert "ungrounded" in out["note"]


class TestFailuresAreLoud:

    def test_missing_model_id_is_an_error_not_a_default(self, monkeypatch):
        monkeypatch.delenv("WEB_SEARCH_MODEL_ID", raising=False)
        monkeypatch.setattr(web_search_handler, "_get_bedrock", lambda: _FakeBedrock([]))
        out = _run()
        assert "WEB_SEARCH_MODEL_ID" in out["error"]

    def test_empty_query_is_refused(self, bedrock):
        assert "error" in _run(query="  ")


def test_what_was_searched_for_is_not_logged(bedrock, caplog):
    """A question can carry what the user is working on; the log keeps only its shape."""
    import logging

    with caplog.at_level(logging.DEBUG):
        web_search_handler.search("query-marker-1f3a acquisition plans of Example Corp", 3)
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "query-marker-1f3a" not in logged and "Example Corp" not in logged
