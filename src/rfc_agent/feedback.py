"""User feedback on a run, recorded where the run's trace already lives: Langfuse.

    python -m rfc_agent.feedback --run-dir <run> --rating up|down [--reason X]... [--comment "..."] [--stage S]

The rating becomes scores on the trace that run produced (its id is in <run>/logs/traces.jsonl, written by
observe.py), so every thumb is joined to the run's cost, gates, stages and session in Langfuse and a dashboard
is just a score chart. Scores:

    rfc_feedback          BOOLEAN      1 = 👍, 0 = 👎 — the headline; the comment rides on it
    rfc_feedback_reason   CATEGORICAL  one score per reason (👎 only), value = the reason
    rfc_feedback_stage    CATEGORICAL  only with --stage: which stage the rating is about

Score ids are deterministic per (trace, name), so rating again overwrites instead of double-counting. A run
with no recorded trace (tracing was off, or it predates traces.jsonl) is scored at its Langfuse *session*
instead. Every call also appends to <run>/logs/feedback.jsonl, so nothing is lost when Langfuse is off or down.

Contract (same as observe.py): feedback never fails the caller. Exit 0 whenever the rating was recorded
anywhere; exit 2 only for bad arguments.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import time
from pathlib import Path

from . import observe, run_facts

FEEDBACK_FILE = "feedback.jsonl"
REASONS = (
    "wrong_repos",  # Scope picked the wrong repos / pages
    "missed_flows",  # a flow or edge the RFC should cover is absent
    "inaccurate",  # claims that don't match the code
    "too_shallow",  # not specific enough to implement from
    "too_long",  # hard to read / padded
    "too_slow",  # took too long
    "too_costly",  # cost too much
    "other",
)
_COMMENT_CAP = 2000


def _read_traces(run_dir: Path) -> list[dict]:
    out: list[dict] = []
    with contextlib.suppress(OSError):
        for line in (run_dir / "logs" / observe.TRACES_FILE).read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                d = json.loads(line)
                if isinstance(d, dict) and d.get("trace_id"):
                    out.append(d)
    return out


def pick_trace(run_dir: Path, stage: str | None = None) -> str | None:
    """The trace to score: for a stage, that stage's latest standalone trace; otherwise the latest whole-run
    (`rfc`) trace, falling back to the latest trace of any kind (a run driven stage by stage)."""
    traces = _read_traces(run_dir)
    if stage:
        mine = [t for t in traces if t.get("entry") == stage]
        if mine:
            return mine[-1]["trace_id"]
    whole = [t for t in traces if t.get("entry") == "rfc"]
    pool = whole or traces
    return pool[-1]["trace_id"] if pool else None


def _prd_ref(run_dir: Path) -> str | None:
    return next((t["prd"] for t in reversed(_read_traces(run_dir)) if t.get("prd")), None)


def _session_id(run_dir: Path) -> str:
    with contextlib.suppress(OSError, json.JSONDecodeError, AttributeError):
        sid = json.loads((run_dir / "identity.json").read_text(encoding="utf-8")).get("session_id")
        if sid:
            return str(sid)
    return run_dir.name


def _clean_comment(text: str | None) -> str | None:
    if not text or not text.strip():
        return None
    from .privacy import scrub_text

    return scrub_text(text.strip()[:_COMMENT_CAP])[0]


def _send(cfg, scores: list[dict]) -> str | None:
    """Push the scores. Returns None on success, else a one-line reason it didn't reach Langfuse."""
    if not observe.settings(cfg)["enabled"]:
        return "Langfuse tracing is off"
    if not all(observe._keys(cfg)):
        return "Langfuse keys are not configured"
    try:
        client = observe._make_client(cfg)
        for s in scores:
            client.create_score(**s)
        client.flush()
        with contextlib.suppress(Exception):
            client.shutdown()
    except Exception as exc:  # noqa: BLE001 — Langfuse trouble must not lose or fail the rating
        return f"{type(exc).__name__}: {exc}"[:300]
    return None


