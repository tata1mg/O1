"""`execute_app` — each CONFIRMED Android UI case runs on an emulator: as its VALIDATED Maestro flow when one
covers it, otherwise an agent drives the app through Maestro MCP and judges it.

The Android twin of `execute_ui_agent`, recording into Kiwi the same way (one run, one execution per case,
the verdict as the comment, a screenshot) but in a run of its own: `<feature> · android-<env> · <app
version>`. Per case (`app_agent.plan`):

  * **flow** — a flow in the maestro-automation repo names the case (`# Covers:` / step labels / `Kiwi #id`)
    and its header says VALIDATED. It runs as written (`maestro test`, no model, no cost), its report is
    enriched by the repo's `maestro_report.py`, and `maestro.attribute` decides each case's status. A
    validated flow that FAILs is reported FAIL — no agent re-run; the flow's owners triage it (decided
    2026-11-17). Several selected cases covered by one flow share one run of it.
  * **agent** — no validated flow. One model session per case with `maestro mcp` on one device, held by
    `rfc_agent.hooks.guard_device`; the verdict is the web agent's (`ui_agent.validate_verdict`) and must
    be backed by device calls in the session's trace. A FAIL is run once more (testing.agent_confirm_fail).
    A PASS may leave `draft.yaml`; the stage lints it and lists it — turning drafts into validated flows is
    `/test-agent:convert-flows`, never this stage. Execute stops at the verdicts.
  * **not run** — gated flows (mock data, real orders), no expected result for an agent, `flow` mode with
    no flow. Listed in the results, not recorded in Kiwi.

Devices: every ready one (`testing.devices` narrows), each a worker pulling the next item; one owner per
device (`devices.DeviceLock`). A case or flow that needs an account profile signs the device in first
(`ensure_account.sh`, static dev OTP); exit 78 makes its cases BLOCKED, never FAIL.

OFF by default (`testing.execute_app`).
"""

from __future__ import annotations

import dataclasses
import shutil
import sys
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from harness.contracts import EngineFailure, RunContext, Stage, StageResult

from ... import app_agent, devices, maestro, maestro_index, ui_agent, ui_record
from ...devices import SKIPPED_EXIT, Android, DeviceLock
from ...store import get_store
from .. import _stage_persona, feature_of, forbid_of, keep_dated, only_cases, read_json, read_json_gated, write_json

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "qa-app-agent" / "SKILL.md"
CHECK_VERDICT = f"{sys.executable} {Path(__file__).resolve().parents[2] / 'check_verdict.py'}"
VERDICT_EVERY = 12
GRACE_S = 60
ERROR = "ERROR"
RESULTS_OUT = f"{app_agent.APP_DIR}/{app_agent.RESULTS}"
TABLE_OUT = f"{app_agent.APP_DIR}/{app_agent.RESULTS_TABLE}"
NOTES_OUT = f"{app_agent.APP_DIR}/{app_agent.NOTES}"
REPORTS_DIR = f"{app_agent.APP_DIR}/reports"
LOCKS_DIR = f"{app_agent.APP_DIR}/locks"
#: The app's own deep-link scheme and the web host its links open in-app (maestro-automation flows use both).
LINK_SCHEMES, LINK_HOSTS = "onemg", "www.1mg.com"
#: The cause on a validated flow's FAIL: the flow's owners triage it (decided 2026-11-17, see the docstring).
TRIAGE = "needs triage"
#: `--env` values that mean the app talks to the production backend.
PROD_ENVS = ("prod", "production", "live")


