"""Flat, JSON-safe facts about a run folder — which PRD, which repos, how big the RFC, how the review ended.

Stamped on the `rfc-run` observation when a traced run closes (observe._close_root) and on every feedback score
(feedback.py), so a rating can be filtered by what the run was about. Read from files the pipeline already
wrote; every field is optional and a missing or unreadable file just drops that field. Never raises.
"""

from __future__ import annotations

import contextlib
import json
from pathlib import Path


def _json(path: Path):
    with contextlib.suppress(OSError, json.JSONDecodeError):
        return json.loads(path.read_text(encoding="utf-8"))
    return None


def _names(items, key: str) -> list[str]:
    return [str(i[key]) for i in items if isinstance(i, dict) and i.get(key)] if isinstance(items, list) else []


def collect(run_dir: Path, prd_ref: str | None = None) -> dict:
    """`prd_ref` is the original --prd argument (scope.json only keeps the local copy's path)."""
    out: dict = {}
    with contextlib.suppress(Exception):
        scope = _json(run_dir / "scope.json") or {}
        out["prd"] = prd_ref or scope.get("prd")
        out["jira"] = scope.get("jira")
        out["repos"] = _names(scope.get("repos"), "repo") or None
        figma = scope.get("figma")
        out["figma_count"] = len(figma) if isinstance(figma, list) else None
        out["context_layer_ok"] = (scope.get("context_layer") or {}).get("ok")
        with contextlib.suppress(OSError):
            out["rfc_chars"] = len((run_dir / "out" / "rfc.md").read_text(encoding="utf-8"))
        status = _json(run_dir / "review" / "status.json")
        if isinstance(status, dict):
            recs = [r for r in status.values() if isinstance(r, dict)]
            out["review_findings"] = len(recs)
            out["review_unresolved"] = sum(1 for r in recs if r.get("status") in ("unresolved", "not_reviewed"))
        run = _json(run_dir / "logs" / "run.json") or {}
        out["cost_usd"] = run.get("cost_usd_total")
        out["duration_s"] = sum(s.get("seconds") or 0 for s in run.get("stages") or [] if isinstance(s, dict)) or None
    return {k: v for k, v in out.items() if v is not None}
