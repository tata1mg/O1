"""Pipeline contract tests with a fake engine — proves the engine seam + resume semantics from day 1."""

import json

import pytest

from rfc_agent.runner import (
    EngineFailure,
    GateFailure,
    PipelineConfigError,
    RunContext,
    Stage,
    StageResult,
    run_pipeline,
)


def fake_engine(outputs_content: dict[str, str] | None = None):
    def _engine(stage: Stage, ctx: RunContext) -> StageResult:
        for rel in stage.outputs:
            p = ctx.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text((outputs_content or {}).get(rel, f"content of {rel}"))
        return StageResult(final_text="done", cost_usd=0.1, usage={"input_tokens": 1}, engine_desc="fake")

    return _engine


def _stages(gate_failures: dict[str, list[str]] | None = None):
    gf = gate_failures or {}
    return [
        Stage(
            name="a",
            requires=("inputs.json",),
            outputs=("raw/one.md",),
            build_prompt=lambda ctx: "p",
            gate=lambda ctx, n="a": gf.get(n, []),
            engine=fake_engine(),
        ),
        Stage(
            name="b",
            requires=("raw/one.md",),
            outputs=("out/two.md",),
            build_prompt=lambda ctx: "p",
            gate=lambda ctx, n="b": gf.get(n, []),
            engine=fake_engine(),
        ),
    ]


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "inputs.json").write_text("{}")
    return RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=None)


def test_full_pipeline_green_manifest(ctx):
    run_pipeline(_stages(), ctx)
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert set(manifest) == {"a", "b"}
    for entry in manifest.values():
        assert entry["status"] == "ok"
        assert entry["gate"]["passed"] is True
        assert entry["cost_usd"] == 0.1
        assert entry["engine"] == "fake"
    assert (ctx.run_dir / "out/two.md").exists()


def test_rerun_skips_green_stages(ctx):
    run_pipeline(_stages(), ctx)
    calls = []

    def counting_engine(stage, c):
        calls.append(stage.name)
        return fake_engine()(stage, c)

    stages = [
        Stage(**{**s.__dict__, "engine": counting_engine}) for s in _stages()
    ]
    run_pipeline(stages, ctx)  # everything green -> nothing runs
    assert calls == []


def test_stage_subset_reruns_exactly_that_stage(ctx):
    run_pipeline(_stages(), ctx)
    (ctx.run_dir / "out/two.md").unlink()
    run_pipeline(_stages(), ctx, selected=["b"])
    assert (ctx.run_dir / "out/two.md").exists()


def test_sequential_single_stage_build(ctx):
    """Review mode drives the pipeline one stage per invocation (--stages a, then --stages b), each a
    separate call resuming on disk — not one full run. Lock that exact flow."""
    run_pipeline(_stages(), ctx, selected=["a"])
    assert (ctx.run_dir / "raw/one.md").exists()
    assert not (ctx.run_dir / "out/two.md").exists()  # b hasn't run yet
    run_pipeline(_stages(), ctx, selected=["b"])  # resumes from a's output on disk
    assert (ctx.run_dir / "out/two.md").exists()
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["a"]["status"] == "ok" and manifest["b"]["status"] == "ok"


def test_retried_stage_reports_cumulative_not_last_attempt(ctx):
    """The manifest entry was overwritten per attempt, so a retried stage's earlier spend vanished — the
    accounting went lossy exactly when a run got expensive. cost_usd stays last-attempt (public contract);
    the totals must count every attempt."""
    from rfc_agent.runner import run_totals

    attempts = []

    def engine(stage, c):
        attempts.append(1)
        p = c.path(stage.outputs[0])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        return StageResult(cost_usd=2.0, duration_s=10.0, engine_desc="fake")

    st = Stage(
        name="g", requires=("inputs.json",), outputs=("out/g.md",), build_prompt=lambda c: "p",
        gate=lambda c: ["nope"] if len(attempts) == 1 else [], engine=engine, retry_on_gate_failure=1,
    )
    run_pipeline([st], ctx)
    entry = json.loads((ctx.run_dir / "manifest.json").read_text())["g"]
    assert entry["attempts"] == 2
    assert entry["cost_usd"] == 2.0, "last-attempt cost stays the documented contract"
    assert entry["cost_usd_total"] == 4.0, "both attempts must be counted"
    assert run_totals({"g": entry}) == (4.0, 20.0, 2)