def settings_of(ctx: RunContext, android: Android) -> dict:
    env_name = str(ctx.inputs.get("env") or ctx.flag("testing.env", "") or "")
    if not env_name:
        raise EngineFailure("execute_app needs --env (the backend environment the app points at): it names the "
                            "run, `<feature> · android-<env> · <app version>`.")
    # Production is the prod build, OR any build whose backend is production: a debug build pointed at
    # api.1mg.com (its in-app hostname setting) acts on real accounts and orders just the same (2026-11-18).
    production = android.variant == "prod" or env_name.lower() in PROD_ENVS
    if production and not bool(ctx.flag("testing.agent_production", False)):
        raise EngineFailure(f"execute_app: this run is on PRODUCTION (app variant {android.variant}, env "
                            f"{env_name}). Say so with --production, which asks you to confirm the device's "
                            f"account is a TEST account.")
    if production and not bool(ctx.flag("testing.agent_test_account", False)):
        raise EngineFailure("execute_app on the production app needs the operator's statement that the device "
                            "is signed in to a TEST account (--test-account). Nothing was run.")
    forbid, allowed = forbid_of(ctx, "execute_app")
    if "neucoins" in allowed and not bool(ctx.flag("testing.agent_test_account", False)):
        raise EngineFailure("execute_app: --allow neucoins lets the agent redeem or toggle the device account's "
                            "NeuCoins. Only on a TEST account: pass --test-account, or drop neucoins from --allow.")
    engine_name = str(getattr(ctx.engine, "name", "") or "")
    if engine_name in ("pi", "axon"):
        raise EngineFailure(f"execute_app runs on the claude-code engine only (this run's is {engine_name!r}): "
                            f"its device guard is a Claude Code hook. Re-run without --engine.")
    return {"environment": env_name, "run_env": f"android-{env_name}",
            "build": "",  # the app version on the device (`dumpsys package`), not the web --build
            "variant": android.variant, "app_id": android.app_id, "production": production,
            "mode": str(ctx.flag("testing.android_mode", "auto") or "auto"),
            "forbid": forbid, "allow": allowed,
            "max_actions": int(ctx.flag("testing.app_max_actions", 60)),
            "case_timeout_s": int(ctx.flag("testing.app_case_timeout_s", 900)),
            "flow_timeout_s": int(ctx.flag("testing.flow_timeout_s", 900)),
            "confirm": bool(ctx.flag("testing.agent_confirm_fail", True))}


# ---- one flow ------------------------------------------------------------------------------------------


def flow_verdicts(run: dict | None, flow: maestro_index.Flow, cases: list, fr) -> dict[str, dict]:
    """{extid: verdict-shaped dict} for the selected cases one flow run covers.

    `maestro.attribute` decides from the enriched report. One rule is added for this repo's cart flows,
    which name one Kiwi case in `# Covers:` and label no steps: a flow that covers ONE case and FAILs (an
    assertion, not the harness) has failed that case — there is no other case the failure could belong to.
    """
    out = {}
    if run is None:
        why = (f"maestro produced no report (exit {fr.exit_code}): {fr.output.strip()[-300:]}" if fr else
               "the flow did not run")
        for c in cases:
            out[c.extid] = {"status": ERROR, "reason": why, "cause": ""}
        return out
    claim = {"covers": set(flow.cases), "labels": maestro.label_cases(flow.path.read_text(errors="ignore"))}
    attributed = maestro.attribute(run, claim)
    for c in cases:
        keys = [c.extid] + ([f"KIWI-{c.store_id}"] if getattr(c, "store_id", "") else [])
        status, detail = next((attributed[k] for k in keys if k in attributed),
                              ("BLOCKED", f"flow {flow.name} reported nothing for this case"))
        if status == "BLOCKED" and run["outcome"] == "FAIL" and len(flow.cases) == 1 and \
                "claimed via Covers" in detail:
            status = "FAIL"
            at = f" at step '{run['failed_label']}'" if run["failed_label"] else ""
            detail = f"flow {flow.name} failed{at}" + \
                (f": {run['message'][:200]}" if run["message"] else "")
        # A flow cannot tell an app bug from a stale flow or a backend that differs from the one it was
        # recorded on (KIWI-205, 2026-11-18: the flow stopped on the product page, nothing was wrong with the
        # app) — so its FAIL is for a person to triage, never a "product" verdict.
        cause = TRIAGE if status == "FAIL" else ("environment" if status in ("BLOCKED", ERROR) else "")
        out[c.extid] = {"status": status, "reason": detail, "cause": cause}
    return out


def flow_comment(extid: str, verdict: dict, run: dict | None, flow, header: dict, report_name: str) -> str:
    if run is None:
        return "\n".join([f"## {extid} — {verdict['status']}", "", f"- flow: `{flow.name}`",
                          f"- device: {header['device']} · app {header['build']}", "", verdict["reason"]])
    declared = [t for t, ids in maestro.label_cases(flow.path.read_text(errors="ignore")) if extid in ids]
    steps = maestro.case_steps(run, {"covers": set(flow.cases), "labels": maestro.label_cases(
        flow.path.read_text(errors="ignore"))}).get(extid)
    return maestro.evidence_comment(extid, verdict["status"], verdict["reason"], run, steps, header["build"],
                                    header["environment"], report_name, declared)


# ---- one agent case --------------------------------------------------------------------------------------


