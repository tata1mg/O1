"""`prerequisites` — what has to be on prestag before the suite can run, as a checklist and a data template.

Two halves, the same split as `evaluate`. Python first writes `qa/prerequisites.facts.json` (see
`..prereqs.facts`): services, deploy waves, migrations and staging smoke checks from the plan's CHANGE blocks,
and per case the services its REQs touch plus its placeholders and invented values. Then a model reads the
suite and writes `qa/prerequisites.json`: the fixtures, config values, external systems and secrets the
cases need, each citing the case ids that need it. The gate (`..prereqs.validate`) checks every case is
accounted for, every cited id exists, a case carrying a placeholder or invented value is never declared
prerequisite-free, and no secret carries a value. Python then renders `qa/prerequisites.md` and
`qa/test_data.template.yaml`; the template, filled, is the `--test-data` source of the next run.

WHY a stage of its own rather than more output from `generate_cases`: that stage is already the run's
longest (19 min on NR-DIGICHEM, mostly reasoning), and a prerequisites gate failure there would re-run the
whole generation. It runs after `store_cases` so a failure here never blocks the upload, and it reads the
final suite.
"""

from __future__ import annotations

import sys
from pathlib import Path

from harness.contracts import RunContext, Stage

from ... import prereqs
from .. import feature_of, guidance_note, read_json, read_json_gated, run_engine_stage, write_json

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "qa-test-prerequisites" / "SKILL.md"
CHECK_PREREQS = f"{sys.executable} {Path(__file__).resolve().parents[2] / 'check_prerequisites.py'}"

FACTS_OUT = "qa/prerequisites.facts.json"
DOC_OUT = "qa/prerequisites.json"
MD_OUT = "qa/prerequisites.md"
TEMPLATE_OUT = "qa/test_data.template.yaml"


def _sources(ctx: RunContext) -> dict[str, dict]:
    inv = read_json(ctx.path("qa/sources.json"), {}) or {}
    return {r["key"]: r for r in inv.get("sources", []) if r.get("status") == "present"}


def _facts(ctx: RunContext) -> dict:
    cases = read_json(ctx.path("qa/cases.json"), []) or []
    plan = _sources(ctx).get("plan", {})
    path = plan.get("local") or plan.get("path")
    text = Path(path).read_text(encoding="utf-8", errors="replace") if path and Path(path).is_file() else ""
    return prereqs.facts(cases, text)


def _build_prompt(ctx: RunContext) -> str:
    src = _sources(ctx)
    facts = read_json(ctx.path(FACTS_OUT), {}) or {}
    cases = facts.get("cases", [])
    flagged = sum(1 for r in cases if r["placeholders"] or r["invented"])
    lines = [
        f"List everything that must exist on prestag before the QA suite for feature {feature_of(ctx)} "
        f"can run.",
        "",
        "The suite (read every case's precondition, steps and test_data):",
        "",
        f"  {ctx.path('qa/cases.json')}  ({len(cases)} cases; {flagged} carry a placeholder or a value "
        f"the generator marked as invented)",
        "",
        "Already computed in Python — read it, do NOT repeat it (services, deploy order, migrations and",
        "smoke checks are rendered from it directly):",
        "",
        f"  {ctx.path(FACTS_OUT)}",
        "",
        "Sources, for how each record is created and which config each service reads:",
        "",
    ]
    for key, label in (("plan", "Implementation Plan"), ("contract", "API Contract"),
                       ("test_data", "Test data the team supplied")):
        if key in src:
            lines.append(f"  {label}: {src[key].get('local') or src[key]['path']}")
    if "test_data" in src:
        lines += ["", "The team supplied real test data: where it covers a fixture, put its values in that",
                  "fixture's `suite_values` and say so in `how_to_create`."]
    lines += [
        "",
        "Write exactly one file, per the skill's 'Harness output contract':",
        "",
        f"  {ctx.path(DOC_OUT)}",
        "",
        "To check it, run this ONE command after writing it (and after any fix). It is the stage's gate:",
        "",
        f"  {CHECK_PREREQS}",
        "",
        "Do not hand-roll jq/grep checks over the suite: each costs a full turn and compound shell forms",
        "are denied in this sandbox. Do not write the markdown or the YAML template — Python renders both.",
        "",
        "In your final message, report the counts per section and the three prerequisites most likely to",
        "block a first run.",
    ]
    return "\n".join(lines) + guidance_note(ctx, "prerequisites")


def _engine(stage: Stage, ctx: RunContext):
    # Facts first, in this process: the prompt quotes them and the gate needs them on disk.
    facts = _facts(ctx)
    write_json(ctx.path(FACTS_OUT), facts)
    result = run_engine_stage(stage, ctx, SKILL_PATH)
    _render(ctx, facts)
    return result


def _render(ctx: RunContext, facts: dict) -> None:
    """Render whatever the model wrote — also after a gate failure, so a revision edits the JSON with the
    outputs on disk instead of starting over. A file that is not JSON renders as empty sections."""
    from rfc_agent import decisions

    doc = read_json(ctx.path(DOC_OUT), {})
    doc = doc if isinstance(doc, dict) else {}
    feature = feature_of(ctx)
    ctx.path(MD_OUT).write_text(prereqs.render_md(feature, facts, doc), encoding="utf-8")
    ctx.path(TEMPLATE_OUT).write_text(prereqs.render_template(feature, doc), encoding="utf-8")
    decisions.record(ctx, "prerequisites", "prerequisites_listed",
                     why="what prestag needs before the suite can run, for the team to provision and fill",
                     services=len(facts.get("services", [])),
                     **{s: len(doc.get(s) or []) for s in prereqs.SECTIONS})


def _gate(ctx: RunContext) -> list[str]:
    doc, bad = read_json_gated(ctx.path(DOC_OUT), expect=dict)
    if bad:
        return bad
    facts, bad = read_json_gated(ctx.path(FACTS_OUT), expect=dict)
    if bad:
        return bad
    cases = read_json(ctx.path("qa/cases.json"), []) or []
    out = prereqs.validate(doc, cases, facts)
    out += [f"{rel} was not rendered" for rel in (MD_OUT, TEMPLATE_OUT) if not ctx.path(rel).exists()]
    return out


STAGE = Stage(
    name="prerequisites",
    requires=("qa/cases.json", "qa/sources.json"),
    outputs=(FACTS_OUT, DOC_OUT, MD_OUT, TEMPLATE_OUT),
    build_prompt=_build_prompt,
    gate=_gate,
    engine=_engine,
    retry_on_gate_failure=1,
    revise_on_retry=True,
    allow_commands=(CHECK_PREREQS,),
)
