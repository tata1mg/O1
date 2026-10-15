"""Findings, fix-log entries, confirmations, final statuses and the "⚠ Unresolved review findings" section.

Structure relied on (docs/REVISE.md):
  review/review.md   ### <R-id>.<n> <SEVERITY>: <claim>
  review/fixes.md    ## Round <N>  →  ### <id> FIXED | DISAGREE | OPEN
  review/*.confirm*  ### <id> RESOLVED | NOT RESOLVED | ACCEPTED   ·   ### NEW BLOCKER: <claim>
"""

from __future__ import annotations

import re

_FINDING = re.compile(r"^###\s+(R-[\w-]+\.N?\d+)\s+(BLOCKER|MAJOR|MINOR):\s*(.+?)(\s+·\s+⚠.*)?\s*$", re.MULTILINE)
_LEAD = re.compile(r"^###\s+(R-[\w-]+\.N?\d+)\s+(FIXED|DISAGREE|OPEN)\b", re.MULTILINE)
_VERDICT = re.compile(r"^###\s+(R-[\w-]+\.N?\d+)\s+(RESOLVED|NOT RESOLVED|ACCEPTED)\b", re.MULTILINE)
_NEW = re.compile(r"^###\s+NEW BLOCKER:\s*(.+?)\s*$", re.MULTILINE)
_NOT_REVIEWED = re.compile(r"^##\s+(R-[\w-]+)\s+—\s+(.+?)\s*\n+⚠ \*\*Not reviewed\*\*", re.MULTILINE)


def _bodies(text: str, rx: re.Pattern) -> list[tuple[re.Match, str]]:
    ms = list(rx.finditer(text))
    out = []
    for i, m in enumerate(ms):
        end = ms[i + 1].start() if i + 1 < len(ms) else len(text)
        body = text[m.end() : end]
        body = re.split(r"^#{2,3}\s", body, maxsplit=1, flags=re.MULTILINE)[0]
        out.append((m, body.strip()))
    return out


def findings(review_md: str) -> dict[str, dict]:
    """{id: {id, reviewer, severity, claim, body}} from the merged review."""
    out = {}
    for m, body in _bodies(review_md, _FINDING):
        fid = m.group(1)
        out[fid] = {
            "id": fid,
            "reviewer": fid.rsplit(".", 1)[0],
            "severity": m.group(2),
            "claim": m.group(3).strip(),
            "body": body,
        }
    return out


def not_reviewed(review_md: str) -> list[tuple[str, str]]:
    return [(m.group(1), m.group(2)) for m in _NOT_REVIEWED.finditer(review_md)]


def lead_entries(fixes_md: str, round_no: int) -> dict[str, dict]:
    """{id: {status, text}} for one round of review/fixes.md."""
    m = re.search(rf"^##\s+Round\s+{round_no}\s*$(.*?)(?=^##\s+Round\s|\Z)", fixes_md, re.MULTILINE | re.DOTALL)
    if not m:
        return {}
    return {x.group(1): {"status": x.group(2), "text": body} for x, body in _bodies(m.group(1), _LEAD)}


def confirmations(text: str, reviewer: str, round_no: int) -> tuple[dict[str, dict], list[dict]]:
    """({id: {verdict, text}}, [new blockers as findings with ids <reviewer>.N<k>])."""
    verdicts = {m.group(1): {"verdict": m.group(2), "text": body} for m, body in _bodies(text, _VERDICT)}
    new = []
    for k, (m, body) in enumerate(_bodies(text, _NEW), 1):
        fid = f"{reviewer}.N{round_no}{k}"
        new.append(
            {
                "id": fid,
                "reviewer": reviewer,
                "severity": "BLOCKER",
                "claim": m.group(1).strip(),
                "body": body,
                "raised_in_round": round_no,
            }
        )
    return verdicts, new


def settle(lead: dict | None, verdict: dict | None) -> str | None:
    """Final status for one finding this round, or None when it stays open for the next round."""
    if not lead or not verdict:
        return None
    v, s = verdict["verdict"], lead["status"]
    if v == "RESOLVED":
        return {"FIXED": "fixed", "OPEN": "open_question", "DISAGREE": "accepted_disagreement"}[s]
    if v == "ACCEPTED":
        return {"DISAGREE": "accepted_disagreement", "OPEN": "open_question", "FIXED": "fixed"}[s]
    return None  # NOT RESOLVED


def _cell(text: str, n: int = 160) -> str:
    return " ".join(str(text or "").split()).replace("|", "\\|")[:n]


def unresolved_section(items: list[dict]) -> str:
    """items: [{id, severity, claim, reviewer_position, lead_position}]."""
    lines = [
        "## ⚠ Unresolved review findings",
        "",
        "Still open after the review's fix → confirm rounds. For disagreements both positions are shown — "
        "decide before building.",
        "",
        "| id | severity | finding | reviewer's position | lead's position |",
        "|---|---|---|---|---|",
    ]
    for it in items:
        lines.append(
            f"| {it['id']} | {it.get('severity', '—')} | {_cell(it.get('claim'), 120)} | "
            f"{_cell(it.get('reviewer_position'))} | {_cell(it.get('lead_position'))} |"
        )
    return "\n".join(lines) + "\n"


def insert_unresolved(rfc: str, section: str | None) -> str:
    """Replace (or remove) the ⚠ section; it goes just before '## 8.' (or at the end). Idempotent."""
    rfc = re.sub(r"^##\s+⚠ Unresolved review findings\s*$.*?(?=^##\s|\Z)", "", rfc, flags=re.MULTILINE | re.DOTALL)
    if not section:
        return rfc
    m = re.search(r"^##\s+8\.?\s", rfc, re.MULTILINE)
    if m:
        return rfc[: m.start()] + section + "\n" + rfc[m.start() :]
    return rfc.rstrip() + "\n\n" + section
