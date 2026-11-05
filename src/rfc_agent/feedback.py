"""User feedback on a run, recorded where the run's trace already lives: Langfuse.

    python -m rfc_agent.feedback --run-dir <run> --context
    python -m rfc_agent.feedback --run-dir <run> --rating up|down [--verdict DIM=V]... [--issue DIM=V]...
        [--run-speed V] [--run-cost V] [--start-ready V] [--note KEY=TEXT]... [--comment "..."] [--stage S]

The rating becomes scores on the trace that run produced (its id is in <run>/logs/traces.jsonl, written by
observe.py), so every thumb is joined to the run's cost, gates, stages and session in Langfuse and a dashboard
is just a score chart. Scores:

    rfc_feedback          BOOLEAN      1 = 👍, 0 = 👎 — the headline; --comment (what was typed under Other on the
                                       overall question) is its comment
    rfc_<dim>             CATEGORICAL  one verdict per dimension (DIMENSIONS): usable, usable_with_gaps, not_usable
                                       or not_rated (not answered); --note <dim>=... is its comment
    rfc_<dim>_issue       CATEGORICAL  one score per issue picked for a dimension; metadata.likely_stages says
                                       which stages to look at first. `other_see_comment` = a gap typed under
                                       Other that matches none of the listed ones (its comment is the typed text)
    rfc_suspect_stage     CATEGORICAL  one score per stage the picked gaps point at (scope, investigate, draft, review,
                                       summarize): "which stage should be looked at", chartable as a breakdown
    rfc_run_speed         CATEGORICAL  fine, too_slow_to_run, too_slow_to_review, too_slow_to_run_and_review, not_rated;
                                       --note run=... too
    rfc_run_cost          CATEGORICAL  worth_it, too_token_heavy, not_rated; --note run=... too
    rfc_start_ready       CATEGORICAL  could implementation start from the RFC: yes, after_a_few_clarifications/many, no
    rfc_feedback_stage    CATEGORICAL  only with --stage: which stage the rating is about

A 👍 can carry issues too: an approved RFC may still have gaps worth charting.

Every rating also carries a `run_brief` (on the headline score's metadata, and saved as <run>/logs/run_brief.md): a
short plain-text account of the run, its review rounds and what the reviewer said, so whoever reads a rating in
Langfuse sees what it was about without opening the run folder. The review comments of every round stay on disk in
<run>/review/ (R-*.md, R-*.confirm*.md, fixes.md, rfc.round*.diff, status.json).

What a person types under Other is stored against the question it was typed on, next to whatever options they
ticked on that same question: the ticked gaps are the issue scores, the text is the comment of the question's score
(and of its `other_see_comment` issue). A note on an area means that area has a gap, so it is never recorded as
`usable`.

Score ids are deterministic per (trace, name), so rating again overwrites instead of double-counting, and every
dimension is written on every rating so an earlier answer cannot survive. An issue picked before but not now is
overwritten with "none". A run with no recorded trace (tracing was off, or it predates
traces.jsonl) is scored at its Langfuse *session* instead. Every call also appends to <run>/logs/feedback.jsonl,
so nothing is lost when Langfuse is off or down.

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

# dimension -> {issue: stages to look at first}. Score names and values are the dashboard's schema.
DIMENSIONS: dict[str, dict[str, tuple[str, ...]]] = {
    "readable": {  # rfc.readable.md
        "too_long_or_repetitive": ("summarize",),
        "hard_to_navigate": ("summarize",),
        "leaves_out_something_important": ("summarize",),
        "wrong_emphasis": ("summarize",),
    },
    "buildability": {  # could a coding agent build from rfc.md
        "too_thin_to_build_from": ("draft", "investigate"),
        "interface_or_contract_unclear": ("draft",),
        "build_order_or_dependency_missing": ("draft",),
        "citation_does_not_support_claim": ("draft", "investigate"),
    },
    "context_layer": {  # right code, APIs and existing services found
        "wrong_or_stale_code_fact": ("investigate", "scope"),
        "missed_existing_code_or_service": ("investigate",),
        "ignored_existing_pattern": ("investigate", "draft"),
        "wrong_repos_or_pages": ("scope",),
    },
    "coverage": {  # PRD and Figma flows handled
        "missing_prd_flow": ("investigate",),
        "missing_figma_flow_or_state": ("investigate", "scope"),
        "unneeded_flow_included": ("investigate",),
        "marked_covered_but_not_addressed": ("draft", "review"),
    },
    "approach": {  # the chosen option fits our infra and engineering standards
        "ignores_existing_infra": ("draft", "investigate"),
        "ignores_engineering_standards": ("draft",),
        "generic_industry_choice_over_local_fit": ("draft",),
        "tradeoff_not_justified": ("draft",),
        "missing_alternative": ("draft",),
    },
    "rollout_risk": {
        "missing_rollout_rollback": ("draft",),
        "missing_compatibility_or_migration": ("draft",),
        "missing_performance_or_scale": ("draft",),
        "missing_security_or_privacy": ("draft",),
        "missing_monitoring": ("draft",),
    },
}
for _gaps in DIMENSIONS.values():
    _gaps["other_see_comment"] = ()  # typed under Other and matching no listed gap: the stage to check is unknown
NOTE_KEYS = (*DIMENSIONS, "run")  # the questions a typed Other can belong to (the thumb's is --comment)
VERDICTS = ("usable", "usable_with_gaps", "not_usable")
PROBLEM_VERDICTS = VERDICTS[1:]
RUN_SPEED = ("fine", "too_slow_to_run", "too_slow_to_review", "too_slow_to_run_and_review")
RUN_COST = ("worth_it", "too_token_heavy")
START_READY = ("yes", "after_a_few_clarifications", "after_many_clarifications", "no")
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


def _norm_form(
    verdicts: dict[str, str] | None,
    issues: dict[str, list[str]] | None,
    notes: dict[str, str] | None = None,
) -> tuple[dict[str, str], dict[str, list[str]]]:
    """Every dimension gets exactly one verdict; issues survive only on a dimension that has gaps.

    Unknown names and values are dropped. A dimension that was not answered is `not_rated`, and issues picked
    without a verdict imply `usable_with_gaps`. A note typed on a dimension is a gap in itself: with no listed gap
    picked it becomes the `other_see_comment` issue, and the dimension is never `usable`."""
    verdicts, issues, notes = verdicts or {}, issues or {}, notes or {}
    out_v: dict[str, str] = {}
    out_i: dict[str, list[str]] = {}
    for dim, allowed in DIMENSIONS.items():
        picked = [i for i in dict.fromkeys(issues.get(dim) or []) if i in allowed]
        if notes.get(dim) and not picked:
            picked = ["other_see_comment"]
        v = verdicts.get(dim)
        if v not in VERDICTS:
            v = "usable_with_gaps" if picked else "not_rated"
        elif v == "usable" and picked:
            v = "usable_with_gaps"  # "nothing wrong" ticked together with a gap or a note: the gap wins
        out_v[dim] = v
        if v in PROBLEM_VERDICTS and picked:
            out_i[dim] = picked
    return out_v, out_i


def suspect_stages(issues: dict[str, list[str]]) -> list[str]:
    """The stages the picked gaps point at, once each (a gap can name two)."""
    return sorted({st for dim, picked in issues.items() for i in picked for st in DIMENSIONS.get(dim, {}).get(i, ())})


def build_scores(
    *,
    rating: str,
    comment: str | None,
    stage: str | None,
    trace_id: str | None,
    session_id: str,
    run_name: str,
    version: str | None,
    facts: dict | None = None,
    verdicts: dict[str, str] | None = None,
    issues: dict[str, list[str]] | None = None,
    run_speed: str | None = None,
    run_cost: str | None = None,
    start_ready: str | None = None,
    notes: dict[str, str] | None = None,
    cleared_issues: tuple[tuple[str, str], ...] = (),
    cleared_stages: tuple[str, ...] = (),
    brief: str | None = None,
    cleared_notes: tuple[str, ...] = (),
    had_comment: bool = False,
    had_stage: bool = False,
) -> list[dict]:
    """The `create_score` kwargs for one piece of feedback (pure — tests assert on this).

    Every dimension is written, so a new rating replaces the whole previous one. `cleared_issues` are gaps picked
    on an earlier rating of the same target and not picked now: they are overwritten with "none". `notes` maps a
    question (a dimension, or `run`) to what was typed under Other on it; it becomes that question's score comment.
    A score sent again without a comment keeps its old one in Langfuse, so a question that had a note (`cleared_notes`)
    or a thumb that had a comment (`had_comment`) and has none now is sent an empty comment, which clears it."""
    target = {"trace_id": trace_id} if trace_id else {"session_id": session_id}
    key = trace_id or f"session:{session_id}"
    meta = {
        k: v
        for k, v in {"run": run_name, "session_id": session_id, "stage": stage, "plugin_version": version}.items()
        if v
    } | (facts or {})
    verdicts, issues, notes = verdicts or {}, issues or {}, notes or {}

    def score(name: str, value, sid: str, note: str | None = None, clear: bool = False, **extra) -> dict:
        return dict(
            name=name,
            value=value,
            data_type="CATEGORICAL",
            score_id=f"fb:{key}:{sid}",
            metadata=extra.pop("metadata", meta),
            **({"comment": note} if note else {"comment": ""} if clear else {}),
            **extra,
            **target,
        )

    scores = [
        dict(
            name="rfc_feedback",
            value=1.0 if rating == "up" else 0.0,
            data_type="BOOLEAN",
            score_id=f"fb:{key}:overall",
            comment=comment if comment else ("" if had_comment else None),
            metadata=meta | ({"run_brief": brief} if brief else {}),
            **target,
        )
    ]
    for dim, allowed in DIMENSIONS.items():
        scores.append(
            score(f"rfc_{dim}", verdicts.get(dim, "not_rated"), f"dim:{dim}", notes.get(dim), dim in cleared_notes)
        )
        for issue in issues.get(dim, []):
            scores.append(
                score(
                    f"rfc_{dim}_issue",
                    issue,
                    f"issue:{dim}:{issue}",
                    notes.get(dim) if issue == "other_see_comment" else None,
                    metadata=meta | {"likely_stages": list(allowed[issue])},
                )
            )
    for dim, issue in cleared_issues:
        if issue not in issues.get(dim, []):
            scores.append(score(f"rfc_{dim}_issue", "none", f"issue:{dim}:{issue}", clear=True))
    stages = suspect_stages(issues)
    scores += [score("rfc_suspect_stage", st, f"suspect:{st}") for st in stages]
    scores += [score("rfc_suspect_stage", "none", f"suspect:{st}") for st in cleared_stages if st not in stages]
    scores.append(
        score("rfc_run_speed", run_speed or "not_rated", "run_speed", notes.get("run"), "run" in cleared_notes)
    )
    scores.append(score("rfc_run_cost", run_cost or "not_rated", "run_cost", notes.get("run"), "run" in cleared_notes))
    scores.append(score("rfc_start_ready", start_ready or "not_rated", "start_ready"))
    if stage:
        scores.append(score("rfc_feedback_stage", stage, "stage"))
    elif had_stage:  # an earlier rating of this trace named a stage and this one does not
        scores.append(score("rfc_feedback_stage", "none", "stage"))
    return scores


def _plugin_version() -> str | None:
    with contextlib.suppress(Exception):
        p = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("version"))
    return None


def _previous(run_dir: Path, trace_id: str | None, session_id: str):
    """What earlier ratings of this same target (the trace, else the session) left behind: the gaps picked, the stages
    they pointed at, the questions that had a typed note, whether the thumb had a comment, and whether a stage was
    named."""
    issues: set[tuple[str, str]] = set()
    stages: set[str] = set()
    notes: set[str] = set()
    had_comment = had_stage = False
    with contextlib.suppress(OSError):
        for line in (run_dir / "logs" / FEEDBACK_FILE).read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(json.JSONDecodeError, AttributeError, TypeError):
                e = json.loads(line)
                if trace_id and e.get("trace_id") != trace_id:
                    continue
                if not trace_id and (e.get("trace_id") or e.get("session_id") != session_id):
                    continue
                for dim, picked in (e.get("issues") or {}).items():
                    issues |= {(dim, i) for i in picked}
                stages |= set(e.get("stages") or suspect_stages(e.get("issues") or {}))
                notes |= set(e.get("notes") or {})
                had_comment = had_comment or bool(e.get("comment"))
                had_stage = had_stage or bool(e.get("stage"))
    return issues, stages, notes, had_comment, had_stage


def record(
    run_dir: Path,
    rating: str,
    *,
    comment: str | None = None,
    stage: str | None = None,
    verdicts: dict[str, str] | None = None,
    issues: dict[str, list[str]] | None = None,
    run_speed: str | None = None,
    run_cost: str | None = None,
    start_ready: str | None = None,
    notes: dict[str, str] | None = None,
    cfg=None,
) -> dict:
    """Record one piece of feedback; returns what happened (`sent`, `target`, `local`, `note`)."""
    run_dir = run_dir.expanduser().resolve()
    comment = _clean_comment(comment)
    notes = {k: t for k, v in (notes or {}).items() if k in NOTE_KEYS and (t := _clean_comment(v))}
    run_speed = run_speed if run_speed in RUN_SPEED else None
    run_cost = run_cost if run_cost in RUN_COST else None
    start_ready = start_ready if start_ready in START_READY else None
    verdicts, issues = _norm_form(verdicts, issues, notes)
    trace_id = pick_trace(run_dir, stage)
    session_id = _session_id(run_dir)
    prev_issues, prev_stages, prev_notes, had_comment, had_stage = _previous(run_dir, trace_id, session_id)
    facts = run_facts.collect(run_dir, _prd_ref(run_dir))
    brief = run_brief(
        run_dir, facts, rating=rating, verdicts=verdicts, issues=issues, notes=notes, comment=comment,
        run_speed=run_speed, run_cost=run_cost,
    )  # fmt: skip
    with contextlib.suppress(OSError):
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs" / "run_brief.md").write_text(brief + "\n", encoding="utf-8")
    scores = build_scores(
        rating=rating,
        comment=comment,
        stage=stage,
        trace_id=trace_id,
        session_id=session_id,
        run_name=run_dir.name,
        version=_plugin_version(),
        facts=facts,
        brief=brief,
        verdicts=verdicts,
        issues=issues,
        run_speed=run_speed,
        run_cost=run_cost,
        start_ready=start_ready,
        notes=notes,
        cleared_issues=tuple(sorted(prev_issues)),
        cleared_stages=tuple(sorted(prev_stages)),
        cleared_notes=tuple(sorted(prev_notes)),
        had_comment=had_comment,
        had_stage=had_stage,
    )
    entry = {
        "ts": time.time(),
        "rating": rating,
        "verdicts": verdicts,
        "issues": issues,
        "stages": suspect_stages(issues),
        "run_speed": run_speed,
        "run_cost": run_cost,
        "start_ready": start_ready,
        "notes": notes,
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


_BRIEF_CAP = 1800


def run_brief(
    run_dir: Path,
    facts: dict,
    *,
    rating: str | None,
    verdicts: dict[str, str],
    issues: dict[str, list[str]],
    notes: dict[str, str],
    comment: str | None,
    run_speed: str | None,
    run_cost: str | None,
) -> str:
    """The run and what the reviewer said, in a few plain lines (no model involved: it is assembled from files).
    `rating=None` is a run nobody has rated yet: the reviewer line says so and nothing is typed."""

    def line(label: str, *bits) -> str | None:
        bits = [b for b in bits if b]
        return f"{label}: " + "; ".join(bits) if bits else None

    f = facts
    attempts = f.get("attempts") or 0
    run = line(
        "Run",
        run_dir.name,
        f.get("status"),
        f"{attempts} attempts ({f.get('resumes', 0)} resumes)" if attempts > 1 else None,
        f"{f['duration_s'] / 60:.0f} min" if f.get("duration_s") else None,
        f"${f['cost_usd']:.2f}" if f.get("cost_usd") is not None else None,
        f"{f['tokens_total'] / 1e6:.1f}M tokens" if f.get("tokens_total") else None,
        f"{f['langfuse_export_failures']} failed Langfuse uploads (some spans may be missing)"
        if f.get("langfuse_export_failures")
        else None,
    )
    inputs = line(
        "Input",
        f"PRD {f['prd_words']:,} words" if f.get("prd_words") else None,
        f"{f['repos_kept']} of {f['repos_proposed']} proposed repos kept" if f.get("repos_proposed") else None,
        f"{f['figma_frames_in_scope']} Figma frames in scope" if f.get("figma_frames_in_scope") else None,
        "no Figma exported" if f.get("figma_count") == 0 else None,  # a reviewer's "missing Figma flow" starts here
        f"{f['figma_sections_skipped']} Figma sections skipped" if f.get("figma_sections_skipped") else None,
    )
    reqs = line(
        "RFC",
        f"{f['flows']} flows" if f.get("flows") else None,
        f"{f['reqs']} requirements ("
        + ", ".join(f"{f[k]} {k[4:]}" for k in ("req_covered", "req_assumes", "req_deferred", "req_open") if f.get(k))
        + ")"
        if f.get("reqs")
        else None,
        f"{f['open_questions']} open questions" if f.get("open_questions") else None,
        f"{f['alternatives']} options compared" if f.get("alternatives") else None,
    )
    sev = ", ".join(
        f"{f[k]} {name}"
        for k, name in (("review_blockers", "blocker"), ("review_majors", "major"), ("review_minors", "minor"))
        if f.get(k)
    )
    review = line(
        "Review",
        f"{f['review_findings']} findings" + (f" ({sev})" if sev else "") if f.get("review_findings") else None,
        f"{f['review_fixed']} fixed" if f.get("review_fixed") else None,
        f"{f['review_ignored']} left unfixed by choice" if f.get("review_ignored") else None,
        f"{f['review_unresolved']} unresolved" if "review_unresolved" in f and f.get("review_findings") else None,
        f"{f['revise_rounds']} revise round(s), {f.get('revise_lines_changed', 0)} lines changed"
        if f.get("revise_rounds")
        else None,
    )
    gaps = [f"{d} ({', '.join(i)})" for d, i in issues.items()]
    fine = [d for d, v in verdicts.items() if v == "usable"]
    said = "Reviewer: not rated yet" if rating is None else line(
        "Reviewer",
        "👍" if rating == "up" else "👎",
        ("gaps in " + "; ".join(gaps)) if gaps else None,
        ("fine: " + ", ".join(fine)) if fine else None,
        ("look at: " + ", ".join(suspect_stages(issues))) if suspect_stages(issues) else None,
        f"speed {run_speed}" if run_speed else None,
        f"cost {run_cost}" if run_cost else None,
    )
    typed = "; ".join([f'{k}: "{v}"' for k, v in notes.items()] + ([f'overall: "{comment}"'] if comment else []))
    files = "review/review.md, review/fixes.md, review/R-*.confirm*.md, review/rfc.round*.diff, out/rfc.md"
    text = "\n".join(
        x for x in (run, inputs, reqs, review, said, f"Typed: {typed}" if typed else None, f"Files: {files}") if x
    )
    return text if len(text) <= _BRIEF_CAP else text[: _BRIEF_CAP - 1] + "…"


def run_summary(run_dir: Path, facts: dict) -> str:
    """What happened in a run, for the end of the run, the trace's Output pane and logs/run_summary.md."""
    return run_brief(
        run_dir, facts, rating=None, verdicts={}, issues={}, notes={}, comment=None, run_speed=None, run_cost=None
    )


