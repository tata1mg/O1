"""The claude-code adapter and its stream-json -> canonical-trace translator.

The fixture `tests/fixtures/stream_full.jsonl` is a synthetic stream-json transcript built from the event
shapes `rfc_agent.streams` parses and `tests/test_streams.py` pins: init, replayed user turns, thinking,
batched tool_use blocks, tool_results, both denial signals, an MCP call, a compact boundary, a subagent,
an ask, unknown event types, a torn line, and a post-result bonus turn. It is the translator's contract.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.engines.claude_code import ClaudeCodeEngine
from harness.engines.claude_translate import (
    OUTPUT_CLIP,
    tool_kind,
    translate_events,
    translate_file,
)
from harness.events import EventType, ToolKind, read_events, summarize

FIXTURE = Path(__file__).parents[1] / "fixtures" / "stream_full.jsonl"


@pytest.fixture(scope="module")
def events() -> list[dict]:
    return [json.loads(e.to_json()) for e in translate_events(FIXTURE.read_text())]


def _of(events: list[dict], t: str) -> list[dict]:
    return [e for e in events if e["t"] == t]


# ---- tool-kind mapping (the ledger keys on this, not on engine tool names) --------------------------

@pytest.mark.parametrize("name,kind", [
    ("Read", ToolKind.READ), ("NotebookRead", ToolKind.READ),
    ("Grep", ToolKind.SEARCH), ("Glob", ToolKind.SEARCH),
    ("LS", ToolKind.LIST),
    ("Write", ToolKind.WRITE),
    ("Edit", ToolKind.EDIT), ("MultiEdit", ToolKind.EDIT), ("NotebookEdit", ToolKind.EDIT),
    ("Bash", ToolKind.SHELL),
    ("WebFetch", ToolKind.FETCH), ("WebSearch", ToolKind.FETCH),
    ("mcp__figma__get_screenshot", ToolKind.MCP), ("mcp__atlassian__getJiraIssue", ToolKind.MCP),
    ("Agent", ToolKind.SUBAGENT), ("Task", ToolKind.SUBAGENT),
    ("AskUserQuestion", ToolKind.ASK),
])
def test_tool_kind_mapping(name, kind):
    assert tool_kind(name) == kind


def test_unknown_tool_is_other_never_a_guess():
    assert tool_kind("SomeFutureTool") == ToolKind.OTHER
    assert tool_kind("") == ToolKind.OTHER


# ---- the translation ------------------------------------------------------------------------------

def test_init_carries_model_session_and_bound_tools(events):
    init = _of(events, EventType.INIT)
    assert len(init) == 1
    assert init[0]["model"] == "claude-sonnet-5"
    assert init[0]["session_id"] == "sess-abc123"
    assert "mcp__figma__get_screenshot" in init[0]["input"]["tools"]
    assert init[0]["input"]["cwd"] == "/runs/demo"
    assert init[0]["engine"] == "claude-code"


def test_seq_is_monotonic_and_schema_version_present(tmp_path):
    trace = translate_file(FIXTURE, tmp_path / "t.events.jsonl")
    seqs = [e["seq"] for e in read_events(trace)]
    assert seqs == list(range(1, len(seqs) + 1))
    assert all(e["v"] == 1 for e in read_events(trace))


def test_every_tool_use_becomes_one_tool_start_with_a_call_id(events):
    starts = _of(events, EventType.TOOL_START)
    assert [s["tool"] for s in starts] == [
        "Read", "Grep", "Bash", "Write", "MultiEdit", "mcp__figma__get_screenshot",
        "Task", "AskUserQuestion", "Glob", "LS", "WebFetch", "NotebookEdit",
    ]
    assert all(s.get("call_id") for s in starts)


def test_partial_message_deltas_are_not_translated(events):
    """--include-partial-messages exists for the live feed; translating deltas would double every call."""
    assert not [e for e in events if e.get("reason", "").startswith("unhandled stream event 'stream_event'")]
    assert len([s for s in _of(events, EventType.TOOL_START) if s["tool"] == "Bash"]) == 1


def test_mcp_calls_carry_server_and_tool(events):
    call = next(s for s in _of(events, EventType.TOOL_START) if s["kind"] == ToolKind.MCP)
    assert call["input"]["server"] == "figma" and call["input"]["tool"] == "get_screenshot"
    assert call["input"]["nodeId"] == "1:23", "the native input must survive alongside the split parts"


def test_tool_end_pairs_by_call_id_and_records_paths(events):
    ends = {e["call_id"]: e for e in _of(events, EventType.TOOL_END)}
    assert ends["tu_1"]["tool"] == "Read" and ends["tu_1"]["ok"] is True
    assert ends["tu_1"]["paths"] == ["raw/prd.md"]
    assert ends["tu_5"]["paths"] == ["findings/findings.md"], "MultiEdit repeats one file per edit"


def test_both_denial_signals_become_denials(events):
    """The CLI's first-class permission_denied event AND a guard-shaped errored tool_result."""
    denials = _of(events, EventType.DENIAL)
    assert [d["call_id"] for d in denials] == ["tu_3", "tu_4"]
    assert "write-guard" in denials[0]["reason"]
    assert denials[1]["tool"] == "Write", "a denial must name the tool it refused"
    # a denied call is NOT also reported as a completed tool_end
    assert "tu_4" not in {e["call_id"] for e in _of(events, EventType.TOOL_END)}


