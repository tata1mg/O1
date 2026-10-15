"""Checks after each tracer / UI reader — liberal by design (docs/INVESTIGATE.md "Check after each tracer").

Must-fix (one retry with the same agent): missing facts file or flow block, a citation that doesn't resolve in
the snapshot, a "not found" claim that rg contradicts, a node id not in the Figma export.
Flags (never retried; passed to the lead in 1c): missing sections, empty Shapes/Callers without "none found",
unanswered verify items, copy that doesn't match the export, unknown REQ ids.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

SECTIONS = (
    "Hop chain",
    "Shapes",
    "Callers",
    "Existing behaviour",
    "Data model",
    "Out-of-scope calls",
    "Verify answers",
    "Unknowns",
)
CITE_RE = re.compile(r"(?<![\w/.-])([A-Za-z0-9][\w.-]*)/([\w./-]+\.[A-Za-z0-9]+):(\d+)(?:-(\d+))?")
NOT_FOUND_RE = re.compile(
    r"(not found|none found|no callers?|no usages?|does not exist|doesn't exist|"
    r"never (?:called|used|read)|no references?)",
    re.IGNORECASE,
)
TICK_RE = re.compile(r"`([A-Za-z_][\w.]{3,})`")


def blocks(text: str) -> dict[str, str]:
    """'## F1 …' blocks keyed by flow id."""
    out: dict[str, str] = {}
    parts = re.split(r"^## +", text, flags=re.MULTILINE)
    for p in parts[1:]:
        fid = p.split(None, 1)[0].strip() if p.strip() else ""
        out[fid] = p
    return out


def _sections(block: str) -> dict[str, str]:
    out: dict[str, str] = {}
    parts = re.split(r"^### +", block, flags=re.MULTILINE)
    for p in parts[1:]:
        head, _, body = p.partition("\n")
        out[head.strip()] = body.strip()
    return out


_SNAP_PREFIX = re.compile(r"(?<![\w/.-])(?:\./)?_snap/")


def citations(text: str, repos: dict[str, dict], run_dir: Path) -> tuple[list[str], int]:
    """(broken citation messages, citations checked). Only `repo/...` with a known repo is checked.

    A `_snap/<repo>/path:line` citation (the snapshot path — seen live from an agent) is checked as
    `<repo>/path:line`, and reported once so the document uses the repo-relative form readers can open."""
    broken, n = [], 0
    prefixed = len(_SNAP_PREFIX.findall(text))
    if prefixed:
        broken.append(f"{prefixed} citation(s) start with '_snap/' — write them as repo/path:line "
                      "(e.g. svc/app/x.py:12), without the snapshot folder")
        text = _SNAP_PREFIX.sub("", text)
    for m in CITE_RE.finditer(text):
        repo, rel, a, b = m.group(1), m.group(2), int(m.group(3)), m.group(4)
        if repo not in repos:
            continue
        n += 1
        snap = repos[repo].get("path")
        if not snap:
            continue  # no snapshot to check against
        f = run_dir / "_snap" / repo / rel
        if not f.is_file():
            broken.append(f"{m.group(0)} — file not found in the {repo} snapshot")
            continue
        try:
            nlines = sum(1 for _ in f.open("rb"))
        except OSError:
            continue
        last = int(b) if b else a
        if a < 1 or last > nlines or last < a:
            broken.append(f"{m.group(0)} — line out of range ({rel} has {nlines} lines)")
    return broken, n


def false_absences(text: str, repos: list[str], run_dir: Path, limit: int = 8) -> list[str]:
    """'not found' claims about a backticked identifier that rg finds in the tracer's repos."""
    out: list[str] = []
    dirs = [str(run_dir / "_snap" / r) for r in repos if (run_dir / "_snap" / r).is_dir()]
    if not dirs:
        return out
    seen: set[str] = set()
    for line in text.splitlines():
        if not NOT_FOUND_RE.search(line):
            continue
        for ident in TICK_RE.findall(line):
            if ident in seen or "/" in ident:
                continue
            seen.add(ident)
            try:
                r = subprocess.run(
                    ["rg", "-n", "-F", "--max-count", "2", ident, *dirs], capture_output=True, text=True, timeout=20
                )
            except (OSError, subprocess.TimeoutExpired):
                continue
            hits = [h.replace(str(run_dir / "_snap") + "/", "") for h in r.stdout.splitlines()[:3]]
            if hits:
                out.append(f"claimed not found: `{ident}` — rg finds it: " + " | ".join(h[:160] for h in hits))
            if len(out) >= limit:
                return out
    return out


