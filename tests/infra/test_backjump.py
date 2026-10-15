"""Cross-stage feedback: REVIEW-style blockers jump back to the producing stage, bounded."""

import json

import pytest

from rfc_agent.runner import GateFailure, RunContext, Stage, StageResult, run_pipeline


def _counting_engine(runs: list[str], name: str):
    def _engine(stage: Stage, ctx: RunContext) -> StageResult:
        runs.append(name)
        for rel in stage.outputs:
            p = ctx.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"content of {rel}, attempt {runs.count(name)}")
        return StageResult(final_text="done", cost_usd=0.1, engine_desc="fake")

    return _engine


def _pipeline(runs: list[str], rev_failures: list[list[str]]):
    """rev's gate pops the next failure list per visit; empty list = pass."""

    def rev_gate(ctx):
        return rev_failures.pop(0) if rev_failures else []

    gen = Stage(
        name="gen", requires=("inputs.json",), outputs=("out/rfc.md",),
        build_prompt=lambda ctx: "p", gate=lambda ctx: [], engine=_counting_engine(runs, "gen"),
    )
    rev = Stage(
        name="rev", requires=("out/rfc.md",), outputs=("review/review.md",),
        build_prompt=lambda ctx: "p", gate=rev_gate, engine=_counting_engine(runs, "rev"),
        retry_on_gate_failure=1, retry_stage="gen",
    )
    return [gen, rev]


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "inputs.json").write_text("{}")
    return RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=None)


def test_blocker_jumps_back_and_recovers(ctx):
    runs: list[str] = []
    run_pipeline(_pipeline(runs, [["review BLOCKER: REQ-3 not covered"]]), ctx)
    assert runs == ["gen", "rev", "gen", "rev"]  # fail -> back-jump -> regenerate -> re-review -> pass
    guidance = (ctx.run_dir / "guidance/gen.md").read_text()
    assert "REQ-3 not covered" in guidance and "'rev' stage" in guidance
    # cross-stage guidance points the reviser at the full findings files (single-line bullets
    # strand multi-line evidence otherwise)
    assert "review/persona_*.md" in guidance
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["gen"]["status"] == "ok" and manifest["rev"]["status"] == "ok"
    decisions = [json.loads(x) for x in (ctx.run_dir / "logs/decisions.jsonl").read_text().splitlines()]
    assert any(d["decision"] == "back_jump" and d["stage"] == "rev" for d in decisions)


def test_backjump_works_as_stages_subset(ctx):
    """Phase 2 of the plugin flow runs `--stages generate,review` as a subset; the review->generate
    back-jump must still fire (target must be in the selected set). Regression guard for that flow."""
    runs: list[str] = []
    run_pipeline(_pipeline(runs, [["review BLOCKER: x"]]), ctx, selected=["gen", "rev"])
    assert runs == ["gen", "rev", "gen", "rev"]  # back-jump works within the subset
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["rev"]["status"] == "ok"


def test_backjump_is_bounded(ctx, monkeypatch):
    from rfc_agent import obs

    finished: list[dict] = []
    monkeypatch.setattr(obs, "finish_run", lambda c, m, ran=None: finished.append(m))
    runs: list[str] = []
    always = [["review BLOCKER: x"], ["review BLOCKER: x"], ["review BLOCKER: x"]]
    with pytest.raises(GateFailure, match="rev"):
        run_pipeline(_pipeline(runs, always), ctx)
    assert runs == ["gen", "rev", "gen", "rev"]  # exactly one jump, then hard failure
    # trace output must be recorded on FAILED runs too (audit: exit-2 traces had output null)
    assert finished and finished[0]["rev"]["status"] == "gate_failed"


