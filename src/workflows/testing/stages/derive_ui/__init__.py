"""`derive_ui` — Playwright tests for the CONFIRMED web UI cases, locators cited from the front-end source.

The UI counterpart of `derive_spec`, and an engine stage where that one is Python: an http case's steps are
a grammar a parser can transcribe, a UI case's are prose ("Assert the OneVoiceButton element is visible")
that has to be resolved against the code that renders it. So Python does the parts that are not judgement
— which cases (CONFIRMED, `exec:ui`, `surface:web`, read from the store, exactly as the run will attach
them), which source trees (the RFC run's repo snapshots under `_snap/`, or `testing.ui_source_roots`) —
and a model writes one test per case. The gate (`..ui_web.validate`) holds it to: every case tested or
skipped with a reason, one test per spec titled with its case id, no hosts or credentials in a spec, every
locator citing a file and line that exist in the snapshot, and Playwright loading every spec.

No CONFIRMED web UI case -> no model call: the plan is written empty and the stage says so.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from harness.contracts import RunContext, Stage, StageResult

from ... import ui_web
from ...store import get_store
from .. import feature_of, guidance_note, only_cases, read_json, read_json_gated, run_engine_stage, write_json

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "qa-ui-playwright" / "SKILL.md"
CHECK_UI = f"{sys.executable} {Path(__file__).resolve().parents[2] / 'check_ui.py'}"

CASES_OUT = f"{ui_web.UI_DIR}/{ui_web.CASES}"
ROOTS_OUT = f"{ui_web.UI_DIR}/roots.json"
PLAN_OUT = f"{ui_web.UI_DIR}/{ui_web.PLAN}"
SETTINGS_OUT = f"{ui_web.UI_DIR}/{ui_web.SETTINGS}"
#: The tags a web UI case carries in the store (schema.case_tags).
WEB_TAGS = ("exec:ui", "surface:web")


def login_required(ctx: RunContext) -> bool:
    """The run logs in with a test account (`--ui-login`), so the plan must carry the login step."""
    return bool(ctx.inputs.get("ui_login") or ctx.flag("testing.ui_login", ""))


def source_roots(ctx: RunContext) -> list[Path]:
    """`testing.ui_source_roots` (comma-separated), else every repo snapshot of the RFC run."""
    explicit = str(ctx.flag("testing.ui_source_roots", "") or "")
    if explicit:
        return [Path(p.strip()).expanduser() for p in explicit.split(",") if p.strip()]
    inv = read_json(ctx.path("qa/sources.json"), {}) or {}
    rfc_run = inv.get("rfc_run_dir") or ctx.inputs.get("rfc_run_dir")
    snap = Path(str(rfc_run)).expanduser() / "_snap" if rfc_run else None
    return sorted(p for p in snap.iterdir() if p.is_dir()) if snap and snap.is_dir() else []


def _build_prompt(ctx: RunContext) -> str:
    cases = read_json(ctx.path(CASES_OUT), []) or []
    roots = read_json(ctx.path(ROOTS_OUT), []) or []
    only = (read_json(ctx.path(SETTINGS_OUT), {}) or {}).get("only") or []
    lines = [
        f"Write Playwright tests for the {len(cases)} CONFIRMED web UI case(s) of feature {feature_of(ctx)}.",
        "",
        f"The cases (precondition, steps, test data, expected result): {ctx.path(CASES_OUT)}",
        "",
        "The front-end source to find routes and locators in (read-only):",
        "",
        *[f"  {r}" for r in roots],
        "",
        "Write, per the skill's 'Harness output contract':",
        "",
        f"  {ctx.path(ui_web.UI_DIR) / ui_web.TESTS_DIR}/<extid>.spec.ts   one test per case",
        f"  {ctx.path(PLAN_OUT)}   every case in `tests` (with cited locators) or `skipped` (with a reason)",
        "",
    ]
    if login_required(ctx):
        lines += [
            "This run logs in with a test account (mobile number + OTP). Before the cases, write the login step:",
            "",
            f"  {ctx.path(ui_web.UI_DIR) / ui_web.TESTS_DIR}/{ui_web.LOGIN_SPEC}   one test titled "
            f"'{ui_web.LOGIN_TITLE}'",
            "",
            "Find the login route and the number / OTP / submit elements in the source, as for the cases. Read",
            f"the number from process.env.{ui_web.LOGIN_ENV[0]} and the OTP from process.env.{ui_web.LOGIN_ENV[1]}",
            "(never write either), wait until the page shows a logged-in state, then",
            "`await page.context().storageState({ path: process.env.STORAGE_STATE })`. Describe it in plan.json",
            "under `login` (route + cited locators). The cases start already logged in: they never log in.",
            "",
        ]
    lines += [
        "To check them, run this ONE command after writing (and after any fix). It is the stage's gate, and",
        "it runs `playwright test --list`, so it also tells you whether each spec compiles:",
        "",
        f"  {CHECK_UI}",
        "",
        "Do not run Playwright against any host and do not install anything: the run executes the tests later",
        "against the environment the tester names.",
        "",
        "In your final message, give the counts (tested / skipped) and the skip reasons grouped.",
    ]
    if only:
        lines[0:1] = [
            f"Add Playwright tests for {len(only)} newly CONFIRMED web UI case(s) of feature {feature_of(ctx)}: "
            f"{', '.join(only)}.",
            "",
            f"{ctx.path(PLAN_OUT)} already covers the other {len(cases) - len(only)} case(s), with their specs in",
            f"{ctx.path(ui_web.UI_DIR) / ui_web.TESTS_DIR}: keep those entries, specs and the login step exactly as",
            "they are. Add each new case to `tests` (its spec + cited locators) or to `skipped` (with a reason).",
        ]
    return "\n".join(lines) + guidance_note(ctx, "derive_ui")


def _engine(stage: Stage, ctx: RunContext):
    from rfc_agent import decisions

    problem = ui_web.runner_problem()
    if problem:
        from harness.contracts import EngineFailure

        raise EngineFailure(f"derive_ui: {problem}")
    store = get_store(ctx)
    only = only_cases(ctx)
    cases = [{"extid": c.extid, "summary": c.summary, "text": c.text}
             for c in store.list_cases(feature_of(ctx), status="CONFIRMED", tags=WEB_TAGS)
             if not only or c.extid in only]
    roots = source_roots(ctx)
    ui = ctx.path(ui_web.UI_DIR)
    ui_web.prepare(ui)
    # Specs from an earlier derive (another case set, e.g. `testing.cases` narrowed the run) are not this
    # plan's: the gate rejects strays. Kept beside it rather than deleted, so nothing written is lost.
    selected = {ui_web.spec_name(c["extid"]) for c in cases}
    for old in (ui / ui_web.TESTS_DIR).glob("*.spec.ts"):
        if old.name not in selected:
            keep = ui / "tests.previous"
            keep.mkdir(exist_ok=True)
            old.replace(keep / old.name)
    # Incremental (`testing.ui_only_new`): keep every test already written for a still-CONFIRMED case, drop
    # the entries of cases that are no longer CONFIRMED, and ask the model only for the cases with neither a
    # test nor a skip — so newly confirmed cases do not mean re-deriving (and re-paying for) all the rest.
    new = [c["extid"] for c in cases]
    previous = read_json(ctx.path(PLAN_OUT), None)
    if bool(ctx.flag("testing.ui_only_new", False)) and isinstance(previous, dict):
        confirmed = {c["extid"] for c in cases}
        kept = {sec: [r for r in previous.get(sec) or [] if isinstance(r, dict) and r.get("extid") in confirmed]
                for sec in ("tests", "skipped")}
        done = {r["extid"] for sec in kept.values() for r in sec}
        new = [e for e in new if e not in done]
        write_json(ctx.path(PLAN_OUT), {**previous, **kept})
    write_json(ctx.path(CASES_OUT), cases)
    write_json(ctx.path(ROOTS_OUT), [str(r) for r in roots])
    write_json(ctx.path(SETTINGS_OUT), {"login_required": login_required(ctx),
                                        "only": new if len(new) < len(cases) else []})
    decisions.record(ctx, stage.name, "ui_cases_selected",
                     why="the CONFIRMED exec:ui surface:web cases the run will attach, and the source the "
                         "locators must come from",
                     cases=len(cases), roots=[str(r) for r in roots])
    if not cases:
        write_json(ctx.path(PLAN_OUT), {"tests": [], "skipped": []})
        return StageResult(final_text="no CONFIRMED web UI case — nothing to derive", engine_desc="python")
    if not new:
        return StageResult(final_text=f"every one of the {len(cases)} CONFIRMED web UI cases already has a test "
                                      f"or a skip — nothing new to derive", engine_desc="python")
    # The Claude engine grants reads outside the run dir only through `--add-dir` on `repos_root`
    # (`Stage.add_repos_root`); the write guard keeps that tree read-only. One directory, so the snapshots'
    # common parent.
    if not roots:
        from harness.contracts import EngineFailure

        raise EngineFailure("derive_ui: no front-end source to take locators from — the RFC run has no "
                            "_snap/ snapshot; set testing.ui_source_roots to the web repo checkout.")
    ctx.inputs["repos_root"] = os.path.commonpath([str(r) for r in roots])
    result = run_engine_stage(stage, ctx, SKILL_PATH, extra_roots=tuple(roots))
    plan = read_json(ctx.path(PLAN_OUT), {}) or {}
    for row in plan.get("skipped") or []:
        if isinstance(row, dict):
            decisions.record(ctx, stage.name, "ui_case_not_automated",
                             why="left for a person to run in the Kiwi test run",
                             extid=row.get("extid"), reason=row.get("reason"))
    return result


def _gate(ctx: RunContext) -> list[str]:
    plan, bad = read_json_gated(ctx.path(PLAN_OUT), expect=dict)
    if bad:
        return bad
    cases = read_json(ctx.path(CASES_OUT), []) or []
    if not cases:
        return [] if not (plan.get("tests") or plan.get("skipped")) else \
            ["plan.json lists cases but no CONFIRMED web UI case was selected"]
    roots = [Path(r) for r in read_json(ctx.path(ROOTS_OUT), []) or []]
    ui = ctx.path(ui_web.UI_DIR)
    titles, list_error = ui_web.list_titles(ui, login=isinstance(plan.get("login"), dict))
    return ui_web.validate(plan, cases, ui, titles, list_error, roots, login_required(ctx))


STAGE = Stage(
    name="derive_ui",
    requires=("qa/sources.json",),
    outputs=(CASES_OUT, ROOTS_OUT, SETTINGS_OUT, PLAN_OUT),
    build_prompt=_build_prompt,
    gate=_gate,
    engine=_engine,
    retry_on_gate_failure=1,
    revise_on_retry=True,
    allow_commands=(CHECK_UI,),
    add_repos_root=True,
)
