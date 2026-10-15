"""Response-as-document: the composer EMITS sections as tagged text; the harness materializes them.

Measured driver (v5, 2026-09-07): the write phase was 239 model turns / 18.9 min moving only ~23K
content tokens — 20 tok/s effective against a 113 tok/s streaming rate, 82% of the wall spent on
per-Edit round-trips. The industry pattern (Bolt/v0 whole-artifact emission with "ALWAYS FULL
content, NEVER placeholders"; Claude artifacts' update-at-most-4-times-else-rewrite; aider's finding
that whole-emission is the MOST reliable format and is avoided only for interactive latency we don't
have) is: the model speaks the document, deterministic code writes it. Expected write phase ~4 min.

Protocol the composer follows (taught in SKILL.md):
  <emit path="out/rfc.md" section="## 5. Design">        one FULL section, no placeholders
  ...markdown...
  </emit>
  section="*" replaces the whole file (contract.md-sized docs, one breath).
Revisions: re-emit the changed section(s), or ONE batch of anchored edits:
  <edits path="out/rfc.md">[{"old": "...exact unique...", "new": "..."}, ...]</edits>
Edits apply order-invariantly and atomically per path (all anchors must match exactly once —
Cline measured +10% edit success from harness-side multi-apply; aider measured 9x errors without
application flexibility, so failures come back as gate text, never silent).

Fallback is automatic: a stream with no emit/edits blocks materializes nothing, and the legacy
tool-written files stand — GENERATE.EMIT=0 also disables parsing outright.

Materialization is INCREMENTAL (v7 incident, 2026-09-07): the composer's in-turn oracle (selfcheck)
drains pending blocks to disk before checking, and the post-turn call drains only the remainder. A
cursor in logs/generate.emit.json — bound to the stream's identity, since streams rotate per attempt —
guarantees every block applies exactly once, in order. The one-shot replay this replaces broke three
ways in one live run: replayed <edits> re-rejected on spent anchors, replayed emits reverted later
fixes (including the gate's own citation auto-repairs), and a replayed emit whose heading had since
changed appended a duplicate section. All three share the root "same event processed twice"; the
cursor removes the root. This is also the bolt.diy StreamingMessageParser pattern the research named
as the reference implementation — materialize as blocks complete, never re-parse from zero.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

_ALLOWED = ("out/rfc.md", "out/contract.md", "out/detail/plan.md")
_EMIT_RE = re.compile(
    r"<emit\s+path=\"(?P<path>[^\"]+)\"\s+section=\"(?P<section>[^\"]+)\"\s*>\n?(?P<body>.*?)\n?</emit>",
    re.DOTALL)
_EDITS_RE = re.compile(r"<edits\s+path=\"(?P<path>[^\"]+)\"\s*>\n?(?P<body>.*?)\n?</edits>", re.DOTALL)
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$", re.MULTILINE)


def emit_enabled(cfg) -> bool:
    val = cfg.get("GENERATE.EMIT", "1") if cfg else "1"
    return str(val).strip().lower() not in ("0", "false", "no", "off")


def _norm_heading(h: str) -> str:
    return re.sub(r"\s+", " ", h.strip().lstrip("#").strip()).lower()


def _heading_level(line: str) -> int:
    m = _HEADING_RE.match(line)
    return len(m.group(1)) if m else 0


def splice_section(text: str, section: str, body: str) -> tuple[str, str]:
    """Replace `section`'s block in `text` with `body`; returns (new_text, action).

    A section block runs from its heading line to just before the next heading of the same or a
    higher level — the same boundary the byte hooks and doc lints use. Matching is by normalized
    heading text so `## 5. Design` matches `section="5. Design"`. An absent heading appends (new
    sections are legal); `section="*"` replaces the whole file."""
    if section.strip() == "*":
        return body.rstrip() + "\n", "replaced-file"
    want = _norm_heading(section)
    lines = text.splitlines()
    start = end = None
    for i, ln in enumerate(lines):
        lvl = _heading_level(ln)
        if lvl and _norm_heading(ln) == want:
            start = i
            for j in range(i + 1, len(lines)):
                nxt = _heading_level(lines[j])
                if nxt and nxt <= lvl:
                    end = j
                    break
            end = end if end is not None else len(lines)
            break
    if start is None:
        sep = "" if text.endswith("\n\n") or not text else ("\n" if text.endswith("\n") else "\n\n")
        return text + sep + body.rstrip() + "\n", "appended"
    new = lines[:start] + body.rstrip().splitlines() + [""] + lines[end:]
    return "\n".join(new).rstrip("\n") + "\n", "replaced"


def apply_edits(text: str, edits: list[dict]) -> tuple[str, list[str]]:
    """Order-invariant, atomic batch: every `old` must appear EXACTLY once (the str_replace
    contract); any failure rejects the whole batch so a half-applied revision can never ship."""
    failures = []
    for k, e in enumerate(edits, 1):
        old = e.get("old") or ""
        if not old or not isinstance(e.get("new"), str):
            failures.append(f"edit {k}: needs non-empty 'old' and string 'new'")
            continue
        n = text.count(old)
        if n != 1:
            failures.append(f"edit {k}: anchor matches {n} time(s), needs exactly 1: "
                            f"{old[:80]!r}")
    if failures:
        return text, failures
    for e in edits:
        text = text.replace(e["old"], e["new"], 1)
    return text, []


def _assistant_events(stream_path: Path) -> tuple[str, list[list[str]]]:
    """(stream identity, per-assistant-event text lists), COMPLETE lines only — a mid-append tail line
    has no trailing newline yet and must not be consumed (it would be re-read, differently, next call).
    Identity is the first line's hash: streams rotate per attempt, and a cursor from attempt N applied
    to attempt N+1's file would silently skip that many fresh events."""
    raw = stream_path.read_text(encoding="utf-8", errors="replace")
    lines = raw.split("\n")
    if lines and not raw.endswith("\n"):
        lines = lines[:-1]
    sid = hashlib.sha1(lines[0].encode()).hexdigest() if lines and lines[0].strip() else ""
    events: list[list[str]] = []
    for line in lines:
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict) or o.get("type") != "assistant":
            continue
        texts = [b["text"] for b in (o.get("message") or {}).get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text" and b.get("text")]
        events.append(texts)
    return sid, events


def _state_path(run_dir: Path) -> Path:
    return run_dir / "logs" / "generate.emit.json"


def _load_state(run_dir: Path) -> dict:
    try:
        st = json.loads(_state_path(run_dir).read_text())
        return st if isinstance(st, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _reject(stats: dict, failure: str) -> None:
    stats["edit_failures"].append(failure)
    stats["edit_failures_total"] += 1


def _resolve_path_failures(stats: dict, rel: str) -> None:
    """A clean apply/emit on `rel` IS the documented recovery for a rejection on `rel` — its standing
    failures stop haunting the gate (a rejection the composer never recovers stays surfaced forever)."""
    stats["edit_failures"] = [f for f in stats["edit_failures"]
                              if f"path={rel}>" not in f and f"path {rel!r}" not in f]


def _apply_blocks(text: str, run_dir: Path, stats: dict) -> None:
    for m in _EMIT_RE.finditer(text):
        rel = m.group("path").strip()
        if rel not in _ALLOWED:
            _reject(stats, f"<emit> path {rel!r} not a deliverable — ignored")
            continue
        p = run_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        cur = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        new, action = splice_section(cur, m.group("section"), m.group("body"))
        p.write_text(new, encoding="utf-8")
        stats["sections"] += 1
        stats[action.replace("-", "_")] = stats.get(action.replace("-", "_"), 0) + 1
        _resolve_path_failures(stats, rel)
    for m in _EDITS_RE.finditer(text):
        rel = m.group("path").strip()
        if rel not in _ALLOWED:
            _reject(stats, f"<edits> path {rel!r} not a deliverable — ignored")
            continue
        stats["edit_batches"] += 1
        try:
            edits = json.loads(m.group("body"))
            assert isinstance(edits, list)
        except (json.JSONDecodeError, AssertionError):
            _reject(stats, f"<edits path={rel}>: body is not a JSON list")
            continue
        p = run_dir / rel
        cur = p.read_text(encoding="utf-8", errors="replace") if p.exists() else ""
        new, failures = apply_edits(cur, edits)
        if failures:
            for f in failures:
                _reject(stats, f"<edits path={rel}> rejected (atomic): {f}")
            continue
        p.write_text(new, encoding="utf-8")
        stats["edits_applied"] += len(edits)
        _resolve_path_failures(stats, rel)


