"""Per-visible-response metrics; these tests never call a model or need Langfuse credentials."""
from __future__ import annotations

import json

import pytest

from rfc_agent.llm_metrics import CallRecorder, enabled


class Clock:
    value = 0.0

    def __call__(self):
        return self.value


class Cfg:
    def __init__(self, **d):
        self.d = d

    def get(self, key, default=None):
        return self.d.get(key, default)


def rows(rec):
    return [json.loads(line) for line in rec.path.read_text().splitlines()]


@pytest.mark.parametrize("engine", ["axon", "pi"])
def test_pi_timing_tokens_cache_and_shutdown(tmp_path, engine):
    clock = Clock()
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine=engine, model="requested", label="T1",
                       stage="investigate", clock=clock, requested_model="logical-model")

    def event(t, ev):
        clock.value = t
        rec.feed(ev)

    event(1, {"type": "session"})
    msg = {"role": "assistant", "model": "actual", "provider": "test"}
    event(2, {"type": "message_start", "message": msg})
    event(3, {"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta"}})
    usage = {"input": 10, "output": 5, "cacheRead": 80, "cacheWrite": 10, "cost": {"total": 0.01}}
    event(4, {"type": "message_end", "message": {**msg, "usage": usage, "stopReason": "toolUse"}})
    event(5, {"type": "message_end", "message": {"role": "toolResult"}})
    event(6, {"type": "message_start", "message": msg})
    event(8, {"type": "message_end", "message": {**msg, "usage": usage, "stopReason": "stop"}})
    event(9, {"type": "agent_settled"})
    clock.value = 11
    summary = rec.finish()
    first, second = rows(rec)
    assert first["observed_turn_s"] == 4 and first["event_span_s"] == 2
    assert first["turn_boundary"] == "process_start_including_startup"
    assert first["time_to_first_delta_s"] == 1 and first["boundary_to_first_delta_s"] == 3
    assert first["cache_hit_ratio"] == 0.8 and first["total_input_tokens"] == 100
    assert first["model"] == "actual" and first["provider"] == "test"
    assert first["requested_model"] == "logical-model"
    assert first["cost_usd"] == 0.01 and first["provider_request_latency_s"] is None
    assert second["observed_turn_s"] == 3 and second["turn_boundary"] == "last_tool_result"
    assert second["tool_and_harness_gap_s"] == 1
    assert first["tool_and_harness_gap_s"] is None
    assert summary["first_event_delay_s"] == 1 and summary["first_assistant_delay_s"] == 2
    assert summary["settled_to_exit_s"] == 2 and summary["event_span_seconds_sum"] == 4
    assert summary["visible_calls"] == 2 and summary["metrics_errors"] == 0
    assert rec.finish() == summary and len(rows(rec)) == 2


def test_claude_usage_snapshots_and_duplicate_assistant_blocks(tmp_path):
    clock = Clock()
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine="claude-code", model="requested", label="lead",
                       stage="draft", clock=clock)

    def event(t, ev):
        clock.value = t
        rec.feed(ev)

    usage = {"input_tokens": 10, "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10}
    msg = {"id": "m1", "model": "actual", "usage": usage}
    event(3, {"type": "stream_event", "event": {"type": "message_start", "message": msg}})
    event(4, {"type": "stream_event", "event": {"type": "content_block_delta"}})
    event(5, {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 7}}})
    event(6, {"type": "assistant", "message": {**msg, "usage": {**usage, "output_tokens": 7}}})
    event(7, {"type": "stream_event", "event": {"type": "message_stop"}})
    event(7.5, {"type": "assistant", "message": {**msg, "usage": {**usage, "output_tokens": 7}}})
    event(8, {"type": "user", "message": {"content": [{"type": "tool_result"}]}})
    event(9, {"type": "assistant", "message": msg})  # duplicate after commit
    rec.finish()
    result, = rows(rec)
    assert result["input_tokens"] == 10 and result["output_tokens"] == 7
    assert result["cache_hit_ratio"] == 0.8 and result["event_span_s"] == 4
    assert result["observed_turn_s"] == 7 and result["time_to_first_delta_s"] == 1
    assert result["event_span_scope"] == "response_stream_only"
    assert result["cost_usd"] is None and result["cost_source"] == "unavailable"
    assert rec.finish()["metrics_errors"] == 0


