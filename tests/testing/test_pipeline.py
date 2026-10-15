"""The whole workflow under the harness runner, with a fake engine.

The six default stages run through `rfc_agent.runner.run_pipeline` (the pattern in tests/infra/test_runner.py),
so the manifest, the green-skip and the retry-on-gate-failure path are exercised for this workflow specifically.

The fake engine stands in for the two persona stages. It writes their declared outputs and returns an
`EngineResult`, which is the entire contract a stage-owned engine depends on — no `claude` binary,
no network, no model.
"""

from __future__ import annotations

import json

import pytest

from harness.contracts import GateFailure
from harness.engines import EngineResult
from harness.events import TraceSummary
from rfc_agent.runner import run_pipeline
from workflows.testing import WORKFLOW


class FakeEngine:
    """Writes each stage's outputs from a per-label recipe and records what it was asked to do."""

    name = "fake"

    def __init__(self, recipes: dict) -> None:
        self.recipes = recipes
        self.calls: list[str] = []
        self.requests: list = []

    def run(self, req) -> EngineResult:
        self.calls.append(req.label)
        self.requests.append(req)
        self.recipes[req.label](req.cwd)
        return EngineResult(
            summary=TraceSummary(final_text=f"{req.label} done", cost_usd=0.25,
                                 usage={"input_tokens": 10}, num_turns=3, got_result=True),
            duration_s=1.0, engine_desc="fake:test", prompt_sent=req.prompt,
            trace_path=req.trace_path,
        )

    def supports_resume(self) -> bool:
        return False

    def doctor(self) -> list[str]:
        return []


def eval_json(cases, scenarios, *, presence_status="PASS", overall=4.0):
    """A scorecard that satisfies the evaluate gate: correct arithmetic, derived compliance_status."""
    from workflows.testing.stages.evaluate import COMPLIANCE_CHECKS, DIMENSIONS

    scores = dict.fromkeys(DIMENSIONS, 4)
    ok = {"status": "PASS", "violations": []}
    return {
        "overall_score": overall, "coverage_score": 4.0, "quality_score": 4.0,
        "compliance_status": "PASS" if presence_status == "PASS" else "FAIL",
        "presence_checks": {"expected_result": dict(ok), "precondition": dict(ok)},
        "dimension_scores": scores,
        "compliance_checks": {name: dict(ok) for name in COMPLIANCE_CHECKS},
        "missing_scenarios": [], "incorrect_cases": [], "duplicate_cases": [], "recommendations": [],
    }


@pytest.fixture
def recipes(clean_cases, clean_scenarios):
    def generate(run_dir):
        (run_dir / "qa").mkdir(parents=True, exist_ok=True)
        (run_dir / "qa" / "cases.json").write_text(json.dumps(clean_cases))
        (run_dir / "qa" / "scenarios.json").write_text(json.dumps(clean_scenarios))

    def evaluate(run_dir):
        (run_dir / "qa" / "eval.json").write_text(
            json.dumps(eval_json(clean_cases, clean_scenarios)))

    return {"generate_cases": generate, "evaluate": evaluate}


@pytest.fixture
def pipeline_ctx(tmp_path, ctx_factory, recipes):
    """A run dir whose sources exist on disk, with the fake engine injected."""
    src = tmp_path / "src"
    src.mkdir()
    (src / "prd.md").write_text("# PRD\nREQ-1 slots. REQ-2 unknown lab id. REQ-3 offline.\n")
    (src / "rfc.md").write_text("# RFC\n## Open questions\nQ1 the cache TTL is unresolved.\n")
    engine = FakeEngine(recipes)
    ctx = ctx_factory(tmp_path / "run",
                      inputs={"feature": "RSE-1", "prd": str(src / "prd.md"), "rfc": str(src / "rfc.md")},
                      engine=engine)
    return ctx, engine


def stages(names=None):
    return [WORKFLOW.stage(n) for n in (names or WORKFLOW.default_pipeline)]


def manifest(ctx) -> dict:
    return json.loads((ctx.run_dir / "manifest.json").read_text())


