"""The `execute_ui_agent` verdict: what an agent that drove a browser through one case must hand back.

No script is derived for this stage. An agent reads the case, acts on the live page, and writes
`qa/agent/<extid>/verdict.json`. This module is the contract for that file — the schema, the rule that
turns a case's parts into one status, and the check that holds a verdict to its evidence.

There is no "partial pass". A case is split into parts (A, B, C…, or one part when the case has none); each
clause of a part's expected result is `met`, `not met` or `not reached`; and the case's status follows by
precedence:

    any clause `not met`      -> FAIL
    else any `not reached`    -> BLOCKED
    else                      -> PASS

"Mostly passed" would bury the clause that failed, Kiwi has no such status, and where the line falls would
be a judgement two runs of one case could make differently. How close a case came is the breakdown's job
(`parts_met`), reported beside the status, never instead of it.

The verdict file:

    {"extid": "TC-CT-01", "status": "FAIL", "cause": "environment",
     "reason": "the package's product page returned 400 Bad Request",
     "parts": [{"label": "B", "expected": [
         {"text": "Opening a package shows its product page", "state": "not met",
          "observed": "the page body is '400 Bad Request'", "evidence": "b-product-page.png"}]}],
     "actions": [{"step": "B1", "action": "click 'Polycystic' package", "saw": "navigated to /labs/…; 400"}],
     "deviations": ["entered through Browse by health category: the home carousels do not render"],
     "observations": ["the page address keeps health_concerns=… after Clear all"],
     "blocked_on": ""}
"""

from __future__ import annotations

import re
from pathlib import Path

AGENT_DIR = "qa/agent"
VERDICT = "verdict.json"

MET, NOT_MET, NOT_REACHED = "met", "not met", "not reached"
STATES = (MET, NOT_MET, NOT_REACHED)
PASS, FAIL, BLOCKED = "PASS", "FAIL", "BLOCKED"
STATUSES = (PASS, FAIL, BLOCKED)
#: Why a case did not pass. `case`: the case expects something the product no longer has, or is not
#: testable as written. `environment`: the test environment, not the feature (a 4xx/5xx page, a flaky load).
CAUSES = ("product", "environment", "case", "test data")
#: Not a verdict an agent may write: Python's, for a case it never handed to an agent.
NOT_EXECUTED = "NOT_EXECUTED"

_RE_EXPECTED = re.compile(r"\*\*Expected Result\*\*\s*(?P<body>.*?)(?=\n\*\*[^*\n]+\*\*\s*\n|\Z)", re.S)
_EMPTY = {"", "-", "n/a", "na", "none", "tbd", "todo", "?"}


#: The tags a web UI case carries in the store (schema.case_tags), and the ones of a web case the
#: generator left for a person — which an agent with a browser may still be able to verify.
WEB_UI_TAGS = ("exec:ui", "surface:web")
MANUAL_WEB_TAGS = ("exec:manual", "surface:web")
RESULTS = "results.json"
NOTES = "notes.md"


def select_cases(store, feature: str, *, include_manual: bool = False, only: set[str] | tuple = ()) -> list:
    """The CONFIRMED cases this stage hands to an agent, as the store's `Case` objects, in store order.

    Web UI cases; with `include_manual` (flag `testing.agent_include_manual`) the web cases tagged
    exec:manual too. `only` (flag `testing.cases`) narrows to those ids.
    """
    cases = list(store.list_cases(feature, status="CONFIRMED", tags=WEB_UI_TAGS))
    if include_manual:
        seen = {c.extid for c in cases}
        cases += [c for c in store.list_cases(feature, status="CONFIRMED", tags=MANUAL_WEB_TAGS)
                  if c.extid not in seen]
    return [c for c in cases if not only or c.extid in only]


def case_dir(run_dir: Path, extid: str) -> Path:
    return run_dir / AGENT_DIR / extid


def expected_result(case_text: str) -> str:
    """The case's Expected Result section (the shape `schema.render_text` writes), or "" when it has none."""
    m = _RE_EXPECTED.search(case_text or "")
    body = (m.group("body") if m else "").strip()
    return "" if body.lower().strip(" .") in _EMPTY else body


def status_from_parts(parts: list[dict]) -> str:
    """The case status its clauses give, by precedence: FAIL over BLOCKED over PASS."""
    states = [c.get("state") for p in parts for c in p.get("expected") or [] if isinstance(c, dict)]
    if NOT_MET in states:
        return FAIL
    return BLOCKED if NOT_REACHED in states else PASS


def parts_met(verdict: dict) -> tuple[int, int]:
    """(parts whose every clause is met, parts)."""
    parts = [p for p in verdict.get("parts") or [] if isinstance(p, dict)]
    met = sum(1 for p in parts
              if p.get("expected") and all(c.get("state") == MET for c in p["expected"] if isinstance(c, dict)))
    return met, len(parts)


