"""`execute_web` — run the Playwright tests `derive_ui` wrote and record each case's outcome in the store.

The web counterpart of `execute_be`, with the same shape: one store run named `<feature> · <env> · <build>`
(the SAME run the backend cases land in, when env and build match), one execution per case, a local
results file (`qa/ui/results.json`) so a run whose terminal scrolled away still has its evidence, and the
failure screenshot and trace attached to the execution they explain.

The run attaches every case in `plan.json` — tested AND skipped. A skipped case stays IDLE in the run: it is
the reviewer's to execute by hand, and leaving it out would make the run look complete when it is not.

Login, one of two ways. `--ui-login <file>`: a JSON file `{"phone": …, "otp": …}` for a test account whose
number and OTP stay fixed on the environment; the login step `derive_ui` wrote runs first, every run, so
nothing expires. The values go to the Playwright process's environment only — never into results, logs,
the store, or a case's evidence (the login step is not a case, so its trace is never attached).
`--ui-auth <file>`: a recorded session (`npx playwright codegen --save-storage=…`), passed through, never
read; it expires with the session.

OFF by default (`testing.execute_web`): it drives a browser against a live environment as a real user.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from harness.contracts import EngineFailure, GateFailure, RunContext, Stage

from ... import ui_web
from ...store import get_store
from .. import feature_of, python_stage, read_json, read_json_gated, write_json

RESULTS_OUT = f"{ui_web.UI_DIR}/results.json"
PLAN_IN = f"{ui_web.UI_DIR}/{ui_web.PLAN}"
#: Playwright attachment name -> the file name it is stored under on the execution. The screenshot only: a
#: trace is 1-2 MB, which XML-RPC sends base64 in one request, and plutokiwi-agent answered such an upload
#: with 503 and stayed down (2026-11-04). The trace stays in the local HTML report; the comment says where.
EVIDENCE = {"screenshot": "screenshot-{extid}"}  # + the extension compact_screenshot gives


@python_stage
def _run(stage: Stage, ctx: RunContext) -> str:
    from rfc_agent import decisions

    feature = feature_of(ctx)
    plan = read_json(ctx.path(PLAN_IN), {}) or {}
    tested = [t["extid"] for t in plan.get("tests") or [] if isinstance(t, dict) and t.get("extid")]
    skipped = [s["extid"] for s in plan.get("skipped") or [] if isinstance(s, dict) and s.get("extid")]
    if not tested:
        raise GateFailure("no Playwright test to run: derive_ui tested no case (see qa/ui/plan.json — every "
                          "CONFIRMED web UI case was skipped, or none is CONFIRMED yet).")
    auth = str(ctx.inputs.get("ui_auth") or ctx.flag("testing.ui_auth", "") or "")
    env_name = str(ctx.inputs.get("env") or ctx.flag("testing.env", "") or "")
    template = str(ctx.flag("testing.base_url_template", "") or "")
    base_url = str(ctx.inputs.get("base_url") or ctx.flag("testing.base_url", "") or "") or \
        (template.format(env=env_name) if template and env_name else "")
    build = str(ctx.inputs.get("build") or ctx.flag("testing.build", "") or "")
    if not base_url.startswith(("http://", "https://")):
        raise EngineFailure("execute_web needs --base-url, the web app's origin on the target environment "
                            "(e.g. https://jupiter.1mg.com), or testing.base_url_template with --env.")
    if not env_name or not build:
        raise EngineFailure("execute_web needs --env and --build: they name the run and are recorded on "
                            "every execution.")
    login = _login(ctx)
    if login and not (ctx.path(ui_web.UI_DIR) / ui_web.TESTS_DIR / ui_web.LOGIN_SPEC).is_file():
        raise EngineFailure("--ui-login was given but derive_ui wrote no login step: rerun derive_ui with "
                            "--ui-login so it writes tests/login.setup.ts.")
    if auth and not Path(auth).expanduser().is_file():
        raise EngineFailure(f"--ui-auth {auth} does not exist. Record it with `npx playwright codegen "
                            f"--save-storage=<file> <base url>` and log in.")
    problem = ui_web.runner_problem()
    if problem:
        raise EngineFailure(f"execute_web: {problem}")

    header = {"feature": feature, "environment": env_name, "build": build, "base_url": base_url,
              "login": "test account (login step)" if login else "recorded session" if auth else "none",
              "device": str(ctx.flag("testing.ui_device", "") or "desktop"),
              "tests": len(tested), "not_automated": skipped}
    store = get_store(ctx)
    run = store.create_run(feature, build, env_name, tested + skipped, require_tag="exec:ui")
    header.update(run_id=run.run_id, run_url=run.url, skipped_unconfirmed=list(run.skipped_unconfirmed),
                  skipped_wrong_execution=list(run.skipped_wrong_execution))

    ui = ctx.path(ui_web.UI_DIR)
    report, err = ui_web.run(ui, base_url=base_url, storage_state=auth, login=login,
                             device=str(ctx.flag("testing.ui_device", "") or ""),
                             timeout_s=int(ctx.flag("testing.ui_timeout_s", 60)),
                             headed=bool(ctx.flag("testing.ui_headed", False)))
    if report is None:
        raise EngineFailure(f"Playwright did not produce a report: {err}")

    attached = set(run.attached)
    results, tally = [], {}
    for row in ui_web.results(report):
        if row["extid"] not in attached:
            continue  # not CONFIRMED any more, or retagged since derive_ui: nowhere to record it
        for value in (login or {}).values():
            row["detail"] = row["detail"].replace(value, "***")  # an error quoting a typed value
        finished_at = row["finished_at"] or datetime.now(UTC)
        tally[row["status"]] = tally.get(row["status"], 0) + 1
        execution_id = store.record_execution(
            run.run_id, row["extid"], row["status"], actual=row["detail"], duration_s=row["duration_s"],
            props={"environment": env_name, "build": build, "base_url": base_url,
                   "playwright_status": row["playwright_status"]},
            finished_at=finished_at)
        attached_now, too_big = [], []
        for att in row["attachments"]:
            name = EVIDENCE.get(att["name"], "").format(extid=row["extid"])
            path = Path(att["path"])
            if not name or not path.is_file():
                continue
            data, ext = ui_web.compact_screenshot(path)
            name += ext
            if len(data) > ui_web.MAX_ATTACH:
                too_big.append(path)
                continue
            if store.attach("execution", execution_id, name, data, replace=True):
                attached_now.append(name)
                decisions.record(ctx, stage.name, "ui_evidence_attached",
                                 why="the failure's screenshot (as JPEG) kept beside the execution it explains",
                                 extid=row["extid"], name=name, bytes=len(data), original_bytes=path.stat().st_size)
        if row["status"] != "PASS":
            # Kiwi's run page renders an execution's comments, not its attachments or long properties:
            # the comment is where a QA reads why it failed.
            store.comment_execution(execution_id, _evidence_comment(row, header, store, execution_id,
                                                                    attached_now, login, too_big))
        results.append({k: row[k] for k in ("extid", "status", "playwright_status", "detail", "duration_s")})
    write_json(ctx.path(RESULTS_OUT), {**header, "results": results, "tally": tally,
                                       "html_report": str(ui / ui_web.HTML_REPORT)})
    return "  ".join(f"{k}={v}" for k, v in sorted(tally.items())) or "nothing recorded"


def _evidence_comment(row: dict, header: dict, store, execution_id: str, attached: list[str],
                      login: dict | None, too_big: list[Path] = ()) -> str:
    trace = next((Path(a["path"]) for a in row["attachments"] if a["name"] == "trace"), None)
    failed = ui_web.failed_requests(trace, header["base_url"]) if trace and trace.is_file() else []
    lines = [f"## {row['extid']} — {row['status']}", "",
             f"- environment: {header['environment']} · build `{header['build']}` · {header['base_url']}",
             f"- browser: {header['device']} · login: {header['login']}",
             f"- Playwright: {row['playwright_status']} after {row['duration_s']}s", "",
             "**Why**", "", "```", row["detail"][:4000], "```", ""]
    if failed:
        lines += ["### Requests that failed on the page (from the trace)", ""] + [f"- `{f}`" for f in failed] + [
            "", "_A 5xx here is the environment's backend, not the page under test._", ""]
    lines += ["### Evidence", ""]
    for name in attached:
        url = store.attachment_url("execution", execution_id, name)
        lines.append(f"- screenshot: [{name}]({url})" if url else f"- screenshot: `{name}` (attached)")
        if url:
            lines += ["", f"![failure screenshot]({url})", ""]
    for path in too_big:
        lines.append(f"- screenshot (kept locally, {path.stat().st_size // 1024} KB is over the upload limit): "
                     f"`{path}`")
    if trace and trace.is_file():
        lines.append(f"- trace (kept locally, not uploaded): `{trace}` — open with "
                     f"`npx playwright show-trace {trace}` from tools/ui-runner")
    text = "\n".join(lines)
    for value in (login or {}).values():
        text = text.replace(value, "***")
    return text


def _login(ctx: RunContext) -> dict | None:
    """{"phone", "otp"} from the --ui-login file, or None. Its values are handed to Playwright and dropped."""
    raw = ctx.inputs.get("ui_login") or ctx.flag("testing.ui_login", "")
    if not raw:
        return None
    path = Path(str(raw)).expanduser()
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise EngineFailure(f"--ui-login {path}: not a readable JSON file ({type(exc).__name__})") from None
    if not isinstance(data, dict) or not all(str(data.get(k) or "").strip() for k in ("phone", "otp")):
        raise EngineFailure(f'--ui-login {path} must be a JSON object with non-empty "phone" and "otp"')
    return {"phone": str(data["phone"]).strip(), "otp": str(data["otp"]).strip()}


def _gate(ctx: RunContext) -> list[str]:
    data, bad = read_json_gated(ctx.path(RESULTS_OUT), expect=dict)
    if bad:
        return bad
    results = data.get("results")
    if not isinstance(results, list):
        return [f"{RESULTS_OUT} has no results array"]
    out = [] if results else ["no web UI case was executed — the run recorded nothing"]
    for row in results:
        for field in ("extid", "status", "detail", "duration_s"):
            if field not in row:
                out.append(f"{row.get('extid', '<no extid>')}: results row is missing {field!r}")
        if row.get("status") not in ("PASS", "FAIL", "BLOCKED", "ERROR"):
            out.append(f"{row.get('extid')}: status {row.get('status')!r} is not PASS/FAIL/BLOCKED/ERROR")
    tally = data.get("tally") or {}
    if isinstance(tally, dict) and sum(tally.values()) != len(results):
        out.append(f"tally sums to {sum(tally.values())} but there are {len(results)} results")
    return out


STAGE = Stage(
    name="execute_web",
    requires=(PLAN_IN,),
    outputs=(RESULTS_OUT,),
    build_prompt=lambda ctx: "",
    gate=_gate,
    engine=_run,
)
