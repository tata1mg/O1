"""Per-repo facts Python gives every agent: does the context layer (fleetlens) have a CODE index for it, and is
that index older than the snapshot (fleetlens `get_index_info.indexed_at` vs. the snapshot's last commit).
Fleetlens indexes the whole fleet at once, so one date covers every repo. Also the fast preset's rg
entry-point pre-pass."""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from ..steering import _tool_result


def _text(r) -> str:
    return "".join(b.get("text", "") for b in (r or {}).get("content", []) if isinstance(b, dict))


def index_info(spec: dict | None, flows: dict, scope: dict) -> dict[str, dict]:
    repos = {r["repo"]: r for r in scope.get("repos") or []}
    out: dict[str, dict] = {r: {"code_index": False, "age_note": "unknown"} for r in repos}
    if not spec:
        return out
    url, headers = spec["url"], spec.get("headers") or {}
    for repo in repos:
        t = _text(_tool_result(url, headers, "find_symbol", {"query": f"code_symbol:{repo}:", "limit": 1}))
        m = re.search(r'"count":\s*(\d+)', t)
        out[repo]["code_index"] = bool(m and int(m.group(1)) > 0)
    # index age: one build date for the whole fleet, against each snapshot's last commit
    built = re.search(r'"indexed_at":\s*"([^"]+)"', _text(_tool_result(url, headers, "get_index_info", {})))
    if not built:
        return out
    stamp = built.group(1)  # "2026-10-12 12:11:40" — no zone given; compared as UTC, like the commit dates
    for repo, info in repos.items():
        commit = str(info.get("commit_date") or "")[:19].replace("T", " ")
        out[repo]["indexed_at"] = stamp
        if not commit:
            out[repo]["age_note"] = f"index built {stamp[:16]}"
        elif commit > stamp:
            out[repo]["age_note"] = (f"index built {stamp[:16]}, before the snapshot's last commit "
                                     f"({commit[:16]}) — the code wins")
        else:
            out[repo]["age_note"] = f"index built {stamp[:16]}, after the snapshot's last commit"
    return out


def entry_prepass(run_dir: Path, tracer: dict, flows: dict) -> Path | None:
    """rg for each hop's route-ish strings in the tracer's snapshots → facts/<tracer>.entry.json."""
    by_flow = {f.get("id"): f for f in flows.get("flows") or []}
    hits: dict[str, list[str]] = {}
    for fid in tracer.get("flows") or []:
        for h in (by_flow.get(fid) or {}).get("hops") or []:
            repo = h.get("repo")
            snap = run_dir / "_snap" / str(repo)
            if repo not in (tracer.get("repos") or []) or not snap.is_dir():
                continue
            what = str(h.get("what") or "")
            cands = re.findall(r"/[\w{}<>:./-]{4,}", what) + re.findall(r"`([\w.]{4,})`", what)
            for c in cands[:3]:
                pat = re.sub(r"\{[^}]+\}|<[^>]+>", "[^/]+", c.strip("/"))
                try:
                    r = subprocess.run(
                        ["rg", "-n", "--max-count", "3", "-e", pat, str(snap)],
                        capture_output=True,
                        text=True,
                        timeout=20,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue
                found = [ln.replace(str(run_dir / "_snap") + "/", "")[:200] for ln in r.stdout.splitlines()[:5]]
                if found:
                    hits.setdefault(f"{fid} hop {h.get('n')} ({repo}): {c}", found)
    if not hits:
        return None
    p = run_dir / "facts" / f"{tracer['id']}.entry.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(hits, indent=2), encoding="utf-8")
    return p
