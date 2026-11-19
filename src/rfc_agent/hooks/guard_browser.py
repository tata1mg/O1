#!/usr/bin/env python3
"""PreToolUse hook: hold the `execute_ui_agent` stage's browser tools to the run's own origin.

Why this exists: every other stage gets read-only MCP tools, so an instruction injected into what the
agent reads has nothing to call. This stage is the exception — its agent clicks and types on a live test
environment, and the page text it reads is exactly the untrusted input that rule was written for. The
exception is therefore bounded here, on every call, rather than by the prompt:

- no origin set for the run (`RFC_AGENT_BROWSER_ORIGIN`) -> every browser tool is denied;
- a tool outside `mcp_tools.READONLY_TOOLS["playwright"]` -> denied, even if a settings file granted it;
- a `url` argument (navigate, a new tab) that is not on the run's origin -> denied.

What it cannot see: where a click leads. A link on the page can still take the browser to another site;
the server's own `--allowed-origins` narrows that, and its documentation says plainly that it is not a
security boundary either. So the stage runs against test environments only and is never given a credential.

Exit 0 = allow, exit 2 = deny (stderr is fed back to the agent).
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlsplit

ORIGIN_ENV = "RFC_AGENT_BROWSER_ORIGIN"
#: The case's action limit and the file its count is kept in. Both set -> the limit is enforced here,
#: where the agent cannot argue with it; either unset -> no limit.
MAX_ACTIONS_ENV = "RFC_AGENT_BROWSER_MAX_ACTIONS"
COUNT_FILE_ENV = "RFC_AGENT_BROWSER_COUNT_FILE"
#: The case's verdict file and how many actions may pass between two writes of it. Both set -> an agent
#: that has not written the file cannot act, and one that has stopped updating it is told to.
VERDICT_FILE_ENV = "RFC_AGENT_BROWSER_VERDICT_FILE"
VERDICT_EVERY_ENV = "RFC_AGENT_BROWSER_VERDICT_EVERY"
#: A regex over what an action targets. A match is refused: the things a run may never do to an account.
FORBID_ENV = "RFC_AGENT_BROWSER_FORBID"
#: The tools whose input names a target (`element`, `text`, `fields`) worth holding to FORBID.
_TARGETED = ("browser_click", "browser_type", "browser_fill_form", "browser_select_option", "browser_check",
             "browser_uncheck", "browser_press_key", "browser_hover")
SERVER = "playwright"
_DEFAULT_PORT = {"http": 80, "https": 443}


def _origin(url: str) -> tuple[str, str, int] | None:
    parts = urlsplit(url.strip())
    if parts.scheme not in _DEFAULT_PORT or not parts.hostname:
        return None
    try:
        port = parts.port or _DEFAULT_PORT[parts.scheme]
    except ValueError:
        return None
    return parts.scheme, parts.hostname.lower(), port


def denial(tool: str, inp: dict, origin: str, granted: tuple[str, ...]) -> str:
    """Why this browser call is refused, or "" when it may run.

    `tool` is the bare tool name (`browser_navigate`), `origin` the run's base address, `granted` the
    stage's tool list.
    """
    if tool not in granted:
        return (f"{tool} is not one of the browser tools this stage may use "
                f"({', '.join(granted)}). Work through the page with those.")
    base = _origin(origin or "")
    if base is None:
        return (f"no base address is set for this run ({ORIGIN_ENV}), so the browser may not be used. This "
                f"is the harness's to fix, not yours: stop and report it.")
    url = inp.get("url") if isinstance(inp, dict) else None
    if url is None or not str(url).strip():
        return ""
    url = str(url).strip()
    parts = urlsplit(url)
    if not parts.scheme and not parts.netloc:
        return "" if url.startswith("/") else f"{url!r} is not a path on {origin} — open a path starting with /"
    if _origin(url) != base:
        return (f"{url!r} is outside this run's test environment ({origin}). Stay on that address; if the "
                f"case needs another site, mark the clause not reached and say so.")
    return ""


def over_limit(tool: str, actions: tuple[str, ...], limit: str, count_file: str) -> str:
    """Count this call when it is an action (looking at the page is free), and refuse it past the limit.

    -> the refusal, or "". An unreadable or unwritable count never blocks a run: the wall-clock timeout
    is the backstop.
    """
    if tool not in actions or not limit.isdigit() or not count_file:
        return ""
    try:
        try:
            used = int(open(count_file, encoding="utf-8").read().strip() or 0)
        except (FileNotFoundError, ValueError):
            used = 0
        if used >= int(limit):
            return (f"this case has used its {limit} browser actions. Do not act again: write your verdict "
                    f"now, marking every clause you have not judged `not reached` with the blocker "
                    f"'action limit reached'. You may still take a snapshot or a screenshot.")
        with open(count_file, "w", encoding="utf-8") as fh:
            fh.write(str(used + 1))
    except OSError:
        return ""
    return ""


def verdict_gap(tool: str, actions: tuple[str, ...], verdict_file: str, every: str, state_file: str) -> str:
    """Refuse an action while the verdict file is missing, or has gone `every` actions without a write.

    Why a hook and not a sentence in the prompt: the prompt already said to keep the file current, and in
    the third pilot both agents wrote it once at the start and never again — so the one that was cut off
    left nothing but its opening page. `state_file` (next to the action counter, out of the agent's
    reach) remembers the file's mtime and the actions taken since it changed. -> the refusal, or "".
    """
    if tool not in actions or not verdict_file or not every.isdigit() or not state_file:
        return ""
    try:
        mtime = os.stat(verdict_file).st_mtime_ns
    except OSError:
        return ("write your verdict file first — every clause of the expected result `not reached` with the "
                "blocker 'not yet checked' — and then act. What is on disk is what survives if this session is "
                "cut off.")
    try:
        try:
            seen, since = (int(x) for x in open(state_file, encoding="utf-8").read().split())
        except (FileNotFoundError, ValueError):
            seen, since = mtime, 0
        if seen != mtime:
            seen, since = mtime, 0
        if since >= int(every):
            return (f"you have taken {since} browser actions since you last wrote your verdict file. Rewrite it "
                    f"now with the state of every clause you have judged so far (and the evidence you saved), "
                    f"then carry on. Looking at the page is still allowed.")
        with open(state_file, "w", encoding="utf-8") as fh:
            fh.write(f"{seen} {since + 1}")
    except OSError:
        return ""
    return ""


def forbidden(tool: str, inp: dict, pattern: str) -> str:
    """Refuse an action whose target matches the run's forbidden pattern. -> the refusal, or "".

    Why this exists: on the first run against a real, logged-in account the agent opened the NeuCoins
    widget and pressed Apply to see a "Neucoins used (if added)" row — a redemption it then could not
    undo. The prompt said what the run was for; nothing said what it must never touch. The pattern is
    matched against the words the agent itself uses for the element (and anything it types), so it is a
    tripwire on declared intent, not a proof: an agent that describes a Pay button as "the red button"
    walks past it. It still stops the honest mistake, which is the one that happened.
    """
    if tool not in _TARGETED or not pattern or not isinstance(inp, dict):
        return ""
    words = " ".join(str(inp.get(k) or "") for k in ("element", "text", "key")) + " " + \
        json.dumps(inp.get("fields") or "", default=str)
    try:
        hit = re.search(pattern, words, re.I)
    except re.error:
        return "the run's forbidden-action pattern is not a valid regex, so no action is allowed. Stop and report it."
    if not hit:
        return ""
    return (f"this run may not touch that ({hit.group(0)!r}): it changes money, an order or the account. Do not "
            f"look for another way to do it. If a clause cannot be judged without it, the clause is `not "
            f"reached` with the blocker 'action not allowed in this run' (cause: test data).")


def main() -> None:
    raw = sys.stdin.read()
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        ev = {}
    name = str(ev.get("tool_name") or "")
    prefix = f"mcp__{SERVER}__"
    if not name.startswith(prefix):
        sys.exit(0)  # not a browser tool: nothing of ours to enforce
    try:
        from rfc_agent.mcp_tools import BROWSER_ACTION_TOOLS, READONLY_TOOLS
        granted = READONLY_TOOLS[SERVER]
    except Exception as exc:  # FAIL CLOSED: exit 1 would be a non-blocking hook error and the call would run
        sys.stderr.write(f"DENIED by rfc_agent browser-guard: cannot load the tool list ({exc}).\n")
        sys.exit(2)
    tool = name[len(prefix):]
    count_file = os.environ.get(COUNT_FILE_ENV, "")
    reason = denial(tool, ev.get("tool_input") or {}, os.environ.get(ORIGIN_ENV, ""), granted) or \
        forbidden(tool, ev.get("tool_input") or {}, os.environ.get(FORBID_ENV, "")) or \
        verdict_gap(tool, BROWSER_ACTION_TOOLS, os.environ.get(VERDICT_FILE_ENV, ""),
                    os.environ.get(VERDICT_EVERY_ENV, ""), f"{count_file}.verdict" if count_file else "") or \
        over_limit(tool, BROWSER_ACTION_TOOLS, os.environ.get(MAX_ACTIONS_ENV, ""), count_file)
    if reason:
        sys.stderr.write(f"DENIED by rfc_agent browser-guard: {reason}\n")
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
