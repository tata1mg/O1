#!/usr/bin/env python3
"""PreToolUse hook: deny any write outside the run dir. CLAUDE TRANSPORT for harness.policy.

Why this exists: GROUND runs with `--add-dir <repos_root>` so the agent can READ ~90 local checkouts,
and `--permission-mode acceptEdits` auto-accepts file edits. Verified empirically (2026-07-25): that
combination makes every one of those checkouts WRITABLE — a `--add-dir` target is a write target, and an
injected instruction in an ingested PRD could plant a diff in a real repo. This hook makes the mount
read-only in fact.

Contract: RFC_AGENT_RUN_DIR names the only writable tree. Write/Edit/NotebookEdit outside it are denied
by path. Bash is denied when the command looks like it mutates or exfiltrates outside the run dir —
a deny-list, i.e. best-effort defense-in-depth, NOT a sandbox (see docs/harness/ARCHITECTURE.md §6).

Exit 0 = allow, exit 2 = deny (stderr is fed back to the agent).

WHAT LIVES WHERE (D6, 2026-09-08): the generic containment POLICY — path confinement, the mutation/egress/
redirect deny-list — moved to `harness.policy` so the Pi engine enforces the same rules through its own
extension instead of a second copy of these regexes. This file is now the Claude ENVELOPE plus the
RFC-workflow-specific rules that are not containment at all (emission-mode and revision-mode authoring
discipline, the use_figma script allowlist). Exit codes, env vars and reason strings are unchanged.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

try:
    from harness.policy import Request as PolicyRequest
    from harness.policy import check_write, mutators
    from harness.policy.paths import WRITE_TOOLS as _WRITE_TOOLS
    from harness.policy.paths import inside as _policy_inside
except ImportError as _exc:  # pragma: no cover - exercised by tests/test_guard_writes.py::test_fails_closed
    # FAIL CLOSED. This hook was stdlib-only before the harness extraction; it now depends on
    # harness.policy. Exit 1 would be a "non-blocking hook error" to Claude Code and the write would
    # PROCEED — containment must never fail open, so a missing policy package denies with a reason that
    # names the fix (exit 2 = deny). The interpreter is sys.executable of the harness process, so this only
    # fires on a partial install or a hand-edited settings file.
    sys.stderr.write(
        "DENIED: write-guard cannot import harness.policy "
        f"({_exc}); the hook must run under the harness interpreter (uv run …) — refusing all writes.\n"
    )
    sys.exit(2)

_ = mutators  # re-exported for tests/tools that inspect the deny-list regexes through this transport

# set by the engine on revision invocations only (see claude_engine): while it is set, an existing
# deliverable may be EDITED but never re-Written whole.
DIFF_ONLY_ENV = "RFC_AGENT_DIFF_ONLY"

# set by the engine on every generate invocation while GENERATE.EMIT is on: deliverables are authored
# by EMISSION (<emit>/<edits> in the response; selfcheck/the engine materialize them), so NO tool
# write to them is ever legitimate — compose included, and regardless of whether the file exists yet.
# Why a hook and not just the prompt (v7 incident, 2026-09-07): a stale oracle instruction convinced
# the composer the emission harness didn't exist, and with only a prompt-level prohibition it
# rationalized a direct Write ("the prohibition describes a different production mode") — the third
# prompt-only rule broken under incentive. Must mirror emit._ALLOWED (pinned by test).
EMIT_DELIVERABLES_ENV = "RFC_AGENT_EMIT_DELIVERABLES"
_DELIVERABLES = ("out/rfc.md", "out/contract.md", "out/detail/plan.md")


def _inside(path: str, run_dir: Path, base: Path | None = None) -> bool:
    """Kept as a module function because tools and tests reach for it; delegates to the policy."""
    return _policy_inside(path, (run_dir,), base)


def _is_deliverable(path: str, run_dir: Path, base: Path | None = None) -> bool:
    try:
        p = Path(path).expanduser()
        if not p.is_absolute() and base is not None:
            p = base / p
        p = p.resolve()
        return any(p == (run_dir / d) for d in _DELIVERABLES)
    except (OSError, ValueError):
        return False


# ---- use_figma containment -----------------------------------------------------------------------
# use_figma executes arbitrary Plugin API JavaScript against the user's Figma file — a WRITE tool.
# It is granted to INGEST anyway (mcp_tools.py) because it is the only channel that reliably
# enumerates large files: get_metadata's page list silently truncated a 25-page file to 1 page
# (2026-08-25) and a full-page dump was 9.5M chars. Containment lives HERE: only these fixed
# read-only scripts pass, copied verbatim with just a node id substituted. Whitespace is normalized
# before matching so an agent's line-wrapping never causes a deny loop; anything else — one extra
# statement, one changed property — is denied. The ingest skill quotes these scripts verbatim
# (pinned by test); keep both in sync when editing.
# Stays in this transport rather than harness.policy: it is a Figma-workflow tool allowlist, not
# containment — no other workflow or engine has a use_figma tool to contain.
_NODE_ID_SLOT = "NODE_ID"
USE_FIGMA_SCRIPTS = (
    "return figma.root.children.map(p => ({id: p.id, name: p.name}));",
    "const n = await figma.getNodeByIdAsync('NODE_ID'); "
    "if (!n) return {error: 'node not found'}; "
    "let p = n; while (p && p.type !== 'PAGE') p = p.parent; "
    "if (!p) return {error: 'no containing page'}; "
    "await p.loadAsync(); "
    "return {page: {id: p.id, name: p.name}, count: p.children.length, "
    "children: p.children.slice(0, 200).map(c => ({id: c.id, name: c.name, type: c.type, "
    "w: Math.round(c.width || 0), h: Math.round(c.height || 0)}))};",
    "const n = await figma.getNodeByIdAsync('NODE_ID'); "
    "if (!n || !('children' in n)) return {error: 'node not found or has no children'}; "
    "let p = n; while (p && p.type !== 'PAGE') p = p.parent; "
    "if (p) await p.loadAsync(); "
    "return {node: {id: n.id, name: n.name, type: n.type}, count: n.children.length, "
    "children: n.children.slice(0, 200).map(c => ({id: c.id, name: c.name, type: c.type, "
    "w: Math.round(c.width || 0), h: Math.round(c.height || 0)}))};",
)


def _use_figma_allowed(code: str) -> bool:
    # strip ALL whitespace before comparing: agents re-wrap lines and pad braces when copying, and a
    # formatting deny-loop would be pure waste — token identity is what matters, and no whitespace
    # placement can turn one of these scripts into different JS without also changing non-space chars
    norm = re.sub(r"\s+", "", code)
    for tpl in USE_FIGMA_SCRIPTS:
        pattern = re.escape(re.sub(r"\s+", "", tpl)).replace(_NODE_ID_SLOT, r"\d+:\d+")
        if re.fullmatch(pattern, norm):
            return True
    return False


def _existing_deliverable(path: str, run_dir: Path, base: Path | None = None) -> bool:
    """An out/ file that already exists. Scoped to out/ deliberately: those are the documents the
    harness maintains (numbering, budgets, healed citations) and the ones a rewrite re-emits whole.
    Everything else in the run dir — new files included — keeps writing normally."""
    try:
        p = Path(path).expanduser()
        if not p.is_absolute() and base is not None:
            p = base / p
        p = p.resolve()
        return p.is_file() and (run_dir / "out") in p.parents
    except (OSError, ValueError):
        return False


def _deny(reason: str) -> None:
    print(f"DENIED by rfc_agent write-guard: {reason}", file=sys.stderr)
    sys.exit(2)


def main() -> None:
    raw = sys.stdin.read()
    run_dir_env = os.environ.get("RFC_AGENT_RUN_DIR")
    if not run_dir_env:
        sys.exit(0)  # not our sandbox; nothing to enforce
    run_dir = Path(run_dir_env).expanduser().resolve()
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        sys.exit(0)  # never break a run over a hook-payload surprise
    tool = ev.get("tool_name") or ""
    inp = ev.get("tool_input") or {}

    cwd = Path(ev.get("cwd") or run_dir)

    if tool in _WRITE_TOOLS:
        target = inp.get("file_path") or inp.get("notebook_path") or ""
        verdict = check_write(PolicyRequest(tool=tool, input=inp, cwd=cwd, writable_roots=(run_dir,)))
        if verdict.decision == "deny":
            _deny(verdict.reason)
        if os.environ.get(EMIT_DELIVERABLES_ENV) and _is_deliverable(str(target), run_dir, cwd):
            _deny(
                f"{tool} on {Path(str(target)).name}: deliverables are authored by EMISSION in this "
                f"run — speak the content as <emit path=\"...\" section=\"...\">...</emit> (or ONE "
                f"<edits path=\"...\">[...]</edits> batch for fixes), then run selfcheck; the oracle "
                f"materializes your emitted blocks to disk itself before checking. The files looking "
                f"unchanged before a selfcheck is normal. Every other file still writes normally."
            )
        if os.environ.get(DIFF_ONLY_ENV) and _existing_deliverable(str(target), run_dir, cwd):
            # DIFF-ONLY REVISION (2026-08-15 forensics): revisions emit MORE output tokens than the
            # composes they revise, 60-80% of it unchanged text re-typed around a small delta — and a
            # full-file Write is how that happens. Edits are the whole point of revision mode; the
            # prompt has said so for two versions and a 29.6KB re-Write still shipped. Enforced here,
            # where saying no actually costs the model nothing but the re-emission.
            if tool == "Write":
                _deny(
                    f"full-file Write of {Path(str(target)).name} in a REVISION session. Emit targeted "
                    f"Edits instead — smallest unique anchor per change, one Edit per fix. A "
                    f"whole-document Edit is denied the same way. The harness owns renumbering "
                    f"(symbolic CHANGE/Q ids) and byte budgets, so there is never a document-wide "
                    f"rewrite to do. Creating a NEW file is still allowed."
                )
            # A Write denial used to be circumventable by an Edit whose old_string was the whole
            # document (observed live 2026-08-18: "I'll use Edit instead to replace the entire file
            # content in one operation") — same re-emission cost, different tool name. Deny an Edit
            # replacing most of the file with the same instruction.
            # ponytail: 50% threshold is a heuristic; tune from decisions.jsonl if it false-positives
            if tool in ("Edit", "MultiEdit"):
                old = inp.get("old_string") or ""
                try:
                    fsize = (run_dir / Path(str(target)).expanduser().resolve().relative_to(run_dir)).stat().st_size
                except (OSError, ValueError):
                    fsize = 0
                if fsize and len(old.encode()) > fsize * 0.5:
                    _deny(
                        f"this Edit replaces {len(old.encode()):,} of {Path(str(target)).name}'s "
                        f"{fsize:,} bytes — that is a whole-document rewrite in Edit clothing, denied "
                        f"in a REVISION session for the same reason full-file Write is. Split it into "
                        f"targeted Edits: smallest unique anchor per change, one Edit per fix."
                    )
        sys.exit(0)

    if tool.endswith("__use_figma"):
        if not _use_figma_allowed(inp.get("code") or ""):
            _deny(
                "use_figma is write-capable and permitted ONLY as the fixed read-only enumeration "
                "scripts the ingest skill quotes (pages list / sibling inventory / --children "
                "inventory), copied VERBATIM with only the node id substituted. Any other script is "
                "denied — for design content use get_screenshot/get_metadata."
            )
        sys.exit(0)

    if tool == "Bash":
        reason = mutators.shell_denial(inp.get("command") or "", (run_dir,), cwd)
        if reason:
            _deny(reason)
    sys.exit(0)


if __name__ == "__main__":
    main()
