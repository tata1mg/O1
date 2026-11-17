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
import sys
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
#: The one command a case's agent may run: the stage's own verdict check, on its own verdict.
CHECK_VERDICT = f"{sys.executable} {Path(__file__).resolve().parents[2] / 'check_verdict.py'}"
#: Browser actions an agent may take between two writes of its verdict file (enforced by the browser guard).
VERDICT_EVERY = 12
RESULTS_OUT = f"{ui_agent.AGENT_DIR}/{ui_agent.RESULTS}"
TABLE_OUT = f"{ui_agent.AGENT_DIR}/{ui_agent.RESULTS_TABLE}"
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
            "read_only": bool(ctx.flag("testing.agent_read_only", False)),
            "forbid": str(ctx.flag("testing.agent_forbid", "") or ""),
            "max_actions": int(ctx.flag("testing.agent_max_actions", 60)),
            "case_timeout_s": int(ctx.flag("testing.agent_case_timeout_s", 900))}


def build_prompt(case, folder: Path, settings: dict, notes: str, code_context: bool, route: str = "") -> str:
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
        f"{settings['case_timeout_s'] // 60} minutes. You cannot see a clock, and a session that runs out is "
        f"cut off without warning — so the verdict file must already be on disk: write it BEFORE your first "
        f"browser call and rewrite it after each part, as the skill says. What is on disk when you are cut "
        f"off is your verdict. The browser will refuse to act until the file exists, and again whenever "
        f"{VERDICT_EVERY} actions have passed since you last wrote it.",
        "You do not have to count actions and you cannot run out without being told: the browser refuses an "
        "action with a message that says so. Until you have seen that message, 'ran out of actions' is not a "
        "blocker you may write.",
        f"Check your verdict whenever you rewrite it, and always before you stop: run "
        f"`{CHECK_VERDICT} {rel}` — it prints exactly what will be rejected. A rejected verdict records "
        f"nothing, so fix what it names and run it again until it prints OK.",
        "To see the page, call `browser_snapshot`: it returns the page inline. The snapshot an ACTION returns is "
        "only a link to a .yml file — do not open those files with Read or Bash, it doubles your calls and "
        "your time. `browser_find` locates an element by text without a full snapshot.",
        "",
        f"Your case folder is {rel}/. Save each screenshot INTO it: give `browser_take_screenshot` the "
        f"`filename` as a path from the run directory, e.g. `{rel}/a-listing.png` (a bare name would land "
        f"outside your folder and the verdict would be rejected). Name the file as the clause's `evidence`.",
        "Make one browser call per message and look at its result before the next.",
        *(["Some actions are refused in this run whatever the case says: anything that redeems or spends "
           "(NeuCoins, wallet, applying a coupon), checkout and payment, adding or removing a Care Plan, "
           "editing an address, patient or profile, uploading a prescription, logging out. Do not try them "
           "and do not look for a way round a refusal. A clause that needs one is `not reached`."]
          if settings.get("forbid") else []),
        *(["THIS RUN IS READ-ONLY. Look, search, sort, filter, open and go back — nothing else. Do not add to "
           "the cart, book, place or cancel an order, pay, save or delete an address or a patient, change the "
           "profile, apply a coupon, or submit any form. A clause that cannot be judged without one of these "
           "is `not reached`, with the blocker 'read-only run' (cause: test data). This overrides the skill's "
           "advice to empty the cart first: leave the cart as it is."] if settings.get("read_only") else []),
        f"Write the verdict to {rel}/{ui_agent.VERDICT} — the object the skill's 'Harness output contract' "
        f"describes, with \"extid\": \"{case.extid}\".",
        f"If you learn something about this environment the next case would need (a screen that appears "
        f"first, a section that does not render, a route that works), write it as short bullet lines to "
        f"{rel}/{ui_agent.NOTES}.",
    ]
    if route.strip():
        lines += ["", "An earlier attempt at this case got to its screens this way. It is a route, not a result: "
                      "use it to get there, then judge what the page shows yourself.", "", route.strip()]
    if code_context:
        lines += ["", "You also have a read-only code lookup (fleetlens). Use it only when you are stuck "
                      "finding where a feature lives or which route opens it. Never use it to decide what "
                      "the expected result is: that comes from the case alone."]
    if notes.strip():
        lines += ["", "Notes from earlier cases in this run:", "", notes.strip()]
    return "\n".join(line for line in lines if line is not None)