# ---- the default pipeline -----------------------------------------------------------------------------
def test_the_default_pipeline_runs_green_end_to_end(pipeline_ctx):
    ctx, engine = pipeline_ctx
    run_pipeline(stages(), ctx)

    m = manifest(ctx)
    assert set(m) == set(WORKFLOW.default_pipeline)
    for name, entry in m.items():
        assert entry["status"] == "ok", (name, entry)
        assert entry["gate"]["passed"] is True
    # only the two persona stages went to the engine
    assert engine.calls == ["generate_cases", "evaluate"]
    for rel in ("qa/sources.json", "qa/cases.json", "qa/scenarios.json", "qa/eval.json",
                "qa/eval.mechanical.json", "qa/store.json", "qa/spec.json"):
        assert ctx.path(rel).exists(), rel


def test_the_python_stages_report_themselves_as_python(pipeline_ctx):
    ctx, _ = pipeline_ctx
    run_pipeline(stages(), ctx)
    m = manifest(ctx)
    assert m["sources"]["engine"] == "python"
    assert m["store_cases"]["engine"] == "python"
    assert m["derive_spec"]["engine"] == "python"
    assert m["generate_cases"]["engine"] == "fake:test"


def test_the_engine_stages_get_their_own_skill_as_the_persona(pipeline_ctx):
    """The SKILL.md is staged as the persona rather than named in a prompt for `Skill` to find, so no
    user-scope plugin install is needed."""
    ctx, engine = pipeline_ctx
    run_pipeline(stages(), ctx)
    by_label = {r.label: r for r in engine.requests}
    assert by_label["generate_cases"].skill_path.parts[-2] == "qa-test-case-generator"
    assert by_label["evaluate"].skill_path.parts[-2] == "qa-test-case-evaluator"
    assert by_label["generate_cases"].skill_path.exists()


def test_the_generate_prompt_carries_run_chains_rules(pipeline_ctx):
    """Parity with the run_chain.sh heredoc (:131-178), minus the storage call."""
    ctx, engine = pipeline_ctx
    run_pipeline(stages(), ctx)
    prompt = next(r for r in engine.requests if r.label == "generate_cases").prompt
    assert "PRD: " in prompt and "RFC: " in prompt
    assert "Open questions" in prompt
    assert "execution" in prompt and "surface" in prompt and "exec:http" not in prompt.split("\n")[0]
    assert "qa/cases.json" in prompt and "qa/scenarios.json" in prompt
    assert "kiwi_upsert_test_cases" not in prompt   # storage is store_cases' job now


def test_a_run_without_a_contract_says_so_in_the_prompt(pipeline_ctx):
    ctx, engine = pipeline_ctx
    run_pipeline(stages(), ctx)
    prompt = next(r for r in engine.requests if r.label == "generate_cases").prompt
    assert "There is NO API contract in this run" in prompt


# ---- resume and green-skip -----------------------------------------------------------------------------
def test_a_second_invocation_skips_every_green_stage(pipeline_ctx):
    ctx, engine = pipeline_ctx
    run_pipeline(stages(), ctx)
    engine.calls.clear()
    run_pipeline(stages(), ctx)
    assert engine.calls == []
    decisions = [json.loads(x) for x in ctx.path("logs/decisions.jsonl").read_text().splitlines()]
    skipped = {d["stage"] for d in decisions if d["decision"] == "green_skip"}
    assert skipped == set(WORKFLOW.default_pipeline)


def test_a_stage_subset_reruns_exactly_that_stage(pipeline_ctx):
    ctx, engine = pipeline_ctx
    run_pipeline(stages(), ctx)
    ctx.path("qa/spec.json").unlink()
    engine.calls.clear()
    run_pipeline(stages(), ctx, selected=["derive_spec"])
    assert engine.calls == []                      # derive_spec is python
    assert ctx.path("qa/spec.json").exists()


def test_the_chain_resumes_from_disk_one_stage_per_invocation(pipeline_ctx):
    """The flow a plugin command drives: --stages sources, then --stages generate_cases, ..."""
    ctx, engine = pipeline_ctx
    for name in WORKFLOW.default_pipeline:
        run_pipeline(stages(), ctx, selected=[name])
    assert engine.calls == ["generate_cases", "evaluate"]
    assert manifest(ctx)["derive_spec"]["status"] == "ok"