def summary_line(facts: dict) -> str:
    """One line of measured numbers for the rating prompt, so a speed or cost answer is anchored."""
    bits = []
    if facts.get("duration_s"):
        wait = facts.get("operator_wait_s")
        waiting = f" ({wait / 60:.0f} min waiting on a person)" if wait else ""
        bits.append(f"{facts['duration_s'] / 60:.0f} min{waiting}")
    if facts.get("cost_usd") is not None:
        bits.append(f"${facts['cost_usd']:.2f}")
    if facts.get("tokens_total"):
        bits.append(f"{facts['tokens_total'] / 1e6:.1f}M tokens")
    if facts.get("prd_words"):
        figma = f", {facts['figma_frames_in_scope']} Figma frames" if facts.get("figma_frames_in_scope") else ""
        bits.append(f"PRD {facts['prd_words']:,} words{figma}")
    if facts.get("reqs"):
        keys = ("req_covered", "req_assumes", "req_deferred", "req_open")
        got = ", ".join(f"{facts[k]} {k[4:]}" for k in keys if facts.get(k))
        bits.append(f"{facts['reqs']} requirements" + (f" ({got})" if got else ""))
    return " · ".join(bits)


def context(run_dir: Path) -> dict:
    """What the rating prompt needs: the run's measured numbers in one line."""
    run_dir = run_dir.expanduser().resolve()
    return {"line": summary_line(run_facts.collect(run_dir, _prd_ref(run_dir)))}


