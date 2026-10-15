"""Path confinement — the one rule every engine transport enforces: writes land inside a writable root.

WHY this is a library and not a hook (D6): the policy was born inside a Claude PreToolUse hook, but the
rule has nothing to do with Claude. GROUND mounts ~90 local checkouts with `--add-dir` so the agent can
READ them, and `--permission-mode acceptEdits` auto-accepts edits; verified empirically 2026-07-25, that
combination makes every checkout WRITABLE. Pi's extension host has the same shape of hole. One copy of
the confinement predicate, two transports.

Everything here is a pure function of its arguments — no os.environ, no sys.exit. The transports
(rfc_agent.hooks.guard_writes for Claude, the pi extension via `python -m harness.policy check`) read
the environment and translate the verdict into their native envelope.
"""

from __future__ import annotations

import re
from pathlib import Path

# The engine's own per-session throwaway dirs (/tmp/claude-*/…): a shard writing a scratch script there
# is legitimate work (observed: a shard scripting its own REQ check was denied and wasted the
# round-trip). No checkout or run artifact lives under these, and inline `python -c` could already
# compute anything in-memory — allowing the scratchpad adds convenience, not capability.
#
# `claude-` ONLY, on purpose. This briefly also matched `pi-`, which widened the hole for EVERY engine:
# under a claude-code run the guard would have permitted writes to /tmp/pi-anything/, a tree no Claude
# session creates or owns, so an injected instruction could name it (`echo x > /tmp/pi-x/y`). Nothing in
# the pi research observed pi creating such a directory, and the pi adapter needs no exemption — so the
# exemption stays scoped to the engine that demonstrably owns these dirs. If a pi scratchpad is ever
# observed, the pi transport should DECLARE it in the policy request (one more root) rather than widen a
# regex that every other engine also obeys.
AGENT_TMP = re.compile(r"^/(?:private/)?tmp/claude-[\w.-]+/")

# Tool names (engine-native, Claude spelling) that write a file. Adapters that use other names pass
# their own set; this is the default because it is what the shipped hook transport enforces.
WRITE_TOOLS: tuple[str, ...] = ("Write", "Edit", "MultiEdit", "NotebookEdit")

# Redirect/exec sinks that are never a real file write.
DEV_SINKS = frozenset({"/dev/null", "/dev/stdout", "/dev/stderr", "/dev/tty"})


def resolve(path: str, base: Path | None = None) -> Path | None:
    """Absolutise `path` (relative ones against `base`, the agent's cwd). None when it is not a path."""
    try:
        p = Path(path).expanduser()
        if not p.is_absolute() and base is not None:
            p = base / p
        return p.resolve()
    except (OSError, ValueError):
        return None


def inside(path: str, roots: tuple[Path, ...] | list[Path], base: Path | None = None) -> bool:
    """True when `path` resolves inside one of `roots` (or the agent scratchpad).

    Relative paths resolve against `base` — `> ../x` used to sail through an absolute-only pattern and
    reach a sibling repo via the run-dir cwd, so relative targets are checked too.
    """
    p = resolve(path, base)
    if p is None:
        return False
    for root in roots:
        if p == root or root in p.parents:
            return True
    return bool(AGENT_TMP.match(str(p)))