def test_revise_on_retry_keeps_previous_outputs(ctx):
    """Revision mode: on a guided rerun the previous attempt stays on disk; without it, hygiene deletes."""
    existed_at_start: list[bool] = []

    def probe_engine(stage, c):
        existed_at_start.append(c.path("out/rfc.md").exists())
        p = c.path("out/rfc.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("v" + str(len(existed_at_start)))
        return StageResult(final_text="done", cost_usd=0.1, engine_desc="fake")

    fails_once = [["fix the coverage table"]]

    def gate(c):
        return fails_once.pop(0) if fails_once else []

    st = Stage(
        name="gen", requires=("inputs.json",), outputs=("out/rfc.md",),
        build_prompt=lambda c: "p", gate=gate, engine=probe_engine,
        retry_on_gate_failure=1, revise_on_retry=True,
    )
    run_pipeline([st], ctx)
    assert existed_at_start == [False, True]  # attempt 1: hygiene wiped; attempt 2 (guided): kept for revision
    decisions = [json.loads(x) for x in (ctx.run_dir / "logs/decisions.jsonl").read_text().splitlines()]
    assert any(d["decision"] == "revise_in_place" for d in decisions)


def test_revision_rotates_stream_and_fresh_run_wipes_rotations(ctx):
    """Trace evidence must span exactly the attempts that produced the current outputs: a revision keeps
    the prior attempt's stream as <stage>.stream.<n>.jsonl (witness spans it), while a fresh regenerate
    wipes current + rotations so a stale stream can't witness citations the new scan never made."""
    attempt = [0]

    def engine(stage, c):
        attempt[0] += 1
        out = c.path(stage.outputs[0])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(f"attempt {attempt[0]}")
        s = c.path(f"logs/{stage.name}.stream.jsonl")  # mimic claude_engine's stream postcondition
        s.parent.mkdir(parents=True, exist_ok=True)
        s.write_text(f'{{"attempt": {attempt[0]}}}')
        return StageResult(cost_usd=0.1, engine_desc="fake")

    fails_once = [["fix one line"]]
    st = Stage(
        name="gr", requires=("inputs.json",), outputs=("findings/f.md",), build_prompt=lambda c: "p",
        gate=lambda c: fails_once.pop(0) if fails_once else [], engine=engine,
        retry_on_gate_failure=1, revise_on_retry=True,
    )
    # stale residue from an older invocation must be wiped by attempt 1 (fresh), not rotated into evidence
    (ctx.run_dir / "logs").mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "logs/gr.stream.jsonl").write_text('{"stale": true}')
    (ctx.run_dir / "logs/gr.stream.1.jsonl").write_text('{"stale": true}')

    run_pipeline([st], ctx)
    rotated = (ctx.run_dir / "logs/gr.stream.1.jsonl").read_text()
    current = (ctx.run_dir / "logs/gr.stream.jsonl").read_text()
    assert rotated == '{"attempt": 1}', "revision must keep attempt 1's stream as evidence, not the stale one"
    assert current == '{"attempt": 2}'
    assert not (ctx.run_dir / "logs/gr.stream.2.jsonl").exists()


def test_stale_guidance_cleared_at_invocation_start(ctx):
    """A new invocation must NOT enter revision mode against a previous run's guidance left on disk.
    Regression: guidance was append-only and never cleared, so a --force/--stages rerun silently made
    targeted edits against stale blockers instead of regenerating cleanly."""
    # simulate residue from a prior run: stale guidance + a stale output on disk
    (ctx.run_dir / "guidance").mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "guidance/gen.md").write_text("- GATE FAILURES from a PREVIOUS run: stale blocker\n")
    (ctx.run_dir / "out").mkdir(parents=True, exist_ok=True)
    (ctx.run_dir / "out/rfc.md").write_text("stale output")

    saw_revision: list[bool] = []

    def probe_engine(stage, c):
        # revision mode is signalled by guidance existing when the engine runs; assert it was cleared
        saw_revision.append((c.run_dir / "guidance/gen.md").exists())
        p = c.path("out/rfc.md")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("fresh output")
        return StageResult(final_text="done", cost_usd=0.1, engine_desc="fake")

    st = Stage(
        name="gen", requires=("inputs.json",), outputs=("out/rfc.md",),
        build_prompt=lambda c: "p", gate=lambda c: [], engine=probe_engine,
        retry_on_gate_failure=1, revise_on_retry=True,
    )
    run_pipeline([st], ctx)
    assert saw_revision == [False]  # stale guidance was cleared before the stage ran — clean regenerate
    decisions_path = ctx.run_dir / "logs/decisions.jsonl"
    decs = [json.loads(x) for x in decisions_path.read_text().splitlines()] if decisions_path.exists() else []
    assert not any(d["decision"] == "revise_in_place" for d in decs)


