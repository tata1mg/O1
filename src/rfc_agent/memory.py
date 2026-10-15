"""Durable per-stage memory: the pipeline's learning loop (Prime-Agent ρ+M, scorer-blind by construction).

Run #50 was exactly as smart as run #1: `guidance/` is per-invocation by design, `decisions.jsonl`
records every retry/repair with a why that nothing ever read back, and the only durable learning
channel was a human editing the repo. This module closes that loop with the discipline the Prime Agent
Factorio episode proved necessary:

- **Scorer-blind:** the promotion pass sees PROCESS signals only — decision records, gate-failure
  classes, attempts, durations. Never review verdicts, never eval scores, never gold RFCs. A learning
  loop that can see its scorer learns to game it (measured externally: the /refine loop that
  industrialized an RCON cheat; Berkeley RDI: every major agent benchmark was gameable).
- **Evidence-required, smallest-edit:** ≤3 notes per run, each carrying its trigger. Speculation is
  rejected by prompt AND by the dedup filter.
- **Human-reviewable:** notes live in ~/.config/rfc-agent/memory/<stage>.md (outside every repo,
  same reasoning as config.json), plain markdown, git-init-able for diff/rollback.
- **Below human guidance in authority:** the engine folds notes in under any human guidance block.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

from .contracts import RunContext

MEMORY_ROOT = Path("~/.config/rfc-agent/memory").expanduser()
_NOTE_CAP_BYTES = 4_000   # per-stage prompt budget: memory must never crowd out the task itself
                          # (raised from 2k on 2026-08-10: outcome annotations lengthen lines, and the
                          # old cap was silently evicting the OLDEST lessons while duplicates rotated in)
_MAX_NOTES_PER_RUN = 3
_STAGES = ("ingest", "extract", "ground", "generate", "review")


def load_notes(stage_name: str) -> str:
    """The stage's durable notes, newest last, capped for prompt inclusion ('' when none)."""
    f = MEMORY_ROOT / f"{stage_name}.md"
    if not f.is_file():
        return ""
    text = f.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) > _NOTE_CAP_BYTES:  # keep the newest notes (file is append-only)
        text = text[-_NOTE_CAP_BYTES:]
        text = text[text.find("\n- ") + 1:] if "\n- " in text else text
    return text


def gather_signals(ctx: RunContext) -> str:
    """PROCESS signals only. This function is the scorer-blindness boundary — it must never read
    review/review.md, eval scores, or gold references (pinned by test)."""
    parts: list[str] = []
    mpath = ctx.run_dir / "manifest.json"
    if mpath.exists():
        try:
            man = json.loads(mpath.read_text())
            for st, entry in man.items():
                if not isinstance(entry, dict):
                    continue
                parts.append(f"[{st}] attempts={entry.get('attempts')} "
                             f"duration_total_s={entry.get('duration_s_total')} status={entry.get('status')}")
                for f in (entry.get("gate") or {}).get("failures", [])[:6]:
                    if "review BLOCKER" in f:  # panel content is scorer territory — strip it
                        continue
                    parts.append(f"  gate-failure: {f[:200]}")
        except (json.JSONDecodeError, OSError):
            pass
    dpath = ctx.run_dir / "logs" / "decisions.jsonl"
    if dpath.exists():
        for line in dpath.read_text(encoding="utf-8", errors="replace").splitlines()[-40:]:
            try:
                o = json.loads(line)
            except json.JSONDecodeError:
                continue
            parts.append(f"decision [{o.get('stage')}] {o.get('decision')}: {str(o.get('why'))[:150]}")
    # tool denials: the richest agent-behavior signal there is (first live promotion missed a run's
    # 8-denial storm entirely because only decisions/gates were visible)
    apath = ctx.run_dir / "logs" / "activity.log"
    if apath.exists():
        denials = [ln for ln in apath.read_text(encoding="utf-8", errors="replace").splitlines()
                   if "⛔ denied" in ln]
        for ln in denials[-15:]:
            parts.append(f"tool-denial: {ln[:200]}")
    return "\n".join(parts)


_NOTE_RE = re.compile(r"^(ingest|extract|ground|generate|review)\s*:\s*(.{20,300})$", re.MULTILINE)


def _default_ask(prompt: str, model: str) -> str:
    r = subprocess.run(
        ["claude", "-p", "--model", model, "--max-turns", "1"],
        input=prompt, capture_output=True, text=True, timeout=120,
    )
    return r.stdout if r.returncode == 0 else ""