def summary(verdict: dict) -> str:
    """One line for the run summary and the store comment: 'FAIL — 2 of 3 parts met; B not met (…)'."""
    status = verdict.get("status", "?")
    met, total = parts_met(verdict)
    line = f"{status} — {met} of {total} parts met" if total else f"{status} — {verdict.get('reason', '')}"
    notes = []
    for part in verdict.get("parts") or []:
        for state in (NOT_MET, NOT_REACHED):
            hit = next((c for c in part.get("expected") or [] if c.get("state") == state), None)
            if hit:
                why = hit.get("observed") if state == NOT_MET else hit.get("blocker")
                notes.append(f"{part.get('label')} {state} ({why})")
                break
    if notes:
        line += "; " + "; ".join(notes)
    return line + (f"; cause: {verdict['cause']}" if verdict.get("cause") else "")


def not_executed(extid: str, why: str) -> dict:
    """The record for a case Python did not hand to an agent (it has no expected result to judge against)."""
    return {"extid": extid, "status": NOT_EXECUTED, "cause": "case", "reason": why, "parts": [],
            "actions": [], "deviations": [], "observations": [], "blocked_on": ""}


def blocked(extid: str, blocked_on: str, reason: str, cause: str = "environment") -> dict:
    """A BLOCKED verdict Python writes when the agent left none (a limit hit, a session that errored)."""
    return {"extid": extid, "status": BLOCKED, "cause": cause, "reason": reason, "parts": [],
            "actions": [], "deviations": [], "observations": [], "blocked_on": blocked_on}


def validate_verdict(verdict, extid: str, folder: Path) -> list[str]:
    """Every problem with one case's verdict. `folder` is the case's `qa/agent/<extid>/`, where its
    evidence files live."""
    if not isinstance(verdict, dict):
        return [f"{extid}: {VERDICT} is not an object"]
    out: list[str] = []
    if verdict.get("extid") != extid:
        out.append(f"{extid}: {VERDICT} names {verdict.get('extid')!r}")
    status = verdict.get("status")
    if status not in STATUSES:
        return out + [f"{extid}: status {status!r} is not PASS/FAIL/BLOCKED — there is no partial pass; "
                      f"mark each clause and let the parts decide"]
    if not str(verdict.get("reason") or "").strip():
        out.append(f"{extid}: no `reason`")
    parts = verdict.get("parts")
    if not isinstance(parts, list):
        return out + [f"{extid}: `parts` missing or not a list"]
    for name in ("actions", "deviations", "observations"):
        if not isinstance(verdict.get(name, []), list):
            out.append(f"{extid}: `{name}` is not a list")
    blocked_on = str(verdict.get("blocked_on") or "").strip()
    if not parts:
        # Only "could not start" has no parts: nothing was judged, so nothing can have passed or failed.
        if status != BLOCKED or not blocked_on:
            out.append(f"{extid}: no `parts` — a {status} needs every clause of the expected result marked; "
                       f"only a BLOCKED with `blocked_on` may have none")
    for part in parts:
        out += _check_part(part, extid, folder)
    if parts and not out:
        computed = status_from_parts(parts)
        if computed != status:
            out.append(f"{extid}: status is {status} but its parts give {computed} (any clause not met -> "
                       f"FAIL; else any not reached -> BLOCKED; else PASS)")
    if status in (PASS, FAIL) and not verdict.get("actions"):
        out.append(f"{extid}: {status} with no `actions` — a verdict needs the steps that led to it")
    if status in (FAIL, BLOCKED) and verdict.get("cause") not in CAUSES:
        out.append(f"{extid}: {status} needs a `cause`, one of: {', '.join(CAUSES)}")
    if status == BLOCKED and not blocked_on:
        out.append(f"{extid}: BLOCKED with no `blocked_on` — name what stopped the run")
    return out


def _check_part(part, extid: str, folder: Path) -> list[str]:
    if not isinstance(part, dict) or not str(part.get("label") or "").strip():
        return [f"{extid}: a part needs a `label`"]
    label = part["label"]
    clauses = part.get("expected")
    if not isinstance(clauses, list) or not clauses:
        return [f"{extid} part {label}: no `expected` clauses"]
    out = []
    for clause in clauses:
        if not isinstance(clause, dict) or not str(clause.get("text") or "").strip():
            out.append(f"{extid} part {label}: an expected clause needs its `text`")
            continue
        text, state = clause["text"], clause.get("state")
        if state not in STATES:
            out.append(f"{extid} part {label}: {text!r} is {state!r} — a clause is met, not met, or not "
                       f"reached; one you did not check must be checked, or name what blocked it")
        elif state == NOT_REACHED:
            if not str(clause.get("blocker") or "").strip():
                out.append(f"{extid} part {label}: {text!r} was not reached but names no `blocker`")
        else:
            if not str(clause.get("observed") or "").strip():
                out.append(f"{extid} part {label}: {text!r} is {state} with no `observed` — say what the page "
                           f"showed")
            why = _evidence_problem(str(clause.get("evidence") or ""), folder)
            if why:
                out.append(f"{extid} part {label}: {text!r} is {state} but its evidence {why}")
    return out


