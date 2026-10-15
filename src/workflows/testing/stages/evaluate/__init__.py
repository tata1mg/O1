"""`evaluate` — the evaluator persona, plus the deterministic half it can no longer skip.

Two things happen here. First, Python computes `qa/eval.mechanical.json` (see `..mechanical`). Then the
evaluator SKILL runs as an engine stage and writes `qa/eval.json` in its own fixed schema. The gate checks
the model's output for schema validity, re-derives the aggregate arithmetic, demands a concrete violation
behind every FAIL, and compares the model's `presence_checks` verdicts against the ones Python computed —
a mismatch fails the stage and shows both, because one of the two is wrong and which one matters.

WHY: the evaluator is a stage so its verdict is gated, not just reported. The acceptance list in the evaluator
skill is the source of the gate below.
"""

from __future__ import annotations

from pathlib import Path

from harness.contracts import RunContext, Stage

from ... import mechanical
from .. import feature_of, guidance_note, read_json, read_json_gated, run_engine_stage, write_json

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "qa-test-case-evaluator" / "SKILL.md"

MECHANICAL_OUT = "qa/eval.mechanical.json"
EVAL_OUT = "qa/eval.json"

#: The evaluator's own JSON keys (SKILL.md:95-134) — the gate checks the shape, not the values.
DIMENSIONS: tuple[str, ...] = (
    "requirement_coverage", "happy_path_coverage", "negative_scenario_coverage", "edge_case_coverage",
    "boundary_value_coverage", "expected_result_quality", "test_case_clarity", "non_duplication",
)
COVERAGE_DIMENSIONS = DIMENSIONS[:5]
QUALITY_DIMENSIONS = DIMENSIONS[5:]
COMPLIANCE_CHECKS: tuple[str, ...] = (
    "traceable_requirement_reference", "structural_completeness", "explicit_verifiable_expected_results",
    "surface_coverage", "positive_negative_pairing", "boundary_coverage", "no_scope_creep",
    "duplicate_consolidation", "error_message_response_coverage", "integration_point_coverage",
    "test_data_consistency", "deterministic_pass_fail", "precondition_dependency_documentation",
    "regression_coverage_identified",
)
PRESENCE_CHECKS: tuple[str, ...] = ("expected_result", "precondition")
#: Aggregates are stated to one decimal, so the recomputation is compared at that tolerance.
SCORE_TOLERANCE = 0.05


def _mechanical(ctx: RunContext) -> dict:
    cases = read_json(ctx.path("qa/cases.json"), []) or []
    scenarios = read_json(ctx.path("qa/scenarios.json"), []) or []
    return mechanical.evaluate(
        cases, scenarios,
        require_case_per_scenario=bool(ctx.flag("testing.require_case_per_scenario", True)),
    )


def _build_prompt(ctx: RunContext) -> str:
    inv = read_json(ctx.path("qa/sources.json"), {}) or {}
    present = [r for r in inv.get("sources", []) if r["status"] == "present"]
    mech = read_json(ctx.path(MECHANICAL_OUT), {}) or {}
    counts = mech.get("counts", {})
    lines = [
        f"Audit the generated QA suite for feature {feature_of(ctx)} against its source requirements.",
        "",
        "Requirement sources (read each):",
        "",
    ]
    lines += [f"{r['label']}: {r['path']}" for r in present]
    lines += [
        "",
        "The suite under review:",
        "",
        f"  {ctx.path('qa/cases.json')}      ({counts.get('cases', '?')} cases, "
        f"{counts.get('http_cases', '?')} tagged execution=http)",
        f"  {ctx.path('qa/scenarios.json')}  ({counts.get('scenarios', '?')} scenarios across "
        f"{counts.get('modules', '?')} modules)",
        "",
        "Write your scorecard to:",
        "",
        f"  {ctx.path(EVAL_OUT)}",
        "",
        "Per the skill's 'Harness output contract': that file holds the JSON object and nothing else.",
        "Show the aggregate arithmetic in your final chat message — the harness recomputes it and fails",
        "the stage if the numbers in the file do not follow from your dimension scores.",
        "",
        "The Deterministic Eval (Presence Check) has also been computed in Python, independently, at:",
        "",
        f"  {ctx.path(MECHANICAL_OUT)}",
        "",
        "Do NOT read it before forming your own verdict, and do not copy it: the two are compared, and a",
        "disagreement fails the stage so a human can see which one is right. Run the sweep yourself over",
        "every row of both files.",
    ]
    return "\n".join(lines) + guidance_note(ctx, "evaluate")


def _engine(stage: Stage, ctx: RunContext):
    # The mechanical half runs FIRST, in this process: the prompt quotes its counts, and the gate needs
    # it on disk whether or not the model stage succeeds.
    write_json(ctx.path(MECHANICAL_OUT), _mechanical(ctx))
    return run_engine_stage(stage, ctx, SKILL_PATH)


def _mean(values: list[float]) -> float:
    """The UNROUNDED mean. The gate compares against this within half a decimal step, because a mean of
    14/3 = 4.6667 is legitimately reported as 4.7 and a mean of exactly 4.05 as either 4.0 or 4.1 —
    penalising the model for its choice of tie-break would be a gate that fails on nothing real."""
    return sum(values) / len(values) if values else 0.0


