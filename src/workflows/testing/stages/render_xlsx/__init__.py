"""`render_xlsx` — the workbook, derived.

D8/D10: the xlsx is a render of cases.json, and openpyxl is the optional extra `testing` rather than a
runtime dependency. When the extra is absent the stage records a decision and skips, because a missing
spreadsheet renderer must not stop a suite from being generated, evaluated and stored.

The file name is `qa/<FEATURE>_QA_Suite.xlsx` — the same shape run_chain.sh:162 pinned, with the same
space->underscore substitution, so an existing consumer keeps finding it.
"""

from __future__ import annotations

from harness.contracts import RunContext, Stage

from ... import xlsx
from .. import feature_of, python_stage, read_json


def workbook_rel(ctx: RunContext) -> str:
    """Run-dir-relative path of the rendered workbook."""
    return f"qa/{feature_of(ctx).replace(' ', '_')}_QA_Suite.xlsx"


@python_stage
def _run(stage: Stage, ctx: RunContext) -> str:
    from rfc_agent import decisions

    if not ctx.flag("testing.xlsx", True):
        return "testing.xlsx is off — no workbook rendered"
    if not xlsx.available():
        decisions.record(ctx, stage.name, "xlsx_skipped",
                         why="openpyxl is not installed (optional extra `testing`), so the derived "
                             "workbook was skipped. cases.json is the canonical artifact; the render "
                             "adds nothing a gate depends on.",
                         fix="uv sync --extra testing")
        return "openpyxl absent — workbook skipped (cases.json is canonical)"
    cases = read_json(ctx.path("qa/cases.json"), []) or []
    scenarios = read_json(ctx.path("qa/scenarios.json"), []) or []
    out = xlsx.write_workbook(ctx.path(workbook_rel(ctx)), cases, scenarios)
    return f"wrote {out.name} ({len(scenarios)} scenarios, {len(cases)} cases)"


def _gate(ctx: RunContext) -> list[str]:
    if not ctx.flag("testing.xlsx", True) or not xlsx.available():
        return []  # deliberately skipped — the decision record is the evidence
    path = ctx.path(workbook_rel(ctx))
    if not path.exists():
        return [f"declared render missing: {workbook_rel(ctx)}"]
    sheets = xlsx.read_sheets(path)
    out = []
    for want in (xlsx.SCENARIO_SHEET, xlsx.CASE_SHEET):
        if want not in sheets:
            out.append(f"{workbook_rel(ctx)}: no {want!r} sheet (has {sorted(sheets)})")
    if xlsx.CASE_SHEET in sheets and sheets[xlsx.CASE_SHEET]:
        header = [h.strip() for h in sheets[xlsx.CASE_SHEET][0]]
        expected = [h for h, _ in xlsx.CASE_COLUMNS]
        if header != expected:
            out.append(f"{xlsx.CASE_SHEET} header is {header} — expected {expected}")
    if xlsx.SCENARIO_SHEET in sheets and sheets[xlsx.SCENARIO_SHEET]:
        header = [h.strip() for h in sheets[xlsx.SCENARIO_SHEET][0]]
        expected = [h for h, _ in xlsx.SCENARIO_COLUMNS]
        if header != expected:
            out.append(f"{xlsx.SCENARIO_SHEET} header is {header} — expected {expected}")
    cases = read_json(ctx.path("qa/cases.json"), []) or []
    if xlsx.CASE_SHEET in sheets and len(sheets[xlsx.CASE_SHEET]) - 1 != len(cases):
        out.append(f"{xlsx.CASE_SHEET} has {len(sheets[xlsx.CASE_SHEET]) - 1} data rows but cases.json "
                   f"has {len(cases)} cases — the render is not a faithful projection")
    return out


STAGE = Stage(
    name="render_xlsx",
    requires=("qa/cases.json", "qa/scenarios.json"),
    # The workbook is conditional (flag + optional dep), so it is NOT a declared output: the runner
    # treats a missing declared output as a hard failure, and a skipped render is not a failure.
    outputs=(),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_run,
)
