"""`convert_flows` — turn the draft flows an `execute_app` agent PASS left behind into flows that run green.

Run on its own, after `execute` has recorded its verdicts (`/test-agent:convert-flows`), never as part of it
(decided 2026-11-17). For each case whose draft `execute_app` marked `ready`:

  1. `flow.yaml` = the draft in the repo's house style (`app_agent.flow_text`: Covers / STATUS / REQUIRES
     header, `appId: ${APP_ID}`, tag `agent-draft`, screenshots dropped);
  2. a plain `maestro test` of it on a device — no model. Green on the first run is the common case when
     the agent's commands were sound;
  3. red -> up to `testing.convert_fix_rounds` rounds in which a model, given the enriched failure (the
     failing step, what was on screen) and the device to inspect, edits `flow.yaml` and nothing else; the
     flow is re-run plainly after each round. It stops early when a round fails at the same step for the
     same reason as the one before (no progress), the draft-to-green rule of maestro-automation;
  4. green -> the header's STATUS becomes `VALIDATED (green) … by convert_flows`; stuck -> it stays DRAFT
     with the last failure in `conversions.json`.

None of these attempts is recorded as an execution in Kiwi (a throwaway attempt must not bury the verdict,
`MAESTRO_DRAFT_TO_GREEN.md` step 2b). The case's existing execution gets ONE comment with the outcome.
Nothing is written into the maestro-automation repo here: `python -m workflows.testing.convert_flows
promote` does that, on a branch, when someone asks.
"""

from __future__ import annotations

import dataclasses
import re
import shutil
import tempfile
import threading
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path

from harness.contracts import EngineFailure, RunContext, Stage, StageResult

from ... import app_agent, maestro
from ...devices import Android, DeviceLock
from ...store import get_store
from .. import feature_of, forbid_of, only_cases, read_json, read_json_gated, write_json
from ..execute_app import LINK_HOSTS, LINK_SCHEMES, LOCKS_DIR
from ..execute_app import RESULTS_OUT as APP_RESULTS

SKILL_PATH = Path(__file__).resolve().parents[2] / "skills" / "qa-flow-fixer" / "SKILL.md"
RESULTS_OUT = f"{app_agent.APP_DIR}/{app_agent.CONVERSIONS}"
GREEN, STUCK, REJECTED = "green", "stuck", "rejected"


def _failure_key(run: dict | None) -> str:
    """What a failed run failed on, for the no-progress check: the step label or message, normalised."""
    if not run:
        return "no report"
    return re.sub(r"\d+", "#", (run.get("failed_label") or run.get("message") or run.get("outcome") or "")[:200])


def _failure_text(run: dict | None, fr) -> str:
    if run is None:
        return f"maestro produced no report (exit {fr.exit_code}):\n{fr.output[-2000:]}"
    return (run.get("failure_body") or run.get("message") or "")[:6000]


def mark_validated(text: str, serial: str, when: str) -> str:
    """The flow's first STATUS line -> VALIDATED, so `maestro_index` runs it as a flow once it is promoted."""
    line = (f"# STATUS: VALIDATED (green) on {serial}, {when}, by convert_flows from an agent draft. "
            f"Review the selectors before freezing.")
    return re.sub(r"^#\s*STATUS:.*$", line, text, count=1, flags=re.M)


def fix_prompt(case_extid: str, rel_flow: str, failure: str, serial: str, app_id: str, round_no: int,
               rounds: int) -> str:
    return "\n".join([
        f"The Maestro flow {rel_flow} (case {case_extid}) failed on device {serial}. Fix it so it passes "
        f"for the right reason. Round {round_no} of {rounds}.",
        "",
        "Rules:",
        f"- Edit {rel_flow} and nothing else. Keep its header and its assertions' meaning: a flow made green "
        "by weakening or deleting an assertion proves nothing.",
        "- Fix selectors and waits only. No `point:` taps. Prefer id, then exact text, then a relative selector.",
        f"- You may look at the device with `inspect_screen` and `take_screenshot` (device_id \"{serial}\"), and "
        f"move it with `run` (inline yaml starting `appId: {app_id}` and `---`) to reach the failing screen.",
        "- If the screen shows the app really does not do what the flow asserts, do not change the assertion: "
        "write one line starting `PRODUCT:` to fix-notes.md in the same folder and stop.",
        "",
        "The failure, as Maestro's enriched report gives it:",
        "",
        failure,
    ])