def _gate(ctx: RunContext) -> list[str]:
    out: list[str] = []
    ev, bad = read_json_gated(ctx.path(EVAL_OUT), expect=dict)
    if bad:
        return bad

    # ---- schema -----------------------------------------------------------------------------------
    dims = ev.get("dimension_scores")
    if not isinstance(dims, dict):
        out.append("dimension_scores is missing or not an object")
        dims = {}
    for name in DIMENSIONS:
        if name not in dims:
            out.append(f"dimension_scores.{name} is missing")
        elif not isinstance(dims[name], (int, float)) or not 0 <= dims[name] <= 5:
            out.append(f"dimension_scores.{name}={dims[name]!r} is not a 0-5 score")
    checks = ev.get("compliance_checks")
    if not isinstance(checks, dict):
        out.append("compliance_checks is missing or not an object")
        checks = {}
    presence = ev.get("presence_checks")
    if not isinstance(presence, dict):
        out.append("presence_checks is missing or not an object")
        presence = {}
    for name in COMPLIANCE_CHECKS:
        if name not in checks:
            out.append(f"compliance_checks.{name} is missing")
    for name in PRESENCE_CHECKS:
        if name not in presence:
            out.append(f"presence_checks.{name} is missing")

    # ---- every FAIL names a case ------------------------------------------------------------------
    known_ids = {str(c.get("id")) for c in (read_json(ctx.path("qa/cases.json"), []) or [])}
    known_ids |= {str(s.get("id")) for s in (read_json(ctx.path("qa/scenarios.json"), []) or [])}
    for group, name, verdict in [("compliance_checks", n, checks.get(n)) for n in COMPLIANCE_CHECKS] + \
                                [("presence_checks", n, presence.get(n)) for n in PRESENCE_CHECKS]:
        if not isinstance(verdict, dict):
            continue
        status = str(verdict.get("status") or "").upper()
        if status not in ("PASS", "FAIL"):
            out.append(f"{group}.{name}.status={verdict.get('status')!r} is not PASS or FAIL")
            continue
        violations = verdict.get("violations")
        if status == "FAIL":
            if not isinstance(violations, list) or not violations:
                out.append(f"{group}.{name} is FAIL with no violations — an empty violations array on a "
                           f"failed check is not acceptable (SKILL.md:138)")
            elif not any(any(i in str(v) for i in known_ids) for v in violations):
                out.append(f"{group}.{name} is FAIL but no violation names a scenario or case id from the "
                           f"suite — name the offending ids")

    # ---- the arithmetic ---------------------------------------------------------------------------
    if all(isinstance(dims.get(n), (int, float)) for n in DIMENSIONS):
        want_cov = _mean([float(dims[n]) for n in COVERAGE_DIMENSIONS])
        want_qual = _mean([float(dims[n]) for n in QUALITY_DIMENSIONS])
        # overall is the mean of the two REPORTED (1-decimal) aggregates, per the SKILL's own wording.
        want_overall = _mean([round(want_cov, 1), round(want_qual, 1)])
        for key, want in (("coverage_score", want_cov), ("quality_score", want_qual),
                          ("overall_score", want_overall)):
            got = ev.get(key)
            if not isinstance(got, (int, float)):
                out.append(f"{key}={got!r} is not a number")
            elif abs(float(got) - want) > SCORE_TOLERANCE + 1e-9:
                out.append(f"{key}={got} does not follow from the dimension scores — "
                           f"recomputed {round(want, 1)}")

    # ---- compliance_status is derived, not asserted -----------------------------------------------
    verdicts = [v.get("status") for v in list(checks.values()) + list(presence.values())
                if isinstance(v, dict)]
    want_status = "PASS" if verdicts and all(str(v).upper() == "PASS" for v in verdicts) else "FAIL"
    if str(ev.get("compliance_status") or "").upper() != want_status:
        out.append(f"compliance_status={ev.get('compliance_status')!r} but the 14 compliance checks and "
                   f"2 presence checks imply {want_status}")

    # ---- the model's presence verdict vs the mechanical one ---------------------------------------
    mech = read_json(ctx.path(MECHANICAL_OUT))
    if not isinstance(mech, dict):
        out.append(f"{MECHANICAL_OUT} is missing — the deterministic half did not run")
    else:
        for name in PRESENCE_CHECKS:
            mine = (mech.get("presence_checks", {}).get(name) or {})
            theirs = presence.get(name) if isinstance(presence.get(name), dict) else {}
            m_status = str(mine.get("status") or "").upper()
            t_status = str(theirs.get("status") or "").upper()
            if m_status and t_status and m_status != t_status:
                out.append(
                    f"presence check {name!r}: the model reported {t_status}, Python computed {m_status}. "
                    f"Python's violations: {list(mine.get('violations') or [])[:5]}. "
                    f"The model's: {list(theirs.get('violations') or [])[:5]}. "
                    f"One of the two is wrong — re-run the mechanical sweep row by row."
                )
    return out


STAGE = Stage(
    name="evaluate",
    requires=("qa/cases.json", "qa/scenarios.json", "qa/sources.json"),
    outputs=(MECHANICAL_OUT, EVAL_OUT),
    build_prompt=_build_prompt,
    gate=_gate,
    engine=_engine,
    retry_on_gate_failure=1,
    revise_on_retry=True,
)
