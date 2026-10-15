"""Draft's gate on out/rfc.md (docs/DRAFT.md "The gate"). Each check returns a list of messages.

Must-fix: coverage, structure, CHANGE blocks, secrets, Mermaid lint — and citations / "not found" claims, which
the write-time hook normally catches as the lead writes (here they are a final assertion: a CLI update once
silently dropped hook feedback for a whole run).
Flags: unknown Q / REQ ids, uncited Current-state bullets, size and TL;DR length.
"""

from __future__ import annotations

import re
from pathlib import Path

from ..investigate import checks as s1

# ---- structure ------------------------------------------------------------------------------------

SECTION_NAMES = {
    1: "Context & problem",
    2: "Requirements coverage",
    3: "Options",
    4: "Design",
    5: "API contract",
    6: "Implementation plan",
    7: "Rollout, monitoring, risks",
    8: "Open questions",
}


def structure(rfc: str, flows: dict) -> list[str]:
    out = []
    for n, name in SECTION_NAMES.items():
        if not re.search(rf"^##\s+{n}\.?\s", rfc, re.MULTILINE):
            out.append(f"missing section '## {n}. {name}'")
    if not re.search(r"^##\s+Recommendation\b", rfc, re.MULTILINE | re.IGNORECASE):
        out.append("missing '## Recommendation: <option>' after §3 Options")
    for f in flows.get("flows") or []:
        fid = f.get("id")
        if fid and not re.search(rf"^###\s+4\.\d+\s+.*\b{re.escape(fid)}\b", rfc, re.MULTILINE):
            out.append(f"no design subsection '### 4.<n> {fid} …' for flow {fid} ({f.get('name', '')})")
    return out


# ---- CHANGE blocks ----------------------------------------------------------------------------------


def _change_blocks(rfc: str) -> dict[str, str]:
    out: dict[str, str] = {}
    parts = re.split(r"^#{3,4}\s+(?=CHANGE-\d+\b)", rfc, flags=re.MULTILINE)
    for p in parts[1:]:
        cid = re.match(r"(CHANGE-\d+)", p).group(1)
        body = re.split(r"^#{1,4}\s+(?!CHANGE-)", p, maxsplit=1, flags=re.MULTILINE)[0]
        out[cid] = body
    return out


_FILE_RE = re.compile(r"`?((?:[\w.-]+/)*[\w.-]+\.[A-Za-z0-9]+)(?::\d+(?:-\d+)?)?`?\s*\((modify|new)\)", re.I)


def change_blocks(rfc: str, run_dir: Path, repos: dict[str, dict]) -> list[str]:
    blocks = _change_blocks(rfc)
    out: list[str] = []
    deps: dict[str, list[str]] = {}
    for cid, body in blocks.items():
        dline = re.search(r"depends on\W*(.*)", body, re.IGNORECASE)
        ds = re.findall(r"CHANGE-\d+", dline.group(1)) if dline else []
        deps[cid] = ds
        for d in ds:
            if d not in blocks:
                out.append(f"{cid}: depends on {d}, which does not exist")
        rm = re.search(r"repo\W+`?([\w.-]+)`?", body, re.IGNORECASE)
        block_repo = rm.group(1) if rm else None
        for path, kind in _FILE_RE.findall(body):
            repo, rel = block_repo, path
            first = path.split("/", 1)[0]
            if first in repos and "/" in path:
                repo, rel = first, path.split("/", 1)[1]
            if repo not in repos or not repos[repo].get("path"):
                continue  # repo without a snapshot: can't check
            exists = (run_dir / "_snap" / repo / rel).exists()
            if kind.lower() == "modify" and not exists:
                out.append(f"{cid}: {repo}/{rel} is marked (modify) but does not exist in the {repo} snapshot")
            if kind.lower() == "new" and exists:
                out.append(
                    f"{cid}: {repo}/{rel} is marked (new) but already exists — it's a modify, or the path is wrong"
                )
    # cycles
    state: dict[str, int] = {}

    def visit(c: str, path: list[str]) -> None:
        state[c] = 1
        for d in deps.get(c, []):
            if state.get(d) == 1:
                out.append("CHANGE dependency cycle: " + " → ".join([*path[path.index(d) :], d]))
            elif d in deps and not state.get(d):
                visit(d, [*path, d])
        state[c] = 2

    for c in deps:
        if not state.get(c):
            visit(c, [c])
    return out


