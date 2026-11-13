"""Stream metrics use protocol fields, share parsing, and never leak a Scope child on callback failure."""
from __future__ import annotations

import json

import pytest

from rfc_agent.investigate import agent as ag
from rfc_agent.llm_metrics import CallRecorder
from rfc_agent.scope import context


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


@pytest.mark.parametrize("already_parsed", [False, True])
def test_stream_event_is_parsed_at_most_once(tmp_path, monkeypatch, already_parsed):
    event = {"type": "rate_limit_event", "rate_limit_info": {"status": "rejected"}}
    line = event if already_parsed else json.dumps(event)
    loads = json.loads
    parsed = []
    observed = []
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine="claude-code", model="m", label="lead", stage="draft")

    class Trace:
        def event(self, value):
            observed.append(value)

    def load(value):
        parsed.append(value)
        return loads(value)

    monkeypatch.setattr(ag.json, "loads", load)
    res = ag.AgentResult(label="lead", ok=False)
    ag._consume(line, res, None, Trace(), calls=rec)
    assert len(parsed) == (0 if already_parsed else 1)
    assert observed == [event] and rec.first_event is not None and res.rate_limited == 1


def test_scope_callback_failure_kills_and_reaps_its_subprocess(tmp_path, monkeypatch):
    slow = tmp_path / "slow-agent"
    slow.write_text("#!/usr/bin/env python3\nimport json, sys, time\nsys.stdin.read()\n"
                    "print(json.dumps({'type': 'system', 'subtype': 'init'}), flush=True)\ntime.sleep(30)\n")
    slow.chmod(0o755)
    popen = context.subprocess.Popen
    procs = []

    def start(*args, **kwargs):
        proc = popen(*args, **kwargs)
        procs.append(proc)
        return proc

    class BadProgress:
        def tick(self, label, kind, data):
            if kind == "start":
                raise FileNotFoundError("simulated status writer failure")

    monkeypatch.setattr(context.subprocess, "Popen", start)
    try:
        with pytest.raises(FileNotFoundError, match="status writer failure"):
            context._run_live([str(slow), "--model", "m"], "synthetic", tmp_path,
                              progress=BadProgress(), timeout_s=30)
        assert len(procs) == 1 and procs[0].poll() is not None
    finally:  # keep the regression test itself safe if process cleanup breaks again
        for proc in procs:
            if proc.poll() is None:
                context.os.killpg(proc.pid, context.signal.SIGKILL)
                proc.wait(timeout=5)


def test_scope_helper_result_error_with_exit_0_reports_its_reason(tmp_path):
    fake = tmp_path / "limited-agent"
    fake.write_text("#!/usr/bin/env python3\nimport json, sys\nsys.stdin.read()\n"
                    "print(json.dumps({'type': 'result', 'is_error': True, 'result': 'weekly limit reached'}))\n")
    fake.chmod(0o755)
    ends = []

    class Progress:
        def tick(self, label, kind, data):
            if kind == "end":
                ends.append(data)

    p = context._run_live([str(fake), "--model", "m"], "synthetic", tmp_path, progress=Progress(), timeout_s=30)
    assert p.returncode == 0
    assert ends == [ends[0]] and ends[0]["ok"] is False and ends[0]["error"] == "weekly limit reached"
