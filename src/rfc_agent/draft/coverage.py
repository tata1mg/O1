"""out/coverage.json → checked, then rendered as the RFC's §2 table; References rendered from scope.json.

The lead never hand-writes the coverage table (the old pipeline's reviewers kept finding it inconsistent with
Open Questions). Python checks: every REQ present, statuses valid, `assumes`/`open` name a question id that
appears in §8, every section number exists as a heading.
"""

from __future__ import annotations

import re

STATUSES = {"covered": "✅ Covered", "assumes": "🟡 Covered, assumes", "deferred": "⏸ Deferred", "open": "❓ Open"}


def _section(text: str, number: str) -> str:
    """Body of the '## <number>. …' section."""
    m = re.search(rf"^##\s+{re.escape(number)}\.?\s.*?$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1) if m else ""


def headings(text: str) -> set[str]:
    """Section numbers that exist as headings: '4', '4.1', '5', 'CHANGE-3' …"""
    out: set[str] = set()
    for m in re.finditer(r"^#{2,4}\s+((?:\d+)(?:\.\d+)*)\.?\s", text, re.MULTILINE):
        out.add(m.group(1))
    for m in re.finditer(r"^#{2,4}\s+(CHANGE-\d+)\b", text, re.MULTILINE):
        out.add(m.group(1))
    return out


def question_ids(text: str) -> set[str]:
    return set(re.findall(r"\bQ\d+\b", _section(text, "8")))


def check(cov: dict, reqs: dict, rfc: str) -> list[str]:
    errs: list[str] = []
    req_ids = [r.get("id") for r in reqs.get("requirements") or []]
    missing = [r for r in req_ids if r not in cov]
    if missing:
        errs.append(f"coverage.json has no entry for: {', '.join(missing)}")
    unknown = [k for k in cov if k not in req_ids]
    if unknown:
        errs.append(f"coverage.json has unknown REQ ids: {', '.join(unknown)}")
    qs, heads = question_ids(rfc), headings(rfc)
    if not re.search(r"^##\s+2\.?\s", rfc, re.MULTILINE):
        errs.append("rfc.md has no '## 2. Requirements coverage' heading")
    for rid, e in cov.items():
        if not isinstance(e, dict):
            errs.append(f"{rid}: entry must be an object")
            continue
        st = e.get("status")
        if st not in STATUSES:
            errs.append(f"{rid}: status must be one of {', '.join(STATUSES)} (got {st!r})")
            continue
        if st in ("covered", "assumes"):
            secs = [str(s) for s in e.get("sections") or []]
            if not secs:
                errs.append(f"{rid}: '{st}' needs 'sections'")
            bad = [s for s in secs if s not in heads]
            if bad:
                errs.append(f"{rid}: sections {', '.join(bad)} are not headings in rfc.md")
        if st in ("assumes", "open"):
            q = e.get("question")
            if not q:
                errs.append(f"{rid}: '{st}' needs a 'question' id")
            elif q not in qs:
                errs.append(f"{rid}: question {q} does not appear in §8 Open questions")
        if st == "deferred" and not e.get("note"):
            errs.append(f"{rid}: 'deferred' needs a 'note' saying why")
    return errs


def _cell(r: dict, e: dict) -> str:
    st = e.get("status")
    label = STATUSES.get(st, "⚠ missing")
    secs = ", ".join(f"§{s}" for s in e.get("sections") or [])
    if st == "covered":
        return f"{label} ({secs})" if secs else label
    if st == "assumes":
        return f"{label} {e.get('question', '?')} ({secs})" + (f" — {e['note']}" if e.get("note") else "")
    if st == "deferred":
        return f"{label} — {e.get('note', '')}"
    if st == "open":
        return f"{label} — {e.get('question', '?')}"
    return label


def render_table(cov: dict, reqs: dict) -> str:
    rows = reqs.get("requirements") or []
    by_id = {r.get("id"): r for r in rows}
    lines = ["| REQ | Requirement | Priority | Status |", "|---|---|---|---|"]

    def row(r: dict, indent: str = "") -> str:
        text = str(r.get("text", "")).replace("|", "\\|")
        return f"| {indent}{r.get('id')} | {text} | {r.get('priority', '')} | {_cell(r, cov.get(r.get('id'), {}))} |"

    parents = [r for r in rows if not r.get("parent")]
    children: dict[str, list[dict]] = {}
    for r in rows:
        if r.get("parent") in by_id:
            children.setdefault(r["parent"], []).append(r)
    for r in parents:
        lines.append(row(r))
        lines += [row(c, "↳ ") for c in children.get(r.get("id"), [])]
    counts = {k: sum(1 for e in cov.values() if isinstance(e, dict) and e.get("status") == k) for k in STATUSES}
    summary = " · ".join(f"{STATUSES[k].split(',')[0]} {v}" for k, v in counts.items() if v)
    return "\n".join([f"*{len(rows)} requirements ({reqs.get('granularity', '?')}): {summary}*", "", *lines])


def insert_table(rfc: str, table: str) -> str:
    """Replace the body of '## 2. …' with the table (idempotent: a rerun replaces it again)."""
    m = re.search(r"^(##\s+2\.?\s.*?)$(.*?)(?=^##\s|\Z)", rfc, re.MULTILINE | re.DOTALL)
    if not m:
        return rfc
    return rfc[: m.start(2)] + "\n\n" + table + "\n\n" + rfc[m.end(2) :]


def references(scope: dict, raw_links: str = "") -> str:
    lines = ["## References", ""]
    prd = scope.get("prd")
    if prd:
        lines.append(f"- PRD: {prd}" + (f" — {scope['prd_title']}" if scope.get("prd_title") else ""))
    if scope.get("jira"):
        lines.append(f"- Jira: {scope['jira']}")
    for p in scope.get("pages_included") or []:
        lines.append(f"- Linked page: {p.get('title')} (page id {p.get('id')})")
    for f in scope.get("figma") or []:
        if f.get("url"):
            lines.append(f"- Figma: {f['url']}")
    lines += ["", "| Repo | Branch | Commit | Source |", "|---|---|---|---|"]
    for r in scope.get("repos") or []:
        lines.append(
            f"| {r.get('repo')} | {r.get('branch', '?')} | {str(r.get('sha') or '—')[:12]} | {r.get('source', '?')} |"
        )
    lines += ["", "*Every `repo/path:line` in this document refers to the commit listed for that repo.*"]
    return "\n".join(lines) + "\n"


def insert_references(rfc: str, refs: str) -> str:
    """Replace an existing '## References' section (to the end of the file) or append one."""
    m = re.search(r"^##\s+References\s*$", rfc, re.MULTILINE)
    base = rfc[: m.start()].rstrip() if m else rfc.rstrip()
    return base + "\n\n" + refs


def stats(rfc: str, cov: dict) -> dict:
    return {
        "size_kb": round(len(rfc.encode("utf-8")) / 1024, 1),
        "design_sections": len(re.findall(r"^###\s+4\.\d+", rfc, re.MULTILINE)),
        "changes": len(re.findall(r"^#{3,4}\s+CHANGE-\d+", rfc, re.MULTILINE)),
        "mermaid_blocks": len(re.findall(r"^```mermaid", rfc, re.MULTILINE)),
        "statuses": {k: sum(1 for e in cov.values() if isinstance(e, dict) and e.get("status") == k) for k in STATUSES},
    }
