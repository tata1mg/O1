#!/usr/bin/env python3
"""PostToolUse hook: continuous byte-budget accounting for the out/ deliverables.

Why (GENERATE speed forensics, 2026-08-15): the 50KB rfc.md ceiling was only ever enforced at the
TERMINAL gate, so the composer learned about it after the document was finished. v1 spent 4.3 minutes
manually binary-searching the ceiling; v2's revision DIED at it. Both are the same defect — a budget
the author cannot see until it is too late to plan around.

Two regimes (2026-08-18 forensics, radiology-slotting-engine run):
- BELOW the ceiling: warn once per threshold. A warning on every edit here would manufacture exactly
  the read-trim-recheck loop the gate's 2x-target policy exists to prevent (measured 2026-08-12:
  6+ minutes over a 2% overshoot).
- PAST the ceiling: fire on EVERY Write/Edit with exact byte numbers (bytes over, delta since the
  last edit, per-section bytes). Here trimming is gate-mandatory, and going silent after one warning
  forced the model to self-account: 27 manual `wc -c` calls and a byte-arithmetic reasoning tax in
  one observed revision. Continuous precise feedback is what kills that loop, not silence.

WRITE-TO-BUDGET pacing (runtime audit 2026-09-03, srp-bottomsheet-chat-history): with only the 90%
threshold, the first budget signal landed when the bytes were already spent — that compose overshot
the hard ceiling mid-draft and then paid a trim spiral (34 of 75 thinking blocks were budget
compliance, "still 2517 words over"; 45 Edits re-emitted 96K chars for a 60K-char deliverable —
2.7x rework, ~5-6 min). A model cannot count bytes while writing, so the earlier thresholds (40%,
70%) hand it the arithmetic while there is still budget to plan with: bytes used, the compose
TARGET (ceiling ÷ 1.2 — the SKILL's own target band), and — for the skeleton-materialized rfc.md —
the count of still-unfilled <FILL:> sections with a per-section allowance. One report per threshold,
planning-signal wording: size the remaining sections, never trim finished ones below the ceiling.

Feedback rides hooks/feedback.emit — JSON additionalContext on stdout, exit 0 (the tool already ran).
The exit-2/stderr channel this used until 2026-09-03 became a silent drop in CLI 2.1.139.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from rfc_agent.hooks.feedback import emit

# run-dir-relative deliverable -> hard ceiling. The GENERATE gate imports this table, so the number
# the model is warned about and the number that fails it can never drift. Ceilings are KB×1024 —
# always surfaced to the model in exact bytes ("30,720-byte"), never as "30KB", which one observed
# run burned reasoning disambiguating (decimal vs 1024-based).
# Ceiling = compose target + ~20% (output-shape audit 2026-08-24: at 2x-target ceilings the ceiling
# became the de-facto target — rfc.md shipped at 196% of target, parked just under 51,200 bytes;
# the ~20KB gold/exemplar RFC proves the target band is achievable for a 3-service feature).
DOC_CEILINGS_KB = {"out/rfc.md": 30, "out/contract.md": 10, "out/detail/plan.md": 18}
# one-shot report thresholds below the ceiling; past the ceiling stays continuous. 40/70 are the
# write-to-budget pacing signals (arithmetic while there is still budget to plan with); 90 is the
# near-ceiling warning. `level` in the state file = thresholds already reported (0-3).
_THRESHOLDS = (0.4, 0.7, 0.9)

_H2_RE = re.compile(r"^##[ \t]+(.+)$", re.MULTILINE)
_FILL_RE = re.compile(r"<FILL:")


def section_sizes(text: str) -> list[tuple[str, int]]:
    """(heading, bytes) per top-level section — the breakdown that names where the bytes went."""
    heads = list(_H2_RE.finditer(text))
    return [
        (m.group(1).strip()[:48],
         len(text[m.start(): heads[i + 1].start() if i + 1 < len(heads) else len(text)].encode()))
        for i, m in enumerate(heads)
    ]


def _state(run_dir: Path) -> tuple[Path, dict]:
    """Per-file hook memory in the run dir (hooks are separate processes). Values are
    {"level": <highest threshold warned>, "size": <bytes at last observation>}; pre-0.22.65 state
    files stored a bare level int — read those too."""
    p = run_dir / "logs" / "byte_budget.json"
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        raw = {}
    return p, {k: (v if isinstance(v, dict) else {"level": v, "size": 0}) for k, v in raw.items()}


def _save(state_p: Path, state: dict) -> None:
    try:
        state_p.parent.mkdir(parents=True, exist_ok=True)
        state_p.write_text(json.dumps(state))
    except OSError:
        pass


def main() -> None:
    run_dir_env = os.environ.get("RFC_AGENT_RUN_DIR")
    if not run_dir_env:
        sys.exit(0)
    try:
        ev = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)  # never break a run over a hook-payload surprise
    inp = ev.get("tool_input") or {}
    target = inp.get("file_path") or ""
    if not target:
        sys.exit(0)
    run_dir = Path(run_dir_env).expanduser().resolve()
    try:
        rel = str(Path(target).expanduser().resolve().relative_to(run_dir))
    except (OSError, ValueError):
        sys.exit(0)
    ceiling_kb = DOC_CEILINGS_KB.get(rel)
    if not ceiling_kb:
        sys.exit(0)
    # BUDGET FEEDBACK DISABLED BY DEFAULT (owner decision 2026-09-07): v5 forensics put the write
    # phase at 18.9 of 30 min with 84 edits on rfc.md, much of it trim churn the pacing feedback
    # feeds; the industry survey found no production agent gating output length. The hook still
    # writes its state file — the hooks-alive canary keys on it — but says nothing unless
    # RFC_AGENT_GENERATE_BYTE_BUDGET re-enables it. "We'll figure something out later."
    if os.environ.get("RFC_AGENT_GENERATE_BYTE_BUDGET", "0").strip().lower() not in ("1", "true", "on"):
        sp, st = _state(run_dir)
        _save(sp, st)  # canary marker only — hooks-alive keys on this file existing
        sys.exit(0)
    # revision waiver (2026-08-25): the gate skips ceilings during revisions (guidance file = the
    # revision signal), so warning here would only manufacture the trim-to-fit anxiety the waiver
    # exists to kill — the agent saw "2,357 bytes over" mid-fix and spent a trim pass on it
    if (run_dir / "guidance" / "generate.md").exists():
        sys.exit(0)
    p = run_dir / rel
    try:
        size = p.stat().st_size
        text = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        sys.exit(0)
    ceiling = ceiling_kb * 1024
    crossed = sum(size >= ceiling * t for t in _THRESHOLDS)  # thresholds this size has reached, 0-3
    if size <= ceiling and crossed == 0:
        sys.exit(0)

    state_p, state = _state(run_dir)
    prev = state.get(rel, {"level": 0, "size": 0})
    state[rel] = {"level": max(prev["level"], 4 if size > ceiling else crossed), "size": size}
    _save(state_p, state)

    if size <= ceiling and prev["level"] >= crossed:
        sys.exit(0)  # each below-ceiling threshold reports once ever — never a trim loop down here

    breakdown = " · ".join(
        f"{h} {n:,}B" for h, n in sorted(section_sizes(text), key=lambda kv: -kv[1])[:6] if n >= 1024
    )
    if size > ceiling:
        delta = f"; this edit changed it by {size - prev['size']:+,} bytes" if prev["size"] else ""
        emit(
            f"BYTE BUDGET — {rel} is {size:,} bytes, {size - ceiling:,} bytes PAST its {ceiling:,}-byte "
            f"hard ceiling{delta}. The gate fails this file until it fits.\n"
            f"Largest sections: {breakdown or 'n/a'}\n"
            f"This report repeats after every edit with exact numbers — NEVER run wc/stat yourself. "
            f"Apply any content-ADDING fixes first, then ONE consolidated trim pass at the end; cut "
            f"restated prose and re-narrated findings, never REQ tags, coverage rows, or citations.",
        )
    elif crossed == 3:
        emit(
            f"BYTE BUDGET — {rel} is {size:,} bytes of its {ceiling:,}-byte hard ceiling "
            f"({100 * size // ceiling}%).\nLargest sections: {breakdown or 'n/a'}\n"
            f"Plan the REMAINING sections against what is left; cut restated prose and re-narrated "
            f"findings, never REQ tags, coverage rows, or citations. Do NOT start a trim-and-recheck "
            f"pass over finished sections — this is a planning signal, not a task.",
        )
    else:
        # write-to-budget pacing (40%/70%): the arithmetic the model cannot do while writing,
        # delivered while there is still budget to plan with (2026-09-03 trim-spiral forensics)
        target = int(ceiling / 1.2)  # ceiling = compose target + 20% (output-shape audit 2026-08-24)
        unfilled = len(_FILL_RE.findall(text))
        left = max(target - size, 0)
        pace = (f"{unfilled} skeleton section(s) still unfilled — remaining target budget "
                f"≈ {left:,} bytes (~{left // unfilled:,} per unfilled section).\n") if unfilled else (
                f"Remaining budget to the compose target ≈ {left:,} bytes.\n")
        emit(
            f"BYTE BUDGET pacing — {rel} is {size:,} bytes: {100 * size // ceiling}% of its "
            f"{ceiling:,}-byte hard ceiling; compose target ~{target:,}.\n"
            f"Largest sections so far: {breakdown or 'n/a'}\n{pace}"
            f"Planning signal, not a task: SIZE the remaining sections to fit — never trim or "
            f"re-measure finished sections below the ceiling (this report returns at the next "
            f"threshold with exact numbers; NEVER run wc/stat yourself).",
        )
    sys.exit(0)


if __name__ == "__main__":
    main()