@pytest.mark.parametrize("tool_times", [(4, 5), (6.5, 7.5), (9, 10)])
def test_claude_tool_results_do_not_finalize_stream_before_usage(tmp_path, tool_times):
    clock = Clock()
    exported = []

    class Trace:
        def call_started(self, _data):
            pass

        def call_finished(self, data):
            exported.append(data)

    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine="claude-code", model="requested", label="lead",
                       stage="draft", clock=clock, trace=Trace())
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10}
    msg = {"id": "m1", "model": "actual", "usage": usage}
    events = [
        (2, {"type": "stream_event", "event": {"type": "message_start", "message": msg}}),
        (3, {"type": "assistant", "message": msg}),
        (3.5, {"type": "stream_event", "event": {"type": "content_block_delta"}}),
        (5.5, {"type": "assistant", "message": msg}),
        (6, {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 4889},
                                               "delta": {"stop_reason": "tool_use"}}}),
        (7, {"type": "assistant", "message": msg}),  # stale preliminary usage must not replace final usage
        (8, {"type": "stream_event", "event": {"type": "message_stop"}}),
        (8.5, {"type": "assistant", "message": msg}),  # duplicate after completion must not reopen the call
        (11, {"type": "stream_event", "event": {"type": "message_start",
                                                "message": {**msg, "id": "m2"}}}),
        (12, {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 111}}}),
        (13, {"type": "stream_event", "event": {"type": "message_stop"}}),
        (14, {"type": "result"}),
    ]
    events.extend((t, {"type": "user", "message": {"content": [{"type": "tool_result"}]}}) for t in tool_times)
    for t, ev in sorted(events, key=lambda item: item[0]):
        clock.value = t
        rec.feed(ev)
        if t < 8:
            assert rec.count == 0 and not rec.path.exists() and not exported
    clock.value = 16
    summary = rec.finish()
    first, second = rows(rec)
    assert first["output_tokens"] == 4889 and first["stop_reason"] == "tool_use"
    assert first["total_input_tokens"] == 100 and first["complete"]
    assert first["event_span_s"] == 6 and first["observed_turn_s"] == 8
    assert first["time_to_first_delta_s"] == 1.5
    assert second["output_tokens"] == 111 and second["complete"]
    assert second["turn_boundary"] == "last_tool_result"
    assert second["observed_turn_s"] == 13 - tool_times[-1]
    assert second["tool_and_harness_gap_s"] == max(0, tool_times[-1] - 8)
    assert len(exported) == 2 and [row["output_tokens"] for row in exported] == [4889, 111]
    assert summary["visible_calls"] == 2 and summary["metrics_errors"] == 0
    assert summary["event_span_seconds_sum"] == 8 and summary["settled_to_exit_s"] == 2


def test_claude_without_partial_events_defers_until_next_message_or_result(tmp_path):
    clock = Clock()
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine="claude-code", model="m", label="lead",
                       stage="draft", clock=clock)
    usage = {"input_tokens": 10, "output_tokens": 5,
             "cache_read_input_tokens": 80, "cache_creation_input_tokens": 10}
    msg = {"id": "m1", "usage": usage}
    events = [
        (2, {"type": "assistant", "message": msg}),
        (3, {"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
        (4, {"type": "assistant", "message": {**msg, "usage": {**usage, "output_tokens": 9}}}),
        (5, {"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
        (6, {"type": "assistant", "message": {**msg, "id": "m2", "usage": {**usage, "output_tokens": 7}}}),
        (7, {"type": "result"}),
    ]
    for t, ev in events:
        clock.value = t
        rec.feed(ev)
        if t < 6:
            assert not rec.path.exists()
    rec.finish()
    first, second = rows(rec)
    assert first["output_tokens"] == 9 and first["complete"]
    assert first["event_span_s"] == 2 and first["observed_turn_s"] == 4
    assert second["output_tokens"] == 7 and second["complete"]
    assert second["turn_boundary"] == "last_tool_result" and second["observed_turn_s"] == 1
    assert second["tool_and_harness_gap_s"] == 1


@pytest.mark.parametrize("terminal", ["finish", "result", "next_message"])
def test_claude_stream_without_message_stop_remains_incomplete(tmp_path, terminal):
    clock = Clock()
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine="claude-code", model="m", label="lead",
                       stage="draft", clock=clock)
    events = [
        (1, {"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m1"}}}),
        (2, {"type": "assistant", "message": {"id": "m1", "usage": {"output_tokens": 5}}}),
        (3, {"type": "user", "message": {"content": [{"type": "tool_result"}]}}),
        (4, {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 10}}}),
    ]
    for t, ev in events:
        clock.value = t
        rec.feed(ev)
    clock.value = 5
    if terminal == "result":
        rec.feed({"type": "result"})
    elif terminal == "next_message":
        rec.feed({"type": "stream_event", "event": {"type": "message_start", "message": {"id": "m2"}}})
    summary = rec.finish()
    first = rows(rec)[0]
    assert first["output_tokens"] == 10 and not first["complete"]
    assert first["event_span_s"] == 4 and first["observed_turn_s"] == 5
    assert first["total_input_tokens"] is None and summary["metrics_errors"] == 0


