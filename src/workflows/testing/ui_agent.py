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
CAUSES = ("product", "environment", "case", "test data", "limit")
#: `limit`: the run used its actions or its time before it got there. Only ever a reason for BLOCKED.
LIMIT = "limit"
#: The blocker a clause carries in the verdict an agent starts with, before it has looked at anything.
PENDING = "not yet checked"
_STATE_SPELLINGS = {re.sub(r"[\s_-]+", "", s): s for s in STATES}


def normalize(verdict) -> None:
    """Repair, in place, the spellings that mean the same thing: `not_reached`, `Not-Met`, `PASSED`.

    A state is one of three words; an agent that writes `not_reached` has said which. Rejecting the whole
    verdict for the underscore cost a case its five judged clauses in the third pilot.
    """
    if not isinstance(verdict, dict):
        return
    status = str(verdict.get("status") or "").strip().upper()
    verdict["status"] = {"PASSED": PASS, "FAILED": FAIL}.get(status, status) if status else verdict.get("status")
    for part in verdict.get("parts") or []:
        for clause in (part.get("expected") or []) if isinstance(part, dict) else []:
            if isinstance(clause, dict) and isinstance(clause.get("state"), str):
                key = re.sub(r"[\s_-]+", "", clause["state"].strip().lower())
                clause["state"] = _STATE_SPELLINGS.get(key, clause["state"])
#: Not a verdict an agent may write: Python's, for a case it never handed to an agent.
NOT_EXECUTED = "NOT_EXECUTED"

_RE_EXPECTED = re.compile(r"\*\*Expected Result\*\*\s*(?P<body>.*?)(?=\n\*\*[^*\n]+\*\*\s*\n|\Z)", re.S)
_EMPTY = {"", "-", "n/a", "na", "none", "tbd", "todo", "?"}
#: An Expected Result that says there is none. The sanity sheet's import wrote "FLAGGED — no Expected Result
#: was present in the original sanity sheet for this row…" into the field (pilot, 2026-11-16): text is
#: there, an expectation is not, and a session spent on it can only come back BLOCKED.
_RE_PLACEHOLDER = re.compile(
    r"^\W*(flagged\s*[—–:-]|no expected result\b|expected result (is )?(missing|not (given|provided|defined)))", re.I)


#: The tags a web UI case carries in the store (schema.case_tags), and the ones of a web case the
#: generator left for a person — which an agent with a browser may still be able to verify.
WEB_UI_TAGS = ("exec:ui", "surface:web")
MANUAL_WEB_TAGS = ("exec:manual", "surface:web")
RESULTS = "results.json"
#: The same results as a table a person reads: one row per case, always with the time it took.
RESULTS_TABLE = "results.md"
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
    return "" if body.lower().strip(" .") in _EMPTY or _RE_PLACEHOLDER.match(body) else body


# ---- which session a case needs --------------------------------------------------------------------------

GUEST, USER = "guest", "user"
AUTH_TAGS = {"auth:guest": GUEST, "auth:user": USER}
_RE_PRECONDITION = re.compile(r"\*\*Precondition\*\*\s*(?P<body>.*?)(?=\n\*\*[^*\n]+\*\*\s*\n|\Z)", re.S)
# "not logged in" / "logged out" are guest words: they are taken out before the logged-in words are looked
# for, or "User is not logged in" would read as a logged-in case.
_RE_GUEST = re.compile(r"\bguest\b|\bnot\s+(?:yet\s+)?(?:logged|signed)[- ]?in\b|\b(?:logged|signed)[- ]?out\b|"
                       r"\bwithout\s+(?:logging|signing)[- ]?in\b|\bno\s+(?:user\s+)?session\b", re.I)
_RE_USER = re.compile(r"\b(?:logged|signed)[- ]?in\b|\blog(?:ged)?\s+in\s+(?:as|with)\b|"
                      r"\bcare\s*plan\s+(?:user|member)\b", re.I)


def precondition(case_text: str) -> str:
    m = _RE_PRECONDITION.search(case_text or "")
    return (m.group("body") if m else "").strip()