def build_scores(
    *,
    rating: str,
    reasons: list[str],
    comment: str | None,
    stage: str | None,
    trace_id: str | None,
    session_id: str,
    run_name: str,
    version: str | None,
    facts: dict | None = None,
) -> list[dict]:
    """The `create_score` kwargs for one piece of feedback (pure — tests assert on this)."""
    target = {"trace_id": trace_id} if trace_id else {"session_id": session_id}
    key = trace_id or f"session:{session_id}"
    meta = {
        k: v
        for k, v in {"run": run_name, "session_id": session_id, "stage": stage, "plugin_version": version}.items()
        if v
    } | (facts or {})
    scores = [
        dict(
            name="rfc_feedback",
            value=1.0 if rating == "up" else 0.0,
            data_type="BOOLEAN",
            score_id=f"fb:{key}:overall",
            comment=comment,
            metadata=meta,
            **target,
        )
    ]
    for r in reasons if rating == "down" else []:
        scores.append(
            dict(
                name="rfc_feedback_reason",
                value=r,
                data_type="CATEGORICAL",
                score_id=f"fb:{key}:reason:{r}",
                metadata=meta,
                **target,
            )
        )
    if stage:
        scores.append(
            dict(
                name="rfc_feedback_stage",
                value=stage,
                data_type="CATEGORICAL",
                score_id=f"fb:{key}:stage",
                metadata=meta,
                **target,
            )
        )
    return scores


def _plugin_version() -> str | None:
    with contextlib.suppress(Exception):
        p = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("version"))
    return None


def record(
    run_dir: Path,
    rating: str,
    *,
    reasons: list[str] | None = None,
    comment: str | None = None,
    stage: str | None = None,
    cfg=None,
) -> dict:
    """Record one piece of feedback; returns what happened (`sent`, `target`, `local`, `note`)."""
    run_dir = run_dir.expanduser().resolve()
    reasons = [r for r in dict.fromkeys(reasons or []) if r in REASONS]
    comment = _clean_comment(comment)
    trace_id = pick_trace(run_dir, stage)
    session_id = _session_id(run_dir)
    scores = build_scores(
        rating=rating,
        reasons=reasons,
        comment=comment,
        stage=stage,
        trace_id=trace_id,
        session_id=session_id,
        run_name=run_dir.name,
        version=_plugin_version(),
        facts=run_facts.collect(run_dir, _prd_ref(run_dir)),
    )
    entry = {
        "ts": time.time(),
        "rating": rating,
        "reasons": reasons,
        "comment": comment,
        "stage": stage,
        "trace_id": trace_id,
        "session_id": session_id,
    }
    local = False
    with contextlib.suppress(OSError):
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        with (run_dir / "logs" / FEEDBACK_FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        local = True
    if cfg is None:
        cfg = observe._load_cfg()
    note = _send(cfg, scores)
    return {"sent": note is None, "target": "trace" if trace_id else "session", "local": local, "note": note}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rfc_agent.feedback", description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--rating", required=True, choices=["up", "down"])
    ap.add_argument(
        "--reason", action="append", default=[], choices=REASONS, help="repeatable; used with --rating down"
    )
    ap.add_argument("--comment", default=None, help="one free-text line")
    ap.add_argument(
        "--stage", default=None, help="the rating is about one stage (scope, investigate, draft, review, revise)"
    )
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.run_dir.expanduser().is_dir():
        print(f"no such run folder: {args.run_dir}", file=sys.stderr)
        return 2
    res = record(args.run_dir, args.rating, reasons=args.reason, comment=args.comment, stage=args.stage)
    if res["sent"]:
        print(f"feedback recorded on the run's Langfuse {res['target']}")
    else:
        print(f"feedback saved locally (not sent to Langfuse: {res['note']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
