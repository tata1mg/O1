"""The canonical trace is a POSTCONDITION OF THE INVOCATION, not of the engine adapter.

WHY this file exists: `logs/<label>.events.jsonl` was originally written by the claude-code adapter's
run()/run_stage(). Every RFC stage owns its fan-out engine and reaches `run_claude` directly, so a live
`rfc-agent --stages ingest` smoke (2026-09-08) passed its gates and produced ZERO trace files — the
ledger, the crash accounting, the activity feed and observability all read that file, and all four were
silently reading nothing. Writing it inside `run_claude` covers all fifteen call sites at once; these
tests are what keeps it that way.

The integration test drives the REAL `run_claude` against a stub `claude` binary (the pattern in
tests/test_steer.py), because the trace is derived from the raw stream a real subprocess wrote.
"""

from __future__ import annotations

import json
import os
import stat
import sys

import pytest

from harness.events import EventType, events_path, events_paths, read_events, summarize
from rfc_agent.contracts import RunContext, Stage

# Two tool calls and a terminal result, so the trace has something to classify as well as account for.
_STUB = """#!/usr/bin/env python3
import json, os, sys
sys.stdin.readline()
print(json.dumps({"type":"system","subtype":"init","model":"stub-model","session_id":"sess-42",
                  "cwd":"/runs/x","tools":["Read","Bash"]}), flush=True)
print(json.dumps({"type":"assistant","message":{"id":"m1","content":[
    {"type":"text","text":"reading"},
    {"type":"tool_use","id":"t1","name":"Read","input":{"file_path":"raw/prd.md"}}]}}), flush=True)
print(json.dumps({"type":"user","message":{"content":[
    {"type":"tool_result","tool_use_id":"t1","content":"# PRD"}]}}), flush=True)
print(json.dumps({"type":"result","is_error":False,"result":"stub done","total_cost_usd":0.125,
                  "num_turns":3,"session_id":"sess-42",
                  "usage":{"input_tokens":11,"output_tokens":22}}), flush=True)
for ln in sys.stdin:
    pass
"""


def _stub_ctx(tmp_path, body: str = _STUB):
    stub = tmp_path / "fake_claude"
    stub.write_text(body.replace("#!/usr/bin/env python3", f"#!{sys.executable}"))
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)

    class Cfg:
        def get(self, k, default=None):
            return {"AGENT.CLAUDE_BIN": str(stub)}.get(k, default)

        def materialize_mcp(self, names, dest):
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text("{}")
            return dest

    return RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=Cfg())


def _stage(name: str = "stub") -> Stage:
    return Stage(name=name, requires=(), outputs=(), build_prompt=lambda c: "p", gate=lambda c: [])


@pytest.fixture
def ran(tmp_path):
    """One real run_claude invocation against the stub, with no engine adapter in sight."""
    from rfc_agent.engine import run_claude

    os.environ["STUB_STDIN_LOG"] = str(tmp_path / "unused.log")
    ctx = _stub_ctx(tmp_path)
    res = run_claude(_stage(), ctx, "KICKOFF", tmp_path / "logs" / "stub.stream.jsonl",
                     timeout=25, live=False)
    return ctx, res


def test_run_claude_writes_the_canonical_trace(ran):
    """The regression: no adapter involved, and the trace must exist anyway."""
    ctx, _res = ran
    trace = events_path(ctx.run_dir, "stub")
    assert trace.is_file(), "run_claude must write logs/<label>.events.jsonl for every invocation"
    kinds = [e["t"] for e in read_events(trace)]
    assert EventType.INIT in kinds and EventType.RESULT in kinds
    assert EventType.TOOL_START in kinds and EventType.TOOL_END in kinds


def test_the_trace_agrees_with_the_parsed_stream(ran):
    """Two readings of one invocation must not disagree — the whole point of the translation."""
    ctx, res = ran
    s = summarize(events_path(ctx.run_dir, "stub"))
    assert s.session_id == res.session_id == "sess-42"
    assert s.cost_usd == res.cost_usd == 0.125
    assert s.num_turns == res.num_turns == 3
    assert s.model == res.model == "stub-model"
    assert s.final_text == res.final_text == "stub done"
    assert s.got_result is True and s.is_error is False
    assert "raw/prd.md" in s.reads


def test_the_raw_stream_is_still_kept_beside_it(ran):
    """The claude-code adapter still writes the raw stream the ledger reads."""
    ctx, _ = ran
    assert (ctx.run_dir / "logs" / "stub.stream.jsonl").is_file()


def test_the_label_names_the_trace_not_the_stream_filename(tmp_path):
    """Raw stream names are inconsistent across the call sites (`extract.stream.requote.jsonl`,
    `ingest.figma.b1.stream.jsonl`); `label` is the canonical key, and events_paths must collect the
    stage's whole corpus through it."""
    from rfc_agent.engine import run_claude

    ctx = _stub_ctx(tmp_path)
    run_claude(_stage("extract"), ctx, "p", tmp_path / "logs" / "extract.stream.requote.jsonl",
               timeout=25, live=False, label="extract.requote")
    assert events_path(ctx.run_dir, "extract.requote").is_file()
    assert [p.name for p in events_paths(ctx.run_dir, "extract")] == ["extract.requote.events.jsonl"]