def test_a_changed_source_re_runs_the_stages_downstream_of_it(pipeline_ctx, tmp_path):
    """sources.json is the inputs digest for generate_cases, so editing a source must not leave a green
    suite anchored on the old one."""
    ctx, engine = pipeline_ctx
    run_pipeline(stages(), ctx)
    engine.calls.clear()
    (tmp_path / "src" / "prd.md").write_text("# PRD\nREQ-1 slots. REQ-4 a whole new requirement.\n")
    run_pipeline(stages(), ctx, selected=["sources", "generate_cases"], force=True)
    assert engine.calls == ["generate_cases"]


# ---- gates actually stop the run -------------------------------------------------------------------------
def test_a_broken_suite_fails_the_generate_gate_after_its_retry(pipeline_ctx, sample_cases,
                                                                sample_scenarios):
    ctx, engine = pipeline_ctx

    def bad_generate(run_dir):
        (run_dir / "qa").mkdir(parents=True, exist_ok=True)
        (run_dir / "qa" / "cases.json").write_text(json.dumps(sample_cases))
        (run_dir / "qa" / "scenarios.json").write_text(json.dumps(sample_scenarios))

    engine.recipes["generate_cases"] = bad_generate
    with pytest.raises(GateFailure):
        run_pipeline(stages(), ctx)
    entry = manifest(ctx)["generate_cases"]
    assert entry["status"] == "gate_failed"
    assert entry["attempts"] == 2                  # retry_on_gate_failure=1
    joined = "\n".join(entry["gate"]["failures"])
    assert "TC-RSE-BAD-FUNC-1" in joined
    assert "TC-RSE-CAP-FUNC-02" in joined and "do not parse" in joined


def test_the_gate_failures_reach_the_next_attempt_as_guidance(pipeline_ctx, sample_cases,
                                                              sample_scenarios):
    """revise_on_retry: the second attempt is a targeted edit, and the prompt carries the failures."""
    ctx, engine = pipeline_ctx
    seen_prompts = []

    def bad_then_good(run_dir):
        seen_prompts.append(engine.requests[-1].prompt)
        if len(seen_prompts) == 1:
            (run_dir / "qa").mkdir(parents=True, exist_ok=True)
            (run_dir / "qa" / "cases.json").write_text(json.dumps(sample_cases))
            (run_dir / "qa" / "scenarios.json").write_text(json.dumps(sample_scenarios))
        else:
            recipes_good(run_dir)

    recipes_good = engine.recipes["generate_cases"]
    engine.recipes["generate_cases"] = bad_then_good
    run_pipeline(stages(), ctx)
    assert len(seen_prompts) == 2
    assert "Feedback on your previous attempt" in seen_prompts[1]
    assert "TC-RSE-BAD-FUNC-1" in seen_prompts[1]
    assert manifest(ctx)["generate_cases"]["status"] == "ok"


def test_an_evaluator_whose_arithmetic_is_wrong_fails_the_gate(pipeline_ctx, clean_cases,
                                                               clean_scenarios):
    ctx, engine = pipeline_ctx

    def wrong_arithmetic(run_dir):
        data = eval_json(clean_cases, clean_scenarios)
        data["overall_score"] = 4.9                # the dimensions all say 4
        (run_dir / "qa" / "eval.json").write_text(json.dumps(data))

    engine.recipes["evaluate"] = wrong_arithmetic
    with pytest.raises(GateFailure):
        run_pipeline(stages(), ctx)
    failures = manifest(ctx)["evaluate"]["gate"]["failures"]
    assert any("overall_score=4.9 does not follow" in f for f in failures)


def test_an_evaluator_disagreeing_with_the_mechanical_presence_check_fails_the_gate(
        pipeline_ctx, sample_cases, sample_scenarios, clean_cases, clean_scenarios):
    """The whole point of computing it twice: a mismatch is surfaced, not resolved silently."""
    ctx, engine = pipeline_ctx

    def broken_suite(run_dir):
        (run_dir / "qa").mkdir(parents=True, exist_ok=True)
        (run_dir / "qa" / "cases.json").write_text(json.dumps(sample_cases))
        (run_dir / "qa" / "scenarios.json").write_text(json.dumps(sample_scenarios))

    def claims_clean(run_dir):
        (run_dir / "qa" / "eval.json").write_text(
            json.dumps(eval_json(sample_cases, sample_scenarios)))

    engine.recipes["evaluate"] = claims_clean
    # run the two stages the evaluator needs, with generation's gate relaxed for this test
    run_pipeline(stages(["sources"]), ctx)
    broken_suite(ctx.run_dir)
    from workflows.testing.stages import evaluate as ev_stage

    ev_stage.STAGE.engine(ev_stage.STAGE, ctx)
    failures = ev_stage.STAGE.gate(ctx)
    mismatch = [f for f in failures if "presence check" in f]
    assert len(mismatch) == 2
    assert "the model reported PASS, Python computed FAIL" in mismatch[0]
    assert "TC-RSE-SLOT-FUNC-02" in mismatch[0]


