"""The `evaluate` gate's schema half, driven directly.

The pipeline tests cover the arithmetic, the presence-check comparison and the violation rules end to end;
these pin the shape checks, which are what turn the evaluator SKILL's "fixed JSON schema" (SKILL.md:85-136)
into something a run can rely on.
"""

from __future__ import annotations

import json

import pytest

from workflows.testing.stages import evaluate as ev
from workflows.testing.stages import write_json


def scorecard(**over) -> dict:
    ok = {"status": "PASS", "violations": []}
    data = {
        "overall_score": 4.0, "coverage_score": 4.0, "quality_score": 4.0,
        "compliance_status": "PASS",
        "presence_checks": {n: dict(ok) for n in ev.PRESENCE_CHECKS},
        "dimension_scores": dict.fromkeys(ev.DIMENSIONS, 4),
        "compliance_checks": {n: dict(ok) for n in ev.COMPLIANCE_CHECKS},
        "missing_scenarios": [], "incorrect_cases": [], "duplicate_cases": [], "recommendations": [],
    }
    data.update(over)
    return data


@pytest.fixture
def eval_ctx(sourced_ctx):
    """A run dir with a clean suite and the mechanical half already computed."""
    write_json(sourced_ctx.path(ev.MECHANICAL_OUT), ev._mechanical(sourced_ctx))
    return sourced_ctx


def gate(ctx, data) -> list[str]:
    ctx.path(ev.EVAL_OUT).parent.mkdir(parents=True, exist_ok=True)
    ctx.path(ev.EVAL_OUT).write_text(json.dumps(data) if not isinstance(data, str) else data)
    return ev.STAGE.gate(ctx)


def test_a_well_formed_scorecard_passes(eval_ctx):
    assert gate(eval_ctx, scorecard()) == []


def test_a_markdown_fence_is_rejected_with_the_fix(eval_ctx):
    failures = gate(eval_ctx, "```json\n{}\n```")
    assert len(failures) == 1 and "no markdown fence" in failures[0]


@pytest.mark.parametrize("dropped", ev.DIMENSIONS)
def test_every_dimension_must_be_present(eval_ctx, dropped):
    dims = {k: v for k, v in dict.fromkeys(ev.DIMENSIONS, 4).items() if k != dropped}
    failures = gate(eval_ctx, scorecard(dimension_scores=dims))
    assert f"dimension_scores.{dropped} is missing" in failures


@pytest.mark.parametrize("bad", [6, -1, "four", None])
def test_a_dimension_outside_0_to_5_is_rejected(eval_ctx, bad):
    dims = {**dict.fromkeys(ev.DIMENSIONS, 4), "requirement_coverage": bad}
    failures = gate(eval_ctx, scorecard(dimension_scores=dims))
    assert any("is not a 0-5 score" in f for f in failures)


def test_every_one_of_the_fourteen_compliance_checks_must_be_present(eval_ctx):
    checks = {n: {"status": "PASS", "violations": []} for n in ev.COMPLIANCE_CHECKS[:-1]}
    failures = gate(eval_ctx, scorecard(compliance_checks=checks))
    assert f"compliance_checks.{ev.COMPLIANCE_CHECKS[-1]} is missing" in failures
    assert len(ev.COMPLIANCE_CHECKS) == 14


def test_both_presence_checks_must_be_present(eval_ctx):
    failures = gate(eval_ctx, scorecard(presence_checks={"expected_result": {"status": "PASS",
                                                                             "violations": []}}))
    assert "presence_checks.precondition is missing" in failures


def test_a_status_that_is_neither_pass_nor_fail_is_rejected(eval_ctx):
    checks = {**{n: {"status": "PASS", "violations": []} for n in ev.COMPLIANCE_CHECKS},
              "no_scope_creep": {"status": "N/A", "violations": []}}
    failures = gate(eval_ctx, scorecard(compliance_checks=checks))
    assert any("no_scope_creep.status='N/A' is not PASS or FAIL" in f for f in failures)


