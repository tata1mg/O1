"""`execute_ui_agent` — an agent drives a real browser through each CONFIRMED web UI case and judges it.

The alternative to `derive_ui -> execute_web`. That pair writes a Playwright script from the source and
then runs it; a script written without seeing the page fails on the first locator the page disagrees with
(8 of the first 11 did). Here nothing is scripted: one agent per case reads the case, looks at the live
page, acts, looks again, finds another route when the written one does not work, and hands back a verdict
with the evidence for every clause (`..ui_agent`).

What Python owns, because none of it is judgement:

  * which cases — CONFIRMED web UI cases from the store (`ui_agent.select_cases`); a case with no expected
    result is never handed to an agent, since there is nothing to judge it against;
  * where — the run's base address, which must be a named test environment (`ui_agent.host_problem`);
  * the browser — one Playwright MCP server per case, own profile, own output folder, started already
    logged in from a recorded session. The agent is never given a credential;
  * the limits — an action count the browser guard enforces, and a wall-clock timeout;
  * the verdict's standing — it is checked against its evidence and against the session's trace; a FAIL is
    run a second time in a fresh browser; a session that leaves no usable verdict is an ERROR (the
    harness's), or BLOCKED when it ran out of actions or time — never a FAIL;
  * the record — the same store run `execute_web` and `execute_be` use, written batch by batch.

Cases run in batches (`testing.agent_parallel`), one agent and one browser each. A batch finishes before
the next starts, so what its agents learned about the environment (`notes.md`) reaches the cases after it.

OFF by default (`testing.execute_ui_agent`): it acts on a live environment as a real user, and it costs a
model session per case on every run.
"""

from __future__ import annotations

import dataclasses
import shutil
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from harness.contracts import EngineFailure, RunContext, Stage, StageResult

from ... import ui_agent, ui_record
from ...store import get_store
from .. import _stage_persona, feature_of, only_cases, read_json, read_json_gated, write_json

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "qa-ui-agent" / "SKILL.md"
RESULTS_OUT = f"{ui_agent.AGENT_DIR}/{ui_agent.RESULTS}"
NOTES_OUT = f"{ui_agent.AGENT_DIR}/{ui_agent.NOTES}"
#: The harness's own outcome, beside the three an agent may return: no usable verdict came back.
ERROR = "ERROR"
#: Seconds past the case's own time limit before the session is cut: room to write the verdict.
GRACE_S = 60


def _servers(ctx: RunContext) -> tuple[str, ...]:
    """The MCP servers a case's agent gets: the browser, and — only when asked — read-only code lookup."""
    extra = ("fleetlens",) if bool(ctx.flag("testing.agent_code_context", False)) else ()
    return (ui_agent.SERVER, *extra)


def _settings(ctx: RunContext) -> dict:
    """Where and how the run drives the browser, or EngineFailure naming what is missing."""
    env_name = str(ctx.inputs.get("env") or ctx.flag("testing.env", "") or "")
    template = str(ctx.flag("testing.base_url_template", "") or "")
    base_url = str(ctx.inputs.get("base_url") or ctx.flag("testing.base_url", "") or "") or \
        (template.format(env=env_name) if template and env_name else "")
    build = str(ctx.inputs.get("build") or ctx.flag("testing.build", "") or "")
    if not base_url.startswith(("http://", "https://")):
        raise EngineFailure("execute_ui_agent needs --base-url, the web app's origin on the target environment "
                            "(e.g. https://jupiter.1mg.com), or testing.base_url_template with --env.")
    if not env_name or not build:
        raise EngineFailure("execute_ui_agent needs --env and --build: they name the run and are recorded on "
                            "every execution.")
    problem = ui_agent.host_problem(base_url, hosts=str(ctx.flag("testing.agent_hosts", "") or ""),
                                    environments=str(ctx.flag("testing.environments", "") or ""),
                                    template=template)
    if problem:
        raise EngineFailure(problem)
    auth = str(ctx.inputs.get("ui_auth") or ctx.flag("testing.ui_auth", "") or "")
    if auth and not Path(auth).expanduser().is_file():
        raise EngineFailure(f"--ui-auth {auth} does not exist. Record it with `npx playwright codegen "
                            f"--save-storage=<file> <base url>` and log in.")
    # `testing.ui_login` (a number and an OTP, for execute_web's login step) is deliberately not used: the
    # agent is never given a credential. Without a recorded session the cases run logged out, and say so.
    browser = str(ctx.flag("testing.agent_browser", "") or "")
    problem = ui_agent.browser_problem(browser)
    if problem:
        raise EngineFailure(f"execute_ui_agent: {problem}")
    engine_name = str(getattr(ctx.engine, "name", "") or "")
    if engine_name in ("pi", "axon"):
        # The browser tools are granted, held to the run's address and counted by Claude Code's hook
        # transport. On Pi none of that exists yet: every browser call would be denied.
        raise EngineFailure(f"execute_ui_agent runs on the claude-code engine only (this run's is "
                            f"{engine_name!r}): its browser guard is a Claude Code hook. Re-run without "
                            f"--engine.")
    return {"environment": env_name, "build": build, "base_url": base_url.rstrip("/"), "auth": auth,
            "device": str(ctx.flag("testing.ui_device", "") or ""),
            "headed": bool(ctx.flag("testing.ui_headed", False)),
            "browser": browser,
            "max_actions": int(ctx.flag("testing.agent_max_actions", 40)),
            "case_timeout_s": int(ctx.flag("testing.agent_case_timeout_s", 480))}