@pytest.mark.parametrize("value,expected", [(True, True), (" true", True), (" YES ", True), ("On", True),
                                            (1, True), (False, False), (" false ", False), (None, False),
                                            ("invalid", False)])
def test_enabled_uses_the_same_boolean_parser_as_tracing(value, expected):
    from rfc_agent.observe import _as_bool

    assert enabled(Cfg(**{"LANGFUSE.PER_CALL": value})) is expected
    assert enabled(Cfg(**{"LANGFUSE.PER_CALL": value})) is (_as_bool(value) is True)
    assert not enabled(None)


@pytest.mark.parametrize("with_calls", [True, False])
def test_reusing_a_stream_path_clears_previous_invocation_metrics(tmp_path, with_calls):
    stream = tmp_path / "test.stream.jsonl"
    first = CallRecorder(stream, engine="pi", model="m", label="lead", stage="draft")
    first.feed({"type": "message_end", "message": {"role": "assistant", "id": "old"}})
    first.finish()
    assert len(rows(first)) == 1 and first.summary_path.exists()
    second = CallRecorder(stream, engine="pi", model="m", label="lead", stage="draft")
    assert not second.path.exists() and not second.summary_path.exists()
    if with_calls:
        second.feed({"type": "message_end", "message": {"role": "assistant", "id": "new"}})
    summary = second.finish()
    assert summary["visible_calls"] == int(with_calls)
    if with_calls:
        row, = rows(second)
        assert row["message_id"] == "new" and row["call_index"] == 1
    else:
        assert not second.path.exists()
    assert json.loads(second.summary_path.read_text()) == summary


@pytest.mark.parametrize("engine", ["axon", "pi"])
def test_pi_replays_do_not_error_or_corrupt_the_next_response(tmp_path, engine):
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine=engine, model="m", label="lead", stage="draft")
    first = {"role": "assistant", "id": "first", "usage": {"output": 7}}
    second = {"role": "assistant", "id": "second", "usage": {"output": 11}}
    rec.feed({"type": "message_end", "message": first})
    rec.feed({"type": "message_end", "message": first})  # replay with no current response
    rec.feed({"type": "message_start", "message": second})
    rec.feed({"type": "message_start", "message": first})
    rec.feed({"type": "message_end", "message": first})  # replay while another response is active
    rec.feed({"type": "message_end", "message": second})
    summary = rec.finish()
    assert summary["visible_calls"] == 2 and summary["metrics_errors"] == 0
    assert [(row["message_id"], row["output_tokens"]) for row in rows(rec)] == [("first", 7), ("second", 11)]


def test_content_errors_and_thinking_are_not_copied_into_metrics(tmp_path):
    secret = "sk-lf-synthetic-do-not-export"
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine="pi", model="m", label="lead", stage="draft")
    rec.feed({"type": "message_start", "message": {"role": "assistant"}})
    rec.feed({"type": "message_update", "assistantMessageEvent": {"type": "thinking_delta", "delta": secret}})
    rec.feed({"type": "message_end", "message": {"role": "assistant", "errorMessage": secret,
              "content": [{"type": "text", "text": secret}], "usage": {"output": 10, "reasoning_tokens": 7}}})
    rec.finish()
    result, = rows(rec)
    assert result["reasoning_tokens"] == 7 and secret not in rec.path.read_text()