# ---- secrets ----------------------------------------------------------------------------------------

_SECRET_RES = [
    ("AWS access key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private key", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("JWT / bearer token", re.compile(r"\beyJ[\w-]{10,}\.[\w-]{10,}\.[\w-]{10,}\b")),
    ("credential in URL", re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{4,}@[\w.-]+")),
    (
        "secret assignment",
        re.compile(r"(?i)\b(api[_-]?key|secret|token|passw(?:or)?d)\b\s*[:=]\s*['\"]([^'\"\s]{12,})['\"]"),
    ),
]
_PLACEHOLDER = re.compile(r"(?i)^(<.*>|x{3,}|\*{3,}|changeme|your[_-].*|example.*|dummy.*|\$\{.*\}|\{\{.*\}\})$")


def secrets(rfc: str) -> list[str]:
    out = []
    for i, line in enumerate(rfc.splitlines(), 1):
        for name, rx in _SECRET_RES:
            m = rx.search(line)
            if not m:
                continue
            value = m.group(m.lastindex) if m.lastindex else m.group(0)
            if _PLACEHOLDER.match(value.strip()):
                continue
            out.append(f"line {i}: looks like a {name} — remove it or replace it with a placeholder")
    return out


# ---- Mermaid lint -------------------------------------------------------------------------------------

_NAME = r"[\w.]+(?:-[\w.]+)*"
_MSG = re.compile(rf"^\s*({_NAME}?)\s*(-->>|->>|--x|-x|--\)|-\)|-->|->)\s*([+-]?)({_NAME})\s*:(.*)$")
_DECL = re.compile(r"^\s*(participant|actor)\s+([\w.-]+)(?:\s+as\s+.+)?\s*$")
_OPEN = re.compile(r"^\s*(alt|opt|loop|par|rect|critical|break)\b")
_MID = re.compile(r"^\s*(else|and|option)\b")
_OK = re.compile(
    r"^\s*(sequenceDiagram|autonumber|activate|deactivate|note\s|Note\s|title|%%|box\b|end\b|"
    r"create\s|destroy\s|links?\s)|^\s*$"
)


def mermaid(rfc: str) -> tuple[list[str], int]:
    out: list[str] = []
    lines = rfc.splitlines()
    i, n_blocks = 0, 0
    while i < len(lines):
        if lines[i].strip().startswith("```mermaid"):
            n_blocks += 1
            start = i + 1
            j = start
            while j < len(lines) and not lines[j].strip().startswith("```"):
                j += 1
            where = f"diagram at line {i + 1}"
            if j >= len(lines):
                out.append(f"{where}: the ```mermaid fence is never closed")
                break
            body = [ln for ln in lines[start:j] if ln.strip() and not ln.strip().startswith("%%")]
            if not body:
                out.append(f"{where}: empty diagram")
            elif body[0].strip() != "sequenceDiagram":
                out.append(f"{where}: must start with 'sequenceDiagram' (found '{body[0].strip()[:40]}')")
            else:
                declared: set[str] = set()
                used: set[str] = set()
                depth = 0
                for k, ln in enumerate(lines[start:j], start + 1):
                    s = ln.strip()
                    if not s or s.startswith("%%") or s == "sequenceDiagram":
                        continue
                    m = _DECL.match(ln)
                    if m:
                        declared.add(m.group(2))
                        continue
                    if _OPEN.match(ln):
                        depth += 1
                        continue
                    if s == "end":
                        depth -= 1
                        if depth < 0:
                            out.append(f"{where}, line {k}: 'end' without an open alt/opt/loop/par block")
                            depth = 0
                        continue
                    if _MID.match(ln) or _OK.match(ln):
                        continue
                    m = _MSG.match(ln)
                    if m:
                        used |= {m.group(1), m.group(4)}
                        continue
                    if "->" in s or "--" in s:
                        out.append(f"{where}, line {k}: malformed message '{s[:60]}' (expected 'A->>B: text')")
                    else:
                        out.append(f"{where}, line {k}: unrecognised line '{s[:60]}'")
                if depth > 0:
                    out.append(f"{where}: {depth} alt/opt/loop/par block(s) not closed with 'end'")
                if declared and used - declared:
                    out.append(f"{where}: participants used but not declared: {', '.join(sorted(used - declared))}")
            i = j + 1
            continue
        i += 1
    return out, n_blocks


# ---- flags ------------------------------------------------------------------------------------------


def id_refs(rfc: str, reqs: dict) -> list[str]:
    out = []
    known_req = {r.get("id") for r in reqs.get("requirements") or []}
    bad = sorted({r for r in re.findall(r"\bREQ-\d+\b", rfc) if r not in known_req})
    if bad:
        out.append(f"REQ ids mentioned but not in reqs.json: {', '.join(bad)}")
    m = re.search(r"^##\s+8\.?\s.*?$(.*?)(?=^##\s|\Z)", rfc, re.MULTILINE | re.DOTALL)
    defined = set(re.findall(r"\bQ\d+\b", m.group(1))) if m else set()
    bad_q = sorted({q for q in re.findall(r"\bQ\d+\b", rfc[: m.start()] if m else rfc) if q not in defined})
    if bad_q:
        out.append(f"question ids mentioned but not in §8 Open questions: {', '.join(bad_q)}")
    return out


def uncited_current_state(rfc: str) -> list[str]:
    out = []
    for m in re.finditer(r"^###\s+(4\.\d+[^\n]*)\n(.*?)(?=^###?\s|\Z)", rfc, re.MULTILINE | re.DOTALL):
        cs = re.search(r"Current state\**\s*\n(.*?)(?=\n\s*\*\*|\Z)", m.group(2), re.DOTALL)
        if not cs:
            continue
        bullets = [b for b in re.findall(r"^\s*[-*]\s+(.*)$", cs.group(1), re.MULTILINE)]
        n = sum(1 for b in bullets if not s1.CITE_RE.search(b))
        if n:
            out.append(f"§{m.group(1).strip()[:50]}: {n} Current-state bullet(s) without a citation")
    return out


def size_and_tldr(rfc: str, target: str = "25–40") -> list[str]:
    out = []
    kb = len(rfc.encode("utf-8")) / 1024
    nums = [float(x) for x in re.findall(r"\d+", str(target))]
    if len(nums) == 2 and not (nums[0] <= kb <= nums[1]):
        out.append(f"size {kb:.0f} KB is outside the {target} KB target")
    tl = re.search(r"TL;DR\**[:\s]*(.*?)(?=\n\s*\n|\n#)", rfc, re.DOTALL)
    if tl:
        sentences = [x for x in re.split(r"(?<=[.!?])\s+", tl.group(1).strip()) if x]
        if len(sentences) > 3:
            out.append(f"TL;DR has {len(sentences)} sentences (target ≤ 3)")
    return out


# ---- all ----------------------------------------------------------------------------------------------


def run_all(
    run_dir: Path,
    rfc: str,
    cov: dict | None,
    cov_err: str,
    reqs: dict,
    flows: dict,
    scope: dict,
    size_target: str = "25–40",
) -> dict:
    from ..draft import coverage

    repos = {r["repo"]: r for r in scope.get("repos") or []}
    must: dict[str, list[str]] = {}
    broken, n_cites = s1.citations(rfc, repos, run_dir)
    must["citations"] = broken
    must["not_found_claims"] = s1.false_absences(rfc, [r for r, v in repos.items() if v.get("path")], run_dir)
    must["coverage"] = [cov_err] if cov_err else coverage.check(cov or {}, reqs, rfc)
    must["structure"] = structure(rfc, flows)
    must["change_blocks"] = change_blocks(rfc, run_dir, repos)
    must["secrets"] = secrets(rfc)
    merm, n_diag = mermaid(rfc)
    must["mermaid"] = merm
    flags = id_refs(rfc, reqs) + uncited_current_state(rfc) + size_and_tldr(rfc, size_target)
    unverifiable = sorted(r for r, v in repos.items() if not v.get("path") and re.search(rf"\b{re.escape(r)}/", rfc))
    if unverifiable:
        flags.append(f"citations into repos without a snapshot can't be checked: {', '.join(unverifiable)}")
    must = {k: v for k, v in must.items() if v}
    counts = {"citations": n_cites, "diagrams": n_diag, "changes": len(_change_blocks(rfc))}
    return {"must_fix": must, "flags": flags, "counts": counts}