def test_a_translation_failure_never_fails_the_stage(tmp_path, monkeypatch, capsys):
    """A stage that did its work must not die because its accounting could not be written."""
    from rfc_agent import engine as rfc_engine

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr("harness.engines.claude_translate.translate_file", boom)
    ctx = _stub_ctx(tmp_path)
    res = rfc_engine.run_claude(_stage(), ctx, "p", tmp_path / "logs" / "stub.stream.jsonl",
                                timeout=25, live=False)
    assert res.cost_usd == 0.125, "the invocation itself still succeeded"
    assert "canonical trace not written" in capsys.readouterr().err


def test_write_canonical_trace_returns_none_on_failure(tmp_path, monkeypatch):
    from rfc_agent.engine import write_canonical_trace

    monkeypatch.setattr("harness.engines.claude_translate.translate_file",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("nope")))
    assert write_canonical_trace(tmp_path, "x", tmp_path / "s.jsonl") is None


# ---- rotation + hygiene: the trace follows its stream ----------------------------------------------

def test_a_revision_rotates_the_trace_with_its_stream(tmp_path):
    """Revision mode keeps the prior attempt's stream as witness evidence; its trace must survive too,
    or the witness corpus would cover only the last attempt."""
    from rfc_agent.contracts import guidance_path
    from rfc_agent.runner import GateFailure, run_pipeline

    (tmp_path / "inputs.json").write_text("{}")
    ctx = _stub_ctx(tmp_path)
    calls: list[int] = []

    def engine(stage, c):
        """A stage-owned engine that reaches run_claude directly — the shape every RFC stage has."""
        from rfc_agent.contracts import StageResult
        from rfc_agent.engine import run_claude

        calls.append(1)
        stream = run_claude(stage, c, "p", tmp_path / "logs" / "rev.stream.jsonl", 25,
                            live=False, label="rev")
        c.path("out.md").write_text("x")
        return StageResult(engine_desc="stub", cost_usd=stream.cost_usd)

    stage = Stage(name="rev", requires=("inputs.json",), outputs=("out.md",),
                  build_prompt=lambda c: "p", gate=lambda c: ["always red"], engine=engine,
                  retry_on_gate_failure=1, revise_on_retry=True)
    with pytest.raises(GateFailure):
        run_pipeline([stage], ctx)
    assert len(calls) == 2 and guidance_path(tmp_path, "rev").exists()
    names = sorted(p.name for p in events_paths(tmp_path, "rev"))
    assert names == ["rev.attempt1.events.jsonl", "rev.events.jsonl"], (
        f"attempt 1's trace must be preserved beside the current one, got {names}")


def test_a_fresh_attempt_wipes_stale_traces(tmp_path):
    """A from-scratch regeneration must not leave traces witnessing reads the new run never made —
    the same invariant the runner already enforces for the raw streams."""
    from rfc_agent.contracts import StageResult
    from rfc_agent.runner import run_pipeline

    (tmp_path / "inputs.json").write_text("{}")
    ctx = _stub_ctx(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    stale = events_path(tmp_path, "fresh.attempt7")
    stale.write_text('{"t":"result","seq":1,"ts":1,"v":1,"ok":true}\n')
    events_path(tmp_path, "fresh").write_text('{"t":"result","seq":1,"ts":1,"v":1,"ok":true}\n')

    def engine(stage, c):
        c.path("out.md").write_text("x")
        return StageResult(engine_desc="stub")

    stage = Stage(name="fresh", requires=("inputs.json",), outputs=("out.md",),
                  build_prompt=lambda c: "p", gate=lambda c: [], engine=engine)
    run_pipeline([stage], ctx)
    assert not stale.exists() and not events_path(tmp_path, "fresh").exists(), \
        "stale traces must be wiped alongside the streams on a fresh attempt"


# ---- `harness record` prefers the canonical trace --------------------------------------------------

def test_record_prefers_the_canonical_trace_over_the_raw_stream(tmp_path):
    """When both exist, the recording carries the CANONICAL trace — it is the engine boundary, and a
    re-translation at record time could differ from what the run actually emitted."""
    from harness.engines.replay import record

    run = tmp_path / "run"
    (run / "logs").mkdir(parents=True)
    (run / "out").mkdir()
    (run / "out" / "a.md").write_text("x")
    events_path(run, "s").write_text(
        '{"t":"init","seq":1,"ts":1,"v":1,"session_id":"canonical"}\n'
        '{"t":"result","seq":2,"ts":2,"v":1,"ok":true,"cost_usd":9.0}\n')
    (run / "logs" / "s.stream.jsonl").write_text(json.dumps(
        {"type": "system", "subtype": "init", "session_id": "from-raw-stream"}) + "\n")
    (run / "manifest.json").write_text(json.dumps(
        {"s": {"status": "ok", "outputs": ["out/a.md"], "duration_s": 1.0}}))

    rec = record(run, tmp_path / "rec")
    s = summarize(rec / "s" / "events.jsonl")
    assert s.session_id == "canonical" and s.cost_usd == 9.0


def test_record_still_falls_back_to_the_raw_stream(tmp_path):
    """Recordings must remain makeable from runs that predate the canonical trace."""
    from harness.engines.replay import record

    run = tmp_path / "run"
    (run / "logs").mkdir(parents=True)
    (run / "out").mkdir()
    (run / "out" / "a.md").write_text("x")
    (run / "logs" / "s.stream.jsonl").write_text(json.dumps(
        {"type": "result", "is_error": False, "result": "old", "total_cost_usd": 1.0}) + "\n")
    (run / "manifest.json").write_text(json.dumps(
        {"s": {"status": "ok", "outputs": ["out/a.md"], "duration_s": 1.0}}))

    rec = record(run, tmp_path / "rec")
    assert summarize(rec / "s" / "events.jsonl").final_text == "old"