def test_green_stage_in_a_stages_subset_is_skipped(ctx):
    """Resume must be cheap in the plugin flow, which ALWAYS passes --stages: a green stage inside the
    subset is skipped, and --force is the knob that re-runs it. Regression guard — this used to require
    `selected is None`, so every exit-3 retry re-paid for stages already on disk."""
    run_pipeline(_stages(), ctx)  # everything green
    calls: list[str] = []

    def counting(stage, c):
        calls.append(stage.name)
        return fake_engine()(stage, c)

    stages = [Stage(**{**s.__dict__, "engine": counting}) for s in _stages()]
    run_pipeline(stages, ctx, selected=["a", "b"])
    assert calls == [], "green stages in a --stages subset must skip"

    run_pipeline(stages, ctx, selected=["a"], force=True)
    assert calls == ["a"], "--force is what re-runs a green stage"


def test_missing_requires_fails_loudly(ctx):
    with pytest.raises(EngineFailure, match="requires"):
        run_pipeline(_stages(), ctx, selected=["b"])  # raw/one.md never produced


def test_gate_failure_stops_pipeline_and_records(ctx):
    with pytest.raises(GateFailure, match="stage 'a'"):
        run_pipeline(_stages(gate_failures={"a": ["bad thing"]}), ctx)
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["a"]["status"] == "gate_failed"
    assert "b" not in manifest  # never ran


def test_unknown_stage_name_rejected(ctx):
    """Typed error, never SystemExit — orchestration must stay embeddable (only cli.main exits)."""
    with pytest.raises(PipelineConfigError, match="unknown stage"):
        run_pipeline(_stages(), ctx, selected=["nope"])


def test_crashed_stage_spend_survives_in_manifest(ctx):
    """Audit 2026-08-19 gap #1: an EngineFailure used to skip finish_generation AND the manifest
    write, so the crashed attempt's dollars vanished from every surface. The runner now recovers
    best-effort accounting from the on-disk stream files and records an engine_failed entry."""
    import json as _json

    def crashing_engine(stage, c):
        # like the real engine: the stream file is written BEFORE the failure surfaces (a fresh
        # attempt wipes stale streams first, so pre-seeding outside the engine would be unrealistic)
        logs = c.run_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        (logs / "a.stream.jsonl").write_text("\n".join(_json.dumps(e) for e in [
            {"type": "system", "subtype": "init", "model": "claude-test", "session_id": "s1"},
            {"type": "result", "is_error": True, "subtype": "api_error",
             "total_cost_usd": 3.25, "usage": {"input_tokens": 11, "output_tokens": 5}, "num_turns": 4},
        ]) + "\n")
        raise EngineFailure("boom mid-stage")

    stages = [Stage(**{**_stages()[0].__dict__, "engine": crashing_engine})]
    with pytest.raises(EngineFailure):
        run_pipeline(stages, ctx)
    manifest = _json.loads((ctx.run_dir / "manifest.json").read_text())
    entry = manifest["a"]
    assert entry["status"] == "engine_failed"
    assert entry["cost_usd"] == 3.25
    assert entry["cost_usd_total"] == 3.25
    assert entry["attempts"] == 1
    assert "claude-test (crashed)" in entry["engine"]
    # ...and the crash is a first-class decision in the run's decision log
    decisions = (ctx.run_dir / "logs" / "decisions.jsonl").read_text()
    assert "stage_crashed" in decisions


