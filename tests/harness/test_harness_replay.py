"""The replay engine + `harness record`: a recorded run re-enacted with no model in the loop.

Replay is the parity suite's instrument. If it lies — invents an output, silently replays nothing, keeps
the recording's clock — the differential test it powers would pass on fiction, so its failure modes are
tested as carefully as its happy path.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from harness.contracts import Stage
from harness.engines import EngineRequest
from harness.engines.replay import (
    MANIFEST_NAME,
    RECORDING_VERSION,
    RecordingError,
    ReplayEngine,
    record,
)
from harness.events import EventType, events_path, read_events

FIXTURE = Path(__file__).parents[1] / "fixtures" / "stream_full.jsonl"


def _finished_run(tmp_path: Path) -> Path:
    """A run dir shaped like a real one: manifest entries, declared outputs, a canonical trace."""
    run = tmp_path / "run"
    (run / "logs").mkdir(parents=True)
    (run / "findings").mkdir()
    (run / "out").mkdir()
    (run / "findings" / "findings.md").write_text("# findings\nREQ-1 grounded\n")
    (run / "out" / "rfc.md").write_text("# RFC\nbody\n")
    (run / "scratch.tmp").write_text("incidental, not declared")
    from harness.engines.claude_translate import translate_file

    translate_file(FIXTURE, events_path(run, "ground"))
    translate_file(FIXTURE, events_path(run, "generate"))
    (run / "manifest.json").write_text(json.dumps({
        "ground": {"status": "ok", "engine": "claude:claude-sonnet-5", "cost_usd": 1.25,
                   "usage": {"input_tokens": 5000, "output_tokens": 900}, "duration_s": 42.5,
                   "outputs": ["findings/findings.md"], "requires": [], "tool_call_count": 12,
                   "attempts": 1, "gate": {"passed": True, "failures": []}},
        "generate": {"status": "ok", "engine": "claude:claude-sonnet-5", "cost_usd": 2.50,
                     "usage": {"input_tokens": 8000}, "duration_s": 300.0,
                     "outputs": ["out/rfc.md"], "requires": ["findings/findings.md"],
                     "attempts": 1, "gate": {"passed": True, "failures": []}},
        "operator_wait": {"total_s": 12},  # a run-level summary, not a stage — must be ignored
    }))
    return run


# ---- recording -------------------------------------------------------------------------------------

def test_record_builds_a_versioned_recording_from_a_run_dir(tmp_path):
    run = _finished_run(tmp_path)
    rec = record(run, tmp_path / "rec")
    meta = json.loads((rec / MANIFEST_NAME).read_text())
    assert meta["version"] == RECORDING_VERSION
    assert meta["labels"] == ["generate", "ground"]
    assert meta["source_run_dir"] == str(run.resolve())
    assert (rec / "ground" / "outputs" / "findings" / "findings.md").read_text().startswith("# findings")
    assert (rec / "ground" / "events.jsonl").is_file()
    assert json.loads((rec / "ground" / "result.json").read_text())["cost_usd"] == 1.25


def test_record_takes_outputs_from_the_manifest_not_a_directory_sweep(tmp_path):
    """The manifest is the run's own record of what each stage DECLARED; incidental files are not
    part of any stage's contract and must not become part of the recording."""
    run = _finished_run(tmp_path)
    rec = record(run, tmp_path / "rec")
    copied = {str(p.relative_to(rec)) for p in rec.rglob("*") if p.is_file()}
    assert not any("scratch.tmp" in c for c in copied)
    assert "ground/outputs/findings/findings.md" in copied


def test_record_ignores_run_level_manifest_summaries(tmp_path):
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    assert not (rec / "operator_wait").exists()


def test_record_can_select_labels(tmp_path):
    rec = record(_finished_run(tmp_path), tmp_path / "rec", labels=["ground"])
    assert json.loads((rec / MANIFEST_NAME).read_text())["labels"] == ["ground"]
    assert not (rec / "generate").exists()


def test_record_names_the_fix_when_asked_for_an_unrecorded_stage(tmp_path):
    with pytest.raises(RecordingError, match="no stage entry"):
        record(_finished_run(tmp_path), tmp_path / "rec", labels=["nope"])


def test_record_refuses_a_dir_that_is_not_a_run(tmp_path):
    (tmp_path / "empty").mkdir()
    with pytest.raises(RecordingError, match="no manifest.json"):
        record(tmp_path / "empty", tmp_path / "rec")


def test_record_translates_a_legacy_run_with_only_a_raw_stream(tmp_path):
    """Recordings must be makeable from runs that predate the canonical trace."""
    run = tmp_path / "legacy"
    (run / "logs").mkdir(parents=True)
    (run / "findings").mkdir()
    (run / "findings" / "f.md").write_text("x")
    (run / "logs" / "ground.stream.jsonl").write_text(FIXTURE.read_text())
    (run / "manifest.json").write_text(json.dumps(
        {"ground": {"status": "ok", "outputs": ["findings/f.md"], "duration_s": 1.0}}))
    rec = record(run, tmp_path / "rec")
    evs = list(read_events(rec / "ground" / "events.jsonl"))
    assert any(e["t"] == EventType.RESULT for e in evs)


# ---- doctor ----------------------------------------------------------------------------------------

def test_engine_requires_a_recording_and_says_how_to_make_one():
    with pytest.raises(ValueError, match="harness record"):
        ReplayEngine()


