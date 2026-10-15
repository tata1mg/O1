"""Bonus patch before stopping: a retry budget exhausted on a mechanical-only residue grants ONE
constrained patch pass instead of halting the run (runtime audit 2026-09-03: a run stopped with 2
mechanical failures, cost an 8-minute human resume, and the resumed patch pass fixed them in 18s)."""

import json

import pytest

from rfc_agent.runner import GateFailure, RunContext, Stage, StageResult, run_pipeline

MECHANICAL = "findings.md: bare citation with no path (index.js:74) — cite the full repo-qualified path"
SEMANTIC = "design gap: REQ-3 has no covering capability"


def _stage(runs: list[str], gate_failures: list[list[str]]) -> Stage:
    def _engine(stage: Stage, ctx: RunContext) -> StageResult:
        runs.append("g")
        for rel in stage.outputs:
            p = ctx.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"attempt {len(runs)}")
        return StageResult(final_text="done", cost_usd=0.1, engine_desc="fake")

    return Stage(
        name="g", requires=("inputs.json",), outputs=("findings/findings.md",),
        build_prompt=lambda ctx: "p", engine=_engine,
        gate=lambda ctx: gate_failures.pop(0) if gate_failures else [],
        retry_on_gate_failure=1, revise_on_retry=True,
    )


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "inputs.json").write_text("{}")
    return RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=None)


def _decisions(ctx):
    p = ctx.run_dir / "logs/decisions.jsonl"
    return [json.loads(x)["decision"] for x in p.read_text().splitlines()] if p.exists() else []


def test_mechanical_residue_gets_one_bonus_pass_and_recovers(ctx):
    runs: list[str] = []
    run_pipeline([_stage(runs, [[MECHANICAL], [MECHANICAL]])], ctx)  # attempt3 passes
    assert runs == ["g", "g", "g"], "initial + budgeted retry + the bonus patch pass"
    assert "bonus_patch_retry" in _decisions(ctx)
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["g"]["status"] == "ok" and manifest["g"]["attempts"] == 3


def test_semantic_residue_still_stops_the_run(ctx):
    runs: list[str] = []
    with pytest.raises(GateFailure):
        run_pipeline([_stage(runs, [[SEMANTIC], [SEMANTIC], [SEMANTIC]])], ctx)
    assert runs == ["g", "g"], "no bonus for a residue that needs an unconstrained revision"
    assert "bonus_patch_retry" not in _decisions(ctx)


def test_mixed_residue_disqualifies_the_bonus(ctx):
    runs: list[str] = []
    with pytest.raises(GateFailure):
        run_pipeline([_stage(runs, [[MECHANICAL], [MECHANICAL, SEMANTIC], [MECHANICAL]])], ctx)
    assert runs == ["g", "g"], "one non-mechanical failure disqualifies the whole set"


def test_bonus_fires_at_most_once(ctx):
    runs: list[str] = []
    forever = [[MECHANICAL]] * 10
    with pytest.raises(GateFailure):
        run_pipeline([_stage(runs, forever)], ctx)
    assert runs == ["g", "g", "g"], "initial + retry + ONE bonus — then the stop is honest"