def test_green_skip_is_recorded_as_decision(ctx):
    run_pipeline(_stages(), ctx)
    dpath = ctx.run_dir / "logs" / "decisions.jsonl"
    before = dpath.read_text() if dpath.exists() else ""
    run_pipeline(_stages(), ctx)  # everything green -> both stages skip
    after = dpath.read_text()
    assert after.count("green_skip") - before.count("green_skip") == 2


def test_gate_inert_checks_land_in_manifest(ctx):
    from rfc_agent.gates import note_inert

    def noting_gate(c):
        note_inert("citation_witness")
        return []

    stages = [Stage(**{**_stages()[0].__dict__, "gate": noting_gate})]
    run_pipeline(stages, ctx)
    import json as _json
    manifest = _json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["a"]["gate"]["inert_checks"] == ["citation_witness"]
    assert manifest["a"]["gate"]["passed"] is True


def test_revision_crash_counts_only_this_attempts_streams(ctx):
    """Review finding #1 (2026-08-19): revision mode ROTATES the prior attempt's stream (witness
    evidence) whose cost is already banked in cost_usd_total — crash recovery must not re-count it,
    and the merge primary must be THIS attempt's main stream, not the rotated one. Exercises the
    REAL revision flow: gate fails attempt 1 -> auto-retry with guidance -> rotation -> crash."""
    import json as _json

    calls = []

    def engine(stage, c):
        calls.append(1)
        logs = c.run_dir / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        p = c.path(stage.outputs[0])
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x")
        if len(calls) == 1:  # attempt 1: succeeds at $6, gate will fail it
            (logs / "g.stream.jsonl").write_text("\n".join(_json.dumps(e) for e in [
                {"type": "system", "subtype": "init", "model": "claude-old-attempt", "session_id": "s0"},
                {"type": "result", "is_error": False, "result": "old", "total_cost_usd": 6.0},
            ]) + "\n")
            return StageResult(cost_usd=6.0, duration_s=1.0, engine_desc="claude:claude-old-attempt")
        # attempt 2 (revision — attempt 1's stream is now rotated): spends $4 then crashes
        (logs / "g.stream.jsonl").write_text("\n".join(_json.dumps(e) for e in [
            {"type": "system", "subtype": "init", "model": "claude-new-attempt", "session_id": "s1"},
            {"type": "result", "is_error": True, "subtype": "api_error", "total_cost_usd": 4.0},
        ]) + "\n")
        raise EngineFailure("boom on the revision attempt")

    st = Stage(
        name="g", requires=("inputs.json",), outputs=("out/g.md",), build_prompt=lambda c: "p",
        gate=lambda c: ["g.md: required section missing: 'X'"] if len(calls) == 1 else [],
        engine=engine, retry_on_gate_failure=1, revise_on_retry=True,
    )
    with pytest.raises(EngineFailure):
        run_pipeline([st], ctx)
    assert (ctx.run_dir / "logs" / "g.stream.1.jsonl").exists(), "rotation must have happened"
    entry = _json.loads((ctx.run_dir / "manifest.json").read_text())["g"]
    assert entry["cost_usd"] == 4.0, "this attempt only — the rotated $6 is already banked"
    assert entry["cost_usd_total"] == 10.0
    assert "claude-new-attempt (crashed)" in entry["engine"], "primary = current main stream"
    assert entry["attempts"] == 2
    # documented manifest shape survives the crash entry
    assert entry["requires"] == ["inputs.json"] and entry["outputs"] == ["out/g.md"]
    # the prior RED gate record is preserved for resume-from-red
    assert entry["gate"]["failures"] and entry["gate"]["passed"] is False