def test_an_evaluator_failing_a_check_with_no_violations_fails_the_gate(pipeline_ctx, clean_cases,
                                                                       clean_scenarios):
    ctx, engine = pipeline_ctx

    def empty_violations(run_dir):
        data = eval_json(clean_cases, clean_scenarios)
        data["compliance_checks"]["no_scope_creep"] = {"status": "FAIL", "violations": []}
        data["compliance_status"] = "FAIL"
        (run_dir / "qa" / "eval.json").write_text(json.dumps(data))

    engine.recipes["evaluate"] = empty_violations
    with pytest.raises(GateFailure):
        run_pipeline(stages(), ctx)
    failures = manifest(ctx)["evaluate"]["gate"]["failures"]
    assert any("no_scope_creep is FAIL with no violations" in f for f in failures)


def test_an_evaluator_failing_a_check_without_naming_an_id_fails_the_gate(pipeline_ctx, clean_cases,
                                                                         clean_scenarios):
    ctx, engine = pipeline_ctx

    def vague_violations(run_dir):
        data = eval_json(clean_cases, clean_scenarios)
        data["compliance_checks"]["no_scope_creep"] = {"status": "FAIL",
                                                       "violations": ["some cases are out of scope"]}
        data["compliance_status"] = "FAIL"
        (run_dir / "qa" / "eval.json").write_text(json.dumps(data))

    engine.recipes["evaluate"] = vague_violations
    with pytest.raises(GateFailure):
        run_pipeline(stages(), ctx)
    assert any("no violation names a scenario or case id" in f
               for f in manifest(ctx)["evaluate"]["gate"]["failures"])


def test_a_compliance_status_that_contradicts_its_checks_fails_the_gate(pipeline_ctx, clean_cases,
                                                                        clean_scenarios):
    ctx, engine = pipeline_ctx

    def lying_status(run_dir):
        data = eval_json(clean_cases, clean_scenarios)
        data["compliance_checks"]["boundary_coverage"] = {
            "status": "FAIL", "violations": ["TC-RSE-SLOT-FUNC-01: no boundary case"]}
        (run_dir / "qa" / "eval.json").write_text(json.dumps(data))   # still says PASS

    engine.recipes["evaluate"] = lying_status
    with pytest.raises(GateFailure):
        run_pipeline(stages(), ctx)
    assert any("compliance_status='PASS'" in f and "imply FAIL" in f
               for f in manifest(ctx)["evaluate"]["gate"]["failures"])


def test_a_missing_source_stops_the_run_before_any_model_is_paid(tmp_path, ctx_factory, recipes):
    ctx = ctx_factory(tmp_path / "run", inputs={"feature": "RSE-1"}, engine=FakeEngine(recipes))
    with pytest.raises(GateFailure):
        run_pipeline(stages(), ctx)
    assert manifest(ctx)["sources"]["status"] == "gate_failed"
    assert "generate_cases" not in manifest(ctx)


# ---- --from-cases -------------------------------------------------------------------------------------
def test_from_cases_skips_the_model_but_not_the_gate(tmp_path, ctx_factory, recipes, clean_cases,
                                                     clean_scenarios):
    suite = tmp_path / "suite.json"
    suite.write_text(json.dumps({"cases": clean_cases, "scenarios": clean_scenarios}))
    src = tmp_path / "src"
    src.mkdir()
    (src / "prd.md").write_text("# PRD\n")
    (src / "rfc.md").write_text("# RFC\n")
    engine = FakeEngine(recipes)
    ctx = ctx_factory(tmp_path / "run",
                      inputs={"feature": "RSE-1", "prd": str(src / "prd.md"), "rfc": str(src / "rfc.md"),
                              "from_cases": str(suite)},
                      engine=engine)
    run_pipeline(stages(["sources", "generate_cases"]), ctx)
    assert engine.calls == []
    assert manifest(ctx)["generate_cases"]["engine"] == "python:from_cases"
    assert json.loads(ctx.path("qa/cases.json").read_text()) == clean_cases