def case_auth(case) -> str:
    """The session a case needs: GUEST, USER, or "" (no opinion: the run's session).

    A Kiwi tag wins (`auth:guest` / `auth:user`): it is explicit. Without one the Precondition decides —
    guest words and no logged-in words -> GUEST; logged-in words and no guest words -> USER; both or
    neither -> no opinion. The words are a heuristic; the tags are the reliable form.
    """
    tags = {str(t).lower() for t in (getattr(case, "tags", ()) or ())}
    for tag, need in AUTH_TAGS.items():
        if tag in tags:
            return need
    pre = precondition(getattr(case, "text", ""))
    guest = bool(_RE_GUEST.search(pre))
    user = bool(_RE_USER.search(_RE_GUEST.sub(" ", pre)))
    return GUEST if guest and not user else USER if user and not guest else ""


# ---- what a run may not touch ----------------------------------------------------------------------------

#: The browser guard's forbidden actions, by group. `testing.agent_allow` names groups the run may touch; the
#: rest are joined into the guard's regex (`testing.agent_forbid`, when set, replaces all of this).
FORBID_GROUPS = {
    "neucoins": r"neu\s*coins?|redeem|wallet",
    "coupons": r"apply\s+(coupon|offer|code)|coupon\s+apply|remove\s+coupon",
    "checkout": r"place\s+order|proceed\s+to\s+(pay|checkout|buy)|check\s*out|pay\s+now|payment",
    "careplan": r"(add|remove|buy|renew|cancel)\s+care\s*plan|care\s*plan\s+(add|remove)",
    "account": r"(save|delete|remove)\s+(this\s+|new\s+)?(address|patient|profile)|delete\s+account",
    "rx": r"upload\s+prescription",
    "logout": r"log\s*out|sign\s*out",
}
#: How the prompt names each group.
FORBID_WORDS = {
    "neucoins": "redeeming or toggling NeuCoins or the wallet", "coupons": "applying or removing a coupon",
    "checkout": "checkout and payment", "careplan": "adding or removing a Care Plan",
    "account": "editing an address, patient or profile, or deleting the account",
    "rx": "uploading a prescription", "logout": "logging out",
}
#: An allowed group the agent must leave as it found it, said in the prompt.
UNDO = {"coupons": "remove every coupon you applied", "neucoins": "switch NeuCoins back to how you found them"}


def allowed_groups(allow: str) -> list[str]:
    """`testing.agent_allow` -> its groups, or ValueError naming an unknown one."""
    groups = [a.strip().lower() for a in (allow or "").split(",") if a.strip()]
    unknown = sorted(set(groups) - set(FORBID_GROUPS))
    if unknown:
        raise ValueError(f"--allow: unknown group(s) {', '.join(unknown)}; choose from "
                         f"{', '.join(FORBID_GROUPS)}")
    return [g for g in FORBID_GROUPS if g in groups]


def forbid_pattern(allow: str = "", override: str = "") -> str:
    """The guard's regex: `override` (testing.agent_forbid) when set, else every group not allowed."""
    if override:
        return override
    allowed = set(allowed_groups(allow))
    return "|".join(p for g, p in FORBID_GROUPS.items() if g not in allowed)


def allow_lines(allowed) -> list[str]:
    """What the prompt says about forbidden actions: the same groups the guard holds it to."""
    refused = [FORBID_WORDS[g] for g in FORBID_GROUPS if g not in allowed]
    out = []
    if allowed:
        undo = "; ".join(UNDO[g] for g in allowed if g in UNDO)
        out.append(f"Allowed in this run: {', '.join(FORBID_WORDS[g] for g in allowed)}. Whatever you do with "
                   f"them you undo before you finish{' (' + undo + ')' if undo else ''}, exactly like the cart "
                   f"rule; if you could not, say so under `deviations`.")
    if refused:
        out.append(f"Some actions are refused in this run whatever the case says: {'; '.join(refused)}. Do not "
                   f"try them and do not look for a way round a refusal. A clause that needs one is `not "
                   f"reached`.")
    return out


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
    if status == FAIL and verdict.get("confirmed") is False:
        status = "FAIL (flaky: passed on the second run)" if verdict.get("flaky") else "FAIL (not confirmed)"
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
    return {"extid": extid, "status": BLOCKED, "cause": LIMIT if blocked_on == LIMIT else cause,
            "reason": reason, "parts": [], "actions": [], "deviations": [], "observations": [],
            "blocked_on": blocked_on}


