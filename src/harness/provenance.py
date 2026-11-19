"""Which O1 code a run ran on: the commit, and whether the checkout had changes that were not committed.

Comparisons between runs (Claude vs Axon, before vs after a fix) are only as good as knowing that the runs used
the same code. Each run records this at its start: rfc-agent in logs/run.json and on its Langfuse trace, the
harness in logs/o1_code.json and its --json summary. A run on a dirty checkout says so and lists the files.
"""

from __future__ import annotations

import functools
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]  # the O1 checkout: src/harness/provenance.py -> O1
MAX_FILES = 30


def _git(*args: str, root: Path) -> str | None:
    try:
        out = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.rstrip("\n") if out.returncode == 0 else None  # leading spaces matter in `status`


@functools.lru_cache(maxsize=4)
def code_version(root: Path = ROOT) -> dict:
    """{"commit", "short", "branch", "dirty", "changed"} for the O1 checkout, or {"commit": None} outside git.

    `changed` lists tracked files that differ from the commit, plus untracked files under src/ (code that ran).
    Untracked notes and docs elsewhere in the checkout cannot change a run, so they do not make it dirty."""
    commit = (_git("rev-parse", "HEAD", root=root) or "").strip()
    if not commit:
        return {"commit": None, "short": None, "branch": None, "dirty": None, "changed": []}
    status = _git("status", "--porcelain", "--untracked-files=all", "--", ".", root=root) or ""
    changed = []
    for line in status.splitlines():
        code, path = line[:2], line[3:].strip().strip('"')
        if code == "??" and not path.startswith("src/"):
            continue
        changed.append(path)
    return {
        "commit": commit,
        "short": commit[:10],
        "branch": (_git("rev-parse", "--abbrev-ref", "HEAD", root=root) or "").strip() or None,
        "dirty": bool(changed),
        "changed_count": len(changed),
        "changed": changed[:MAX_FILES],
    }


def label(v: dict | None = None) -> str:
    """`O1 3f2a9c1d07 (feat/test_agent)`, with `+ N uncommitted change(s)` when the checkout was dirty."""
    v = v if v is not None else code_version()
    if not v.get("commit"):
        return "O1 (not a git checkout)"
    out = f"O1 {v['short']} ({v.get('branch') or 'detached'})"
    return out + (f" + {v.get('changed_count', len(v['changed']))} uncommitted change(s)" if v.get("dirty") else "")