def _pair(allowed: dict[str, tuple[str, ...]]):
    """argparse type for DIM=VALUE where VALUE must belong to DIM."""

    def parse(text: str) -> tuple[str, str]:
        dim, _, value = text.partition("=")
        if dim not in DIMENSIONS or value not in allowed.get(dim, ()):
            raise argparse.ArgumentTypeError(f"{text!r}: want DIM=VALUE with DIM in {', '.join(DIMENSIONS)}")
        return dim, value

    return parse


def _note(text: str) -> tuple[str, str]:
    """argparse type for KEY=TEXT: what was typed under Other on the question KEY (a dimension, or `run`)."""
    key, _, value = text.partition("=")
    if key not in NOTE_KEYS or not value.strip():
        raise argparse.ArgumentTypeError(f"{text!r}: want KEY=TEXT with KEY in {', '.join(NOTE_KEYS)}")
    return key, value.strip()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rfc_agent.feedback", description=__doc__.split("\n\n")[0])
    ap.add_argument("--run-dir", required=True, type=Path)
    ap.add_argument("--context", action="store_true", help="print what the rating prompt needs, as JSON, and exit")
    ap.add_argument("--rating", choices=["up", "down"])
    ap.add_argument(
        "--verdict",
        action="append",
        default=[],
        type=_pair({d: VERDICTS for d in DIMENSIONS}),
        metavar="DIM=VALUE",
        help=f"repeatable; VALUE one of {', '.join(VERDICTS)}",
    )
    ap.add_argument(
        "--issue",
        action="append",
        default=[],
        type=_pair({d: tuple(i) for d, i in DIMENSIONS.items()}),
        metavar="DIM=ISSUE",
        help="repeatable; kept only on a dimension whose verdict has gaps",
    )
    ap.add_argument("--run-speed", choices=RUN_SPEED)
    ap.add_argument("--run-cost", choices=RUN_COST)
    ap.add_argument("--start-ready", choices=START_READY, help="could implementation start from this RFC")
    ap.add_argument(
        "--note",
        action="append",
        default=[],
        type=_note,
        metavar="KEY=TEXT",
        help="what was typed under Other on one question (a dimension, or run); repeat per question",
    )
    ap.add_argument("--comment", default=None, help="what was typed under Other on the overall question")
    ap.add_argument(
        "--stage", default=None, help="the rating is about one stage (scope, investigate, draft, review, revise)"
    )
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)
    if not args.run_dir.expanduser().is_dir():
        print(f"no such run folder: {args.run_dir}", file=sys.stderr)
        return 2
    if args.context:
        print(json.dumps(context(args.run_dir)))
        return 0
    if not args.rating:
        print("--rating is required (or pass --context)", file=sys.stderr)
        return 2
    issues: dict[str, list[str]] = {}
    for dim, issue in args.issue:
        issues.setdefault(dim, []).append(issue)
    res = record(
        args.run_dir,
        args.rating,
        comment=args.comment,
        stage=args.stage,
        verdicts=dict(args.verdict),
        issues=issues,
        run_speed=args.run_speed,
        run_cost=args.run_cost,
        start_ready=args.start_ready,
        notes=dict(args.note),
    )
    if res["sent"]:
        print(f"feedback recorded on the run's Langfuse {res['target']}")
    else:
        print(f"feedback saved locally (not sent to Langfuse: {res['note']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
