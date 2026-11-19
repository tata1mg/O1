"""Recording a web UI case's outcome in the store — shared by `execute_web` and `execute_ui_agent`.

One execution per case in the run `<feature> · <env> · <build>`, the failure screenshot attached to the
execution it explains, and — for anything that did not pass — a comment, because Kiwi's run page renders an
execution's comments, not its attachments or long properties: the comment is where a QA reads why.

A row is what `ui_web.results` yields: `extid`, `status`, `playwright_status`, `detail`, `duration_s`,
`finished_at`, `attachments` ([{name, path}]). A row may bring its own comment body (`comment`, Markdown the
executor wrote for Kiwi's renderer; the Evidence section is appended) and ask for it on a PASS too
(`comment_on_pass`); such a row is recorded with `executor: agent` instead of `playwright_status`.
`header` carries `environment`, `build`, `base_url`, `device`, `login`.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from harness.contracts import RunContext

from . import ui_web

#: Playwright attachment name -> the file name it is stored under on the execution. The screenshot only: a
#: trace is 1-2 MB, which XML-RPC sends base64 in one request, and plutokiwi-agent answered such an upload
#: with 503 and stayed down (2026-11-04). The trace stays in the local HTML report; the comment says where.
EVIDENCE = {"screenshot": "screenshot-{extid}"}  # + the extension compact_screenshot gives


def record_row(ctx: RunContext, stage_name: str, store, run_id: str, row: dict, header: dict,
               login: dict | None = None) -> dict:
    """Record one case's execution, its evidence and (unless it passed, or the row asks for one) its comment.

    -> the row as the results file keeps it. Login values are masked in `row["detail"]` in place, before
    anything is written.
    """
    from rfc_agent import decisions

    for value in (login or {}).values():
        row["detail"] = row["detail"].replace(value, "***")  # an error quoting a typed value
    custom = isinstance(row.get("comment"), str) and bool(row["comment"].strip())
    finished_at = row["finished_at"] or datetime.now(UTC)
    # A row with its own comment was judged by an agent unless it says otherwise (`executor`: the Android
    # stage records `maestro-flow` rows this way too); `props` adds what that executor knows (device, flow).
    props = {"environment": header["environment"], "build": header["build"],
             "base_url": header.get("base_url", ""),
             **({"executor": row.get("executor") or "agent"} if custom else
                {"playwright_status": row["playwright_status"]}),
             **{k: str(v) for k, v in (row.get("props") or {}).items() if v not in (None, "")}}
    execution_id = store.record_execution(
        run_id, row["extid"], row["status"], actual=row["detail"], duration_s=row["duration_s"],
        props=props, finished_at=finished_at)
    row["execution_id"] = execution_id  # for a later comment on the same execution (convert_flows)
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
            decisions.record(ctx, stage_name, "ui_evidence_attached",
                             why="the failure's screenshot (as JPEG) kept beside the execution it explains",
                             extid=row["extid"], name=name, bytes=len(data), original_bytes=path.stat().st_size)
    if row["status"] != "PASS" or row.get("comment_on_pass"):
        store.comment_execution(
            execution_id,
            _own_comment(row, store, execution_id, attached_now, login, too_big) if custom else
            evidence_comment(row, header, store, execution_id, attached_now, login, too_big))
    return {k: row.get(k) for k in ("extid", "status", "playwright_status", "detail", "duration_s")}


def evidence_comment(row: dict, header: dict, store, execution_id: str, attached: list[str],
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
    lines += ["### Evidence", ""] + _evidence_lines(row, store, execution_id, attached, too_big, trace)
    return _masked("\n".join(lines), login)


def _masked(text: str, login: dict | None) -> str:
    for value in (login or {}).values():
        text = text.replace(value, "***")
    return text


def _own_comment(row: dict, store, execution_id: str, attached: list[str], login: dict | None,
                 too_big: list[Path] = ()) -> str:
    """The comment a row brings (`row["comment"]`), then the Evidence section when there is any."""
    items = _evidence_lines(row, store, execution_id, attached, too_big, None, alt="screenshot")
    text = row["comment"].rstrip()
    if items:
        text += "\n\n" + "\n".join(["### Evidence", ""] + items)
    return _masked(text, login)


def _evidence_lines(row: dict, store, execution_id: str, attached: list[str], too_big, trace,
                    alt: str = "failure screenshot") -> list[str]:
    """The bullets under "### Evidence": attached screenshots (linked, inline image), too-big notes, trace."""
    lines: list[str] = []
    for name in attached:
        url = store.attachment_url("execution", execution_id, name)
        lines.append(f"- screenshot: [{name}]({url})" if url else f"- screenshot: `{name}` (attached)")
        if url:
            lines += ["", f"![{alt}]({url})", ""]
    for path in too_big:
        lines.append(f"- screenshot (kept locally, {path.stat().st_size // 1024} KB is over the upload limit): "
                     f"`{path}`")
    if trace and trace.is_file():
        lines.append(f"- trace (kept locally, not uploaded): `{trace}` — open with "
                     f"`npx playwright show-trace {trace}` from tools/ui-runner")
    return lines