def validate_verdict(verdict, extid: str, folder: Path) -> list[str]:
    """Every problem with one case's verdict. `folder` is the case's `qa/agent/<extid>/`, where its
    evidence files live."""
    if not isinstance(verdict, dict):
        return [f"{extid}: {VERDICT} is not an object"]
    normalize(verdict)
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
    if status == FAIL and verdict.get("cause") == LIMIT:
        out.append(f"{extid}: a FAIL's cause cannot be `limit` — a clause was not met, so say whether that is "
                   f"the product, the environment, the case or the test data")
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
            # Judged on the thing the case names, or on something like it somewhere else? Both pilots
            # produced a clause marked met (or not met) from a similar element on another screen, and each
            # time the agent said so in its own words — so it is asked, and a "no" cannot be a judgement.
            if clause.get("on_target") is not True:
                out.append(
                    f"{extid} part {label}: {text!r} is {state} without `on_target: true`. Set it only when "
                    f"you observed the very screen and element the case names. If you looked at a similar "
                    f"one elsewhere, the clause is `not reached` and what you saw goes under `observations`")
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
        # named by its path from the run dir (qa/agent/<id>/shot.png) rather than by its bare name
        path = folder.resolve() / Path(evidence).name
        if Path(evidence).is_absolute() or ".." in Path(evidence).parts or not path.is_file():
            return f"{evidence!r} is outside the case's folder"
    elif not path.is_file():
        path = folder.resolve() / Path(evidence).name
    if not path.is_file() or not path.stat().st_size:
        return f"{evidence!r} does not exist in {folder.name}/ (or is empty)"
    return ""


# ---- the browser a case's agent drives ------------------------------------------------------------------

#: The MCP server name the stage exposes, the one `rfc_agent.mcp_tools` holds the tool list for.
SERVER = "playwright"
RUNNER_DIR = Path(__file__).resolve().parents[3] / "tools" / "ui-runner"
MCP_CLI = RUNNER_DIR / "node_modules" / "@playwright" / "mcp" / "cli.js"


def browser_problem(browser: str = "") -> str:
    """Why the browser server cannot be started, or "" when it can.

    `browser` is `testing.agent_browser`. Empty means the server's bundled Chromium, and that build is
    its OWN: @playwright/mcp carries a newer Playwright than the runner's, so `npx playwright install` in
    tools/ui-runner installs a different revision. Checked here because the alternative is a paid session
    per case that each discover the browser will not start.
    """
    import json
    import os
    import shutil
    import sys

    if not shutil.which("node"):
        return "node is not installed (Playwright MCP needs Node 18+)"
    if not MCP_CLI.is_file():
        return f"Playwright MCP is not installed: run `npm ci` in {RUNNER_DIR}"
    if browser:
        return ""
    nested = MCP_CLI.parent / "node_modules"
    try:
        listed = json.loads((nested / "playwright-core" / "browsers.json").read_text())["browsers"]
        revision = next(b["revision"] for b in listed if b["name"] == "chromium")
    except (OSError, ValueError, KeyError, StopIteration):
        return ""  # a package layout we do not recognise: let the server speak for itself
    cache = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or (
        "~/Library/Caches/ms-playwright" if sys.platform == "darwin" else "~/.cache/ms-playwright")
    if (Path(cache).expanduser() / f"chromium-{revision}").is_dir():
        return ""
    return (f"Playwright MCP needs Chromium build {revision}, which is not installed. Either run "
            f"`node {nested / 'playwright' / 'cli.js'} install chromium`, or use an installed browser with "
            f"--set testing.agent_browser=chrome.")