def promote(ctx: RunContext, ask=None) -> list[str]:
    """Post-run promotion pass: propose ≤3 durable, evidence-backed per-stage notes from this run's
    process signals. Returns the notes written (possibly empty). Never raises — learning is an
    accelerator, not a failure source."""
    try:
        signals = gather_signals(ctx)
        if not signals.strip():
            return []
        cfg = ctx.config
        model = ((cfg.get("AGENT.STAGE_MODELS", {}) or {}).get("promote") if cfg else None) or "claude-haiku-4-5"
        prompt = (
            "You are the post-run learning pass of a staged RFC pipeline (stages: ingest, extract, "
            "ground, generate, review). Below are THIS run's process signals: per-stage attempts, "
            "durations, deterministic gate failures, harness decisions, and tool denials.\n\n"
            "Propose AT MOST 3 durable one-line lessons that would make FUTURE runs converge faster. "
            "A lesson is an instruction to the STAGE AGENT about how IT should behave differently — "
            "never a restatement of what the harness/decision records already do mechanically (e.g. "
            "'build scope deterministically' or 'apply patch_revision' are the harness's own moves and "
            "are WORTHLESS as lessons; the first live promotion produced exactly those). The strongest "
            "lessons come from REPEATED tool denials (what command shapes to avoid and what to do "
            "instead) and repeated gate-failure classes (what to get right the first time). Rules: "
            "traceable to a concrete signal below; actionable by the agent; skip anything a gate "
            "already enforces; skip run-specific facts (ticket ids, dates, file names). If no signal "
            "supports a real lesson, output NOTHING — an empty answer beats a filler note. Output "
            "format, one per line, nothing else:\n"
            "<stage>: <lesson>\n\n"
            f"SIGNALS:\n{signals[:12000]}"
        )
        out = (ask or _default_ask)(prompt, model)
        written: list[str] = []
        MEMORY_ROOT.mkdir(parents=True, exist_ok=True)
        for m in list(_NOTE_RE.finditer(out))[:_MAX_NOTES_PER_RUN]:
            stage_name, note = m.group(1), m.group(2).strip()
            f = MEMORY_ROOT / f"{stage_name}.md"
            existing = f.read_text(encoding="utf-8", errors="replace") if f.exists() else ""
            # dedup IS measurement: re-proposing an existing lesson means its signal recurred while the
            # note was in the prompt — the strongest possible evidence the prose isn't binding. v5
            # (2026-08-10): ground.md accumulated THREE paraphrases of "never search from root" because
            # the old prefix check couldn't see paraphrase; each duplicate now bumps the original's
            # violation count instead of appending a copy.
            dup = _find_duplicate(existing, note)
            if dup:
                f.write_text(existing.replace(dup, _bump_outcome(dup, ctx.run_dir.name), 1))
                continue
            if note[:60].lower() in existing.lower():  # crude dedup: don't re-learn the same lesson
                continue
            entry = f"- [{date.today().isoformat()} run:{ctx.run_dir.name}] {note} (outcome: pending)\n"
            with f.open("a") as fh:
                if not existing:
                    fh.write(f"# {stage_name} — durable lessons (evidence-backed, human-reviewable)\n\n")
                fh.write(entry)
            written.append(f"{stage_name}: {note}")
        if written:
            print(f"  [memory] promoted {len(written)} durable note(s) -> {MEMORY_ROOT}", file=sys.stderr)
        return written
    except Exception as exc:  # noqa: BLE001 — never let learning break a finished run
        print(f"  [memory] promotion skipped ({exc})", file=sys.stderr)
        return []


# ---- outcome audit: the teeth the notes alone never had (v5 postmortem, 2026-08-10) --------------
# Verified failure of the advisory loop: generate.md carried "validate RFC citations before output"
# since m13, the v5 session's own summary acknowledged reading it, and the run still wrote 34 bare
# citations; ground repeated `find /` against a note warning exactly that. Notes were write-only —
# every one `(outcome: pending)` forever, nothing measured whether a lesson ever held. This audit
# closes the loop mechanically: each class maps note keywords to a recurrence signal in the finished
# run's own artifacts, and classes the harness has since absorbed into code retire their prose.

_STOPWORDS = frozenset("a an the of to in for and or not on with use avoid when via is are".split())
_OUTCOME_RE = re.compile(r"\s*\(outcome:[^)]*\)\s*$")


def _tokens(s: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z][a-z_-]+", s.lower()) if w not in _STOPWORDS)


def _find_duplicate(existing: str, note: str) -> str | None:
    """The existing note line this note paraphrases (Jaccard >= 0.45), or None."""
    new = _tokens(note)
    if not new:
        return None
    best, best_j = None, 0.45
    for line in existing.splitlines():
        if not line.startswith("- ["):
            continue
        old = _tokens(_OUTCOME_RE.sub("", line))
        j = len(new & old) / len(new | old) if new | old else 0.0
        if j >= best_j:
            best, best_j = line, j
    return best