def test_from_cases_reads_a_workbook_with_its_execution_column(tmp_path, ctx_factory, recipes,
                                                               clean_cases, clean_scenarios):
    from workflows.testing import load_from_cases, xlsx

    if not xlsx.available():
        pytest.skip("openpyxl not installed (optional extra `testing`)")
    book = tmp_path / "suite.xlsx"
    xlsx.write_workbook(book, clean_cases, clean_scenarios)
    cases, scenarios = load_from_cases(book)
    assert {c["execution"] for c in cases} == {"http", "ui", "manual"}
    assert [s["id"] for s in scenarios] == [s["id"] for s in clean_scenarios]


# ---- the workflow declaration -------------------------------------------------------------------------
def test_every_stages_requires_are_produced_by_an_earlier_stage():
    """Slot compatibility: the pipeline cannot be composed with a hole in it."""
    produced: set[str] = set()
    for name in (*WORKFLOW.default_pipeline, "execute_be"):
        stage = WORKFLOW.stage(name)
        missing = [r for r in stage.requires if r not in produced]
        assert missing == [], f"{name} requires {missing}, which nothing earlier produces"
        produced |= set(stage.outputs)


def test_the_workflow_declares_its_flags_and_decisions():
    from workflows.testing.stages import DECISIONS

    assert WORKFLOW.decisions == DECISIONS
    assert all(n.startswith("testing.") for n in WORKFLOW.flags)
    assert "testing.storage" in WORKFLOW.flags


def test_every_declared_flag_is_registered():
    from harness.flags import registry

    known = registry()
    for name in WORKFLOW.flags:
        assert name in known, name
        assert known[name].help, f"{name} has no help text"
        assert known[name].owner == "testing"


# ---- pipeline composition from the workflow's own flags -------------------------------------------------
def test_compose_pipeline_defaults_to_the_declared_default(ctx):
    from workflows.testing import compose_pipeline

    assert compose_pipeline(ctx.flags) == WORKFLOW.default_pipeline


@pytest.mark.parametrize("flags,want", [
    ({}, ("sources", "generate_cases", "render_xlsx", "evaluate", "store_cases", "derive_spec")),
    ({"testing.evaluate": False},
     ("sources", "generate_cases", "render_xlsx", "store_cases", "derive_spec")),
    ({"testing.execute": True},
     ("sources", "generate_cases", "render_xlsx", "evaluate", "store_cases", "derive_spec",
      "execute_be")),
    # no store -> nothing that reads from it, so the operator is told by the shape of the run
    ({"testing.store": False}, ("sources", "generate_cases", "render_xlsx", "evaluate")),
    ({"testing.store": False, "testing.execute": True},
     ("sources", "generate_cases", "render_xlsx", "evaluate")),
    ({"testing.evaluate": False, "testing.store": False},
     ("sources", "generate_cases", "render_xlsx")),
])
def test_compose_pipeline_honours_the_composition_flags(tmp_path, ctx_factory, flags, want):
    from workflows.testing import compose_pipeline

    ctx = ctx_factory(tmp_path / "run", flags=flags)
    assert compose_pipeline(ctx.flags) == want


def test_compose_pipeline_only_ever_names_registered_stages(tmp_path, ctx_factory):
    from workflows.testing import compose_pipeline

    ctx = ctx_factory(tmp_path / "run", flags={"testing.execute": True})
    known = {s.name for s in WORKFLOW.stages}
    assert set(compose_pipeline(ctx.flags)) <= known


def test_a_composed_pipeline_actually_runs(pipeline_ctx):
    """The flags and `--disable-stage` must end at the same place: a composed list is a valid pipeline."""
    from workflows.testing import compose_pipeline

    ctx, engine = pipeline_ctx
    names = compose_pipeline(ctx.flags)
    run_pipeline(stages(names), ctx)
    assert set(manifest(ctx)) == set(names)