_RE_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,120}")


def safe_id(extid: str) -> bool:
    """Whether a case id can name the case's folder and log label. Ids this workflow writes always can; one
    read from a hand-written Kiwi tag is free text, and `..` or a slash there would point outside."""
    return bool(_RE_SAFE_ID.fullmatch(extid or "")) and ".." not in extid


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
            f"test environment. If it is one, name it: pass --hosts {host or '<host>'} to `execute ui-agent` "
            f"(or set testing.agent_hosts), or add the environment to testing.environments. If it is "
            f"PRODUCTION, say so: pass --production, which asks you to confirm the account is a test account.")


def browser_calls(tool_calls: list[dict]) -> int:
    """How many browser tool calls a session's trace holds — the proof a verdict was worked for."""
    def is_browser(call: dict) -> bool:
        inp = call.get("input") or {}
        return "browser_" in str(call.get("name") or "") or str(inp.get("tool") or "").startswith("browser_")

    return sum(1 for c in tool_calls or [] if isinstance(c, dict) and is_browser(c))


def browser_actions(tool_calls: list[dict]) -> int:
    """The browser calls that ACT (what the per-case limit counts) — looking at the page is not one."""
    from rfc_agent.mcp_tools import BROWSER_ACTION_TOOLS

    def name(call: dict) -> str:
        raw = str((call.get("input") or {}).get("tool") or call.get("name") or "")
        return raw.rsplit("__", 1)[-1]

    return sum(1 for c in tool_calls or [] if isinstance(c, dict) and name(c) in BROWSER_ACTION_TOOLS)


#: A note is a fact about the environment for the next case. One that names a verdict is advice about the
#: outcome, and the pilot showed where that leads: "any case starting here will be BLOCKED" made the next
#: agents stop looking for another route.
_RE_VERDICT_TALK = re.compile(r"\b(BLOCKED|PASS(ED)?|FAIL(ED)?|verdict|not reached|not met)\b")
NOTE_MAX = 500


def usable_note(line: str) -> str:
    """A note line as it may be passed on, or "" when it should not be: whole, never cut mid-sentence."""
    line = line.strip()
    if not line or line.startswith("#") or _RE_VERDICT_TALK.search(line):
        return ""
    if len(line) > NOTE_MAX:
        cut = line[:NOTE_MAX].rfind(". ")
        return line[:cut + 1] if cut > 20 else ""  # a whole first sentence, or nothing
    return line


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


# ---- the verdict as the comment on its Kiwi execution ---------------------------------------------------

#: Kiwi's comment box. The renderer is only known to handle `##`/`###` headings, `-` bullets, `**bold**`,
#: backtick spans and fences (see maestro.evidence_comment); no tables, no `<details>`, and a heading needs a
#: blank line before it. This comment is prose, so it uses headings, bullets and bold only.
COMMENT_MAX = 6000
_ELLIPSIS = "…"
_RE_PAIRED_SQ = re.compile(r"(?<![\w'])'([^'\n]+?)'(?![\w'])")
_RE_OPEN_DQ = re.compile(r'(^|[\s(\[{—–-])"')
_RE_LEADING_MARKUP = re.compile(r"^(?:#+|[-*+>]+(?=\s)|>+)\s*")
_MARKS = {MET: "✅ **Met**", NOT_MET: "❌ **Not met**", NOT_REACHED: "⚪ **Not reached**"}