def _bump_outcome(line: str, run_name: str) -> str:
    """pending/held -> violated; violated xN -> xN+1. A duplicate promotion or a matched recurrence
    signal both mean: the lesson was in the prompt and the behavior happened anyway."""
    m = re.search(r"\(outcome: violated ×(\d+)", line)
    n = int(m.group(1)) + 1 if m else 1
    tail = f"(outcome: violated ×{n} — last run:{run_name}; prose is not binding, needs a mechanism)"
    return _OUTCOME_RE.sub("", line).rstrip() + " " + tail


# (note-keyword pattern, recurrence test over this run's signals, mechanism that now absorbs the
# class). ORDER MATTERS — first match wins, so interpreter notes must precede the compound class:
# the hook deliberately DEFERS python/node (unauditable), so "python extractor" lessons stay measured,
# not absorbed.
_SIGNAL_CLASSES: list[tuple[re.Pattern, re.Pattern, str | None]] = [
    (re.compile(r"(?i)python|subprocess|json directly|Read tool"),
     re.compile(r"(?i)python"),
     None),
    (re.compile(r"(?i)root|find /|filesystem find|system-wide"),
     re.compile(r"find /(?:\s|$)"),
     "approve_readonly hook denies root-find with a scoped substitution hint (2026-08-10)"),
    (re.compile(r"(?i)pip(?:e|ing|ed)|chain|compound|cd &&|cd-based|for-loop|separate (?:calls|Read)"),
     re.compile(r"\||&&|\bfor \w+ in\b"),
     "approve_readonly hook auto-approves read-only compound Bash (2026-08-10)"),
    (re.compile(r"(?i)citation"),
     re.compile(r"(?i)bare citation|citations?/coverage"),
     "qualify_citations hard line-count filter + doc-cited tie-break (2026-08-10)"),
]


def audit(ctx: RunContext) -> None:
    """Post-run outcome pass over every stage's notes. For each note: if a mechanism now owns its
    class, retire it (absorbed); else if its recurrence signal fired in THIS run, mark it violated
    (the note was in the prompt and didn't bind); else, if its stage ran, mark it held. Never raises."""
    try:
        apath = ctx.run_dir / "logs" / "activity.log"
        denials = [ln for ln in apath.read_text(encoding="utf-8", errors="replace").splitlines()
                   if "⛔ denied" in ln] if apath.exists() else []
        gate_text = ""
        dpath = ctx.run_dir / "logs" / "decisions.jsonl"
        if dpath.exists():
            gate_text = dpath.read_text(encoding="utf-8", errors="replace")
        mpath = ctx.run_dir / "manifest.json"
        ran = set(json.loads(mpath.read_text()).keys()) if mpath.exists() else set()

        for f in MEMORY_ROOT.glob("*.md"):
            stage = f.stem
            stage_denials = "\n".join(ln for ln in denials if f"[{stage}" in ln)
            out_lines: list[str] = []
            changed = 0
            for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
                if not (line.startswith("- [") and "(outcome:" in line):
                    out_lines.append(line)
                    continue
                if "absorbed" in line or "retracted" in line:
                    out_lines.append(line)
                    continue
                body = _OUTCOME_RE.sub("", line)
                cls = next((c for c in _SIGNAL_CLASSES if c[0].search(body)), None)
                if cls is None:
                    out_lines.append(line)  # unmeasurable note — an honest audit stamps nothing
                    continue
                if cls[2]:
                    out_lines.append(body.rstrip() + f" (outcome: absorbed — {cls[2]})")
                elif cls[1].search(stage_denials) or (stage == "generate" and cls[1].search(gate_text)):
                    out_lines.append(_bump_outcome(line, ctx.run_dir.name))
                elif stage in ran and ("(outcome: pending)" in line or "(outcome: held" in line):
                    # never downgrade a violated note — one quiet run doesn't erase the tally
                    out_lines.append(body.rstrip() + f" (outcome: held run:{ctx.run_dir.name})")
                else:
                    out_lines.append(line)
                    continue
                changed += 1
            if changed:
                f.write_text("\n".join(out_lines) + "\n")
                print(f"  [memory] audited {changed} note(s) in {f.name}", file=sys.stderr)
        violated = [ln for f in MEMORY_ROOT.glob("*.md") for ln in f.read_text().splitlines()
                    if "violated ×2" in ln or "violated ×3" in ln]
        for ln in violated:
            print(f"  [memory] ESCALATE (prose failed >=2 runs, build a mechanism): {ln[:160]}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 — the audit must never break a finished run
        print(f"  [memory] audit skipped ({exc})", file=sys.stderr)