def test_unknown_tokens_are_not_zero_and_incomplete_response_is_retained(tmp_path):
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine="pi", model="m", label="lead", stage="draft")
    rec.feed({"type": "message_start", "message": {"role": "assistant"}})
    rec.finish()
    result, = rows(rec)
    assert not result["complete"] and result["input_tokens"] is None
    assert result["cache_hit_ratio"] is None and result["total_input_tokens"] is None
    assert result["time_to_first_delta_s"] is None


def test_retry_compaction_and_malformed_events_do_not_crash(tmp_path):
    rec = CallRecorder(tmp_path / "test.stream.jsonl", engine="pi", model="m", label="lead", stage="draft")
    for ev in (None, [], {"type": "message_end", "message": "bad"}, {"type": "auto_retry_start"},
               {"type": "auto_compaction_start"}):
        rec.feed(ev)
    rec.feed_json("not-json")
    summary = rec.finish()
    assert summary["retry_events"] == 1 and summary["compaction_events"] == 1
    assert summary["metrics_errors"] == 1 and summary["visible_calls"] == 0
    assert summary["hidden_provider_retries_or_compaction_calls"] == "not_observable"


FAKE = r'''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
native = "--mode" in sys.argv
model = sys.argv[sys.argv.index("--model") + 1]
text = json.dumps({"services": [], "summary": "Synthetic summary", "surfaces": []})
def emit(event): print(json.dumps(event), flush=True)
emit({"type": "agent_start"} if native else
     {"type": "system", "subtype": "init", "model": model, "session_id": "synthetic-session"})
for n in range(2):
    usage = {"input_tokens": 10, "output_tokens": 2,
             "cache_read_input_tokens": 100, "cache_creation_input_tokens": 0}
    if native:
        msg = {"id": str(n), "role": "assistant", "model": model, "provider": "test"}
        emit({"type": "message_start", "message": msg})
        emit({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "x"}})
        content = ([{"type": "toolCall", "id": "t1", "name": "read", "arguments": {"path": "dummy"}}]
                   if n == 0 else [{"type": "text", "text": text}])
        emit({"type": "message_end", "message": {**msg, "content": content, "stopReason": "stop",
              "usage": {"input": 10, "output": 2, "cacheRead": 100, "cacheWrite": 0,
                        "cost": {"total": 0.001}}}})
        if n == 0:
            emit({"type": "message_end", "message": {"role": "toolResult", "toolCallId": "t1", "content": []}})
    else:
        msg = {"id": str(n), "model": model, "usage": {**usage, "output_tokens": 1}}
        emit({"type": "stream_event", "event": {"type": "message_start", "message": msg}})
        emit({"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"text": "x"}}})
        content = ([{"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "dummy"}}]
                   if n == 0 else [{"type": "text", "text": text}])
        emit({"type": "assistant", "message": {**msg, "content": content}})
        if n == 0:
            emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "t1"}]}})
        emit({"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 2}}})
        emit({"type": "stream_event", "event": {"type": "message_stop"}})
if native:
    emit({"type": "agent_settled"})
else:
    emit({"type": "result", "subtype": "success", "result": text, "num_turns": 2, "total_cost_usd": 0.002,
          "usage": {"input_tokens": 20, "output_tokens": 4,
                    "cache_read_input_tokens": 200, "cache_creation_input_tokens": 0}})
'''


def fake_cfg(tmp_path, engine):
    fake = tmp_path / "fake-agent"
    fake.write_text(FAKE)
    fake.chmod(0o755)
    return Cfg(**{"AGENT.ENGINE": engine, "AGENT.CLAUDE_BIN": str(fake), "AGENT.AXON": {"BIN": str(fake)},
                  "AGENT.PI": {"BIN": str(fake)}, "LANGFUSE.PER_CALL": True})


@pytest.mark.parametrize("engine", ["claude-code", "axon", "pi"])
def test_all_transports_save_local_records_without_langfuse(tmp_path, monkeypatch, engine):
    from rfc_agent.investigate import agent as ag

    monkeypatch.setenv("RFC_AGENT_ENGINE", engine)
    cfg = fake_cfg(tmp_path, engine)
    spec = ag.AgentSpec("lead", "test-model", None, (), (), use_context=False, timeout_s=10)
    result = ag.run(spec, "synthetic", tmp_path, cfg)
    assert result.ok, result.error
    data = [json.loads(line) for p in (tmp_path / "logs").glob("*.llm-calls.jsonl")
            for line in p.read_text().splitlines()]
    assert len(data) == 2 and sum(r["cache_read_input_tokens"] for r in data) == 200
    assert sum(r["output_tokens"] for r in data) == 4
    assert all(r["complete"] and r["engine"] == engine for r in data)
    assert result.extra["llm_metrics"]["visible_calls"] == 2
    assert result.extra["llm_metrics"]["metrics_errors"] == 0


