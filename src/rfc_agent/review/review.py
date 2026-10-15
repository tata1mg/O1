"""Parse reviewers' Markdown findings, check them (liberally), merge them into review/review.md.

The only structure relied on is the `### <SEVERITY>: <claim>` heading (docs/REVIEW.md "Finding format").
"""

from __future__ import annotations

import re
from pathlib import Path

from ..investigate import checks as s1

SEVERITIES = ("BLOCKER", "MAJOR", "MINOR")
_HEAD = re.compile(r"^###\s+([A-Za-z]+)\s*[:—-]\s*(.+?)\s*$", re.MULTILINE)


def parse(text: str) -> list[dict]:
    """[{severity, claim, body, recognised}] in file order."""
    out = []
    heads = list(_HEAD.finditer(text))
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(text)
        body = text[m.end() : end]
        body = re.split(r"^(?:##\s|Verdict:)", body, maxsplit=1, flags=re.MULTILINE)[0].strip()
        sev = m.group(1).upper()
        out.append(
            {
                "severity": sev if sev in SEVERITIES else "MINOR",
                "claim": m.group(2).strip(),
                "body": body,
                "recognised": sev in SEVERITIES,
            }
        )
    return out


def check(findings: list[dict], repos: dict[str, dict], run_dir: Path) -> list[str]:
    """Flags only — never a retry (docs: Checks)."""
    flags = []
    for i, f in enumerate(findings, 1):
        b = f["body"].lower()
        missing = [
            k
            for k, key in (("Evidence", "evidence:"), ("Failure scenario", "failure scenario:"), ("Fix", "fix:"))
            if key not in b and not (k == "Failure scenario" and f["severity"] == "MINOR")
        ]
        if missing:
            flags.append(f'#{i} "{f["claim"][:60]}": missing {", ".join(missing)}')
        if not f["recognised"]:
            flags.append(f'#{i} "{f["claim"][:60]}": no recognised severity — counted as MINOR')
        broken, _ = s1.citations(f["body"], repos, run_dir)
        if broken:
            f["unverified"] = True
            flags.append(f'#{i} "{f["claim"][:60]}": evidence unverified ({broken[0]})')
    return flags


def counts(findings: list[dict]) -> dict[str, int]:
    return {s: sum(1 for f in findings if f["severity"] == s) for s in SEVERITIES}


def merge(reviews: dict[str, dict]) -> str:
    """reviews: reviewer id -> {"title", "findings", "flags", "status"} (cross-cutting first, then flows)."""
    order = sorted(reviews, key=lambda r: (r != "R-X", r))
    total = {s: 0 for s in SEVERITIES}
    lines = ["# Review", "", "| Reviewer | Blocker | Major | Minor | Status |", "|---|---|---|---|---|"]
    for rid in order:
        c = counts(reviews[rid]["findings"])
        for s in SEVERITIES:
            total[s] += c[s]
        lines.append(
            f"| {rid} — {reviews[rid]['title']} | {c['BLOCKER']} | {c['MAJOR']} | {c['MINOR']} | "
            f"{reviews[rid]['status']} |"
        )
    lines.append(f"| **Total** | **{total['BLOCKER']}** | **{total['MAJOR']}** | **{total['MINOR']}** | |")
    for rid in order:
        r = reviews[rid]
        lines += ["", f"## {rid} — {r['title']}", ""]
        if r["status"] == "not reviewed":
            lines.append("⚠ **Not reviewed** — the reviewer failed twice; this part has had no independent review.")
            continue
        if not r["findings"]:
            lines.append("Sound — no findings.")
        for i, f in enumerate(r["findings"], 1):
            tag = " · ⚠ evidence unverified" if f.get("unverified") else ""
            lines += [f"### {rid}.{i} {f['severity']}: {f['claim']}{tag}", f["body"], ""]
        if r.get("dismissed"):
            lines += ["**Dismissed gate items**", r["dismissed"], ""]
    flags = [(rid, x) for rid in order for x in reviews[rid]["flags"]]
    if flags:
        lines += ["", "## Flags from the checks", ""] + [f"- {rid} {x}" for rid, x in flags]
    return "\n".join(lines).rstrip() + "\n"


def dismissed(text: str) -> str:
    m = re.search(r"^##\s+Dismissed gate items\s*$(.*?)(?=^##\s|\Z)", text, re.MULTILINE | re.DOTALL)
    return m.group(1).strip() if m else ""


def flow_parts(rfc: str, flow: dict) -> tuple[str, list[str]]:
    """(the flow's design section number, CHANGE ids that carry one of its REQs)."""
    fid = flow.get("id", "")
    m = re.search(rf"^###\s+(4\.\d+)\s+.*\b{re.escape(fid)}\b", rfc, re.MULTILINE)
    section = f"§{m.group(1)}" if m else f"the §4 subsection for {fid}"
    reqs = set(flow.get("reqs") or [])
    changes = []
    for cm in re.finditer(r"^#{3,4}\s+(CHANGE-\d+)\b(.*)$", rfc, re.MULTILINE):
        if reqs & set(re.findall(r"REQ-\d+", cm.group(2))):
            changes.append(cm.group(1))
    return section, changes