def test_a_missing_dimension_object_is_named(eval_ctx):
    failures = gate(eval_ctx, scorecard(dimension_scores="four out of five"))
    assert "dimension_scores is missing or not an object" in failures


def test_the_arithmetic_is_recomputed_from_the_dimensions(eval_ctx):
    """coverage = mean(1-5), quality = mean(6-8), overall = mean of those two."""
    dims = {"requirement_coverage": 5, "happy_path_coverage": 5, "negative_scenario_coverage": 4,
            "edge_case_coverage": 3, "boundary_value_coverage": 0,
            "expected_result_quality": 4, "test_case_clarity": 5, "non_duplication": 5}
    good = scorecard(dimension_scores=dims, coverage_score=3.4, quality_score=4.7, overall_score=4.1)
    assert gate(eval_ctx, good) == []
    bad = scorecard(dimension_scores=dims, coverage_score=4.4, quality_score=4.7, overall_score=4.1)
    failures = gate(eval_ctx, bad)
    assert any("coverage_score=4.4 does not follow" in f and "recomputed 3.4" in f for f in failures)


def test_either_tie_break_of_a_half_step_is_accepted(eval_ctx):
    """quality is 14/3 = 4.6667 and overall (3.4 + 4.7) / 2 = 4.05 — both 4.0 and 4.1 are defensible
    roundings, and a gate that failed on the tie-break would be failing on nothing real."""
    dims = {"requirement_coverage": 5, "happy_path_coverage": 5, "negative_scenario_coverage": 4,
            "edge_case_coverage": 3, "boundary_value_coverage": 0,
            "expected_result_quality": 4, "test_case_clarity": 5, "non_duplication": 5}
    for overall in (4.0, 4.1):
        card = scorecard(dimension_scores=dims, coverage_score=3.4, quality_score=4.7,
                         overall_score=overall)
        assert gate(eval_ctx, card) == [], overall
    off = scorecard(dimension_scores=dims, coverage_score=3.4, quality_score=4.6, overall_score=4.0)
    assert any("quality_score=4.6 does not follow" in f for f in gate(eval_ctx, off))


def test_a_zero_score_is_legitimate_and_not_an_error(eval_ctx):
    """"Treat 0 as a legitimate, expected score for a dimension that plainly doesn't apply"."""
    dims = dict.fromkeys(ev.DIMENSIONS, 0)
    assert gate(eval_ctx, scorecard(dimension_scores=dims, coverage_score=0.0, quality_score=0.0,
                                    overall_score=0.0)) == []


def test_a_missing_mechanical_file_is_reported(eval_ctx):
    eval_ctx.path(ev.MECHANICAL_OUT).unlink()
    failures = gate(eval_ctx, scorecard())
    assert any("the deterministic half did not run" in f for f in failures)


def test_the_stage_writes_the_mechanical_half_before_calling_the_engine(sourced_ctx):
    """The prompt quotes its counts, and the gate needs it whether or not the model stage succeeds."""
    class Boom:
        def run(self, req):
            raise RuntimeError("engine exploded")

    sourced_ctx.engine = Boom()
    with pytest.raises(RuntimeError, match="engine exploded"):
        ev.STAGE.engine(ev.STAGE, sourced_ctx)
    mech = json.loads(sourced_ctx.path(ev.MECHANICAL_OUT).read_text())
    assert mech["computed_by"] == "python" and mech["compliance_status"] == "PASS"


def test_the_prompt_tells_the_model_not_to_copy_the_mechanical_file(sourced_ctx):
    write_json(sourced_ctx.path(ev.MECHANICAL_OUT), ev._mechanical(sourced_ctx))
    prompt = ev.STAGE.build_prompt(sourced_ctx)
    assert "do not copy it" in prompt
    assert "qa/eval.json" in prompt
    assert "6 cases" in prompt   # the counts the mechanical half computed
