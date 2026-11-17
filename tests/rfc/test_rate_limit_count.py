"""Rate-limit counting reads protocol fields and failed-result text, never content that discusses limits."""
from __future__ import annotations

import json

import pytest

from rfc_agent.investigate import agent as ag


@pytest.mark.parametrize("event,expected", [
    ({"type": "stream_event", "event": {"type": "content_block_delta",
      "delta": {"text": "rate_limit and overloaded_error are RFC topics"}}}, 0),
    ({"type": "assistant", "message": {"content": [{"type": "text", "text": "rate_limit"}]}}, 0),
    ({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "input": {"file_path": "rate_limit.py"}}]}}, 0),
    ({"type": "user", "message": {"content": [{"type": "tool_result", "content": "rate_limit"}]}}, 0),
    ({"type": "result", "result": "RFC: rate_limit"}, 0),
    ({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}, 0),
    ({"type": "rate_limit_event", "rate_limit_info": {"status": "allowed_warning"}}, 0),
    ({"type": "rate_limit_event"}, 0),
    ({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}, 1),
    ({"type": "error", "error": {"type": "rate_limit_error"}}, 1),
    ({"type": "error", "error": {"type": "overloaded_error"}}, 1),
    ({"type": "assistant", "error": "rate_limit"}, 1),
    ({"type": "overloaded_error"}, 1),
    ({"type": "result", "is_error": True, "result": "API Error: 429 {\"type\":\"rate_limit_error\"}"}, 1),
    ({"type": "result", "is_error": True, "result": "provider retries exhausted: 429 rate_limit_error"}, 1),
    ({"type": "result", "is_error": True, "result": "You've hit your weekly limit"}, 0),
])
def test_rate_limit_count_uses_protocol_fields_not_content(event, expected):
    res = ag.AgentResult(label="lead", ok=False)
    ag._consume(json.dumps(event), res, None)
    assert res.rate_limited == expected


def test_non_object_lines_are_ignored():
    res = ag.AgentResult(label="lead", ok=False)
    for line in ("[1, 2]", "42", '"rate_limit"'):
        ag._consume(line, res, None)
    assert res.rate_limited == 0
