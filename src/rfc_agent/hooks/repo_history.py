#!/usr/bin/env python3
"""repo-history: the ONE safe path to git history for headless agents (ACI pattern).

Agents legitimately need file-scoped history mid-work (observed: composers asking
`git log --oneline -- app/repository/booking.py` and burning 4-6 denial round-trips per run), but no
raw-shell form is safely allowlistable: `cd && git` is CLI-blocked, `git -C:*` would also allow
`git -C x push` AND sidestep the write-guard's git-mutator regex. SWE-agent's ACI finding applies:
purpose-built narrow commands beat raw shell — this wrapper can only ever run `git log` (read-only,
no hooks) against a repo under RFC_AGENT_REPOS_ROOT, with sanitized arguments.

Usage (as the stage skill documents):
    repo-history <repo> [path/within/repo] [-n N]

The engine writes the absolute script path into each child's settings allowlist, so invocations are
permitted exactly and only in this form.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_MAX_N = 50


def main() -> int:
    args = [a for a in sys.argv[1:] if a]
    n = 20
    if "-n" in args:
        i = args.index("-n")
        try:
            n = min(_MAX_N, max(1, int(args[i + 1])))
        except (IndexError, ValueError):
            print("usage: repo-history <repo> [path/within/repo] [-n N]", file=sys.stderr)
            return 2
        del args[i:i + 2]
    if not args:
        print("usage: repo-history <repo> [path/within/repo] [-n N]", file=sys.stderr)
        return 2
    repos_root = Path(os.environ.get("RFC_AGENT_REPOS_ROOT", "")).expanduser()
    if not repos_root.is_dir():
        print("repo-history: RFC_AGENT_REPOS_ROOT not set/found", file=sys.stderr)
        return 2
    repo = args[0]
    # repo must be a plain name resolving to a git repo directly under repos_root — no traversal
    if not re.fullmatch(r"[A-Za-z0-9._-]+", repo) or not (repos_root / repo / ".git").exists():
        print(f"repo-history: unknown repo '{repo}' (must be a checkout under {repos_root})", file=sys.stderr)
        return 2
    cmd = ["git", "-C", str(repos_root / repo), "log", "--oneline", "--no-decorate", f"-{n}"]
    if len(args) > 1:
        rel = args[1]
        # path stays inside the repo: no absolute paths, no traversal, no option injection
        if rel.startswith(("-", "/")) or ".." in rel.split("/"):
            print(f"repo-history: invalid path '{rel}'", file=sys.stderr)
            return 2
        cmd += ["--", rel]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    sys.stdout.write(out.stdout)
    sys.stderr.write(out.stderr)
    return out.returncode


if __name__ == "__main__":
    sys.exit(main())