@pytest.mark.parametrize("engine", ["claude-code", "axon", "pi"])
def test_scope_helpers_save_both_streams_in_run_logs(tmp_path, monkeypatch, engine):
    from rfc_agent.scope import context

    monkeypatch.setenv("RFC_AGENT_ENGINE", engine)
    cfg = fake_cfg(tmp_path, engine)
    logs = tmp_path / "logs"
    logs.mkdir()
    assert context.summarise("# synthetic", cfg, logs)["ok"]
    assert context.discover("# synthetic", cfg, {"url": "http://unused.invalid"}, logs)["ok"]
    files = list(logs.glob("*.llm-calls.jsonl"))
    assert len(files) == 2
    assert all(len(p.read_text().splitlines()) == 2 for p in files)
    assert all(sum(json.loads(line)["output_tokens"] for line in p.read_text().splitlines()) == 4 for p in files)
    assert all(json.loads(p.read_text())["metrics_errors"] == 0 for p in logs.glob("*.llm-metrics.json"))


@pytest.mark.parametrize("engine", ["claude-code", "axon", "pi"])
@pytest.mark.parametrize("per_call", [False, True])
def test_gather_shares_progress_and_logs_both_helpers_regardless_of_metrics(tmp_path, monkeypatch, engine, per_call):
    import threading

    from rfc_agent.investigate import progress
    from rfc_agent.scope import context

    barrier = threading.Barrier(2)
    owners = []

    class SharedProgress(progress.Progress):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            owners.append(self)

        def tick(self, label, kind, data):
            if kind == "start":
                barrier.wait(timeout=10)  # force the helpers to emit progress concurrently
            super().tick(label, kind, data)

        def _flush(self, force=False):
            super()._flush(force=True)  # inspect the final state without the UI's normal throttle

    monkeypatch.setattr(progress, "Progress", SharedProgress)
    monkeypatch.setattr(context, "prd_mentioned", lambda *args: [])
    monkeypatch.setenv("RFC_AGENT_ENGINE", engine)
    cfg = fake_cfg(tmp_path, engine)
    cfg.d["LANGFUSE.PER_CALL"] = per_call
    logs = tmp_path / "logs"
    meta = {}
    context.gather(cfg, {"url": "http://unused.invalid"}, True, "synthetic", tmp_path, [], meta, logs)
    assert not meta["summary_error"] and meta["discovery"]["ok"]
    assert len(owners) == 1
    state = json.loads((logs / "status.json").read_text())
    assert {agent["id"] for agent in state["agents"]} == {"summary", "discovery"}
    sessions = json.loads((logs / "sessions.json").read_text())["sessions"]
    assert set(sessions) == {"scope.summary.run", "scope.discovery.run"}
    assert len(list(logs.glob("scope.*.stream.jsonl"))) == 2
    assert len(list((logs / "agents").glob("scope.*.log"))) == 2
    assert len(list(logs.glob("*.llm-calls.jsonl"))) == (2 if per_call else 0)


def test_discovery_without_log_dir_uses_a_private_directory_and_removes_it(tmp_path, monkeypatch):
    from rfc_agent.scope import context

    cfg = fake_cfg(tmp_path, "claude-code")
    monkeypatch.setenv("RFC_AGENT_ENGINE", "claude-code")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    dirs = []
    run_live = context._run_live

    def capture(cmd, prompt, log_dir, **kwargs):
        dirs.append(log_dir)
        return run_live(cmd, prompt, log_dir, **kwargs)

    monkeypatch.setattr(context, "_run_live", capture)
    for _ in range(2):
        assert context.discover("synthetic", cfg, {"url": "http://unused.invalid"})["ok"]
    assert dirs[0] != dirs[1] and all(path.name.startswith("rfc-scope-discovery-") for path in dirs)
    assert not (tmp_path / "scope.discovery.llm-calls.jsonl").exists()
    assert not any(path.exists() for path in dirs)  # the scratch folder held the token-bearing MCP config