def build_prompt(case, folder: Path, settings: dict, notes: str, code_context: bool) -> str:
    rel = f"{ui_agent.AGENT_DIR}/{folder.name}"
    lines = [
        f"Execute test case {case.extid} in the browser and return a verdict.",
        "",
        f"Case: {case.summary}" if case.summary else "",
        "",
        case.text,
        "",
        f"Environment: {settings['base_url']} (environment {settings['environment']}, build "
        f"{settings['build']}). Open pages by path on that address; you may not leave it.",
        "The browser is already logged in." if settings["auth"] else
        "The browser is NOT logged in. A clause that needs a logged-in user is `not reached` (cause: test data).",
        f"Limits: {settings['max_actions']} browser actions (looking at the page is free) and "
        f"{settings['case_timeout_s'] // 60} minutes. When either is close, stop and write the verdict.",
        "",
        f"Your case folder is {rel}/. Save each screenshot INTO it: give `browser_take_screenshot` the "
        f"`filename` as a path from the run directory, e.g. `{rel}/a-listing.png` (a bare name would land "
        f"outside your folder and the verdict would be rejected). Name the file as the clause's `evidence`.",
        "Make one browser call per message and look at its result before the next.",
        f"Write the verdict to {rel}/{ui_agent.VERDICT} — the object the skill's 'Harness output contract' "
        f"describes, with \"extid\": \"{case.extid}\".",
        f"If you learn something about this environment the next case would need (a screen that appears "
        f"first, a section that does not render, a route that works), write it as short bullet lines to "
        f"{rel}/{ui_agent.NOTES}.",
    ]
    if code_context:
        lines += ["", "You also have a read-only code lookup (fleetlens). Use it only when you are stuck "
                      "finding where a feature lives or which route opens it. Never use it to decide what "
                      "the expected result is: that comes from the case alone."]
    if notes.strip():
        lines += ["", "Notes from earlier cases in this run:", "", notes.strip()]
    return "\n".join(line for line in lines if line is not None)


