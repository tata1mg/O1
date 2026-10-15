"""The PostToolUse feedback contract shared by the hooks (byte_budget, doc_lints) and the canary.

Why JSON stdout and not exit-2 stderr (2026-09-03): CLI 2.1.139 turned a PostToolUse exit-2 into a
silent drop unless the hook entry sets continueOnBlock — a full 25-minute compose ran with zero
per-edit feedback before anyone noticed, and even with the key the text arrives labelled
`blockingError`. `hookSpecificOutput.additionalContext` is the documented NON-blocking channel
("added to Claude's context as a system message"): advice rather than an error, and not hostage to
the next exit-code default change. Every hook emits through here so the contract lives in one place;
hooks_canary.py proves end-to-end delivery once per CLI version.
"""

from __future__ import annotations

import json
import sys

EVENT = "PostToolUse"


def emit(text: str) -> None:
    """Hand `text` to the model as system context. Exit 0 afterwards — the tool already ran."""
    sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": EVENT, "additionalContext": text}}) + "\n")
    sys.stdout.flush()


def parse(stdout: str) -> str | None:
    """The additionalContext text a hook emitted (tests + canary), or None when it stayed silent."""
    for line in stdout.splitlines():
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(d, dict):
            ctx = (d.get("hookSpecificOutput") or {}).get("additionalContext")
            if ctx:
                return str(ctx)
    return None