def _evidence_problem(evidence: str, folder: Path) -> str:
    if not evidence.strip():
        return "is missing — save a screenshot or snapshot when you observe it and name the file"
    path = (folder / evidence).resolve()
    if not path.is_relative_to(folder.resolve()):
        return f"{evidence!r} is outside the case's folder"
    if not path.is_file() or not path.stat().st_size:
        return f"{evidence!r} does not exist in {folder.name}/ (or is empty)"
    return ""


# ---- the browser a case's agent drives ------------------------------------------------------------------

#: The MCP server name the stage exposes, the one `rfc_agent.mcp_tools` holds the tool list for.
SERVER = "playwright"
RUNNER_DIR = Path(__file__).resolve().parents[3] / "tools" / "ui-runner"
MCP_CLI = RUNNER_DIR / "node_modules" / "@playwright" / "mcp" / "cli.js"


def browser_problem() -> str:
    """Why the browser server cannot be started, or "" when it can."""
    import shutil

    if not shutil.which("node"):
        return "node is not installed (Playwright MCP needs Node 18+)"
    if not MCP_CLI.is_file():
        return f"Playwright MCP is not installed: run `npm ci` in {RUNNER_DIR}"
    return ""


def browser_server(folder: Path, *, storage_state: str = "", device: str = "", headed: bool = False,
                   browser: str = "") -> dict:
    """The MCP server definition for ONE case: its own in-memory profile, its own output folder.

    Not a config-file entry, because every argument that matters belongs to the case. `--allowed-origins`
    is deliberately absent: it filters every request the page makes, so naming only the run's origin
    would block the CDN and API hosts a real page loads from — and the server's own documentation says it
    is not a security boundary. Where the browser may be SENT is held by the harness's guard instead
    (`rfc_agent.hooks.guard_browser`). Images are left out of tool responses: the agent reads the
    accessibility snapshot, and a screenshot is evidence on disk, not context.
    """
    args = [str(MCP_CLI), "--isolated", "--output-dir", str(folder), "--image-responses", "omit",
            "--ignore-https-errors"]
    if not headed:
        args.append("--headless")
    if storage_state:
        args += ["--storage-state", str(Path(storage_state).expanduser())]
    if device:
        args += ["--device", device]
    if browser:
        args += ["--browser", browser]
    return {"type": "stdio", "command": "node", "args": args}


def host_problem(base_url: str, *, hosts: str = "", environments: str = "", template: str = "") -> str:
    """Why the agent may not drive `base_url`, or "" when it may.

    The stage clicks and types as a real user, so the address has to be one somebody named as a test
    environment: a host in `testing.agent_hosts`, or `testing.base_url_template` filled with an
    environment from `testing.environments`. Anything else — production included — is refused.
    """
    from urllib.parse import urlsplit

    host = (urlsplit(base_url).hostname or "").lower()
    allowed = {h.strip().lower() for h in hosts.split(",") if h.strip()}
    if template:
        for env in (e.strip() for e in environments.split(",")):
            if env:
                allowed.add((urlsplit(template.format(env=env)).hostname or "").lower())
    allowed.discard("")
    if host and host in allowed:
        return ""
    return (f"execute_ui_agent drives a real browser as a real user, and {host or base_url!r} is not a named "
            f"test environment. Add the host to testing.agent_hosts (never production), or use --env with "
            f"testing.environments + testing.base_url_template.")


def browser_calls(tool_calls: list[dict]) -> int:
    """How many browser tool calls a session's trace holds — the proof a verdict was worked for."""
    def is_browser(call: dict) -> bool:
        inp = call.get("input") or {}
        return "browser_" in str(call.get("name") or "") or str(inp.get("tool") or "").startswith("browser_")

    return sum(1 for c in tool_calls or [] if isinstance(c, dict) and is_browser(c))


def report(verdict: dict) -> str:
    """The verdict as the text a reviewer reads on the execution: the summary, then every clause."""
    lines = [summary(verdict), "", verdict.get("reason", "")]
    if verdict.get("confirmation"):
        lines += ["", f"Second run: {verdict['confirmation']}"]
    for part in verdict.get("parts") or []:
        lines += ["", f"Part {part.get('label')}"]
        for c in part.get("expected") or []:
            saw = c.get("observed") if c.get("state") != NOT_REACHED else f"blocked: {c.get('blocker')}"
            lines.append(f"- [{c.get('state')}] {c.get('text')} — {saw}")
    for title, key in (("Departures from the written steps", "deviations"), ("Also noticed", "observations")):
        if verdict.get(key):
            lines += ["", title] + [f"- {x}" for x in verdict[key]]
    if verdict.get("blocked_on"):
        lines += ["", f"Blocked on: {verdict['blocked_on']}"]
    return "\n".join(lines)
