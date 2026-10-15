"""Per-run, read-only repo snapshots pinned to each repo's default branch.

Never reads the operator's working checkout: it may be on any branch (web_app_vite was on a month-old
test branch, 2026-10-06) or carry uncommitted work. Every snapshot is a detached git worktree under
<run>/_snap/<repo>, made from a SOURCE repo:

  your checkout (REPOS_ROOT/<repo>)  -> used when present (and not --fresh-clone): fetch, then worktree
  the repo cache (~/.cache/rfc-agent/repos/<repo>)
                                     -> repos you have not cloned (and everything under --fresh-clone):
                                        shallow-cloned ONCE, then only fetched on later runs
  fetch fails     -> snapshot the source's origin/<default> as last fetched, and say how old it is
  nothing works   -> source "context-index-only" (flagged in the RFC; never a hang or a prompt)

`git fetch` only moves remote-tracking refs; the source's own branch and files are never touched. All
repos run in parallel, each under a hard timeout, so one slow remote cannot stall the run.

Before the confirm screen, every repo's remote is probed (`ls-remote`, which also yields the default
branch), so a context-layer service that maps to a repo name Bitbucket does not have is flagged while
the operator can still fix it — instead of silently degrading to context-index-only later.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

GIT_TIMEOUT_S = 120
LS_REMOTE_TIMEOUT_S = 20
CACHE_ROOT = Path(os.environ.get("RFC_AGENT_REPO_CACHE", "~/.cache/rfc-agent/repos")).expanduser()
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_SSH_COMMAND": "ssh -o BatchMode=yes -o ConnectTimeout=10"}

# Bitbucket says this for a repo that does not exist AND for one you cannot read — same fix either way
_MISSING = ("no longer exists", "does not exist", "repository not found", "may not have access", "not found")
_UNREACHABLE = ("whitelist", "timed out", "could not resolve", "connection refused", "network is unreachable",
                "operation timed out", "connection closed")


def _git(args: list[str], cwd: Path | None = None, timeout: int = GIT_TIMEOUT_S) -> tuple[int, str]:
    try:
        r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout, env=_ENV)
        return r.returncode, (r.stdout + r.stderr).strip()
    except subprocess.TimeoutExpired:
        return 124, f"timed out after {timeout}s"
    except OSError as e:
        return 127, str(e)


def _short_error(out: str) -> str:
    low = out.lower()
    if "whitelist" in low:
        return "IP not whitelisted (VPN off?)"
    if any(m in low for m in _MISSING):
        return "repo not found on the remote, or no access"
    lines = [ln for ln in out.splitlines() if ln.strip()]
    return (lines[-1] if lines else "unknown error")[:160]


def classify_remote_error(out: str) -> str:
    """'missing' (wrong name / no access) | 'unreachable' (network, VPN) | 'unknown'."""
    low = out.lower()
    if any(m in low for m in _UNREACHABLE):
        return "unreachable"
    if any(m in low for m in _MISSING):
        return "missing"
    return "unknown"


def remote_template(repos_root: Path) -> str | None:
    """'{repo}' URL template from any checkout's origin — all org repos share one Bitbucket workspace."""
    if not repos_root.is_dir():
        return None
    for d in sorted(p for p in repos_root.iterdir() if (p / ".git").exists()):
        code, url = _git(["-C", str(d), "remote", "get-url", "origin"], timeout=10)
        if code == 0 and url:
            return re.sub(r"[^/:]+?(\.git)?/?$", r"{repo}\1", url.strip(), count=1)
    return None


@dataclass
class RemoteInfo:
    branch: str
    how: str      # how the branch was decided
    remote: str   # ok | missing | unreachable | unknown | no-url


def _source_dirs(repo: str, repos_root: Path, cache_root: Path) -> tuple[Path | None, Path | None]:
    local = repos_root / repo
    cached = cache_root / repo
    return (local if (local / ".git").exists() else None, cached if (cached / ".git").exists() else None)


def probe(local: Path | None, url: str | None) -> RemoteInfo:
    """Default branch + whether the remote exists. Remote HEAD (authoritative) -> local origin/HEAD ->
    origin/master|main -> 'master'. `local` may be the operator's checkout or the repo cache."""
    remote = "no-url"
    target = ["-C", str(local), "ls-remote", "--symref", "origin", "HEAD"] if local else \
             (["ls-remote", "--symref", url, "HEAD"] if url else None)
    if target:
        code, out = _git(target, timeout=LS_REMOTE_TIMEOUT_S)
        m = re.search(r"^ref:\s+refs/heads/(\S+)\s+HEAD", out, re.MULTILINE)
        if code == 0 and m:
            return RemoteInfo(m.group(1), "remote HEAD", "ok")
        remote = "ok" if code == 0 else classify_remote_error(out)
    if local:
        code, out = _git(["-C", str(local), "symbolic-ref", "--short", "refs/remotes/origin/HEAD"], timeout=10)
        if code == 0 and out.startswith("origin/"):
            return RemoteInfo(out.split("/", 1)[1], "local origin/HEAD (remote unreachable)", remote)
        for b in ("master", "main"):
            ref = f"refs/remotes/origin/{b}"
            if _git(["-C", str(local), "rev-parse", "--verify", "--quiet", ref], timeout=10)[0] == 0:
                return RemoteInfo(b, f"origin/{b} exists locally (remote unreachable)", remote)
    return RemoteInfo("master", "guessed (nothing resolvable)", remote)


def default_branch(local: Path | None, url: str | None) -> tuple[str, str]:
    info = probe(local, url)
    return info.branch, info.how


