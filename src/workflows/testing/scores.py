"""The testing workflow's trace scores (`Workflow.run_scores`), sent when a traced run closes.

Why: the NR-DIGICHEM run (2026-11-02) carried 16 scores, every one a gate pass/fail or attempt outcome,
and every one passed — they cannot tell one suite from another. What does tell them apart was on disk
only: the evaluator's 4.0/5 and compliance FAIL with 11 missing scenarios, and 0 of 77 cases runnable
over HTTP. As scores they chart across features and across prompt/skill changes.

Each family is emitted only when the stage that produced its file ran in this invocation, so a resume
that only re-runs `store_cases` does not re-score the evaluation it never touched.
"""

from __future__ import annotations

from collections import Counter

from .stages import read_json

_EVAL_NUMBERS = ("overall_score", "coverage_score", "quality_score")
_EVAL_COUNTS = ("missing_scenarios", "incorrect_cases", "duplicate_cases")


def run_scores(ctx, ran: set | None = None) -> dict[str, dict]:
    def did(stage: str) -> bool:
        return ran is None or stage in ran

    out: dict[str, dict] = {}
    if did("evaluate"):
        ev = read_json(ctx.path("qa/eval.json"), {}) or {}
        for key in _EVAL_NUMBERS:
            if isinstance(ev.get(key), int | float):
                out[f"eval_{key}"] = {"value": float(ev[key]), "comment": "evaluator, 1-5"}
        if ev.get("compliance_status"):
            out["eval_compliance"] = {"value": str(ev["compliance_status"]).upper(), "data_type": "CATEGORICAL"}
        for key in _EVAL_COUNTS:
            if isinstance(ev.get(key), list):
                out[f"eval_{key}"] = {"value": float(len(ev[key]))}
        for dim, v in (ev.get("dimension_scores") or {}).items():
            if isinstance(v, int | float):
                out[f"eval_dim_{dim}"] = {"value": float(v)}
    if did("generate_cases"):
        cases = read_json(ctx.path("qa/cases.json"), []) or []
        if isinstance(cases, list) and cases:
            by_exec = Counter(str(c.get("execution") or "") for c in cases if isinstance(c, dict))
            out["cases_total"] = {"value": float(len(cases))}
            for kind in ("http", "ui", "manual"):
                out[f"cases_{kind}"] = {"value": float(by_exec.get(kind, 0))}
    if did("prerequisites"):
        doc = read_json(ctx.path("qa/prerequisites.json"), {}) or {}
        if isinstance(doc, dict) and doc:
            for section in ("fixtures", "config", "external_systems", "secrets"):
                if isinstance(doc.get(section), list):
                    out[f"prereq_{section}"] = {"value": float(len(doc[section]))}
    if did("sources"):
        inv = read_json(ctx.path("qa/sources.json"), {}) or {}
        rows = {r.get("label"): r for r in inv.get("sources", []) if isinstance(r, dict)}
        if "API Contract" in rows:
            row = rows["API Contract"]
            out["source_contract_present"] = {
                "value": 1.0 if row.get("status") == "present" else 0.0, "data_type": "BOOLEAN",
                "comment": f"extracted from the {row['extracted_from']}" if row.get("extracted_from") else ""}
    return out