def test_same_stage_retry_still_records_decision(ctx):
    runs: list[str] = []
    fails_once = [["out too thin"]]

    def gate(c):
        return fails_once.pop(0) if fails_once else []

    st = Stage(
        name="solo", requires=("inputs.json",), outputs=("out/x.md",),
        build_prompt=lambda c: "p", gate=gate, engine=_counting_engine(runs, "solo"),
        retry_on_gate_failure=1,
    )
    run_pipeline([st], ctx)
    assert runs == ["solo", "solo"]
    decisions = [json.loads(x) for x in (ctx.run_dir / "logs/decisions.jsonl").read_text().splitlines()]
    assert decisions[0]["decision"] == "auto_retry_on_gate_failure" and decisions[0]["why"]


def test_resume_from_red_review_jumps_straight_to_generate(ctx):
    """v10 (2026-08-11): a budget-exhausted review stop discarded verified findings — resume re-ran
    review from scratch (~10min) and the fresh panel re-sampled the defect population, losing the
    booking.py:2214 blocker filed at 11:56 until a THIRD round re-derived it at 12:14. Resume must
    behave like the back-jump the stop interrupted: recorded failures -> generate, review only after."""
    runs: list[str] = []
    with pytest.raises(GateFailure):
        run_pipeline(_pipeline(runs, [["review BLOCKER: seam a"], ["review BLOCKER: seam b (still)"]]), ctx)
    assert runs == ["gen", "rev", "gen", "rev"]  # back-jump used, second review fails, run stops

    runs2: list[str] = []
    run_pipeline(_pipeline(runs2, [[]]), ctx)  # resume; review passes after the revision
    assert runs2 == ["gen", "rev"]  # gen revised FIRST from the recorded failures — no fresh review pass
    guidance = (ctx.run_dir / "guidance/gen.md").read_text()
    assert "seam b (still)" in guidance and "'rev' stage" in guidance
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["gen"]["status"] == "ok" and manifest["rev"]["status"] == "ok"


def test_resume_from_red_revision_records_decision_once(tmp_path):
    """Cross-invocation resume of a red revise_on_retry stage: the seeder writes guidance AND records
    REVISE_IN_PLACE; the rerun loop must not record it a second time (T3/T4 traces showed duplicate
    decision chains at the same instant with different whys)."""
    runs: list[str] = []
    st = Stage(
        name="gen", requires=("inputs.json",), outputs=("out/rfc.md",),
        build_prompt=lambda ctx: "p", gate=lambda ctx: [], engine=_counting_engine(runs, "gen"),
        retry_on_gate_failure=1, revise_on_retry=True,
    )
    (tmp_path / "inputs.json").write_text("{}")
    (tmp_path / "out").mkdir()
    (tmp_path / "out/rfc.md").write_text("previous attempt output")
    (tmp_path / "manifest.json").write_text(json.dumps({
        "gen": {"status": "gate_failed", "attempts": 2,
                "gate": {"passed": False, "failures": ["citation broken"]}}
    }))
    ctx = RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=None)
    run_pipeline([st], ctx)
    entries = [json.loads(line) for line in (tmp_path / "logs/decisions.jsonl").read_text().splitlines()]
    revises = [e for e in entries if e["decision"] == "revise_in_place"]
    assert len(revises) == 1, revises
    assert revises[0]["attempt"] == 3  # the attempt the revision became