def test_doctor_findings_name_the_fix(tmp_path):
    assert "harness record" in ReplayEngine(tmp_path / "nope").doctor()[0]
    plain = tmp_path / "plain"
    plain.mkdir()
    assert "not a recording" in ReplayEngine(plain).doctor()[0]
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / MANIFEST_NAME).write_text(json.dumps({"version": 999}))
    assert "re-record" in ReplayEngine(bad).doctor()[0]


def test_doctor_is_clean_on_a_real_recording(tmp_path):
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    eng = ReplayEngine(rec)
    assert eng.doctor() == []
    assert eng.labels() == ["generate", "ground"]
    assert eng.supports_resume() is False, "a replay has no session; claiming one would be a lie"


def test_engine_is_registered_and_satisfies_the_protocol(tmp_path):
    from harness import engines

    assert "replay" in engines.names()
    eng = engines.get_engine("replay", recording_dir=tmp_path)
    assert isinstance(eng, engines.Engine) and eng.name == "replay"


# ---- replay ----------------------------------------------------------------------------------------

def test_run_restores_outputs_and_reemits_the_trace(tmp_path):
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    req = EngineRequest(prompt="p", cwd=fresh, label="ground",
                        trace_path=events_path(fresh, "ground"), timeout_s=10)
    res = ReplayEngine(rec).run(req)
    assert (fresh / "findings" / "findings.md").read_text().startswith("# findings")
    assert res.trace_path.exists()
    # the SUMMARY is derived from the re-emitted trace (whose bonus turn totals $1.30, last-wins);
    # the recorded manifest accounting is what run_stage reports — see the run_stage test below
    assert res.summary.cost_usd == 1.30 and res.summary.denials == 2
    assert res.duration_s == 42.5, "the RECORDED duration, so a parity manifest matches"
    assert res.engine_desc == "claude:claude-sonnet-5"
    assert res.exit_code == 0


def test_reemitted_trace_has_fresh_timestamps_and_seq(tmp_path):
    """A replayed trace describes a run happening NOW; keeping the recording's clock would make the
    events pre-date the run and break every duration derived from them."""
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    before = time.time()
    trace = ReplayEngine(rec).reemit(rec / "ground", events_path(fresh, "ground"))
    recorded = list(read_events(rec / "ground" / "events.jsonl"))
    replayed = list(read_events(trace))
    assert len(replayed) == len(recorded)
    assert [e["t"] for e in replayed] == [e["t"] for e in recorded], "content is preserved exactly"
    assert [e["seq"] for e in replayed] == list(range(1, len(replayed) + 1))
    assert all(e["ts"] >= before for e in replayed)
    assert replayed[0]["ts"] != recorded[0]["ts"]


def test_reemit_is_idempotent_not_append(tmp_path):
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    eng = ReplayEngine(rec)
    n = len(list(read_events(eng.reemit(rec / "ground", events_path(fresh, "ground")))))
    assert len(list(read_events(eng.reemit(rec / "ground", events_path(fresh, "ground"))))) == n


def test_unrecorded_label_fails_loudly_and_lists_what_exists(tmp_path):
    """A replay that silently produced nothing would read downstream as a stage that legitimately
    wrote no files."""
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    req = EngineRequest(prompt="p", cwd=fresh, label="review",
                        trace_path=events_path(fresh, "review"), timeout_s=10)
    with pytest.raises(RecordingError) as exc:
        ReplayEngine(rec).run(req)
    assert "review" in str(exc.value) and "ground" in str(exc.value)


def test_run_stage_returns_the_recorded_accounting(tmp_path):
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    stage = Stage(name="ground", requires=(), outputs=("findings/findings.md",),
                  build_prompt=lambda c: "", gate=lambda c: [])
    ctx = type("C", (), {"run_dir": fresh})()
    res = ReplayEngine(rec).run_stage(stage, ctx)
    assert res.cost_usd == 1.25 and res.duration_s == 42.5
    assert res.usage == {"input_tokens": 5000, "output_tokens": 900}
    assert res.engine_desc == "claude:claude-sonnet-5"
    assert res.denials == 2 and res.compactions == 1
    assert len(res.tool_calls) == 12
    assert (fresh / "findings" / "findings.md").is_file()


def test_run_stage_reports_a_recording_that_cannot_satisfy_the_contract(tmp_path):
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    stage = Stage(name="ground", requires=(), outputs=("findings/findings.md", "findings/extra.md"),
                  build_prompt=lambda c: "", gate=lambda c: [])
    ctx = type("C", (), {"run_dir": fresh})()
    with pytest.raises(RecordingError, match="findings/extra.md"):
        ReplayEngine(rec).run_stage(stage, ctx)


def test_replay_is_deterministic_across_runs(tmp_path):
    """Same recording, two replays, identical outputs — the property the differential test rests on."""
    rec = record(_finished_run(tmp_path), tmp_path / "rec")
    digests = []
    for i in (1, 2):
        fresh = tmp_path / f"fresh{i}"
        fresh.mkdir()
        ReplayEngine(rec).run(EngineRequest(prompt="p", cwd=fresh, label="ground",
                                            trace_path=events_path(fresh, "ground"), timeout_s=10))
        digests.append((fresh / "findings" / "findings.md").read_bytes())
    assert digests[0] == digests[1]