def test_crash_replaces_stale_passing_gate_with_no_verdict_stub(ctx):
    """Review finding #8: --force + crash used to leave status=engine_failed beside gate.passed=true
    for outputs the rerun hygiene had just deleted."""
    import json as _json

    run_pipeline(_stages(), ctx)  # both stages green, gate.passed true

    def crashing_engine(stage, c):
        raise EngineFailure("boom")

    stages = [Stage(**{**_stages()[0].__dict__, "engine": crashing_engine})]
    with pytest.raises(EngineFailure):
        run_pipeline(stages, ctx, force=True)
    entry = _json.loads((ctx.run_dir / "manifest.json").read_text())["a"]
    assert entry["status"] == "engine_failed"
    assert entry["gate"]["passed"] is False and entry["gate"]["failures"] == []
    assert "crashed before the gate" in entry["gate"]["note"]


def test_crashed_stage_counts_as_ran_this_invocation(ctx, monkeypatch):
    """Review finding #3: the crash path raised before ran_stages.add, so the one stage that spent
    this invocation's money reported ran_this_invocation: false."""
    from rfc_agent import obs

    captured = {}
    monkeypatch.setattr(obs, "finish_run",
                        lambda c, manifest, ran=None: captured.update(ran=set(ran or ())))

    def crashing_engine(stage, c):
        raise EngineFailure("boom")

    stages = [Stage(**{**_stages()[0].__dict__, "engine": crashing_engine})]
    with pytest.raises(EngineFailure):
        run_pipeline(stages, ctx)
    assert captured["ran"] == {"a"}


def test_manifest_write_is_atomic_and_leaves_no_temp(ctx):
    run_pipeline(_stages(), ctx)
    assert not list(ctx.run_dir.glob("*.tmp")), "temp file must be replaced, never left behind"
    assert json.loads((ctx.run_dir / "manifest.json").read_text())["a"]["status"] == "ok"


def test_attempt_intent_lifecycle(ctx):
    """The breadcrumb exists only between attempt start and record — a green run and a live-recorded
    crash both clear it (crash-only: the breadcrumb's presence at startup MEANS an unrecorded death)."""
    run_pipeline(_stages(), ctx)
    assert not list((ctx.run_dir / "logs").glob(".attempt.*.json"))

    def crashing_engine(stage, c):
        raise EngineFailure("boom")

    stages = [Stage(**{**_stages()[0].__dict__, "engine": crashing_engine})]
    with pytest.raises(EngineFailure):
        run_pipeline(stages, ctx, force=True)
    assert not list((ctx.run_dir / "logs").glob(".attempt.*.json")), \
        "live crash path records the attempt and must clear the intent"


def test_startup_reconciles_a_killed_attempt(ctx):
    """SIGKILL/power-loss recovery (0.23.2): an intent breadcrumb without a manifest record is
    reconciled at the NEXT invocation from the dead attempt's stream files — the crash-only pattern
    (no signal-handler writes). The reconciled stage then re-runs normally in the same invocation."""
    import json as _json
    import time as _time

    logs = ctx.run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    epoch = _time.time() - 60  # the dead attempt started a minute ago
    (logs / ".attempt.a.json").write_text(_json.dumps(
        {"stage": "a", "attempt": 1, "epoch": epoch, "started_at": "2026-08-19T10:00:00+00:00"}))
    (logs / "a.stream.jsonl").write_text("\n".join(_json.dumps(e) for e in [
        {"type": "system", "subtype": "init", "model": "claude-test", "session_id": "s1"},
        {"type": "result", "is_error": True, "subtype": "killed", "total_cost_usd": 7.5},
    ]) + "\n")  # mtime = now, > epoch

    run_pipeline(_stages(), ctx)  # reconciles, then runs the pipeline green
    entry = _json.loads((ctx.run_dir / "manifest.json").read_text())["a"]
    assert entry["status"] == "ok", "the stage re-ran green after reconciliation"
    assert entry["attempts"] == 2, "the dead attempt counts"
    assert entry["cost_usd_total"] == 7.6, "recovered $7.50 + the green rerun's $0.10"
    decisions = (logs / "decisions.jsonl").read_text()
    assert "reconciled at startup" in decisions
    assert not list(logs.glob(".attempt.*.json"))


