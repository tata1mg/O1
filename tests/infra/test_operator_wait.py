"""Run self-accounting for the two audit buckets nobody could read off the manifest (runtime audit
2026-09-03): operator-wait (~28 of 109 min, excavated from four log files) and rework (attempts
beyond the first — 33% of machine time)."""

import json

import pytest

from rfc_agent import ask
from rfc_agent.runner import (
    RunContext,
    Stage,
    StageResult,
    _record,
    run_pipeline,
    run_totals,
)


def test_record_wait_and_summary_roundtrip(tmp_path):
    ask.record_wait(tmp_path, "ground:scope_approval", 714.2)
    ask.record_wait(tmp_path, "ground:answers", 466.0)
    ask.record_wait(tmp_path, "ground:answers", 10.0)
    s = ask.operator_wait_summary(tmp_path)
    assert s["asks"] == 3
    assert s["by"] == {"ground:scope_approval": 714.2, "ground:answers": 476.0}
    assert s["total_s"] == 1190.2


def test_summary_is_none_without_asks(tmp_path):
    assert ask.operator_wait_summary(tmp_path) is None


def test_rework_s_counts_attempts_beyond_the_first():
    manifest: dict = {}
    stage = Stage(name="g", requires=(), outputs=(), build_prompt=lambda ctx: "p", gate=lambda ctx: [])
    _record(manifest, stage, "t0", StageResult(duration_s=100.0, engine_desc="fake"), ["fail"])
    assert manifest["g"]["rework_s"] == 0.0, "the first attempt is work, not rework"
    _record(manifest, stage, "t1", StageResult(duration_s=40.0, engine_desc="fake"), ["fail"])
    _record(manifest, stage, "t2", StageResult(duration_s=7.0, engine_desc="fake"), [])
    assert manifest["g"]["rework_s"] == 47.0
    assert manifest["g"]["duration_s_total"] == 147.0


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "inputs.json").write_text("{}")
    return RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=None)


def test_manifest_carries_operator_wait_and_totals_ignore_it(ctx):
    ask.record_wait(ctx.run_dir, "ground:answers", 300.0)

    def _engine(stage, c):
        p = c.path("out/x.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return StageResult(final_text="done", cost_usd=0.1, engine_desc="fake")

    stage = Stage(name="g", requires=("inputs.json",), outputs=("out/x.md",),
                  build_prompt=lambda c: "p", gate=lambda c: [], engine=_engine)
    run_pipeline([stage], ctx)
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["operator_wait"]["total_s"] == 300.0
    assert manifest["operator_wait"]["by"] == {"ground:answers": 300.0}
    cost, secs, tries = run_totals(manifest)
    assert tries == 1, "the run-level summary entry must never count as a stage attempt"