def run_agent_case(stage: Stage, ctx: RunContext, case, folder: Path, label: str, settings: dict,
                   serial: str, notes: str, repo: Path) -> dict:
    """One agent session for one case on one device -> its verdict (the agent's when it stands, else Python's)."""
    from harness.engines import Containment, EngineRequest
    from harness.events import events_path
    from rfc_agent.hooks import guard_device as gd

    if folder.exists():
        shutil.rmtree(folder)
    folder.mkdir(parents=True)
    count_file = Path(tempfile.gettempdir()) / f"rfc-agent-device-{uuid.uuid4().hex}"
    rel = str(folder.relative_to(ctx.run_dir))
    req = EngineRequest(
        prompt=app_agent.build_prompt(case, rel, settings, serial, notes, CHECK_VERDICT, VERDICT_EVERY),
        cwd=ctx.run_dir, label=label, trace_path=events_path(ctx.run_dir, label),
        timeout_s=settings["case_timeout_s"] + GRACE_S, skill_path=SKILL_PATH,
        model=(ctx.flag("model", "") or None), live=False,
        env={gd.SERIAL_ENV: serial, gd.APP_ID_ENV: settings["app_id"], gd.CASE_DIR_ENV: str(folder),
             gd.SUBFLOWS_ENV: str(repo / ".maestro" / "flows"), gd.LINK_SCHEMES_ENV: LINK_SCHEMES,
             gd.LINK_HOSTS_ENV: LINK_HOSTS, gd.MAX_ACTIONS_ENV: str(settings["max_actions"]),
             gd.COUNT_FILE_ENV: str(count_file), gd.VERDICT_FILE_ENV: str(folder / ui_agent.VERDICT),
             gd.VERDICT_EVERY_ENV: str(VERDICT_EVERY), gd.FORBID_ENV: settings.get("forbid", "")},
        containment=Containment(writable_roots=(ctx.run_dir,),
                                mcp_servers={app_agent.SERVER: app_agent.maestro_server(folder)}),
        native_ctx={"stage": dataclasses.replace(stage, mcp_servers=(app_agent.SERVER,)), "ctx": ctx},
    )
    t0 = time.monotonic()
    failure, calls, cost, acted, trace = "", None, None, 0, {}
    try:
        summary = ctx.engine.run(req).summary
        if getattr(summary, "is_error", False):
            failure = f"the session ended with an error ({getattr(summary, 'error_reason', None) or 'unknown'})"
    except Exception as exc:  # one case's session must not take the others with it
        failure = f"the session did not finish ({type(exc).__name__}: {str(exc)[:300]})"
        summary = None
    if summary is not None:
        tool_calls = list(getattr(summary, "tool_calls", None) or [])
        calls, acted = app_agent.device_calls(tool_calls), app_agent.device_actions(tool_calls)
        cost = getattr(summary, "cost_usd", None)
        trace = {"tool_calls": tool_calls, "usage": getattr(summary, "usage", None),
                 "num_turns": getattr(summary, "num_turns", None)}
    duration = round(time.monotonic() - t0, 1)

    verdict = read_json(folder / ui_agent.VERDICT, None)
    problems = ui_agent.validate_verdict(verdict, case.extid, folder) if verdict is not None else \
        [f"{case.extid}: the agent wrote no {ui_agent.VERDICT}"]
    if not problems and verdict["status"] in (ui_agent.PASS, ui_agent.FAIL) and calls is not None \
            and calls < len(verdict["parts"]):
        problems = [f"{case.extid}: the verdict judges {len(verdict['parts'])} part(s) but the session's trace "
                    f"holds {calls} device call(s) — it was not worked for"]
    if problems:
        if verdict is not None:
            write_json(folder / "verdict.rejected.json", verdict)
        try:
            used = int(count_file.read_text().strip() or 0)
        except (OSError, ValueError):
            used = 0
        out_of_time = duration >= settings["case_timeout_s"]
        if used >= settings["max_actions"] or out_of_time:
            limit = f"{settings['case_timeout_s']}s" if out_of_time else f"{settings['max_actions']} commands"
            verdict = ui_agent.blocked(case.extid, ui_agent.LIMIT, f"the case used its {limit} without a verdict")
        else:
            verdict = {**ui_agent.blocked(case.extid, "no usable verdict",
                                          "; ".join(([failure] if failure else []) + problems)[:1500], cause=""),
                       "status": ERROR}
    count_file.unlink(missing_ok=True)
    Path(f"{count_file}.verdict").unlink(missing_ok=True)
    draft = draft_state(folder, verdict, settings["app_id"])
    verdict = {**verdict, "duration_s": duration, "cost_usd": cost}
    write_json(folder / ui_agent.VERDICT, verdict)
    return {**verdict, "engine": app_agent.AGENT_ENGINE, "device": serial, "device_calls": calls or 0,
            "device_actions": acted, "cost_known": cost is not None, "_trace": [trace], "draft": draft,
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"), "folder": str(folder)}


def draft_state(folder: Path, verdict: dict, app_id: str) -> dict:
    """What became of the case's draft flow: none (not a PASS, or the agent wrote none), rejected (lint
    errors), or ready (convert-flows may run it). Lint warnings ride along for the reviewer."""
    path = folder / app_agent.DRAFT
    if verdict.get("status") != ui_agent.PASS:
        if path.is_file():
            path.rename(folder / "draft.not-a-pass.yaml")  # a FAIL's steps are not a flow for this case
        return {"state": "none"}
    if not path.is_file():
        return {"state": "none", "why": f"the agent wrote no {app_agent.DRAFT}"}
    errors, warnings = app_agent.lint_draft(path.read_text(encoding="utf-8", errors="replace"), app_id)
    if errors:
        return {"state": "rejected", "why": "; ".join(errors), "warnings": warnings}
    return {"state": "ready", "path": str(path), "warnings": warnings}


def confirm_agent_fail(stage, ctx, case, first: dict, settings, serial, repo) -> dict:
    """Run an agent FAIL once more on the same device. The status stays FAIL either way."""
    folder = ctx.path(app_agent.APP_DIR) / f"{case.extid}.confirm"
    second = run_agent_case(stage, ctx, case, folder, f"{stage.name}.{case.extid}.confirm", settings, serial,
                            "", repo)
    if second["status"] == ui_agent.FAIL:
        note, confirmed = "FAIL again — confirmed", True
    elif second["status"] == ui_agent.PASS:
        note, confirmed = "PASS — the failure did not repeat (flaky); both runs are kept", False
    else:
        note = f"{second['status']} — the second run could not get there, so the failure is NOT confirmed"
        confirmed = False
    return {**first, "confirmation": note, "confirmed": confirmed, "flaky": second["status"] == ui_agent.PASS,
            "_trace": first["_trace"] + second["_trace"],
            "duration_s": round(first["duration_s"] + second["duration_s"], 1),
            "cost_known": first["cost_known"] and second["cost_known"],
            "cost_usd": (first.get("cost_usd") or 0) + (second.get("cost_usd") or 0) or None}


# ---- the stage -----------------------------------------------------------------------------------------


def _row(verdict: dict, header: dict, comment: str, shot: str = "") -> dict:
    finished = datetime.fromisoformat(verdict["finished_at"]) if verdict.get("finished_at") else None
    return {"extid": verdict["extid"], "status": verdict["status"], "playwright_status": verdict["engine"],
            "detail": ui_agent.report(verdict) if verdict["engine"] == app_agent.AGENT_ENGINE else verdict["reason"],
            "comment": comment, "comment_on_pass": True, "duration_s": verdict.get("duration_s", 0.0),
            "finished_at": finished, "executor": f"maestro-{verdict['engine']}",
            "props": {"device": verdict.get("device"), "app_variant": header["variant"],
                      "flow": verdict.get("flow"), "account": verdict.get("profile")},
            "attachments": [{"name": "screenshot", "path": shot}] if shot and Path(shot).is_file() else []}


def _agent_shot(verdict: dict) -> str:
    wanted = [ui_agent.NOT_MET] + ([ui_agent.MET] if verdict["status"] == ui_agent.PASS else [])
    clauses = [c for p in verdict.get("parts") or [] for c in p.get("expected") or [] if c.get("evidence")]
    name = next((c["evidence"] for state in wanted for c in clauses if c.get("state") == state), "")
    return str(Path(verdict["folder"]) / Path(name).name) if name else ""


def _engine(stage: Stage, ctx: RunContext) -> StageResult:
    from rfc_agent import decisions

    t0, started = time.monotonic(), datetime.now(UTC)
    feature = feature_of(ctx)
    store = get_store(ctx)
    repo = Path(str(ctx.flag("testing.maestro_repo", "~/o_1/maestro-automation"))).expanduser()
    android = Android(repo, str(ctx.flag("testing.app_variant", "dev-release") or "dev-release"))
    include_manual = bool(ctx.flag("testing.agent_include_manual", False))
    try:
        cases = app_agent.select_cases(store, feature, include_manual=include_manual, only=only_cases(ctx))
    except Exception as exc:
        raise EngineFailure(f"execute_app could not read the cases from the store ({type(exc).__name__}: "
                            f"{str(exc)[:300]}). Nothing was run.") from None
    if not cases:
        write_json(ctx.path(RESULTS_OUT), {"feature": feature, "results": [], "tally": {}, "not_run": [],
                                           "drafts": []})
        return StageResult(final_text="no CONFIRMED Android UI case — nothing to execute", engine_desc="python")
    problem = android.problem()
    if problem:
        raise EngineFailure(f"execute_app: {problem}.")
    settings = settings_of(ctx, android)
    wanted = [s.strip() for s in str(ctx.flag("testing.devices", "") or "").split(",") if s.strip()]
    booted = android.ready(wanted or None)
    versions = {s: android.app_version(s) for s in booted}
    serials = [s for s in booted if versions[s]]
    if not serials:
        raise EngineFailure(f"execute_app: {android.why_not_ready(booted, wanted or None)}. Nothing was run.")
    settings["build"] = settings["build"] or versions[serials[0]]
    if len({versions[s] for s in serials}) > 1:
        decisions.record(ctx, stage.name, "app_build_mismatch", why="the devices carry different app builds; the "
                         "run is named after the first", builds=versions)

    flows = maestro_index.index(repo / ".maestro" / "flows")
    planned = app_agent.plan(cases, flows, settings["mode"])
    not_run = [{"extid": p["case"].extid, "reason": p["why"]} for p in planned if p["engine"] == app_agent.NONE]
    flow_groups: dict[str, list] = {}
    for p in planned:
        if p["engine"] == app_agent.FLOW_ENGINE:
            flow_groups.setdefault(p["flow"].name, []).append(p["case"])
    agent_cases = [p["case"] for p in planned if p["engine"] == app_agent.AGENT_ENGINE]
    roles = device_roles(ctx, repo, serials)
    flow_groups, agent_cases, unplaced = place(roles, flow_groups, agent_cases, flows)
    not_run += unplaced
    decisions.record(ctx, stage.name, "app_cases_planned",
                     why="each CONFIRMED Android case: its VALIDATED flow, else an agent, else not run",
                     flows=sorted(flow_groups), agent=[c.extid for c in agent_cases],
                     not_run=[n["extid"] for n in not_run], devices=roles)

    runnable = [c.extid for c in cases if c.extid not in {n["extid"] for n in not_run}]
    try:
        run = store.create_run(feature, settings["build"], settings["run_env"], runnable, require_tag="exec:ui")
        attached = set(run.attached)
        if include_manual:
            attached |= set(store.create_run(feature, settings["build"], settings["run_env"], runnable,
                                             require_tag="exec:manual").attached)
    except Exception as exc:
        raise EngineFailure(f"execute_app could not create the run in the store ({type(exc).__name__}: "
                            f"{str(exc)[:300]}). Nothing was run.") from None
    header = {"feature": feature, "environment": settings["run_env"], "build": settings["build"],
              "base_url": "", "device": ", ".join(serials), "variant": settings["variant"],
              "app_id": settings["app_id"], "login": "device session", "production": settings["production"],
              "run_id": run.run_id, "run_url": run.url}

    if agent_cases:
        _stage_persona(SKILL_PATH, ctx.run_dir, stage.name)
    ctx.path(NOTES_OUT).unlink(missing_ok=True)
    work = [("flow", flows[name], group) for name, group in flow_groups.items()] + \
        [("agent", None, [case]) for case in agent_cases]

    lock = threading.Lock()
    results, tally, cost = [], {}, 0.0
    tool_calls, unpriced = [], []
    profiles: dict[str, str] = {}

    def save() -> None:
        drafts = [{"extid": r["extid"], **r["draft"]} for r in results
                  if (r.get("draft") or {}).get("state") == "ready"]
        # `environment` is the plain one (`execute` reuses it as --env); the Kiwi run's is `kiwi_environment`
        write_json(ctx.path(RESULTS_OUT), {**header, "environment": settings["environment"],
                                           "kiwi_environment": settings["run_env"],
                                           "results": results, "tally": tally, "not_run": not_run,
                                           "drafts": drafts, "cost_usd": round(cost, 4) or None,
                                           "cost_unknown_for": unpriced, "cases": len(runnable)})
        ctx.path(TABLE_OUT).write_text(results_table(results, not_run) + "\n", encoding="utf-8")

    def record(verdict: dict, comment: str, shot: str) -> None:
        nonlocal cost
        with lock:
            row = _row(verdict, header, comment, shot)
            if verdict["extid"] in attached:
                try:
                    ui_record.record_row(ctx, stage.name, store, run.run_id, row, header)
                except Exception as exc:  # a store hiccup must not lose the verdict on disk
                    verdict["record_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            tally[verdict["status"]] = tally.get(verdict["status"], 0) + 1
            cost += verdict.get("cost_usd") or 0
            if verdict["engine"] == app_agent.AGENT_ENGINE and verdict.get("cost_known") is False:
                unpriced.append(verdict["extid"])
            for trace in verdict.get("_trace") or []:
                tool_calls.extend(trace.get("tool_calls") or [])
            met, total = ui_agent.parts_met(verdict)
            results.append({
                **{k: verdict.get(k) for k in ("extid", "status", "cause", "reason", "blocked_on", "engine",
                                               "flow", "device", "profile", "duration_s", "cost_usd",
                                               "confirmation", "confirmed", "flaky", "draft")},
                "summary": ui_agent.summary(verdict) if verdict["engine"] == app_agent.AGENT_ENGINE else
                f"{verdict['status']} — {verdict['reason']}",
                "parts_met": met, "parts": total, "recorded": verdict["extid"] in attached,
                "execution_id": row.get("execution_id"), "record_error": verdict.get("record_error")})
            if verdict["status"] != ui_agent.PASS:
                decisions.record(ctx, stage.name, "app_case_not_passed", why=results[-1]["summary"][:300],
                                 extid=verdict["extid"], engine=verdict["engine"])
            save()

    def signed_in(serial: str, profile: str) -> str:
        """"" when the device is (now) signed in to `profile`, else why not. Once per device per profile."""
        if not profile or profiles.get(serial) == profile or roles[serial] == profile:
            return ""  # a device whose role is the profile is kept signed in to it (by hand, on prod)
        code, out = android.sign_in(serial, profile)
        if code == 0:
            profiles[serial] = profile
            return ""
        profiles.pop(serial, None)
        return (f"the device could not be signed in to account profile {profile!r} unattended"
                if code == SKIPPED_EXIT else f"signing in to {profile!r} failed (exit {code}): {out.strip()[-200:]}")

    def notes_now() -> str:
        path = ctx.path(NOTES_OUT)
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def merge_notes(folder: Path) -> None:
        own = folder / app_agent.NOTES
        if not own.is_file():
            return
        with lock:
            text = notes_now()
            for line in map(ui_agent.usable_note, own.read_text(encoding="utf-8", errors="replace").splitlines()):
                if line and line not in text:
                    text += ("" if not text or text.endswith("\n") else "\n") + line + "\n"
            path = ctx.path(NOTES_OUT)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("\n".join(text.splitlines()[-40:]) + "\n", encoding="utf-8")

    def do_flow(serial: str, flow, group: list) -> None:
        profile = flow.requires
        why = signed_in(serial, profile)
        fr, parsed = None, None
        if not why:
            fr = android.run_flow(flow.path, serial, ctx.path(REPORTS_DIR) / serial, settings["flow_timeout_s"])
            runs = maestro.parse_report(fr.report) if fr.report.is_file() else []
            parsed = next((r for r in runs if r["flow"] == flow.name), runs[0] if runs else None)
        for case in group:
            if why:
                v = {"status": ui_agent.BLOCKED, "reason": why, "cause": "test data"}
            else:
                v = flow_verdicts(parsed, flow, [case], fr)[case.extid]
            verdict = {"extid": case.extid, **v, "engine": app_agent.FLOW_ENGINE, "flow": flow.name,
                       "device": serial, "profile": profile, "duration_s": fr.duration_s if fr else 0.0,
                       "cost_usd": None, "blocked_on": why if why else "",
                       "finished_at": datetime.now(UTC).isoformat(timespec="seconds"), "draft": {"state": "none"}}
            folder = app_agent.case_dir(ctx.run_dir, case.extid)
            folder.mkdir(parents=True, exist_ok=True)
            write_json(folder / ui_agent.VERDICT, verdict)
            shot = (parsed or {}).get("fields", {}).get("screenshot", "") if parsed else ""
            record(verdict, flow_comment(case.extid, verdict, parsed, flow, header,
                                         fr.report.name if fr else ""), shot)

    def do_agent(serial: str, case) -> None:
        profile = app_agent.profile_of(case)
        why = signed_in(serial, profile)
        if why:
            verdict = {**ui_agent.blocked(case.extid, "account", why, cause="test data"),
                       "engine": app_agent.AGENT_ENGINE, "device": serial, "profile": profile, "duration_s": 0.0,
                       "cost_usd": None, "cost_known": True, "draft": {"state": "none"},
                       "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
                       "folder": str(app_agent.case_dir(ctx.run_dir, case.extid))}
            Path(verdict["folder"]).mkdir(parents=True, exist_ok=True)
            write_json(Path(verdict["folder"]) / ui_agent.VERDICT, verdict)
            record(verdict, ui_agent.kiwi_comment(verdict, _agent_header(header, serial)), "")
            return
        case_settings = {**settings, "profile": profile}
        folder = app_agent.case_dir(ctx.run_dir, case.extid)
        verdict = run_agent_case(stage, ctx, case, folder, f"{stage.name}.{case.extid}", case_settings, serial,
                                 notes_now(), repo)
        if verdict["status"] == ui_agent.FAIL and settings["confirm"]:
            verdict = confirm_agent_fail(stage, ctx, case, verdict, case_settings, serial, repo)
        verdict["profile"] = profile
        merge_notes(folder)
        record(verdict, ui_agent.kiwi_comment(verdict, _agent_header(header, serial)), _agent_shot(verdict))

    def worker(serial: str) -> None:
        while True:
            with lock:
                item = take(work, roles, serial, flows)
            if item is None:
                return
            kind, flow, group = item
            try:
                with DeviceLock(serial, ctx.path(LOCKS_DIR)):
                    if kind == "flow":
                        do_flow(serial, flow, group)
                    else:
                        do_agent(serial, group[0])
            except Exception as exc:  # one item's crash must not stop the device's queue
                for case in group:
                    verdict = {"extid": case.extid, "status": ERROR, "engine": kind if kind == "flow" else
                               app_agent.AGENT_ENGINE, "reason": f"{type(exc).__name__}: {str(exc)[:300]}",
                               "cause": "", "device": serial, "duration_s": 0.0, "cost_usd": None,
                               "flow": flow.name if flow else None, "draft": {"state": "none"},
                               "finished_at": datetime.now(UTC).isoformat(timespec="seconds")}
                    record(verdict, f"## {case.extid} — ERROR\n\n{verdict['reason']}", "")

    save()
    threads = [threading.Thread(target=worker, args=(s,), name=f"execute_app-{s}") for s in serials]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    save()
    keep_dated(ctx, started, (RESULTS_OUT, TABLE_OUT))
    text = "  ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "no case could be executed"
    drafts = [r["extid"] for r in results if (r.get("draft") or {}).get("state") == "ready"]
    if not_run:
        text += f"  ·  {len(not_run)} not run"
    if drafts:
        text += f"  ·  {len(drafts)} draft flow(s) ready for /test-agent:convert-flows: {', '.join(drafts)}"
    text += "\n\n" + results_table(results, not_run)
    return StageResult(final_text=text, duration_s=time.monotonic() - t0, cost_usd=cost or None,
                       tool_calls=tool_calls, engine_desc=f"flows x{len(flow_groups)}, agent x{len(agent_cases)} "
                                                          f"on {len(serials)} device(s)")


def device_roles(ctx: RunContext, repo: Path, serials: list[str]) -> dict[str, str]:
    """{serial: role} for the ready devices — testing.device_roles, else the repo's devices.yaml (see
    `devices.assign_roles`)."""
    manifest = repo / devices.MANIFEST
    text = manifest.read_text(encoding="utf-8", errors="replace") if manifest.is_file() else ""
    return devices.assign_roles(serials, devices.parse_manifest(text),
                                devices.parse_role_flag(str(ctx.flag("testing.device_roles", "") or "")))


def _profile(item: tuple, flows: dict) -> str:
    kind, flow, group = item
    return flow.requires if kind == "flow" else app_agent.profile_of(group[0])


def place(roles: dict[str, str], flow_groups: dict, agent_cases: list, flows: dict) -> tuple[dict, list, list]:
    """Drop the work no ready device's role accepts -> (flow_groups, agent_cases, not_run rows)."""
    def fits(profile: str) -> bool:
        return any(devices.accepts(r, profile) for r in roles.values())

    def why(profile: str) -> str:
        have = ", ".join(f"{s}={r}" for s, r in roles.items())
        return (f"no ready device may run a {'guest' if not profile else repr(profile) + ' account'} case "
                f"(device roles: {have}) — add a pool device or one with that role (--device-roles)")

    unplaced = []
    kept_flows = {}
    for name, group in flow_groups.items():
        if fits(flows[name].requires):
            kept_flows[name] = group
        else:
            unplaced += [{"extid": c.extid, "reason": why(flows[name].requires)} for c in group]
    kept_agent = []
    for case in agent_cases:
        if fits(app_agent.profile_of(case)):
            kept_agent.append(case)
        else:
            unplaced.append({"extid": case.extid, "reason": why(app_agent.profile_of(case))})
    return kept_flows, kept_agent, unplaced


def take(work: list, roles: dict[str, str], serial: str, flows: dict):
    """Pop the next item `serial`'s role accepts, or None. A pool device leaves to a dedicated device (guest
    or an account's) the work that device will do, unless nothing else is left for it."""
    role = roles[serial]
    fits = [i for i, item in enumerate(work) if devices.accepts(role, _profile(item, flows))]
    if role == devices.POOL:
        dedicated = {r for s, r in roles.items() if s != serial and r != devices.POOL}
        own = [i for i in fits if (_profile(work[i], flows) or devices.GUEST) not in dedicated]
        fits = own or fits
    return work.pop(fits[0]) if fits else None


def _agent_header(header: dict, serial: str) -> dict:
    """What `ui_agent.kiwi_comment` prints on its environment line, for a device instead of a browser."""
    return {**header, "base_url": f"{header['app_id']} ({header['variant']})", "device": serial}


def results_table(results: list[dict], not_run: list[dict] = ()) -> str:
    """Case | Engine | Verdict | Time | Cost | What was found — with a Total row, and the cases not run."""
    def cell(text) -> str:
        return " ".join(str(text or "").split()).replace("|", "/")

    lines = ["| Case | Engine | Verdict | Time | Cost | What was found |", "|---|---|---|---|---|---|"]
    total_s, total_cost = 0.0, 0.0
    for r in results:
        status = str(r.get("status") or "?")
        if status == ui_agent.FAIL and r.get("confirmed") is False:
            status += " (flaky)" if r.get("flaky") else " (not confirmed)"
        engine = f"flow `{r['flow']}`" if r.get("engine") == app_agent.FLOW_ENGINE else "agent"
        cost = r.get("cost_usd")
        total_s += r.get("duration_s") or 0
        total_cost += cost or 0
        found = r.get("reason") or ""
        if r.get("parts"):
            found += f" ({r['parts_met']} of {r['parts']} parts met)"
        draft = (r.get("draft") or {}).get("state")
        if draft in ("ready", "rejected"):
            found += f" · draft flow {draft}"
        lines.append(f"| {cell(r.get('extid'))} | {engine} | {status} | {ui_agent.took(r.get('duration_s'))} | "
                     f"{'—' if cost is None else f'${cost:.2f}'} | {cell(found)} |")
    lines.append(f"| **Total** | | {len(results)} cases | {ui_agent.took(total_s)} | ${total_cost:.2f} | |")
    if not_run:
        lines += ["", "Not run:", ""] + [f"- {n['extid']} — {n['reason']}" for n in not_run]
    return "\n".join(lines)


def _gate(ctx: RunContext) -> list[str]:
    data, bad = read_json_gated(ctx.path(RESULTS_OUT), expect=dict)
    if bad:
        return bad
    results = data.get("results")
    if not isinstance(results, list) or not isinstance(data.get("not_run"), list):
        return [f"{RESULTS_OUT} has no results / not_run arrays"]
    out = []
    for row in results:
        extid = row.get("extid", "<no extid>")
        if row.get("status") not in (*ui_agent.STATUSES, ERROR):
            out.append(f"{extid}: status {row.get('status')!r} is not PASS/FAIL/BLOCKED/ERROR")
        verdict = read_json(app_agent.case_dir(ctx.run_dir, extid) / ui_agent.VERDICT, None)
        if not isinstance(verdict, dict) or verdict.get("status") != row.get("status"):
            out.append(f"{extid}: {RESULTS_OUT} says {row.get('status')} but its {ui_agent.VERDICT} does not")
    tally = data.get("tally") or {}
    if isinstance(tally, dict) and sum(tally.values()) != len(results):
        out.append(f"tally sums to {sum(tally.values())} but there are {len(results)} results")
    if isinstance(data.get("cases"), int) and len(results) < data["cases"]:
        out.append(f"the run stopped after {len(results)} of {data['cases']} cases — re-run to execute the rest")
    if results and all(r.get("status") == ERROR for r in results):
        out.append("every case ended in ERROR — see each case's verdict.json (is the device usable? is "
                   "`maestro mcp` starting?)")
    return out


STAGE = Stage(
    name="execute_app",
    requires=(),
    outputs=(RESULTS_OUT,),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_engine,
    allow_commands=(CHECK_VERDICT,),
)  # no `mcp_servers` here: the device server is defined per case (`run_agent_case`)