def _run_case(stage: Stage, ctx: RunContext, case, folder: Path, label: str, settings: dict,
              notes: str, route: str = "") -> dict:
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
        prompt=build_prompt(case, folder, settings, notes, "fleetlens" in servers, route),
        cwd=ctx.run_dir, label=label, trace_path=events_path(ctx.run_dir, label),
        timeout_s=settings["case_timeout_s"] + GRACE_S, skill_path=SKILL_PATH,
        model=(ctx.flag("model", "") or None), live=False,
        env={guard_browser.ORIGIN_ENV: settings["base_url"],
             guard_browser.MAX_ACTIONS_ENV: str(settings["max_actions"]),
             guard_browser.COUNT_FILE_ENV: str(count_file),
             guard_browser.VERDICT_FILE_ENV: str(folder / ui_agent.VERDICT),
             guard_browser.VERDICT_EVERY_ENV: str(VERDICT_EVERY),
             guard_browser.FORBID_ENV: settings.get("forbid", "")},
        containment=Containment(
            writable_roots=(ctx.run_dir,),
            mcp_servers={ui_agent.SERVER: ui_agent.browser_server(
                folder, storage_state=settings["auth"], device=settings["device"],
                headed=settings["headed"], browser=settings["browser"])}),
        # the child stage carries the server list the claude-code adapter grants tools from
        native_ctx={"stage": dataclasses.replace(stage, mcp_servers=servers), "ctx": ctx},
    )
    t0 = time.monotonic()
    failure, calls, cost, acted, trace = "", None, None, 0, {}
    try:
        summary = ctx.engine.run(req).summary
        if getattr(summary, "is_error", False):
            failure = f"the session ended with an error ({getattr(summary, 'error_reason', None) or 'unknown'})"
    except Exception as exc:  # one case's session must not take the batch with it
        failure = f"the session did not finish ({type(exc).__name__}: {str(exc)[:300]})"
        # A session cut off at its time limit raises before its trace is written, so its calls and cost
        # vanished from the run's accounting (second pilot: a 119-call case counted as 0). The raw stream
        # is still on disk: translate it.
        summary = _recovered_summary(ctx, label, req.trace_path)
    if summary is not None:
        tool_calls = list(getattr(summary, "tool_calls", None) or [])
        calls, acted = ui_agent.browser_calls(tool_calls), ui_agent.browser_actions(tool_calls)
        cost = getattr(summary, "cost_usd", None)
        trace = {"tool_calls": tool_calls, "usage": getattr(summary, "usage", None),
                 "num_turns": getattr(summary, "num_turns", None)}
    duration = round(time.monotonic() - t0, 1)

    verdict = read_json(folder / ui_agent.VERDICT, None)
    problems = ui_agent.validate_verdict(verdict, case.extid, folder) if verdict is not None else \
        [f"{case.extid}: the agent wrote no {ui_agent.VERDICT}"]
    if not problems and verdict["status"] in (ui_agent.PASS, ui_agent.FAIL) and calls is not None \
            and calls < len(verdict["parts"]):
        # A verdict is a claim about a page. Fewer browser calls than parts judged means some of it was
        # not looked at in this session, whatever the file says. (`calls` is None only when the session
        # died and left no readable trace: then there is nothing to check the claim against.)
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
    Path(f"{count_file}.verdict").unlink(missing_ok=True)
    if not problems and failure and verdict["status"] == ui_agent.BLOCKED:
        # the agent's own running verdict, taken off disk after the session was cut off: say that it was
        verdict = {**verdict, "reason": f"{verdict.get('reason', '')} (The session was cut off: {failure}.)"}
        write_json(folder / ui_agent.VERDICT, verdict)
    calls = calls or 0
    # the case's own file says how long it took and what it cost, not only the run's summary
    write_json(folder / ui_agent.VERDICT, {**verdict, "duration_s": duration, "cost_usd": cost})
    return {**verdict, "duration_s": duration, "browser_calls": calls, "browser_actions": acted,
            # None with a session that ran: it was cut off before reporting its cost, which is then unknown
            "cost_usd": cost, "cost_known": cost is not None, "_trace": [trace],
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"), "folder": str(folder)}


def _recovered_summary(ctx: RunContext, label: str, trace_path: Path):
    """The trace summary of a session that raised, rebuilt from its raw stream — or None when it cannot be."""
    raw = ctx.run_dir / "logs" / f"{label}.stream.jsonl"
    writer = getattr(ctx.engine, "write_trace", None)
    if writer is None or not raw.is_file():
        return None
    try:
        from harness.events import summarize

        written = writer(raw, trace_path)
        return summarize(written) if written else None
    except Exception:  # accounting must never be what fails a case
        return None


def _confirm(stage: Stage, ctx: RunContext, case, first: dict, settings: dict, notes: str) -> dict:
    """Run a FAIL once more in a fresh browser. The status stays FAIL either way: a product that fails one
    time in two is not passing. What changes is what the reader is told about it."""
    folder = ctx.path(ui_agent.AGENT_DIR) / f"{case.extid}.confirm"
    # The second run is told HOW the first got to the screens — what it did, step by step, and where it
    # departed from the written steps — and nothing of what it SAW or concluded. Without the route it
    # cannot confirm anything (first pilot: it never found the page the first run had failed on). With the
    # first run's notes it is not a second opinion (guest pilot: the notes said "it does NOT show a Sign in
    # sheet", and the confirmation agreed with what it had just been told).
    # Only the actions, and no run notes at all: `deviations` and notes are prose, and in the care-plan
    # run both carried the first run's findings into the second ("opened a Remove Care Plan sheet rather
    # than an expanded coupon view"; "no header, no progress bar render inline").
    did = [str(a.get("action") or "").strip() for a in first.get("actions") or [] if isinstance(a, dict)]
    route = "\n".join(f"- {d}" for d in did if d)
    second = _run_case(stage, ctx, case, folder, f"{stage.name}.{case.extid}.confirm", settings, "", route)
    if second["status"] == ui_agent.FAIL:
        note, confirmed = "FAIL again — confirmed", True
    elif second["status"] == ui_agent.PASS:
        note, confirmed = "PASS — the failure did not repeat (flaky); both runs are kept", False
    else:
        note = f"{second['status']} — the second run could not get there, so the failure is NOT confirmed " \
               f"({second.get('reason', '')[:200]})"
        confirmed = False
    return {**first, "confirmation": note, "confirmed": confirmed, "flaky": second["status"] == ui_agent.PASS,
            "confirm_folder": str(folder), "_trace": first["_trace"] + second["_trace"],
            "first_run_s": first["duration_s"], "second_run_s": second["duration_s"],
            "browser_calls": first["browser_calls"] + second["browser_calls"],
            "browser_actions": first["browser_actions"] + second["browser_actions"],
            "cost_known": first["cost_known"] and second["cost_known"],
            "duration_s": round(first["duration_s"] + second["duration_s"], 1),
            "cost_usd": (first.get("cost_usd") or 0) + (second.get("cost_usd") or 0) or None}


def _merge_notes(ctx: RunContext, batch: list) -> str:
    """Fold each finished case's notes into the run's, so the next batch starts knowing them."""
    path = ctx.path(NOTES_OUT)
    text = path.read_text(encoding="utf-8") if path.is_file() else ""
    for case in batch:
        own = ctx.path(ui_agent.AGENT_DIR) / case.extid / ui_agent.NOTES
        if own.is_file():
            for line in map(ui_agent.usable_note, own.read_text(encoding="utf-8", errors="replace").splitlines()):
                if line and line not in text:
                    text += ("" if not text or text.endswith("\n") else "\n") + line + "\n"
    if text:
        path.parent.mkdir(parents=True, exist_ok=True)
        text = "\n".join(text.splitlines()[-40:]) + "\n"  # a note file is a hint, not a transcript
        path.write_text(text, encoding="utf-8")
    return text


def _row(verdict: dict) -> dict:
    """A verdict as the row `ui_record.record_row` records."""
    # the screenshot of what was WRONG; a picture of a clause that passed explains nothing on a case that did not
    shot = next((c["evidence"] for p in verdict.get("parts") or [] for c in p.get("expected") or []
                 if c.get("state") == ui_agent.NOT_MET and c.get("evidence")), "")
    finished = datetime.fromisoformat(verdict["finished_at"]) if verdict.get("finished_at") else None
    return {"extid": verdict["extid"], "status": verdict["status"], "playwright_status": "agent",
            "detail": ui_agent.report(verdict), "duration_s": verdict.get("duration_s", 0.0),
            "finished_at": finished,
            "attachments": [{"name": "screenshot", "path": str(Path(verdict["folder"]) / shot)}] if shot else []}


def _store_down(doing: str, exc: Exception) -> EngineFailure:
    """A store that cannot be reached, said in a sentence instead of a traceback (third pilot: Kiwi answered
    503 and the run ended in an xmlrpc ProtocolError). Both calls it guards come before any session starts."""
    return EngineFailure(f"execute_ui_agent could not {doing} the case store ({type(exc).__name__}: "
                         f"{str(exc)[:200]}). Nothing was executed and no browser was started. If that is Kiwi "
                         f"answering 503 it is down, not misconfigured: re-run when it is back.")


def _engine(stage: Stage, ctx: RunContext) -> StageResult:
    from rfc_agent import decisions

    t0 = time.monotonic()
    feature = feature_of(ctx)
    store = get_store(ctx)
    include_manual = bool(ctx.flag("testing.agent_include_manual", False))
    try:
        cases = ui_agent.select_cases(store, feature, include_manual=include_manual, only=only_cases(ctx))
    except Exception as exc:
        raise _store_down("read the cases from", exc) from None
    # in the order `testing.cases` names them, when it does: the order is the tester's, and it decides which
    # case's notes the others start with
    asked = [c.strip() for c in str(ctx.flag("testing.cases", "") or "").split(",") if c.strip()]
    cases.sort(key=lambda c: asked.index(c.extid) if c.extid in asked else len(asked))
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
    try:
        run = store.create_run(feature, settings["build"], settings["environment"], extids, require_tag="exec:ui")
        attached = set(run.attached)
        if include_manual:
            attached |= set(store.create_run(feature, settings["build"], settings["environment"], extids,
                                             require_tag="exec:manual").attached)
    except Exception as exc:
        raise _store_down("create the run in", exc) from None
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
    tool_calls, turns, usage, unpriced = [], 0, {}, []

    def save() -> None:
        write_json(ctx.path(RESULTS_OUT), {**header, "results": results, "tally": tally,
                                           "parts_met": parts_met, "parts": parts_total,
                                           "not_executed": not_executed, "cost_usd": round(cost, 4) or None,
                                           # sessions cut off before they reported a cost: the total is a floor
                                           "cost_unknown_for": unpriced,
                                           "cases": len(runnable)})
        ctx.path(TABLE_OUT).write_text(ui_agent.results_table(results, not_executed, unpriced) + "\n",
                                       encoding="utf-8")

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
            if verdict.get("cost_known") is False:
                unpriced.append(verdict["extid"])
            for trace in verdict.get("_trace") or []:    # the stage's own accounting: every session's calls
                tool_calls += trace.get("tool_calls") or []
                turns += trace.get("num_turns") or 0
                for key, value in (trace.get("usage") or {}).items():
                    if isinstance(value, int | float) and not isinstance(value, bool):
                        usage[key] = usage.get(key, 0) + value
            if verdict["extid"] in attached:
                ui_record.record_row(ctx, stage.name, store, run.run_id, _row(verdict), header)
            results.append({k: verdict.get(k) for k in (
                "extid", "status", "cause", "reason", "blocked_on", "confirmation", "confirmed", "flaky",
                "duration_s", "first_run_s", "second_run_s", "browser_calls", "browser_actions", "cost_usd")} | {
                    "summary": ui_agent.summary(verdict), "parts_met": met, "parts": total,
                    "recorded": verdict["extid"] in attached})
            if verdict["status"] != ui_agent.PASS:
                decisions.record(ctx, stage.name, "ui_agent_case_not_passed",
                                 why=ui_agent.summary(verdict), extid=verdict["extid"],
                                 status=verdict["status"], cause=verdict.get("cause"))
        save()
    text = "  ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "no case could be executed"
    text += f"  ·  {parts_met} of {parts_total} parts met"
    if not_executed:
        text += f"  ·  {len(not_executed)} not executed"
    # every report of this stage carries the per-case table, time included
    text += "\n\n" + ui_agent.results_table(results, not_executed, unpriced)
    return StageResult(final_text=text, duration_s=time.monotonic() - t0, cost_usd=cost or None,
                       tool_calls=tool_calls, usage=usage or None, num_turns=turns or None,
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
    allow_commands=(CHECK_VERDICT,),
)  # no `mcp_servers` here: the browser is defined per case (`_run_case`), not in the config file