def test_compaction_and_replayed_user_turns(events):
    assert len(_of(events, EventType.COMPACTION)) == 1
    users = [u["text"] for u in _of(events, EventType.USER)]
    assert users == ["kickoff: ground the RFC", "steering: keep it short"]


def test_thinking_and_text_are_separate_types(events):
    assert [t["text"] for t in _of(events, EventType.THINKING)] == ["I should read the requirements first."]
    assert "Reading the inputs." in [t["text"] for t in _of(events, EventType.TEXT)]


def test_result_carries_cost_usage_and_session(events):
    results = _of(events, EventType.RESULT)
    assert len(results) == 2, "a post-result bonus turn is evidence, not something to drop"
    first = results[0]
    assert first["ok"] is True and first["text"] == "findings written"
    assert first["cost_usd"] == 1.25 and first["num_turns"] == 12
    assert first["duration_ms"] == 23000 and first["session_id"] == "sess-abc123"
    assert first["usage"]["output_tokens"] == 900
    assert results[1]["extra"] == {"extra_result": True}


def test_errored_result_keeps_reason_and_blanks_text():
    evs = translate_events(
        '{"type":"result","is_error":true,"subtype":"error_during_execution",'
        '"terminal_reason":"api_error","total_cost_usd":1.99}'
    )
    r = json.loads(evs[0].to_json())
    assert r["ok"] is False and r["error"] == "api_error" and r.get("text", "") == ""
    assert r["cost_usd"] == 1.99, "a cut-off run still spent money and must stay observable"


def test_unknown_events_are_logged_not_dropped(events):
    logs = _of(events, EventType.LOG)
    assert any("totally_unknown_event" in x["reason"] for x in logs)
    assert any("some_future_subtype" in json.dumps(x) for x in logs)


def test_torn_and_garbage_lines_are_skipped_like_parse_stream():
    evs = translate_events('not json\n\n{"type":"result","is_error":false,"result":"ok"}\n{"type":"resu')
    assert [e.t for e in evs] == [EventType.RESULT]


def test_output_is_clipped_but_shape_survives():
    big = "x" * (OUTPUT_CLIP * 3)
    evs = translate_events(json.dumps({
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": "z", "content": big}]},
    }))
    assert len(evs[0].output) == OUTPUT_CLIP


def test_string_message_body_survives():
    """A string message body is legal in the stream (pinned by test_streams) and must not crash."""
    evs = translate_events('{"type":"assistant","message":{"content":"just prose"}}')
    assert [e.t for e in evs] == [EventType.TEXT] and evs[0].text == "just prose"