def _plain(text) -> str:
    """Agent-written text, made safe for one bullet of a Kiwi comment.

    Kiwi HTML-escapes what it stores, and its renderer shows the escape literally wherever a straight quote
    sat (`'` as `&#x27;`, `"` as `&quot;`), so quotes become typographic ones, which are not escaped:
    a phrase in paired single quotes becomes “…”, any other `'` (CTA's, I'm) becomes ’, and straight double
    quotes become “ ”. Newlines and runs of whitespace collapse to one space, so a multi-line sentence cannot
    end its bullet. Markup that would restructure the comment is removed: backticks (a fence or a span would
    be syntax-highlighted and shown unescaped) and a leading `#`, `-`, `*`, `+` or `>`. `<`, `>` and `&` are
    left alone: Kiwi escapes them and, outside a code block, they render correctly.
    """
    text = " ".join(str(text or "").split()).replace("`", "")
    text = _RE_LEADING_MARKUP.sub("", text)
    text = _RE_PAIRED_SQ.sub(r"“\1”", text).replace("'", "’")
    text = _RE_OPEN_DQ.sub(lambda m: m.group(1) + "“", text).replace('"', "”")
    return text


def _cut(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:max(limit - 1, 0)].rstrip() + _ELLIPSIS


def kiwi_comment(verdict: dict, header: dict) -> str:
    """The verdict as Markdown a person reads on Kiwi's run page: headline, what the agent found, one
    section per part with a bullet per clause, then departures and other observations.

    Prose only, no code fences or backtick spans (see `_plain`). Over `COMMENT_MAX` characters the longest
    `saw:`/`blocked by:` texts are shortened first, then the other free text; the structure is never cut.
    """
    status = str(verdict.get("status") or "?")
    qualifier = ""
    if status == FAIL and verdict.get("confirmed") is False:
        qualifier = " (flaky)" if verdict.get("flaky") else " (not confirmed)"
    elif status == FAIL and verdict.get("confirmed"):
        qualifier = " (confirmed)"
    #: every free-text field, by key, so truncation can find the longest
    texts: dict[tuple, str] = {("found",): _plain(verdict.get("reason"))}
    parts = [p for p in verdict.get("parts") or [] if isinstance(p, dict)]
    for i, part in enumerate(parts):
        for j, c in enumerate(c for c in part.get("expected") or [] if isinstance(c, dict)):
            texts[("clause", i, j)] = _plain(c.get("text"))
            texts[("saw", i, j)] = _plain(c.get("blocker") if c.get("state") == NOT_REACHED else c.get("observed"))
    for key in ("deviations", "observations"):
        for k, item in enumerate(verdict.get(key) or []):
            texts[(key, k)] = _plain(item)
    if verdict.get("blocked_on"):
        texts[("blocked_on",)] = _plain(verdict["blocked_on"])

    def render() -> str:
        met, total = parts_met(verdict)
        lines = [f"## {_plain(verdict.get('extid'))} — {status}{qualifier}", ""]
        if texts[("found",)]:
            lines += [f"**What the agent found:** {texts[('found',)]}", ""]
        cause = f"cause: {_plain(verdict['cause'])}" if verdict.get("cause") and status != PASS else ""
        if total:
            lines.append(f"- result: {met} of {total} parts met" + (f" · {cause}" if cause else ""))
        elif cause:
            lines.append(f"- {cause}")
        if verdict.get("confirmation"):
            lines.append(f"- second run: {_plain(verdict['confirmation'])}")
        env = f"{header.get('environment')}" + (" · PRODUCTION" if header.get("production") else "")
        lines.append(f"- environment: {_plain(env)} · build {_plain(header.get('build'))} · "
                     f"{_plain(header.get('base_url'))}")
        run = [f"browser: {_plain(header.get('device'))}", f"login: {_plain(header.get('login'))}"]
        if verdict.get("duration_s") is not None:
            run.append(f"time: {took(verdict['duration_s'])}")
        if "cost_usd" in verdict:
            cost = verdict["cost_usd"]
            run.append(f"cost: ${cost:.2f}" if isinstance(cost, int | float) and not isinstance(cost, bool)
                       else "cost: unknown")
        lines.append("- " + " · ".join(run))
        for i, part in enumerate(parts):
            lines += ["", f"### Part {_plain(part.get('label'))}", ""]
            for j, c in enumerate(c for c in part.get("expected") or [] if isinstance(c, dict)):
                state = c.get("state")
                lines.append(f"- {_MARKS.get(state, _plain(state))} — {texts[('clause', i, j)]}")
                if state == NOT_REACHED:
                    lines.append(f"  - blocked by: {texts[('saw', i, j)]}")
                else:
                    lines.append(f"  - saw: {texts[('saw', i, j)]}")
        if not parts and ("blocked_on",) in texts:
            lines += ["", f"- blocked on: {texts[('blocked_on',)]}"]
        for title, key in (("Departures from the written steps", "deviations"), ("Also noticed", "observations")):
            items = [texts[(key, k)] for k in range(len(verdict.get(key) or [])) if texts[(key, k)]]
            if items:
                lines += ["", f"### {title}", ""] + [f"- {x}" for x in items]
        return "\n".join(lines)

    text = render()
    for group in ("saw", None):          # the observations first, then everything else
        while len(text) > COMMENT_MAX:
            pool = [k for k in texts if (k[0] == group if group else k[0] != "saw") and len(texts[k]) > 40]
            if not pool:
                break
            key = max(pool, key=lambda k: len(texts[k]))
            texts[key] = _cut(texts[key], max(40, len(texts[key]) - (len(text) - COMMENT_MAX)))
            text = render()
    return text