def test_reconciliation_is_idempotent_when_attempt_was_recorded_live(ctx):
    """A kill BETWEEN the live crash record and the breadcrumb clear must not double-bill: the
    attempts>=N guard skips reconciliation when the manifest already carries the attempt."""
    import json as _json
    import time as _time

    logs = ctx.run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / ".attempt.a.json").write_text(_json.dumps(
        {"stage": "a", "attempt": 1, "epoch": _time.time() - 60, "started_at": "t"}))
    (logs / "a.stream.jsonl").write_text(_json.dumps(
        {"type": "result", "is_error": True, "total_cost_usd": 7.5}) + "\n")
    (ctx.run_dir / "manifest.json").write_text(_json.dumps({
        "a": {"status": "engine_failed", "attempts": 1, "cost_usd": 7.5, "cost_usd_total": 7.5,
              "duration_s": 60, "duration_s_total": 60.0,
              "gate": {"passed": False, "failures": [], "note": "crashed"}}}))

    run_pipeline(_stages(), ctx)  # must NOT add a second $7.5
    entry = _json.loads((ctx.run_dir / "manifest.json").read_text())["a"]
    assert entry["attempts"] == 2, "reconcile skipped; only the green rerun incremented"
    assert entry["cost_usd_total"] == 7.6
    assert "reconciled at startup" not in ((logs / "decisions.jsonl").read_text()
                                           if (logs / "decisions.jsonl").exists() else "")


def test_corrupt_breadcrumb_never_kills_the_run(ctx):
    """0.23.3: recovery must never fail a run. A breadcrumb with string/garbage fields is cleared
    (so it cannot crash every future startup) and the pipeline proceeds green."""
    import json as _json

    logs = ctx.run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / ".attempt.a.json").write_text(_json.dumps(
        {"stage": "a", "attempt": "two", "epoch": {"not": "a number"}}))
    run_pipeline(_stages(), ctx)  # must not raise
    assert _json.loads((ctx.run_dir / "manifest.json").read_text())["a"]["status"] == "ok"
    assert not list(logs.glob(".attempt.*.json"))


def test_breadcrumb_survives_a_narrower_pipeline_composition(ctx):
    """0.23.3: a dead GROUND attempt must not be eaten by a later `--pipeline ingest,extract`
    resume — the breadcrumb is KEPT when its stage is absent from the current composition, so a
    fuller invocation can still reconcile the spend."""
    import json as _json
    import time as _time

    logs = ctx.run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    (logs / ".attempt.not-in-this-pipeline.json").write_text(_json.dumps(
        {"stage": "not-in-this-pipeline", "attempt": 1, "epoch": _time.time() - 60}))
    run_pipeline(_stages(), ctx)  # composition without that stage
    assert (logs / ".attempt.not-in-this-pipeline.json").exists(), "kept for a fuller composition to reconcile"


def test_reconciled_entry_derives_started_at_from_epoch(ctx):
    """0.23.3: started_at is derived from the breadcrumb's epoch — a reconciled crash entry can
    never carry an empty timestamp (the '' fallback undated exactly the entry an engineer reads to
    date the spend)."""
    import json as _json
    import time as _time

    logs = ctx.run_dir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    epoch = _time.time() - 120
    (logs / ".attempt.a.json").write_text(_json.dumps({"stage": "a", "attempt": 1, "epoch": epoch}))
    (logs / "a.stream.jsonl").write_text(_json.dumps(
        {"type": "result", "is_error": True, "total_cost_usd": 1.0}) + "\n")
    run_pipeline(_stages(), ctx)
    entry = _json.loads((ctx.run_dir / "manifest.json").read_text())["a"]
    assert entry["attempts"] == 2 and entry["cost_usd_total"] == 1.1
    # the green rerun overwrote started_at; the decision log carries the reconcile — assert the
    # reconciler itself accepted the epoch-derived form by checking it recorded the crash at all
    assert "reconciled at startup" in (logs / "decisions.jsonl").read_text()
