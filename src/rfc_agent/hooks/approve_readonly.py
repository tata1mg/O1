#!/usr/bin/env python3
"""PreToolUse hook: auto-approve verifiably READ-ONLY Bash commands, compounds included.
CLAUDE TRANSPORT for harness.policy.classify_readonly.

Why this exists (v5 postmortem, 2026-08-10): the CLI decomposes compound commands and headless
auto-denies any segment matching no allow rule. The curated allowlist deliberately omits awk/sed/find
(as PREFIX rules they'd admit `sed -i`, `find -delete`, `awk system()`), so pure text-reading pipelines
like `grep -n "def " file | awk -F: '$1<5900'` died at the ask — 14 denials in one run, and three runs'
worth of "avoid piping/chaining" memory notes that demonstrably never changed model behavior. Facts
about permission walls belong in the environment, not in advisory prose: this hook inspects the FULL
command (flags and all), which prefix rules cannot, and approves only what it can prove is read-only.

Precision contract (SWE-agent ACI rule — a guardrail must only fire when it is definitely right):
- ALLOW only when EVERY segment of the compound is in the verified read-only set (harness.policy.shell).
- DENY only `find /` (root-scoped filesystem walk — always denied anyway; this makes the feedback
  instant and instructive instead of a silent headless timeout).
- Inside the pipeline sandbox ONLY (RFC_AGENT_RUN_DIR set): additionally DENY command classes that
  are NEVER approvable headless (unsafe awk/sed, unsafe find, ad-hoc interpreters, xargs) with a
  reason NAMING the sanctioned alternative. Outcome-identical to the CLI's auto-deny — the win is
  the message: the reason string reaches the child as a tool_result (verified live, ground shard
  2026-08-11: the find-/ hint arrived verbatim and both shards recovered in one turn), so recovery
  costs 1 turn instead of a 3-5 denial probe (measured: PROJ-3394-figma-v2 lines 49-56).
- Everything else: NO OPINION (exit 0, no output) — the normal allowlist/ask flow decides.

Composition safety: guard_writes.py runs on the same PreToolUse event and the most restrictive
decision wins (deny > defer > ask > allow, per Claude Code hook docs) — an "allow" from here can
never override the containment guard's deny.

WHAT LIVES WHERE (D6, 2026-09-08): the classifier itself (segment splitting, the verified read-only
command set, the never-approvable classes and their reason strings) is `harness.policy.shell`, so the Pi
engine reaches the same verdicts through its own extension. This file is the Claude envelope: read the
hook event, decide the sandbox gate, print `hookSpecificOutput`. Exit codes and JSON are unchanged.
"""

from __future__ import annotations

import json
import os
import sys

try:
    from harness.policy import ALLOW, DENY, classify_readonly
    from harness.policy.shell import FIND_ROOT_REASON

    # ABSOLUTE, not relative: this file is executed as a SCRIPT by the CLI hook runner, so it has no
    # parent package. The import is safe here because the fail-closed branch below already requires
    # the package to be importable at all.
    from rfc_agent.hooks import broker_command
except ImportError as _exc:  # pragma: no cover - see guard_writes.py: containment must fail closed
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": "deny",
        "permissionDecisionReason": f"read-only approver cannot import harness.policy ({_exc}); "
                                    "run under the harness interpreter (uv run …)"}}))
    sys.exit(0)

# one definition, shared with the RFC workflow's HARNESS_POLICY_BROKER_HINT export (see hooks/__init__)
_BROKER = broker_command()


def _emit(decision: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse", "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


def main() -> None:
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)
    if (ev.get("tool_name") or "") != "Bash":
        sys.exit(0)
    cmd = (ev.get("tool_input") or {}).get("command") or ""
    # the never-approvable classes are a SANDBOX rule, not a universal one: outside the pipeline
    # (RFC_AGENT_RUN_DIR unset) this hook has no business editorialising about a user's own awk.
    sandboxed = bool(os.environ.get("RFC_AGENT_RUN_DIR"))
    verdict = classify_readonly(
        cmd,
        broker_hint=_BROKER,
        repos_root=os.environ.get("RFC_AGENT_REPOS_ROOT") or "<repos_root>",
        # pinned rfc_agent entrypoints ARE the sanctioned alternative (allowlisted by exact form)
        exempt_marker="rfc_agent",
    )
    if verdict.decision == DENY:
        # `find /` is denied everywhere; everything else only inside the sandbox — outside it, defer.
        if sandboxed or verdict.reason == FIND_ROOT_REASON:
            _emit("deny", verdict.reason)
        sys.exit(0)
    if verdict.decision == ALLOW:
        _emit("allow", verdict.reason)
    sys.exit(0)


if __name__ == "__main__":
    main()