# ---- the results as a person reads them -------------------------------------------------------------------


def took(seconds) -> str:
    """A duration as `3m 08s` / `42s` — or `—` when there is none."""
    if not isinstance(seconds, int | float) or isinstance(seconds, bool) or seconds < 0:
        return "—"
    minutes, secs = divmod(int(round(seconds)), 60)
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def results_table(results: list[dict], not_executed: list[dict] = (), unpriced: list[str] = ()) -> str:
    """The run's cases as a Markdown table: Case | Verdict | Time | Cost | What the agent found.

    The last column is the agent's own sentence about what it saw on the page (the verdict's `reason`), for
    a PASS as much as for a FAIL — "1 of 1 parts met" tells a reader nothing they can check — followed by
    how many parts were met and, when the case did not pass, the cause.

    Time is always there: how long a case takes is what decides whether running it this way is worth it,
    and it was the one figure the first reports left out. A FAIL's time includes its second run.
    """
    def cell(text) -> str:
        return " ".join(str(text or "").split()).replace("|", "/")

    lines = ["| Case | Verdict | Time | Cost | What the agent found |", "|---|---|---|---|---|"]
    total_s, total_cost = 0.0, 0.0
    for row in results or []:
        status = str(row.get("status") or "?")
        if status == FAIL and row.get("confirmed") is False:
            status += " (flaky)" if row.get("flaky") else " (not confirmed)"
        elif status == FAIL and row.get("confirmed"):
            status += " (confirmed)"
        cost = row.get("cost_usd")
        price = f"${cost:.2f}" if isinstance(cost, int | float) else "unknown"
        if row.get("extid") in (unpriced or ()):
            price = "unknown"
        total_s += row.get("duration_s") or 0
        total_cost += cost if isinstance(cost, int | float) else 0
        found = cell(row.get("reason") or row.get("summary"))[:400]
        tail = []
        if row.get("parts"):
            tail.append(f"{row.get('parts_met', 0)} of {row['parts']} parts met")
        if row.get("status") != PASS and row.get("cause"):
            tail.append(f"cause: {row['cause']}")
        if row.get("confirmation") and row.get("confirmed") is False:
            tail.append(f"second run: {cell(row['confirmation'])[:120]}")
        if tail:
            found += f" ({'; '.join(tail)})"
        lines.append(f"| {cell(row.get('extid'))} | {status} | {took(row.get('duration_s'))} | {price} | {found} |")
    for row in not_executed or []:
        lines.append(f"| {cell(row.get('extid'))} | not executed | — | — | {cell(row.get('reason'))[:300]} |")
    floor = " or more" if unpriced else ""
    lines.append(f"| **Total** | {len(results or [])} run | {took(total_s)} of agent time | "
                 f"${total_cost:.2f}{floor} | |")
    return "\n".join(lines)
