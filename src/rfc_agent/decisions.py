"""First-class decision records — Saurav (oracle-scan review, 2026-07-23): "wherever a decision is
being taken, I need a signal output there — record what decisions were taken and why."

Every harness decision lands in TWO sinks: logs/decisions.jsonl in the run dir (the auditable artifact,
one JSON object per line) and a Langfuse `decision:<name>` event (the aggregatable signal). Recording
never fails a run.
"""

from __future__ import annotations

import contextlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import RunContext


def record(ctx: RunContext, stage: str, decision: str, why: str, attempt: int | None = None,
           obs_parent=None, **details) -> None:
    """`obs_parent` (a live observation handle) routes the Langfuse event through the handle instead
    of the ambient OTEL context — REQUIRED from worker threads (fan-out pumps), where ambient is
    empty and the event would otherwise be dropped (the 0.22.66 tool-span class, unfixed for
    decisions until 0.24.2). It never lands in decisions.jsonl."""
    entry = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds"),
        "stage": stage,
        "decision": decision,
        "why": why,
        **({"attempt": attempt} if attempt is not None else {}),
        **details,
    }
    with contextlib.suppress(Exception):
        p = ctx.path("logs/decisions.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(entry, default=str) + "\n")
    from . import obs

    obs.decision_event(ctx, stage, decision, why, details, attempt=attempt, parent=obs_parent)