def _ensure_cache(repo: str, url: str, branch: str, cache_root: Path) -> tuple[Path | None, bool, str]:
    """(cache path, freshly cloned?, error). Clones into a temp dir and renames, so a concurrent run or an
    interrupted clone never leaves a half-cloned cache behind."""
    dest = cache_root / repo
    if (dest / ".git").exists():
        return dest, False, ""
    cache_root.mkdir(parents=True, exist_ok=True)
    tmp = cache_root / f".tmp-{repo}-{uuid.uuid4().hex[:8]}"
    code, out = _git(["clone", "--depth=1", "--no-tags", "--branch", branch, url, str(tmp)])
    if code != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, False, f"clone failed: {_short_error(out)}"
    try:
        tmp.rename(dest)
    except OSError:  # another run won the race — use its clone
        shutil.rmtree(tmp, ignore_errors=True)
    return dest, True, ""


def snapshot(repo: str, repos_root: Path, snap_root: Path, template: str | None,
             branch: tuple[str, str] | None = None, fresh_clone: bool = False,
             cache_root: Path | None = None) -> dict:
    """Materialize one repo at <snap_root>/<repo>. Returns its scope.json record."""
    cache_root = cache_root or CACHE_ROOT
    local, cached = _source_dirs(repo, repos_root, cache_root)
    url = template.format(repo=repo) if template else None
    dest = snap_root / repo
    rec: dict = {"repo": repo, "path": str(dest), "local_checkout": str(local) if local else None}
    if branch is None:  # normally already resolved for the confirm screen
        info = probe(local or cached, url)
        branch = (info.branch, info.how)
    br, how = branch
    rec.update(branch=br, branch_from=how)
    if dest.exists():
        remove_snapshot(dest)

    source, fresh = (local, False) if (local and not fresh_clone) else (cached, False)
    rec["via"] = "your checkout" if source is local and local else "repo cache"
    if source is None:
        if not url:
            rec.update(source="context-index-only", path=None, error="no remote URL (no checkout to derive it from)")
            return rec
        source, fresh, err = _ensure_cache(repo, url, br, cache_root)
        if source is None:
            rec.update(source="context-index-only", path=None, error=err)
            return rec
        rec["cache"] = "cloned"

    if fresh:
        fetched = True
        rec["fetch"] = "ok (fresh clone)"
    else:
        code, out = _git(["-C", str(source), "fetch", "--depth=1", "--no-tags", "origin",
                          f"+refs/heads/{br}:refs/remotes/origin/{br}"])
        fetched = code == 0
        rec["fetch"] = "ok" if fetched else f"failed: {_short_error(out)}"
    _git(["-C", str(source), "worktree", "prune"], timeout=30)  # a crashed run's stale registration
    code, out = _git(["-C", str(source), "worktree", "add", "--detach", str(dest), f"origin/{br}"])
    if code != 0:
        rec.update(source="context-index-only", path=None, error=f"worktree add failed: {_short_error(out)}")
        return rec
    rec["source"] = "snapshot" if fetched else "snapshot-of-last-fetch"
    return _stamp(rec, dest)


def _stamp(rec: dict, dest: Path) -> dict:
    code, out = _git(["-C", str(dest), "log", "-1", "--format=%H%x09%cI"], timeout=10)
    if code == 0 and "\t" in out:
        rec["sha"], rec["commit_date"] = out.split("\t", 1)
    return rec


def snapshot_all(repos: list[str], repos_root: Path, snap_root: Path, *,
                 branches: dict[str, tuple[str, str]] | None = None, fresh_clone: bool = False,
                 cache_root: Path | None = None) -> list[dict]:
    """branches: repo -> (branch, how it was decided), from the confirm screen; missing ones are resolved."""
    snap_root.mkdir(parents=True, exist_ok=True)
    template = remote_template(repos_root)
    known = branches or {}
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(repos)))) as pool:
        futs = [pool.submit(snapshot, r, repos_root, snap_root, template, known.get(r), fresh_clone, cache_root)
                for r in repos]
        return [f.result() for f in futs]


def probe_all(repos: list[str], repos_root: Path, cache_root: Path | None = None) -> dict[str, RemoteInfo]:
    """Default branch + remote existence per repo, in parallel — shown on the confirm screen."""
    cache_root = cache_root or CACHE_ROOT
    template = remote_template(repos_root)

    def one(r: str) -> RemoteInfo:
        local, cached = _source_dirs(r, repos_root, cache_root)
        return probe(local or cached, template.format(repo=r) if template else None)

    if not repos:
        return {}
    with ThreadPoolExecutor(max_workers=max(1, min(8, len(repos)))) as pool:
        return dict(zip(repos, pool.map(one, repos), strict=True))


def is_cached(repo: str, cache_root: Path | None = None) -> bool:
    return ((cache_root or CACHE_ROOT) / repo / ".git").exists()


def remove_snapshot(dest: Path) -> None:
    """Remove a worktree (and its registration in the source repo) or a plain clone."""
    gitfile = dest / ".git"
    if gitfile.is_file():  # a worktree: .git is a pointer file into <source>/.git/worktrees/<name>
        m = re.search(r"gitdir:\s*(.+)", gitfile.read_text(encoding="utf-8", errors="replace"))
        if m:
            src = Path(m.group(1).strip()).parents[2]
            _git(["-C", str(src), "worktree", "remove", "--force", str(dest)], timeout=60)
            _git(["-C", str(src), "worktree", "prune"], timeout=30)
    shutil.rmtree(dest, ignore_errors=True)


def cleanup(snap_root: Path) -> int:
    n = 0
    if snap_root.exists():
        for d in snap_root.iterdir():
            if d.is_dir():
                remove_snapshot(d)
                n += 1
        shutil.rmtree(snap_root, ignore_errors=True)
    return n