def _fix_round(stage: Stage, ctx: RunContext, folder: Path, extid: str, failure: str, serial: str,
               settings: dict, repo: Path, round_no: int, rounds: int) -> dict:
    from harness.engines import Containment, EngineRequest
    from harness.events import events_path
    from rfc_agent.hooks import guard_device as gd

    label = f"{stage.name}.{extid}.round{round_no}"
    count_file = Path(tempfile.gettempdir()) / f"rfc-agent-device-{uuid.uuid4().hex}"
    req = EngineRequest(
        prompt=fix_prompt(extid, str((folder / app_agent.FLOW).relative_to(ctx.run_dir)), failure, serial,
                          settings["app_id"], round_no, rounds),
        cwd=ctx.run_dir, label=label, trace_path=events_path(ctx.run_dir, label),
        timeout_s=settings["round_timeout_s"], skill_path=SKILL_PATH, model=(ctx.flag("model", "") or None),
        live=False,
        env={gd.SERIAL_ENV: serial, gd.APP_ID_ENV: settings["app_id"], gd.CASE_DIR_ENV: str(folder),
             gd.SUBFLOWS_ENV: str(repo / ".maestro" / "flows"), gd.LINK_SCHEMES_ENV: LINK_SCHEMES,
             gd.LINK_HOSTS_ENV: LINK_HOSTS, gd.MAX_ACTIONS_ENV: "40", gd.COUNT_FILE_ENV: str(count_file),
             gd.FORBID_ENV: settings.get("forbid", "")},
        containment=Containment(writable_roots=(folder,),
                                mcp_servers={app_agent.SERVER: app_agent.maestro_server(folder)}),
        native_ctx={"stage": dataclasses.replace(stage, mcp_servers=(app_agent.SERVER,)), "ctx": ctx},
    )
    cost = None
    try:
        summary = ctx.engine.run(req).summary
        cost = getattr(summary, "cost_usd", None)
    except Exception as exc:  # a round that dies is a round with no fix
        return {"error": f"{type(exc).__name__}: {str(exc)[:200]}", "cost_usd": None}
    finally:
        count_file.unlink(missing_ok=True)
    return {"cost_usd": cost}


def convert_one(stage, ctx, case, row: dict, serial: str, android: Android, settings: dict) -> dict:
    """One draft -> {extid, state, rounds, …}. Plain run first; bounded model rounds only on failure."""
    folder = app_agent.case_dir(ctx.run_dir, case.extid)
    draft = (folder / app_agent.DRAFT).read_text(encoding="utf-8", errors="replace")
    errors, warnings = app_agent.lint_draft(draft, settings["app_id"])
    out = {"extid": case.extid, "device": serial, "rounds": 0, "warnings": warnings, "cost_usd": 0.0,
           "flow": str(folder / app_agent.FLOW)}
    if errors:
        return {**out, "state": REJECTED, "why": "; ".join(errors)}
    flow_path = folder / app_agent.FLOW
    flow_path.write_text(app_agent.flow_text(draft, case, run_label=f"Kiwi run {row.get('run_id') or '?'}",
                                             profile=row.get("profile") or ""), encoding="utf-8")
    reports = ctx.path(f"{app_agent.APP_DIR}/convert-reports/{case.extid}")
    last_key, rounds = None, int(settings["rounds"])
    for attempt in range(rounds + 1):
        fr = android.run_flow(flow_path, serial, reports / f"try{attempt}", settings["flow_timeout_s"])
        runs = maestro.parse_report(fr.report) if fr.report.is_file() else []
        run = runs[0] if runs else None
        if run is not None and run["outcome"] == "PASS":
            when = datetime.now(UTC).strftime("%Y-%m-%d")
            flow_path.write_text(mark_validated(flow_path.read_text(encoding="utf-8"), serial, when), encoding="utf-8")
            errors, warnings = app_agent.lint_draft(flow_path.read_text(encoding="utf-8"), settings["app_id"])
            if errors:  # a fix round may have broken a house rule on the way to green
                return {**out, "state": REJECTED, "rounds": attempt, "why": "green, but " + "; ".join(errors)}
            return {**out, "state": GREEN, "rounds": attempt, "warnings": warnings, "duration_s": fr.duration_s}
        key = _failure_key(run)
        notes = folder / "fix-notes.md"
        if notes.is_file() and "PRODUCT:" in notes.read_text(encoding="utf-8", errors="replace"):
            return {**out, "state": STUCK, "rounds": attempt,
                    "why": "the fixer judged the app does not do what the flow asserts: " +
                           notes.read_text(encoding="utf-8", errors="replace").strip()[:300]}
        if attempt == rounds:
            return {**out, "state": STUCK, "rounds": attempt, "why": f"still failing after {rounds} round(s): {key}"}
        if last_key is not None and key == last_key:
            return {**out, "state": STUCK, "rounds": attempt,
                    "why": f"no progress: failed at the same step twice ({key})"}
        last_key = key
        before = flow_path.read_text(encoding="utf-8")
        res = _fix_round(stage, ctx, folder, case.extid, _failure_text(run, fr), serial, settings,
                         android.repo, attempt + 1, rounds)
        out["cost_usd"] += res.get("cost_usd") or 0
        if flow_path.read_text(encoding="utf-8") == before and not notes.is_file():
            return {**out, "state": STUCK, "rounds": attempt + 1,
                    "why": f"the fix round changed nothing ({res.get('error') or key})"}
    return {**out, "state": STUCK}


