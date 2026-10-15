"""Harness-level user interaction at stage boundaries.

Decision (team, 2026-07-24): input questions are asked upfront (CLI entry); target/surface scoping is
asked directly (B4 kept simple in M1); scan-era open questions are asked at the GROUND boundary. Everything
degrades cleanly to non-interactive (eval harness): no TTY or --no-interactive => no prompts, questions stay
open in the docs, and skipped asks are logged.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from .constants import CheckpointAction

_QUESTION_LINE = re.compile(r"^\s*(?:[-*]|\d+\.)\s+(.{10,})", re.MULTILINE)


def parse_open_questions(findings_md: str) -> list[str]:
    """Bullet/numbered items under the '## Open questions' section."""
    m = re.search(r"^##\s*Open questions\s*$(.*?)(?=^##\s|\Z)", findings_md, re.MULTILINE | re.DOTALL | re.IGNORECASE)
    if not m:
        return []
    return [q.strip() for q in _QUESTION_LINE.findall(m.group(1))]


def record_wait(run_dir: Path, where: str, seconds: float) -> None:
    """One line per blocking ask in logs/operator_wait.jsonl. Runtime audit 2026-09-03: ~28 of a
    109-minute run was operator-wait, and every second of it had to be excavated from timestamp
    archaeology across four log files — the runner folds this file into manifest['operator_wait']
    so a run self-reports how long it sat blocked on a human, and where."""
    try:
        p = Path(run_dir) / "logs" / "operator_wait.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            from datetime import UTC, datetime

            ts = datetime.now(UTC).isoformat(timespec="seconds")
            f.write(json.dumps({"ts": ts, "where": where, "seconds": round(seconds, 1)}) + "\n")
    except OSError:
        pass


def operator_wait_summary(run_dir: Path) -> dict | None:
    """Aggregate of logs/operator_wait.jsonl: {'total_s', 'asks', 'by': {where: seconds}} — the
    run-dir-lifetime truth, same cumulative semantics as the manifest's *_total fields."""
    p = Path(run_dir) / "logs" / "operator_wait.jsonl"
    if not p.exists():
        return None
    by: dict[str, float] = {}
    asks = 0
    try:
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                rec = json.loads(line)
                by[str(rec.get("where"))] = round(by.get(str(rec.get("where")), 0) + float(rec.get("seconds") or 0), 1)
                asks += 1
            except (json.JSONDecodeError, TypeError, ValueError):
                continue
    except OSError:
        return None
    return {"total_s": round(sum(by.values()), 1), "asks": asks, "by": by} if asks else None


def prompt_answers(questions: list[str], source: str) -> dict[str, str]:
    """One prompt per question on the controlling TTY. Empty answer = leave it open."""
    answers: dict[str, str] = {}
    print(f"\n{len(questions)} open question(s) from {source} — Enter to leave open:", file=sys.stderr)
    for i, q in enumerate(questions, 1):
        print(f"\n[{i}/{len(questions)}] {q}", file=sys.stderr)
        try:
            reply = input("answer (Enter to skip): ").strip()
        except EOFError:
            break
        if reply:
            answers[q] = reply
    return answers


def write_answers(path: Path, answers: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = ["# Human answers to open questions", ""]
    lines += [f"- **Q:** {q}\n  **A:** {a}" for q, a in answers.items()]
    path.write_text("\n".join(lines) + "\n")


def log_operator_action(run_dir: Path, action: str, **detail) -> None:
    """Append one operator action to logs/operator_actions.jsonl — the file obs flushes to Langfuse as
    `operator:*` events on the next invocation. Previously ONLY the plugin's slash-command markdown
    wrote this file (prompt-enforced), so CLI-native asks left the human edited out of the loop."""
    from datetime import UTC, datetime

    p = run_dir / "logs" / "operator_actions.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "action": action,
           **({"detail": detail} if detail else {})}
    with p.open("a") as f:
        f.write(json.dumps(rec, default=str) + "\n")


def log_skipped(run_dir: Path, note: str, **fields) -> None:
    """Record an ask that did NOT happen. A non-interactive run must not silently swallow the questions:
    without this the questions vanish and nobody knows they were owed an answer."""
    from datetime import UTC, datetime

    p = run_dir / "logs" / "asks_skipped.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as f:
        # ts/action match the operator-actions record shape — one consumer (obs._flush_jsonl_events)
        # reads both files, and shapeless skip records reached Langfuse without a timestamp
        f.write(json.dumps({"ts": datetime.now(UTC).isoformat(timespec="seconds"),
                            "action": "ask_skipped", "note": note, **fields}, default=str) + "\n")


def boundary_checkpoint(stage, ctx) -> CheckpointAction:
    """The per-stage gate question: after a stage passes its deterministic gate, the human decides
    continue / rerun (with guidance) / quit. Returns a CheckpointAction.
    Non-interactive runs never reach this (runner guards on ctx.inputs['interactive'])."""
    import time as _time

    _t0 = _time.monotonic()
    try:
        return _boundary_checkpoint(stage, ctx)
    finally:
        record_wait(ctx.run_dir, f"checkpoint:{stage.name}", _time.monotonic() - _t0)


def _boundary_checkpoint(stage, ctx) -> CheckpointAction:
    print(f"\n─ checkpoint: stage '{stage.name}' passed its gate ─", file=sys.stderr)
    for rel in stage.outputs:
        p = ctx.path(rel)
        print(f"  {rel}  ({p.stat().st_size} bytes)" if p.exists() else f"  {rel}  (missing)", file=sys.stderr)
    try:
        reply = input("[Enter=continue / r=rerun this stage with guidance / q=stop the run]: ").strip().lower()
    except EOFError:
        return CheckpointAction.CONTINUE
    if reply == "q":
        log_operator_action(ctx.run_dir, "checkpoint_quit", stage=stage.name)
        return CheckpointAction.QUIT
    if reply == "r":
        note = input("guidance for the rerun (appended to the stage prompt): ").strip()
        if note:
            from .contracts import guidance_path

            gpath = guidance_path(ctx.run_dir, stage.name)
            gpath.parent.mkdir(parents=True, exist_ok=True)
            with gpath.open("a") as f:
                f.write(f"- {note}\n")
        # the note TEXT rides the operator action (the runner's checkpoint decision carries only the
        # action) — previously the human's actual words never reached the trace
        log_operator_action(ctx.run_dir, "checkpoint_rerun", stage=stage.name, note=note[:500])
        return CheckpointAction.RERUN
    return CheckpointAction.CONTINUE