# ---- the trace file + summary ---------------------------------------------------------------------

def test_translate_file_writes_a_canonical_trace_and_summarizes(tmp_path):
    trace = translate_file(FIXTURE, tmp_path / "logs" / "ground.events.jsonl")
    assert trace.exists()
    s = summarize(trace)
    assert s.model == "claude-sonnet-5" and s.session_id == "sess-abc123"
    # LAST-result-wins for cost/usage/turns, matching parse_stream: the fixture's post-result bonus
    # turn spent $1.30 in total, and a bonus turn's spend must not disappear from the accounting.
    assert s.cost_usd == 1.30 and s.num_turns == 13
    assert s.extra_results == 1
    assert s.final_text == "findings written" and s.got_result is True and s.is_error is False
    assert s.denials == 2 and s.compactions == 1
    assert "raw/prd.md" in s.reads
    assert "findings/findings.md" in s.writes
    assert {"pattern": "checkout", "path": "raw"} in s.searches
    assert len(s.tool_calls) == 12


def test_translate_file_is_idempotent_not_append(tmp_path):
    """A re-translated attempt must replace its trace, never append to the previous one."""
    dest = tmp_path / "t.events.jsonl"
    first = len(list(read_events(translate_file(FIXTURE, dest))))
    assert len(list(read_events(translate_file(FIXTURE, dest)))) == first


def test_missing_stream_yields_an_empty_trace_not_an_exception(tmp_path):
    """The ledger and witness gates degrade to a loud WARN on an absent trace; a crashed child must
    not also crash its own accounting."""
    trace = translate_file(tmp_path / "nope.jsonl", tmp_path / "out.events.jsonl")
    assert trace.exists() and list(read_events(trace)) == []


# ---- the adapter ----------------------------------------------------------------------------------

def test_engine_is_registered_under_its_flag_name():
    from harness import engines

    assert "claude-code" in engines.names()
    eng = engines.get_engine("claude-code")
    assert isinstance(eng, ClaudeCodeEngine) and eng.name == "claude-code"
    assert eng.supports_resume() is True


def test_engine_satisfies_the_protocol():
    from harness.engines import Engine

    assert isinstance(ClaudeCodeEngine(), Engine)


def test_doctor_reports_a_missing_binary_with_the_fix():
    problems = ClaudeCodeEngine(binary="definitely-not-a-real-binary-xyz").doctor()
    assert len(problems) == 1
    assert "not found on PATH" in problems[0] and "AGENT.CLAUDE_BIN" in problems[0], \
        "a doctor finding must name the fix, not just the symptom"