def outcome_comment(extid: str, result: dict) -> str:
    state = result["state"]
    head = {GREEN: "a validated flow is ready", STUCK: "the draft flow is stuck", REJECTED: "the draft was rejected"}
    lines = [f"## {extid} — draft flow: {head.get(state, state)}", "",
             f"- converted by convert_flows on {result.get('device', '?')} after {result.get('rounds', 0)} "
             f"fix round(s)",
             f"- flow file: `{result.get('flow', '')}`"]
    if result.get("why"):
        lines.append(f"- why: {result['why']}")
    for w in result.get("warnings") or []:
        lines.append(f"- review: {w}")
    if state == GREEN:
        lines += ["", "Not yet in maestro-automation: promote it (`convert_flows promote`) and review the branch. "
                      "Until then this case keeps running by agent."]
    return "\n".join(lines)


def _engine(stage: Stage, ctx: RunContext) -> StageResult:
    from rfc_agent import decisions

    t0 = time.monotonic()
    feature = feature_of(ctx)
    app = read_json(ctx.path(APP_RESULTS), None)
    if not isinstance(app, dict):
        raise EngineFailure(f"convert_flows needs {APP_RESULTS} from an `execute` Android run in this run folder.")
    only = only_cases(ctx)
    rows = {r["extid"]: r for r in app.get("results") or []
            if (r.get("draft") or {}).get("state") == "ready" and (not only or r["extid"] in only)}
    if not rows:
        write_json(ctx.path(RESULTS_OUT), {"feature": feature, "results": []})
        return StageResult(final_text="no draft flow is ready — nothing to convert", engine_desc="python")
    if ctx.engine is None:
        raise EngineFailure("convert_flows needs a model engine for its fix rounds — run it through the harness CLI.")
    repo = Path(str(ctx.flag("testing.maestro_repo", "~/o_1/maestro-automation"))).expanduser()
    android = Android(repo, str(app.get("variant") or ctx.flag("testing.app_variant", "dev-release")))
    problem = android.problem()
    if problem:
        raise EngineFailure(f"convert_flows: {problem}.")
    wanted = [s.strip() for s in str(ctx.flag("testing.devices", "") or "").split(",") if s.strip()]
    serials = [s for s in android.ready(wanted or None) if android.app_version(s)]
    if not serials:
        raise EngineFailure(f"convert_flows: no Android device is ready with {android.app_id} installed.")
    store = get_store(ctx)
    cases = {c.extid: c for c in app_agent.select_cases(store, feature, include_manual=True) if c.extid in rows}
    settings = {"app_id": android.app_id, "rounds": int(ctx.flag("testing.convert_fix_rounds", 3)),
                "flow_timeout_s": int(ctx.flag("testing.flow_timeout_s", 900)), "round_timeout_s": 900,
                "forbid": forbid_of(ctx, "convert_flows")[0]}
    from .. import _stage_persona
    _stage_persona(SKILL_PATH, ctx.run_dir, stage.name)

    pending = [cases[e] for e in rows if e in cases]
    missing = [e for e in rows if e not in cases]
    results = [{"extid": e, "state": STUCK, "why": "the case is no longer CONFIRMED for Android in the store"}
               for e in missing]
    lock = threading.Lock()

    def worker(serial: str) -> None:
        while True:
            with lock:
                if not pending:
                    return
                case = pending.pop(0)
            row = {**rows[case.extid], "run_id": app.get("run_id")}
            try:
                with DeviceLock(serial, ctx.path(LOCKS_DIR)):
                    if row.get("profile"):
                        code, _ = android.sign_in(serial, row["profile"])
                        if code != 0:
                            raise RuntimeError(f"could not sign in to {row['profile']!r} (exit {code})")
                    result = convert_one(stage, ctx, case, row, serial, android, settings)
            except Exception as exc:
                result = {"extid": case.extid, "state": STUCK, "device": serial, "rounds": 0,
                          "why": f"{type(exc).__name__}: {str(exc)[:300]}"}
            with lock:
                results.append(result)
                decisions.record(ctx, stage.name, "flow_converted", why=result.get("why") or result["state"],
                                 extid=case.extid, state=result["state"])
                execution = row.get("execution_id")
                if execution:
                    try:
                        store.comment_execution(execution, outcome_comment(case.extid, result))
                    except Exception as exc:
                        result["comment_error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
                write_json(ctx.path(RESULTS_OUT), {"feature": feature, "results": results})

    threads = [threading.Thread(target=worker, args=(s,)) for s in serials]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    write_json(ctx.path(RESULTS_OUT), {"feature": feature, "results": results})
    tally: dict[str, int] = {}
    for r in results:
        tally[r["state"]] = tally.get(r["state"], 0) + 1
    cost = sum(r.get("cost_usd") or 0 for r in results)
    text = "  ".join(f"{k}={v}" for k, v in sorted(tally.items()))
    text += "\n\n" + "\n".join(f"- {r['extid']}: {r['state']}" + (f" — {r['why']}" if r.get("why") else "")
                               for r in results)
    return StageResult(final_text=text, duration_s=time.monotonic() - t0, cost_usd=cost or None,
                       engine_desc=f"convert x{len(rows)} on {len(serials)} device(s)")


def _gate(ctx: RunContext) -> list[str]:
    data, bad = read_json_gated(ctx.path(RESULTS_OUT), expect=dict)
    if bad:
        return bad
    out = []
    for r in data.get("results") or []:
        if r.get("state") not in (GREEN, STUCK, REJECTED):
            out.append(f"{r.get('extid')}: state {r.get('state')!r} is not green/stuck/rejected")
        if r.get("state") == GREEN and not Path(str(r.get("flow") or "")).is_file():
            out.append(f"{r.get('extid')}: green but its flow file is missing")
    return out


STAGE = Stage(
    name="convert_flows",
    requires=(),
    outputs=(RESULTS_OUT,),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_engine,
)


# ---- promotion into maestro-automation (a separate, explicit step) ------------------------------------------


def promote(run_dir: Path, repo: Path, *, feature_folder: str, apply: bool = False,
            today: str | None = None) -> dict:
    """The green flows of a run, copied onto a new branch of the maestro-automation repo — in a git worktree
    beside it, so the checkout people work in is untouched. Nothing is committed or pushed: the branch is for
    a person to review. With `apply=False` only says what it would do."""
    import subprocess

    data = read_json(Path(run_dir) / RESULTS_OUT, {}) or {}
    green = [r for r in data.get("results") or [] if r.get("state") == GREEN and Path(str(r.get("flow"))).is_file()]
    day = today or datetime.now(UTC).strftime("%Y%m%d")
    slug = re.sub(r"[^a-z0-9-]+", "-", feature_folder.lower()).strip("-") or "agent"
    branch = f"agent-flows/{slug}-{day}"
    worktree = Path(f"{Path(repo).expanduser()}-{slug}-{day}")
    target = worktree / ".maestro" / "flows" / slug / "agent"
    plan = {"branch": branch, "worktree": str(worktree),
            "files": [{"from": r["flow"], "to": str(target / f"{r['extid'].lower()}.yaml")} for r in green]}
    if not apply or not green:
        return {**plan, "applied": False}
    if not worktree.exists():
        subprocess.run(["git", "-C", str(Path(repo).expanduser()), "worktree", "add", "-b", branch, str(worktree)],
                       check=True, capture_output=True, text=True)
    target.mkdir(parents=True, exist_ok=True)
    for f in plan["files"]:
        shutil.copyfile(f["from"], f["to"])
    return {**plan, "applied": True}


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json
    import sys

    ap = argparse.ArgumentParser(prog="convert_flows",
                                 description="promote: copy a run's green flows onto a maestro-automation branch")
    ap.add_argument("action", choices=("promote",))
    ap.add_argument("run_dir")
    ap.add_argument("--repo", default="~/o_1/maestro-automation")
    ap.add_argument("--folder", required=True, help="flows sub-folder for this feature, e.g. cart")
    ap.add_argument("--apply", action="store_true", help="create the worktree branch and copy (default: show only)")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    print(json.dumps(promote(Path(args.run_dir), Path(args.repo), feature_folder=args.folder, apply=args.apply),
                     indent=2))
    return 0