def _run_case(stage: Stage, ctx: RunContext, case, folder: Path, label: str, settings: dict,
              notes: str) -> dict:
    """One agent session for one case -> its verdict (the agent's when it stands, else Python's)."""
    from harness.engines import Containment, EngineRequest
    from harness.events import events_path
    from rfc_agent.hooks import guard_browser

    if folder.exists():
        shutil.rmtree(folder)  # a verdict or a screenshot from an earlier run must not pass for this one's
    folder.mkdir(parents=True)
    # Outside the run dir on purpose: the agent may write anywhere inside it, and a counter it could
    # reset is not a limit.
    count_file = Path(tempfile.gettempdir()) / f"rfc-agent-actions-{uuid.uuid4().hex}"
    servers = _servers(ctx)
    req = EngineRequest(
        prompt=build_prompt(case, folder, settings, notes, "fleetlens" in servers),
        cwd=ctx.run_dir, label=label, trace_path=events_path(ctx.run_dir, label),
        timeout_s=settings["case_timeout_s"] + GRACE_S, skill_path=SKILL_PATH,
        model=(ctx.flag("model", "") or None), live=False,
        env={guard_browser.ORIGIN_ENV: settings["base_url"],
             guard_browser.MAX_ACTIONS_ENV: str(settings["max_actions"]),
             guard_browser.COUNT_FILE_ENV: str(count_file)},
        containment=Containment(
            writable_roots=(ctx.run_dir,),
            mcp_servers={ui_agent.SERVER: ui_agent.browser_server(
                folder, storage_state=settings["auth"], device=settings["device"],
                headed=settings["headed"], browser=settings["browser"])}),
        # the child stage carries the server list the claude-code adapter grants tools from
        native_ctx={"stage": dataclasses.replace(stage, mcp_servers=servers), "ctx": ctx},
    )
    t0 = time.monotonic()
    failure, calls, cost = "", 0, None
    try:
        res = ctx.engine.run(req)
        calls = ui_agent.browser_calls(getattr(res.summary, "tool_calls", None))
        cost = getattr(res.summary, "cost_usd", None)
        if getattr(res.summary, "is_error", False):
            failure = f"the session ended with an error ({getattr(res.summary, 'error_reason', None) or 'unknown'})"
    except Exception as exc:  # one case's session must not take the batch with it
        failure = f"the session did not finish ({type(exc).__name__}: {str(exc)[:300]})"
    duration = round(time.monotonic() - t0, 1)

    verdict = read_json(folder / ui_agent.VERDICT, None)
    problems = ui_agent.validate_verdict(verdict, case.extid, folder) if verdict is not None else \
        [f"{case.extid}: the agent wrote no {ui_agent.VERDICT}"]
    if not problems and verdict["status"] in (ui_agent.PASS, ui_agent.FAIL) and calls < len(verdict["parts"]):
        # A verdict is a claim about a page. Fewer browser calls than parts judged means some of it was
        # not looked at in this session, whatever the file says.
        problems = [f"{case.extid}: the verdict judges {len(verdict['parts'])} part(s) but the session's "
                    f"trace holds {calls} browser call(s) — it was not worked for"]
    if problems:
        if verdict is not None:
            write_json(folder / "verdict.rejected.json", verdict)
        try:
            used = int(count_file.read_text().strip() or 0)
        except (OSError, ValueError):
            used = 0
        out_of_time = duration >= settings["case_timeout_s"]
        if used >= settings["max_actions"] or out_of_time:
            limit = f"{settings['case_timeout_s']}s" if out_of_time else f"{settings['max_actions']} actions"
            verdict = ui_agent.blocked(case.extid, "limit",
                                       f"the case used its {limit} without reaching a verdict")
        else:
            verdict = {**ui_agent.blocked(case.extid, "no usable verdict",
                                          "; ".join(([failure] if failure else []) + problems)[:1500],
                                          cause=""), "status": ERROR}
        write_json(folder / ui_agent.VERDICT, verdict)
    count_file.unlink(missing_ok=True)
    return {**verdict, "duration_s": duration, "browser_calls": calls, "cost_usd": cost,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"), "folder": str(folder)}


def _confirm(stage: Stage, ctx: RunContext, case, first: dict, settings: dict, notes: str) -> dict:
    """Run a FAIL once more in a fresh browser. The status stays FAIL either way: a product that fails one
    time in two is not passing. What changes is what the reader is told about it."""
    folder = ctx.path(ui_agent.AGENT_DIR) / f"{case.extid}.confirm"
    second = _run_case(stage, ctx, case, folder, f"{stage.name}.{case.extid}.confirm", settings, notes)
    if second["status"] == ui_agent.FAIL:
        note = "FAIL again — confirmed"
    elif second["status"] == ui_agent.PASS:
        note = "PASS — the failure did not repeat (flaky); both runs are kept"
    else:
        note = f"{second['status']} — could not be confirmed ({second.get('reason', '')[:200]})"
    return {**first, "confirmation": note, "flaky": second["status"] == ui_agent.PASS,
            "confirm_folder": str(folder),
            "duration_s": round(first["duration_s"] + second["duration_s"], 1),
            "cost_usd": (first.get("cost_usd") or 0) + (second.get("cost_usd") or 0) or None}


def _merge_notes(ctx: RunContext, batch: list) -> str:
    """Fold each finished case's notes into the run's, so the next batch starts knowing them."""
    path = ctx.path(NOTES_OUT)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for case in batch:
        own = ctx.path(ui_agent.AGENT_DIR) / case.extid / ui_agent.NOTES
        if own.is_file():
            for line in own.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip()
                if line and line not in text:
                    text += ("" if not text or text.endswith("\n") else "\n") + line[:300] + "\n"
    if text:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(text.splitlines()[-40:]) + "\n"  # a note file is a hint, not a transcript
        path.write_text(text, encoding="utf-8")
    return text


def _row(verdict: dict) -> dict:
    """A verdict as the row `ui_record.record_row` records."""
    shot = next((c["evidence"] for state in (ui_agent.NOT_MET, ui_agent.MET) for p in verdict.get("parts") or []
                 for c in p.get("expected") or [] if c.get("state") == state and c.get("evidence")), "")
    finished = datetime.fromisoformat(verdict["finished_at"]) if verdict.get("finished_at") else None
    return {"extid": verdict["extid"], "status": verdict["status"], "playwright_status": "agent",
            "detail": ui_agent.report(verdict), "duration_s": verdict.get("duration_s", 0.0),
            "finished_at": finished,
            "attachments": [{"name": "screenshot", "path": str(Path(verdict["folder"]) / shot)}] if shot else []}


def _engine(stage: Stage, ctx: RunContext) -> StageResult:
    from rfc_agent import decisions

    t0 = time.monotonic()
    feature = feature_of(ctx)
    store = get_store(ctx)
    include_manual = bool(ctx.flag("testing.agent_include_manual", False))
    cases = ui_agent.select_cases(store, feature, include_manual=include_manual, only=only_cases(ctx))
    if not cases:
        write_json(ctx.path(RESULTS_OUT), {"feature": feature, "results": [], "tally": {}, "not_executed": []})
        return StageResult(final_text="no CONFIRMED web UI case — nothing to execute", engine_desc="python")
    if ctx.engine is None:
        raise EngineFailure("execute_ui_agent needs a model engine but none was resolved — run it through "
                            "the harness CLI.")
    settings = _settings(ctx)
    unusable = {c.extid: "the case id cannot name a folder — give the case a plain extid: tag"
                for c in cases if not ui_agent.safe_id(c.extid)}
    unusable |= {c.extid: "the case states no expected result — it needs QA input before anyone can pass "
                          "or fail it"
                 for c in cases if c.extid not in unusable and not ui_agent.expected_result(c.text)}
    runnable = [c for c in cases if c.extid not in unusable]
    decisions.record(ctx, stage.name, "ui_agent_cases_selected",
                     why="the CONFIRMED web UI cases an agent will execute in a browser, and those it "
                         "cannot judge (no expected result, or an id that cannot name a folder)",
                     cases=len(runnable), not_executed=sorted(unusable), base_url=settings["base_url"])

    # The store run first, and each batch recorded as it finishes: a run that dies in batch 3 has still
    # recorded batches 1 and 2. Two attach calls, because the store's run gate is one required tag at a time.
    extids = [c.extid for c in cases if ui_agent.safe_id(c.extid)]
    run = store.create_run(feature, settings["build"], settings["environment"], extids, require_tag="exec:ui")
    attached = set(run.attached)
    if include_manual:
        attached |= set(store.create_run(feature, settings["build"], settings["environment"], extids,
                                         require_tag="exec:manual").attached)
    header = {"feature": feature, "environment": settings["environment"], "build": settings["build"],
              "base_url": settings["base_url"], "device": settings["device"] or "desktop",
              "login": "recorded session" if settings["auth"] else "none",
              "run_id": run.run_id, "run_url": run.url}
    not_executed = []
    for extid, why in unusable.items():
        if ui_agent.safe_id(extid):
            write_json(ui_agent.case_dir(ctx.run_dir, extid) / ui_agent.VERDICT, ui_agent.not_executed(extid, why))
        not_executed.append({"extid": extid, "reason": why})
        decisions.record(ctx, stage.name, "ui_agent_case_not_executed", why=why, extid=extid)

    _stage_persona(SKILL_PATH, ctx.run_dir, stage.name)  # once, before any fan-out
    # A visible browser is for watching, and five windows taking focus from each other cannot be watched.
    size = 1 if settings["headed"] else max(1, int(ctx.flag("testing.agent_parallel", 5)))
    confirm = bool(ctx.flag("testing.agent_confirm_fail", True))
    # Notes are this run's: an earlier run's were about another build, perhaps another environment.
    ctx.path(NOTES_OUT).unlink(missing_ok=True)
    notes = ""
    results, tally, parts_met, parts_total, cost = [], {}, 0, 0, 0.0

    def save() -> None:
        write_json(ctx.path(RESULTS_OUT), {**header, "results": results, "tally": tally,
                                           "parts_met": parts_met, "parts": parts_total,
                                           "not_executed": not_executed, "cost_usd": round(cost, 4) or None,
                                           "cases": len(runnable)})

    save()
    for start in range(0, len(runnable), size):
        batch = runnable[start:start + size]

        def one(case, notes=notes) -> dict:
            folder = ctx.path(ui_agent.AGENT_DIR) / case.extid
            verdict = _run_case(stage, ctx, case, folder, f"{stage.name}.{case.extid}", settings, notes)
            if verdict["status"] == ui_agent.FAIL and confirm:
                verdict = _confirm(stage, ctx, case, verdict, settings, notes)
            return verdict

        with ThreadPoolExecutor(max_workers=len(batch)) as pool:
            verdicts = list(pool.map(one, batch))
        notes = _merge_notes(ctx, batch)
        for verdict in verdicts:
            tally[verdict["status"]] = tally.get(verdict["status"], 0) + 1
            met, total = ui_agent.parts_met(verdict)
            parts_met, parts_total = parts_met + met, parts_total + total
            cost += verdict.get("cost_usd") or 0
            if verdict["extid"] in attached:
                ui_record.record_row(ctx, stage.name, store, run.run_id, _row(verdict), header)
            results.append({k: verdict.get(k) for k in (
                "extid", "status", "cause", "reason", "blocked_on", "confirmation", "flaky", "duration_s",
                "browser_calls", "cost_usd")} | {"summary": ui_agent.summary(verdict), "parts_met": met,
                                               "parts": total, "recorded": verdict["extid"] in attached})
            if verdict["status"] != ui_agent.PASS:
                decisions.record(ctx, stage.name, "ui_agent_case_not_passed",
                                 why=ui_agent.summary(verdict), extid=verdict["extid"],
                                 status=verdict["status"], cause=verdict.get("cause"))
        save()
    text = "  ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "no case could be executed"
    text += f"  ·  {parts_met} of {parts_total} parts met"
    if not_executed:
        text += f"  ·  {len(not_executed)} not executed"
    return StageResult(final_text=text, duration_s=time.monotonic() - t0, cost_usd=cost or None,
                       engine_desc=f"agent x{len(runnable)} (batches of {size})")


def _gate(ctx: RunContext) -> list[str]:
    data, bad = read_json_gated(ctx.path(RESULTS_OUT), expect=dict)
    if bad:
        return bad
    results = data.get("results")
    if not isinstance(results, list) or not isinstance(data.get("not_executed"), list):
        return [f"{RESULTS_OUT} has no results / not_executed arrays"]
    out = []
    for row in results:
        extid = row.get("extid", "<no extid>")
        if row.get("status") not in (*ui_agent.STATUSES, ERROR):
            out.append(f"{extid}: status {row.get('status')!r} is not PASS/FAIL/BLOCKED/ERROR")
        verdict = read_json(ui_agent.case_dir(ctx.run_dir, extid) / ui_agent.VERDICT, None)
        if not isinstance(verdict, dict) or verdict.get("status") != row.get("status"):
            out.append(f"{extid}: {RESULTS_OUT} says {row.get('status')} but its {ui_agent.VERDICT} does not")
    tally = data.get("tally") or {}
    if isinstance(tally, dict) and sum(tally.values()) != len(results):
        out.append(f"tally sums to {sum(tally.values())} but there are {len(results)} results")
    if isinstance(data.get("cases"), int) and len(results) < data["cases"]:
        out.append(f"the run stopped after {len(results)} of {data['cases']} cases — the rest were never "
                   f"executed; re-run to execute them")
    if results and all(r.get("status") == ERROR for r in results):
        out.append("every case ended in ERROR — no agent returned a usable verdict; see each case's "
                   f"{ui_agent.VERDICT} for why (the browser may not start: check testing.agent_browser)")
    return out


STAGE = Stage(
    name="execute_ui_agent",
    requires=(),
    outputs=(RESULTS_OUT,),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_engine,
)  # no `mcp_servers` here: the browser is defined per case (`_run_case`), not in the config file