def check_tracer(tracer: dict, flows: dict, scope_repos: dict[str, dict], run_dir: Path) -> dict:
    path = run_dir / "facts" / f"{tracer['id']}.md"
    res = {"must_fix": [], "flags": [], "citations_checked": 0}
    if not path.exists():
        res["must_fix"].append(f"{path.relative_to(run_dir)} was not written")
        return res
    text = path.read_text(encoding="utf-8", errors="replace")
    blk = blocks(text)
    by_flow = {f.get("id"): f for f in flows.get("flows") or []}
    for fid in tracer.get("flows") or []:
        if fid not in blk:
            res["must_fix"].append(f"no '## {fid} …' block for flow {fid}")
            continue
        secs = _sections(blk[fid])
        for s in SECTIONS:
            if s not in secs:
                res["flags"].append(f"{fid}: section '{s}' missing")
        for s in ("Shapes", "Callers"):
            body = secs.get(s, "")
            if s in secs and not CITE_RE.search(body) and not re.search(r"none found|searched", body, re.I):
                res["flags"].append(
                    f"{fid}: {s} has no citation and no 'none found — searched: …' (unclear if searched)"
                )
        verify = (by_flow.get(fid) or {}).get("verify") or []
        answers = secs.get("Verify answers", "")
        answered = len(re.findall(r"^\s*-\s*V\d+", answers, re.MULTILINE))
        if answered < len(verify):
            res["flags"].append(f"{fid}: {len(verify) - answered} of {len(verify)} verify items unanswered")
        elif verify and answered and len(re.findall(r"could not determine", answers, re.I)) >= answered:
            res["flags"].append(f"{fid}: every verify answer is 'could not determine'")
    broken, n = citations(text, scope_repos, run_dir)
    res["must_fix"] += broken
    res["citations_checked"] = n
    res["must_fix"] += false_absences(text, tracer.get("repos") or [], run_dir)
    return res


def check_ui(reader: dict, flows: dict, reqs: dict, run_dir: Path) -> dict:
    """reader: {"id", "flows": [...], "node_ids": set, "texts": set}."""
    path = run_dir / "facts" / f"{reader['id']}.md"
    res = {"must_fix": [], "flags": []}
    if not path.exists():
        res["must_fix"].append(f"{path.relative_to(run_dir)} was not written")
        return res
    text = path.read_text(encoding="utf-8", errors="replace")
    blk = blocks(text)
    for fid in reader.get("flows") or []:
        if fid not in blk:
            res["flags"].append(f"{fid}: has screens in this reader's share but no block was written")
    known = reader.get("node_ids") or set()
    bad = sorted({m for m in re.findall(r"\b\d+:\d+(?:;\d+:\d+)*\b", text) if m not in known})
    # node-id-shaped tokens that are clearly times ("10:30") are rare in these files; report the first few
    if bad:
        res["must_fix"].append(f"node ids not in the export: {', '.join(bad[:10])}")
    texts = reader.get("texts") or set()
    quoted = re.findall(r"[\"“]([^\"”\n]{6,120})[\"”]", text)
    paraphrased = [q for q in quoted if q not in texts and not any(q in t for t in texts)]
    if paraphrased:
        res["flags"].append(
            f'{len(paraphrased)} quoted string(s) not found in the export text, e.g. "{paraphrased[0][:60]}"'
        )
    req_ids = {r.get("id") for r in reqs.get("requirements") or []}
    unknown = sorted({r for r in re.findall(r"\bREQ-\d+\b", text) if r not in req_ids})
    if unknown:
        res["flags"].append(f"unknown REQ ids: {', '.join(unknown)}")
    return res


def check_understanding(run_dir: Path, scope_repos: dict[str, dict]) -> list[str]:
    path = run_dir / "work" / "understanding.md"
    if not path.exists():
        return ["work/understanding.md was not written"]
    broken, _ = citations(path.read_text(encoding="utf-8", errors="replace"), scope_repos, run_dir)
    return broken


# ---- write-time checks (called by the agent hook on every Write/Edit of a checked file) -------------------

_CODEISH = re.compile(r"_|[a-z][A-Z]|\(\)$")  # snake_case, camelCase, or a call — not plain words like `patient`


def misplaced_lines(text: str, repos: dict[str, dict], run_dir: Path, window: int = 3, limit: int = 10) -> list[str]:
    """Citations whose cited line (±window) doesn't mention the code identifier the same bullet names.

    Catches the most common real citation error — a line number that is off — which an existence check can't.
    Only identifiers that look like code (snake_case, camelCase, calls) are used; a bullet without one is skipped.
    """
    text = _SNAP_PREFIX.sub("", text)
    out: list[str] = []
    for line in text.splitlines():
        idents = [i.rstrip("()").split(".")[-1] for i in TICK_RE.findall(line) if _CODEISH.search(i)]
        idents = [i for i in idents if len(i) >= 4]
        if not idents:
            continue
        for m in CITE_RE.finditer(line):
            repo, rel, a, b = m.group(1), m.group(2), int(m.group(3)), m.group(4)
            if repo not in repos or not repos[repo].get("path"):
                continue
            f = run_dir / "_snap" / repo / rel
            try:
                src = f.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue  # missing file: reported by citations()
            lo, hi = max(0, a - 1 - window), min(len(src), (int(b) if b else a) + window)
            near = "\n".join(src[lo:hi])
            if near and not any(i in near for i in idents):
                out.append(f"{m.group(0)} doesn't mention `{idents[0]}` (lines {lo + 1}-{hi}) — wrong line?")
            if len(out) >= limit:
                return out
    return out


def write_time(path: Path, run_dir: Path) -> list[str]:
    """Problems in one just-written file: broken citations, false "not found" claims, misplaced lines."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    import json as _json

    try:
        scope = _json.loads((run_dir / "scope.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    repos = {r["repo"]: r for r in scope.get("repos") or []}
    broken, _ = citations(text, repos, run_dir)
    absent = false_absences(text, [r for r, v in repos.items() if v.get("path")], run_dir)
    misplaced = misplaced_lines(text, repos, run_dir)
    return broken[:10] + absent + misplaced
