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
import sys
from urllib.parse import urlsplit

ORIGIN_ENV = "RFC_AGENT_BROWSER_ORIGIN"
#: The case's action limit and the file its count is kept in. Both set -> the limit is enforced here,
#: where the agent cannot argue with it; either unset -> no limit.
MAX_ACTIONS_ENV = "RFC_AGENT_BROWSER_MAX_ACTIONS"
COUNT_FILE_ENV = "RFC_AGENT_BROWSER_COUNT_FILE"
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
    reason = denial(tool, ev.get("tool_input") or {}, os.environ.get(ORIGIN_ENV, ""), granted) or \
        over_limit(tool, BROWSER_ACTION_TOOLS, os.environ.get(MAX_ACTIONS_ENV, ""),
                   os.environ.get(COUNT_FILE_ENV, ""))
    if reason:
        sys.stderr.write(f"DENIED by rfc_agent browser-guard: {reason}\n")
        sys.exit(2)
    sys.exit(0)


if __name__ == "__main__":
    main()
