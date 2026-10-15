#!/usr/bin/env python3
"""Investigate agent hook — two jobs, one script (`hook.py pre` / `hook.py post`).

pre  (PreToolUse on Write/Edit/NotebookEdit): an agent may write only the paths in INVESTIGATE_WRITE_ALLOW
     (comma list, relative to INVESTIGATE_RUN_DIR; a trailing '*' matches a prefix). Everything else is denied —
     the snapshots under _snap/ and every checkout stay read-only. Exit 2 = deny, stderr goes to the agent.
post (PostToolUse on every tool), two parts, both sent back as additionalContext (never blocks):
     - write-time citation check: after a Write/Edit of a file listed in INVESTIGATE_CHECK_FILES, every
       repo/path:line in it is checked against the snapshots (exists, line in range), "not found" claims are
       re-checked with rg, and a cited line that doesn't mention the identifier its bullet names is reported
       as "wrong line?". The agent fixes it while the code is still in front of it — no resume round.
     - budget: counts tool calls in INVESTIGATE_COUNTER; at INVESTIGATE_CALL_BUDGET sends ONE "wrap up now" message.

Agents get no Bash, so there is no shell path to police here.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def _allowed(target: str) -> bool:
    run_dir = Path(os.environ.get("INVESTIGATE_RUN_DIR", "")).resolve()
    if not str(run_dir) or not target:
        return False
    p = Path(target)
    p = (p if p.is_absolute() else Path.cwd() / p).resolve()
    try:
        rel = p.relative_to(run_dir).as_posix()
    except ValueError:
        return False
    for pat in [x.strip() for x in os.environ.get("INVESTIGATE_WRITE_ALLOW", "").split(",") if x.strip()]:
        if (pat.endswith("*") and rel.startswith(pat[:-1])) or rel == pat:
            return True
    return False


def pre(event: dict) -> int:
    inp = event.get("tool_input") or {}
    target = inp.get("file_path") or inp.get("notebook_path") or ""
    if _allowed(target):
        return 0
    allow = os.environ.get("INVESTIGATE_WRITE_ALLOW", "")
    sys.stderr.write(f"DENIED: you may only write {allow} (inside the run folder). {target} is read-only.\n")
    return 2


def _citation_feedback(event: dict) -> str:
    if event.get("tool_name") not in ("Write", "Edit", "MultiEdit"):
        return ""
    target = (event.get("tool_input") or {}).get("file_path") or ""
    run_dir = Path(os.environ.get("INVESTIGATE_RUN_DIR", "")).resolve()
    checked = [x.strip() for x in os.environ.get("INVESTIGATE_CHECK_FILES", "").split(",") if x.strip()]
    if not target or not checked or not str(run_dir):
        return ""
    p = Path(target)
    p = (p if p.is_absolute() else Path.cwd() / p).resolve()
    try:
        rel = p.relative_to(run_dir).as_posix()
    except ValueError:
        return ""
    if not any((c.endswith("*") and rel.startswith(c[:-1])) or rel == c for c in checked):
        return ""
    try:
        from rfc_agent.investigate.checks import write_time
    except ImportError:
        return ""
    problems = write_time(p, run_dir)
    if not problems:
        return ""
    return f"Citation check on {rel} — {len(problems)} problem(s); fix them now, in place:\n- " + "\n- ".join(problems)


def post(event: dict) -> int:
    msgs = []
    try:
        fb = _citation_feedback(event)
    except Exception:  # noqa: BLE001 — a check bug must never break the agent's turn
        fb = ""
    if fb:
        msgs.append(fb)
    counter = os.environ.get("INVESTIGATE_COUNTER")
    budget = int(os.environ.get("INVESTIGATE_CALL_BUDGET") or 0)
    if counter and budget > 0:
        path = Path(counter)
        try:
            n = int(path.read_text() or 0) + 1
        except (OSError, ValueError):
            n = 1
        path.write_text(str(n))
        if n == budget:
            target = os.environ.get("INVESTIGATE_WRITE_ALLOW", "your output file").split(",")[0]
            msgs.append(
                f"Tool-call budget reached ({budget}). Wrap up now: stop exploring and write {target} "
                "with what you have — put whatever is left under Unknowns."
            )
    if msgs:
        sys.stdout.write(
            json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "\n\n".join(msgs)}})
            + "\n"
        )
    return 0


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else ""
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        event = {}
    if mode == "pre":
        return pre(event)
    if mode == "post":
        return post(event)
    return 0


if __name__ == "__main__":
    sys.exit(main())
