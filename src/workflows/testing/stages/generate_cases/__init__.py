"""`generate_cases` — the generator persona, headless, gated.

D14: the interactive skill becomes a stage. The prompt below is `run_chain.sh:131-178` re-expressed: same
source list, same contract-verbatim rule, same plan `verify:` treatment, same open-questions rule, same
execution/surface tagging paragraph — asking for JSON instead of an xlsx (D8), and with the storage call
removed because `store_cases` owns that now.

The gate is what test_agent never had. It runs the schema validators, then re-parses every `exec:http`
case's steps through `be_spec.parse_case` — the SAME parser the deriver will use later. WHY that matters:
in test_agent the http step grammar was written in the SKILL, restated in the shell prompt, and encoded a
third time as regexes in be_spec.py, and the three only agreed by luck. A case that does not parse now
fails generation instead of silently dropping out of the executable set weeks later.
"""

from __future__ import annotations

from pathlib import Path

from harness.contracts import RunContext, Stage, StageResult

from ... import be_spec, schema
from .. import feature_of, guidance_note, read_json, read_json_gated, run_engine_stage, write_json

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "qa-test-case-generator" / "SKILL.md"


def _build_prompt(ctx: RunContext) -> str:
    feature = feature_of(ctx)
    inv = read_json(ctx.path("qa/sources.json"), {}) or {}
    rows = {r["label"]: r for r in inv.get("sources", [])}
    present = [r for r in inv.get("sources", []) if r["status"] == "present"]
    missing = [r for r in inv.get("sources", []) if r["status"] != "present"]

    lines = [
        f"Produce an execution-ready QA suite for feature {feature}.",
        "",
        "Sources (read each file before generating):",
        "",
    ]
    lines += [f"{r['label']}: {r['path']}" for r in present]
    if missing:
        lines += [
            "",
            "Not available (report each in your Step 0 inventory with this status, and do not pretend "
            "otherwise):",
        ]
        lines += [f"{r['label']}: {r['status']}{' — ' + r['note'] if r['note'] else ''}" for r in missing]
    lines += [
        "",
        "The RFC is the grounded technical design produced from that PRD — use it for the implementation",
        "detail the PRD leaves out, and the PRD for intended behaviour.",
        "",
    ]
    if rows.get("API Contract", {}).get("status") == "present":
        lines += [
            "The API Contract is the authority on every route, request shape, response field, nullability",
            "rule and error/empty state. Copy routes and parameter names from it VERBATIM — never",
            "paraphrase a route, never reconstruct one from RFC prose or a code citation, and never invent",
            "query parameters. A case whose route is wrong is worse than a missing case: it runs, it goes",
            "green against the wrong endpoint, and nobody notices. If the contract does not cover an",
            "endpoint a case needs, say so in that case's qa_comment and tag it `manual` rather than",
            "guessing the path.",
            "",
        ]
    else:
        lines += [
            "There is NO API contract in this run. Do not reconstruct routes from RFC prose or code",
            "citations: tag any case that would need a route you cannot copy verbatim as `manual` and say",
            "why in its qa_comment. Measured on TASK-29354-V2 without a contract: 1 of 3 generated BE",
            "cases named a route that actually works.",
            "",
        ]
    if rows.get("Implementation Plan", {}).get("status") == "present":
        lines += [
            "The Implementation Plan's CHANGE blocks carry a `verify:` line each — that is the author's own",
            "statement of how the change should be proven. Treat those as required coverage, not",
            "suggestions.",
            "",
        ]
    lines += [
        'Treat anything listed in the RFC under "Open questions" as an explicit coverage gap: do NOT',
        "invent expected behaviour for it. Note it in qa_comment instead, and write a scenario that",
        "documents the actual behaviour.",
        "",
        "Every case MUST carry both `execution` (http|ui|manual) and `surface` (backend|android|ios|web),",
        'per the skill\'s "Execution & Surface tagging" section. These become the store tags',
        "`exec:<execution>` and `surface:<surface>`, and the BE executor runs ONLY `exec:http` cases — an",
        "untagged case never runs and nobody notices. Tag `http` only where one request against the",
        "deployed service, asserting on that single response, is enough to verify the case.",
        "",
        "Write exactly two files, per the skill's 'Harness output contract' section:",
        "",
        f"  {ctx.path('qa/cases.json')}",
        f"  {ctx.path('qa/scenarios.json')}",
        "",
        "Do not build a workbook and do not write anywhere else — the run dir is the single audit trail",
        "for this feature. A deterministic gate reads both files the moment you finish; every rule in that",
        "contract is checked mechanically, including that each `execution: http` case's steps parse under",
        "the http step grammar.",
        "",
        "In your final message, report the Step 0 resource inventory table, the scenario/case counts by",
        "module, and how many cases you tagged `execution: http`.",
    ]
    return "\n".join(lines) + guidance_note(ctx, "generate_cases")


def _engine(stage: Stage, ctx: RunContext):
    """`--from-cases` short-circuits the model; everything else still gates the suite.

    test_agent had no such path: a failure anywhere after generation meant regenerating the whole suite
    from scratch, with new model output and possibly drifted ids (survey §10.4). An existing suite —
    hand-edited, or carried over from a previous run — is loaded and then held to exactly the same gate.
    """
    import time

    from ... import load_from_cases

    src = ctx.inputs.get("from_cases")
    if src:
        t0 = time.monotonic()
        cases, scenarios = load_from_cases(src)
        write_json(ctx.path("qa/cases.json"), cases)
        write_json(ctx.path("qa/scenarios.json"), scenarios)
        return StageResult(
            final_text=f"loaded {len(cases)} cases and {len(scenarios)} scenarios from {src}",
            duration_s=time.monotonic() - t0, engine_desc="python:from_cases")
    return run_engine_stage(stage, ctx, SKILL_PATH)


def _gate(ctx: RunContext) -> list[str]:
    cases, bad = read_json_gated(ctx.path("qa/cases.json"), expect=list)
    if bad:
        return bad
    scenarios, bad = read_json_gated(ctx.path("qa/scenarios.json"), expect=list)
    if bad:
        return bad
    failures = schema.validate_suite(
        cases, scenarios,
        require_case_per_scenario=bool(ctx.flag("testing.require_case_per_scenario", True)),
    )
    # The http grammar check: the same parser the deriver runs, so "executable" is decided once.
    for c in schema.http_cases(cases):
        entry, why = be_spec.parse_case(str(c.get("id") or "<no id>"), schema.render_text(c))
        if entry is None:
            failures.append(f"{c.get('id')}: tagged execution=http but its steps do not parse — {why}. "
                            f"Either restate the steps in the required Send/Assert forms or retag the "
                            f"case ui/manual.")
            continue
        if "expect_status" not in entry:
            failures.append(f"{c.get('id')}: tagged execution=http but no `Assert status == <code>` step — "
                            f"the status is always stated, never assumed.")
    return failures


STAGE = Stage(
    name="generate_cases",
    requires=("qa/sources.json",),
    outputs=("qa/cases.json", "qa/scenarios.json"),
    build_prompt=_build_prompt,
    gate=_gate,
    engine=_engine,
    retry_on_gate_failure=1,
    revise_on_retry=True,
)