def test_doctor_accepts_a_working_binary(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho 2.1.263 Claude Code\n")
    fake.chmod(0o755)
    eng = ClaudeCodeEngine(binary=str(fake))
    assert eng.doctor() == []
    assert "2.1.263" in eng.version()


def test_doctor_reports_a_failing_binary(tmp_path):
    fake = tmp_path / "claude"
    fake.write_text("#!/bin/sh\necho boom >&2\nexit 7\n")
    fake.chmod(0o755)
    problems = ClaudeCodeEngine(binary=str(fake)).doctor()
    assert len(problems) == 1 and "exited 7" in problems[0] and "boom" in problems[0]


def test_run_without_native_ctx_says_exactly_what_is_missing(tmp_path):
    from harness.engines import EngineRequest

    req = EngineRequest(prompt="p", cwd=tmp_path, label="x", trace_path=tmp_path / "t.jsonl", timeout_s=1)
    with pytest.raises(ValueError, match="native_ctx"):
        ClaudeCodeEngine().run(req)


def test_run_maps_the_request_onto_run_claude(tmp_path, monkeypatch):
    """The adapter's whole job: EngineRequest -> run_claude args, StreamResult -> trace + EngineResult."""
    from harness.engines import Containment, EngineRequest
    from rfc_agent import engine as rfc_engine
    from rfc_agent.streams import StreamResult

    seen: dict = {}

    def fake_run_claude(stage, ctx, prompt, log_path, timeout, **kw):
        seen.update(stage=stage, ctx=ctx, prompt=prompt, log_path=log_path, timeout=timeout, **kw)
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(FIXTURE.read_text())
        res = StreamResult(final_text="done", model="claude-opus-5", prompt_sent=prompt + "+sandbox")
        return res

    monkeypatch.setattr(rfc_engine, "run_claude", fake_run_claude)

    class _Cfg:
        def get(self, k, d=None):
            return d

    stage = object()
    ctx = type("C", (), {"run_dir": tmp_path, "inputs": {}, "config": _Cfg()})()
    req = EngineRequest(
        prompt="ground it", cwd=tmp_path, label="ground.b2",
        trace_path=tmp_path / "logs" / "ground.b2.events.jsonl", timeout_s=1234,
        model="claude-sonnet-5", effort="high", resume_session="sess-9", live=False,
        env={"K": "V"}, steer_epoch=99.0, containment=Containment(),
        native_ctx={"stage": stage, "ctx": ctx},
    )
    result = ClaudeCodeEngine().run(req)

    assert seen["prompt"] == "ground it" and seen["timeout"] == 1234
    assert seen["label"] == "ground.b2" and seen["model"] == "claude-sonnet-5"
    assert seen["effort"] == "high" and seen["resume"] == "sess-9"
    assert seen["live"] is False and seen["steer_epoch"] == 99.0
    assert seen["extra_env"] == {"K": "V"}
    # the raw stream lands exactly where the ledger already looks for it
    assert seen["log_path"] == tmp_path / "logs" / "ground.b2.stream.jsonl"

    assert result.native.final_text == "done"
    assert result.engine_desc == "claude-code:claude-opus-5"
    assert result.prompt_sent == "ground it+sandbox"
    assert result.trace_path == req.trace_path and result.trace_path.exists()
    assert result.summary.cost_usd == 1.30 and result.summary.denials == 2
    assert result.raw_trace_path.exists(), "the raw stream is kept exactly where it is today"


def test_run_stage_delegates_to_claude_engine_and_translates_every_attempt(tmp_path, monkeypatch):
    """run_stage keeps manifests byte-identical by calling the SAME claude_engine as before, then
    translates every attempt's stream — rotations included, because revision mode keeps prior attempts
    as witness evidence."""
    from harness.contracts import rotated_stream_path, stream_path
    from rfc_agent import engine as rfc_engine
    from rfc_agent.contracts import StageResult

    stage = type("S", (), {"name": "generate"})()
    ctx = type("C", (), {"run_dir": tmp_path})()

    def fake_claude_engine(s, c):
        stream_path(tmp_path, "generate").parent.mkdir(parents=True, exist_ok=True)
        stream_path(tmp_path, "generate").write_text(FIXTURE.read_text())
        rotated_stream_path(tmp_path, "generate", 1).write_text(FIXTURE.read_text())
        return StageResult(final_text="ok", engine_desc="claude:claude-sonnet-5")

    monkeypatch.setattr(rfc_engine, "claude_engine", fake_claude_engine)
    res = ClaudeCodeEngine().run_stage(stage, ctx)
    assert isinstance(res, StageResult) and res.engine_desc == "claude:claude-sonnet-5", \
        "the legacy StageResult must pass through untouched — that is what keeps the manifest golden"
    logs = tmp_path / "logs"
    assert (logs / "generate.events.jsonl").exists()
    assert (logs / "generate.attempt1.events.jsonl").exists()


# ---- hook toggling (Containment.hooks_enabled / disabled_hooks) -------------------------------------

def test_hook_settings_default_materialises_every_hook(tmp_path):
    from rfc_agent.engine import _write_guard_settings

    hooks = json.loads(_write_guard_settings(tmp_path, tmp_path / "s.json").read_text())["hooks"]
    cmds = json.dumps(hooks)
    for name in ("guard_writes.py", "approve_readonly.py", "byte_budget.py"):
        assert name in cmds
    assert len(hooks["PostToolUse"]) == 1, "the feedback hooks share one entry, as shipped"


def test_hooks_master_switch_and_per_hook_disable(tmp_path):
    from rfc_agent.engine import _write_guard_settings

    off = json.loads(_write_guard_settings(tmp_path, tmp_path / "a.json",
                                           hooks_enabled=False).read_text())
    assert off["hooks"] == {}
    assert off["permissions"]["allow"], "disabling hooks must not disable the read-only allowlist"

    part = json.loads(_write_guard_settings(tmp_path, tmp_path / "b.json",
                                            disabled_hooks=("approve_readonly", "doc_lints")).read_text())
    cmds = json.dumps(part["hooks"])
    assert "guard_writes.py" in cmds and "byte_budget.py" in cmds
    assert "approve_readonly.py" not in cmds and "doc_lints.py" not in cmds


def test_init_is_not_stamped_with_the_translation_time(tmp_path):
    """Only `assistant`/`user` lines carry a timestamp (verified on a live CLI 2.1.263 stream, 156
    lines). Stamping the rest with "now" put `system/init` AFTER the terminal result, so the session
    appeared to start after it finished. Untimestamped events carry the nearest real stamp instead."""
    import time as _time

    translated_at = _time.time()
    trace = translate_file(FIXTURE, tmp_path / "t.events.jsonl")
    evs = list(read_events(trace))
    init = next(e for e in evs if e["t"] == EventType.INIT)
    result = [e for e in evs if e["t"] == EventType.RESULT][-1]
    assert init["ts"] <= result["ts"], "init must not sort after the run it starts"
    assert init["ts"] < translated_at, "init must carry the run's clock, not the translator's"
    assert init["ts"] == min(e["ts"] for e in evs), "init is the earliest event in the session"


def test_seq_not_ts_is_the_ordering_authority(tmp_path):
    """`ts` is the ENGINE's clock and is deliberately not clamped — the CLI's own stamps can arrive a
    few ms out of order (observed live: a replayed user turn stamped 3ms before the tool_result emitted
    ahead of it). Clamping a real timestamp to enforce an order `seq` already carries would be a lie."""
    trace = translate_file(FIXTURE, tmp_path / "t.events.jsonl")
    seqs = [e["seq"] for e in read_events(trace)]
    assert seqs == list(range(1, len(seqs) + 1)), "seq is dense, monotone and per-file"


def test_a_stream_with_no_timestamps_at_all_still_translates(tmp_path):
    """The wall-clock fallback: every event gets the same stamp, which is honest and still monotone."""
    import time as _time

    before = _time.time()
    evs = translate_events(
        '{"type":"system","subtype":"init","session_id":"s"}\n'
        '{"type":"result","is_error":false,"result":"ok"}'
    )
    assert [e.t for e in evs] == [EventType.INIT, EventType.RESULT]
    assert all(e.ts >= before for e in evs)
    assert evs[0].ts == evs[1].ts, "one wall-clock stamp for the whole stream — honest, not invented"


def test_summarize_agrees_with_parse_stream_on_the_whole_fixture(tmp_path):
    """The strongest pin available: the canonical summary and the reference stream parser must report
    the same accounting for the same run. The fixture deliberately contains both denial signals for
    one call and a post-result bonus turn — the two places they used to diverge."""
    from rfc_agent.streams import parse_stream

    ref = parse_stream(FIXTURE.read_text())
    got = summarize(translate_file(FIXTURE, tmp_path / "t.events.jsonl"))
    assert got.cost_usd == ref.cost_usd
    assert got.usage == ref.usage
    assert got.num_turns == ref.num_turns
    assert got.denials == ref.denials
    assert got.compactions == ref.compactions
    assert got.final_text == ref.final_text
    assert got.is_error is ref.is_error
    assert got.session_id == ref.session_id
    assert got.model == ref.model
    assert got.extra_results == ref.extra_results
    assert len(got.tool_calls) == len(ref.tool_calls)
