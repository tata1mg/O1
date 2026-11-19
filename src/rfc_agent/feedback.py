"""User feedback on a run, recorded where the run's trace already lives: Langfuse.

    python -m rfc_agent.feedback --run-dir <run> --context
    python -m rfc_agent.feedback --run-dir <run> --rating up|down [--verdict DIM=V]... [--issue DIM=V]...
        [--run-speed V] [--run-cost V] [--start-ready V] [--minutes-saved N] [--minutes-spent N]
        [--minutes-skipped] [--note KEY=TEXT]... [--comment "..."] [--stage S] [--rerate] [--preview]
    python -m rfc_agent.feedback --run-dir <run> [--run-end stopped|failed|finished] [--stop-reason V]
        [--failure-check yes|no] [--failure-reported V] --rerun yes|no [--note stop|failure=TEXT]

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
    rfc_minutes_status    CATEGORICAL  given (both minutes typed), partial (one typed), skipped (--minutes-skipped),
                                       not_rated (no answer passed). Written on every rating, so a re-rate replaces it.
    rfc_minutes_saved     NUMERIC      minutes the RFC saved versus writing it yourself (--minutes-saved; only if given)
    rfc_minutes_spent     NUMERIC      minutes the person spent reviewing and correcting the RFC after it was generated
                                       (--minutes-spent; only if given). The run itself and pauses are already on
                                       the trace.
    rfc_feedback_stage    CATEGORICAL  only with --stage: which stage the rating is about

A 👍 can carry issues too: an approved RFC may still have gaps worth charting.

A run that stopped or failed has no RFC to rate. `--context` says how the latest attempt ended (`end`: finished,
stopped, failed or unknown), taken from the run's own record (run_end.py), so the form opens on the matching path.
A stopped or failed attempt is rated with these scores, each written only when asked:

    rfc_run_stop_reason          CATEGORICAL  stopped: the main reason (STOP_REASON); --note stop=... is its comment
    rfc_failure_cause_confirmed  CATEGORICAL  failed: yes/no, was the recorded cause what happened; --note failure=...
    rfc_failure_cause_reported   CATEGORICAL  failed with no recorded cause (killed): what the person says
    rfc_rerun                    CATEGORICAL  yes/no, would you run it again
    rfc_run_end                  CATEGORICAL  the person's answer, only when the run has no end record (killed)

Each attempt of a run is its own trace, so feedback belongs to the attempt it was given on: a run that was stopped,
rated, resumed and finished keeps the stopped attempt's feedback and gets a separate rating for the finished one.
Rating the same attempt again replaces only that attempt's feedback. Every score carries `attempt`, `attempts` (how
many there were) and `run_state` (how the run stood when it was rated) in its metadata.

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

from . import feedback_summary, observe, run_facts
from .run_end import state as run_end_state

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
        "poor_approach": ("draft", "investigate"),  # wrong service or place, or worse given how our systems work
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
# the questions a typed Other can belong to (the thumb's is --comment)
NOTE_KEYS = (*DIMENSIONS, "run", "stop", "failure")
VERDICTS = ("usable", "usable_with_gaps", "not_usable")
PROBLEM_VERDICTS = VERDICTS[1:]
RUN_SPEED = ("fine", "too_slow_to_run", "too_slow_to_review", "too_slow_to_run_and_review")
RUN_COST = ("worth_it", "too_token_heavy")
START_READY = ("yes", "after_a_few_clarifications", "after_many_clarifications", "no")
RUN_END = ("finished", "stopped", "failed")  # the person's answer, asked only when the run has no end record
STOP_REASON = ("too_slow", "too_costly", "wrong_repos_or_pages", "bad_input", "changed_mind", "other")
FAILURE_CHECK = ("yes", "no")  # was the cause the run recorded what happened
FAILURE_REPORTED = ("usage_or_login_limit", "connector", "connection", "other")  # when no cause was recorded
RERUN = ("yes", "no")
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


def minutes_status(saved: int | None, spent: int | None, skipped: bool = False) -> str:
    """given (both typed), partial (one typed), skipped (the person said skip), else not_rated (no answer passed)."""
    answered = (saved is not None) + (spent is not None)
    if answered == 2:
        return "given"
    if answered == 1:
        return "partial"
    return "skipped" if skipped else "not_rated"


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
    minutes_saved: int | None = None,
    minutes_spent: int | None = None,
    minutes_skipped: bool = False,
    notes: dict[str, str] | None = None,
    cleared_issues: tuple[tuple[str, str], ...] = (),
    cleared_stages: tuple[str, ...] = (),
    brief: str | None = None,
    cleared_notes: tuple[str, ...] = (),
    had_comment: bool = False,
    had_stage: bool = False,
    attempt_meta: dict | None = None,
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
    } | (facts or {}) | (attempt_meta or {})
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
    status = minutes_status(minutes_saved, minutes_spent, minutes_skipped)
    # The status is written on every rating, so a re-rate always replaces it. The numbers are NUMERIC and written only
    # when typed (a score cannot be "not rated"): when the status is not `given` or `partial`, the numbers left on
    # the trace by an earlier rating are stale, and the status is the one to read.
    scores.append(score("rfc_minutes_status", status, "minutes_status"))
    for name, sid, value in (
        ("rfc_minutes_saved", "minutes_saved", minutes_saved),
        ("rfc_minutes_spent", "minutes_spent", minutes_spent),
    ):
        if value is not None:
            scores.append(
                dict(
                    name=name,
                    value=float(value),
                    data_type="NUMERIC",
                    score_id=f"fb:{key}:{sid}",
                    metadata=meta,
                    **target,
                )
            )
    if stage:
        scores.append(score("rfc_feedback_stage", stage, "stage"))
    elif had_stage:  # an earlier rating of this trace named a stage and this one does not
        scores.append(score("rfc_feedback_stage", "none", "stage"))
    return scores


def build_run_end_scores(
    *,
    trace_id: str | None,
    session_id: str,
    run_name: str,
    version: str | None,
    facts: dict | None = None,
    attempt_meta: dict | None = None,
    run_end: str | None = None,
    stop_reason: str | None = None,
    failure_check: str | None = None,
    failure_reported: str | None = None,
    rerun: str | None = None,
    notes: dict[str, str] | None = None,
    cleared_notes: tuple[str, ...] = (),
    brief: str | None = None,
) -> list[dict]:
    """The `create_score` kwargs for feedback on a stopped or failed attempt (pure). Only what was answered is
    written. Ids are per (trace, name): rating the same attempt again replaces its feedback and nothing else."""
    target = {"trace_id": trace_id} if trace_id else {"session_id": session_id}
    key = trace_id or f"session:{session_id}"
    meta = {
        k: v
        for k, v in {"run": run_name, "session_id": session_id, "plugin_version": version}.items()
        if v
    } | (facts or {}) | (attempt_meta or {})
    notes = notes or {}
    scores: list[dict] = []
    for name, sid, value, note_key in (
        ("rfc_run_end", "run_end", run_end, None),
        ("rfc_run_stop_reason", "stop_reason", stop_reason, "stop"),
        ("rfc_failure_cause_confirmed", "failure_confirmed", failure_check, "failure"),
        ("rfc_failure_cause_reported", "failure_reported", failure_reported, "failure"),
        ("rfc_rerun", "rerun", rerun, None),
    ):
        if not value:
            continue
        note = notes.get(note_key) if note_key else None
        scores.append(
            dict(
                name=name,
                value=value,
                data_type="CATEGORICAL",
                score_id=f"fb:{key}:{sid}",
                metadata=meta | ({"run_brief": brief} if brief and not scores else {}),
                **({"comment": note} if note else {"comment": ""} if note_key in cleared_notes else {}),
                **target,
            )
        )
    return scores


def _attempt_meta(run_dir: Path, state: dict) -> dict:
    """Which attempt this feedback is about and how the run stood when it was given."""
    end, stage = state["end"], state.get("stage")
    states = {"finished": "done", "stopped": f"stopped at {stage}", "failed": f"failed at {stage}"}
    run_state = states.get(end, "unknown")
    return {"attempt": state["attempt"], "attempts": state["attempts"], "run_state": run_state}


def _plugin_version() -> str | None:
    with contextlib.suppress(Exception):
        p = Path(__file__).resolve().parents[2] / ".claude-plugin" / "plugin.json"
        return str(json.loads(p.read_text(encoding="utf-8")).get("version"))
    return None


def _local_entries(run_dir: Path, trace_id: str | None, session_id: str) -> list[dict]:
    """The ratings in <run>/logs/feedback.jsonl given on this same target (the trace, else the session), oldest first.
    A trace is one attempt, so these are ratings of one attempt only."""
    out: list[dict] = []
    with contextlib.suppress(OSError):
        for line in (run_dir / "logs" / FEEDBACK_FILE).read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(json.JSONDecodeError, AttributeError, TypeError):
                e = json.loads(line)
                if trace_id and e.get("trace_id") != trace_id:
                    continue
                if not trace_id and (e.get("trace_id") or e.get("session_id") != session_id):
                    continue
                out.append(e)
    return out


def _langfuse_scores(cfg, trace_id: str | None, session_id: str) -> list[dict]:
    """The feedback scores Langfuse holds for this target (read only; [] on any trouble). Used only when the run
    folder has no record of the rating, e.g. its feedback.jsonl was deleted."""
    import base64
    import os
    import urllib.parse
    import urllib.request

    public, secret = observe._keys(cfg)
    host = str(
        cfg.get("LANGFUSE.HOST")
        or os.environ.get("LANGFUSE_BASE_URL")
        or os.environ.get("LANGFUSE_HOST")
        or "https://cloud.langfuse.com"
    ).rstrip("/")
    prefix = f"fb:{trace_id}:" if trace_id else f"fb:session:{session_id}:"
    base = {"limit": 100, "fields": "core,details"} | (
        {"traceId": trace_id} if trace_id else {"sessionId": session_id}
    )
    headers = {"Authorization": "Basic " + base64.b64encode(f"{public}:{secret}".encode()).decode()}
    out: list[dict] = []
    cursor = None
    try:
        for _ in range(5):  # a rating is ~25 scores, the page is 100: more than one page is not expected
            query = urllib.parse.urlencode(base | ({"cursor": cursor} if cursor else {}))
            req = urllib.request.Request(f"{host}/api/public/v3/scores?{query}", headers=headers)  # noqa: S310
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310 — the configured Langfuse host
                page = json.load(resp)
            out += [x for x in page.get("data", []) if str(x.get("id", "")).startswith(prefix)]
            cursor = (page.get("meta") or {}).get("cursor")
            if not cursor:
                break
    except Exception:  # noqa: BLE001 — Langfuse trouble means a blank form, never a failed rating
        return []
    return out


def _entry_from_scores(scores: list[dict]) -> dict | None:
    """A feedback.jsonl-shaped entry rebuilt from Langfuse scores, or None when there is nothing to rebuild."""
    e: dict = {"verdicts": {}, "issues": {}, "notes": {}}
    saved = spent = status = None
    for sc in scores:
        name, val = sc.get("name"), sc.get("value")
        note = (sc.get("comment") or "").strip()
        if name == "rfc_feedback":
            e["rating"] = "up" if val in (True, 1, "True", "true") else "down"
            e["comment"] = note or None
        elif name == "rfc_run_speed" and val in RUN_SPEED:
            e["run_speed"] = val
            if note:
                e["notes"]["run"] = note
        elif name == "rfc_run_cost" and val in RUN_COST:
            e["run_cost"] = val
            if note:
                e["notes"].setdefault("run", note)
        elif name == "rfc_start_ready" and val in START_READY:
            e["start_ready"] = val
        elif name == "rfc_minutes_status":
            status = val
        elif name == "rfc_minutes_saved" and isinstance(val, (int, float)):
            saved = int(val)
        elif name == "rfc_minutes_spent" and isinstance(val, (int, float)):
            spent = int(val)
        elif name == "rfc_feedback_stage" and val not in (None, "none"):
            e["stage"] = val
        elif name == "rfc_run_stop_reason" and val in STOP_REASON:
            e["stop_reason"] = val
            if note:
                e["notes"]["stop"] = note
        elif name == "rfc_failure_cause_confirmed" and val in FAILURE_CHECK:
            e["failure_check"] = val
            if note:
                e["notes"]["failure"] = note
        elif name == "rfc_failure_cause_reported" and val in FAILURE_REPORTED:
            e["failure_reported"] = val
        elif name == "rfc_rerun" and val in RERUN:
            e["rerun"] = val
        elif name == "rfc_run_end" and val in RUN_END:
            e["run_end"] = val
        else:
            for dim, allowed in DIMENSIONS.items():
                if name == f"rfc_{dim}" and val in VERDICTS:
                    e["verdicts"][dim] = val
                    if note:
                        e["notes"][dim] = note
                elif name == f"rfc_{dim}_issue" and val in allowed:
                    e["issues"].setdefault(dim, []).append(val)
    if status in ("given", "partial"):  # a number left by an earlier rating is stale under any other status
        e["minutes_saved"], e["minutes_spent"] = saved, spent
    e["minutes_status"] = status
    e["stages"] = suspect_stages(e["issues"])
    answered = ("rating", "stop_reason", "failure_check", "failure_reported", "rerun", "run_end")
    return e if any(k in e for k in answered) else None


def _langfuse_entries(cfg, trace_id: str | None, session_id: str) -> list[dict]:
    """The earlier rating of this target as Langfuse has it, as a one-entry list ([] when off, down or unrated)."""
    try:
        cfg = cfg if cfg is not None else observe._load_cfg()
        if not observe.settings(cfg)["enabled"] or not all(observe._keys(cfg)):
            return []
        entry = _entry_from_scores(_langfuse_scores(cfg, trace_id, session_id))
    except Exception:  # noqa: BLE001
        return []
    return [entry | {"source": "langfuse"}] if entry else []


def _earlier(run_dir: Path, trace_id: str | None, session_id: str, cfg=None) -> list[dict]:
    """Earlier ratings of this attempt, oldest first: the run folder's, else (only then) Langfuse's."""
    local = _local_entries(run_dir, trace_id, session_id)
    return [{**e, "source": "run folder"} for e in local] or _langfuse_entries(cfg, trace_id, session_id)


def _previous(run_dir: Path, trace_id: str | None, session_id: str, cfg=None, entries: list[dict] | None = None):
    """What earlier ratings of this same target (the trace, else the session) left behind: the gaps picked, the stages
    they pointed at, the questions that had a typed note, whether the thumb had a comment, and whether a stage was
    named. Read from the run folder; only when it has nothing for this target, from Langfuse."""
    issues: set[tuple[str, str]] = set()
    stages: set[str] = set()
    notes: set[str] = set()
    had_comment = had_stage = False
    for e in entries if entries is not None else _earlier(run_dir, trace_id, session_id, cfg):
        with contextlib.suppress(AttributeError, TypeError):
            for dim, picked in (e.get("issues") or {}).items():
                issues |= {(dim, i) for i in picked}
            stages |= set(e.get("stages") or suspect_stages(e.get("issues") or {}))
            notes |= set(e.get("notes") or {})
            had_comment = had_comment or bool(e.get("comment"))
            had_stage = had_stage or bool(e.get("stage"))
    return issues, stages, notes, had_comment, had_stage


_PREFILL_KEYS = (
    "rating", "comment", "stage", "verdicts", "issues", "notes", "run_speed", "run_cost", "start_ready",
    "minutes_saved", "minutes_spent", "run_end", "stop_reason", "failure_check", "failure_reported", "rerun",
)  # fmt: skip


def _shape_previous(entry: dict) -> dict:
    """An earlier rating as the form and `--rerate` use it: only the answers, in one shape."""
    out = {k: entry[k] for k in _PREFILL_KEYS if entry.get(k) not in (None, "", {}, [])}
    out["verdicts"] = {d: v for d, v in (entry.get("verdicts") or {}).items() if v in VERDICTS}
    out["minutes_status"] = entry.get("minutes_status")
    if entry.get("minutes_status") not in ("given", "partial"):  # a skipped or unasked time question leaves no number
        out.pop("minutes_saved", None)
        out.pop("minutes_spent", None)
    out["source"] = entry.get("source", "run folder")
    return out


def previous_rating(run_dir: Path, cfg=None) -> dict | None:
    """The latest earlier rating of the attempt now being rated, for the form to show prefilled (None when this
    attempt was not rated yet). Every attempt is its own trace, so another attempt's feedback is never offered.
    Read from <run>/logs/feedback.jsonl; only when that has nothing for this attempt, from Langfuse, and never mixed:
    the form shows one earlier rating, whole."""
    run_dir = run_dir.expanduser().resolve()
    earlier = _earlier(run_dir, pick_trace(run_dir), _session_id(run_dir), cfg)
    return _shape_previous(earlier[-1]) if earlier else None


def record(
    run_dir: Path,
    rating: str | None,
    *,
    comment: str | None = None,
    stage: str | None = None,
    verdicts: dict[str, str] | None = None,
    issues: dict[str, list[str]] | None = None,
    run_speed: str | None = None,
    run_cost: str | None = None,
    start_ready: str | None = None,
    minutes_saved: int | None = None,
    minutes_spent: int | None = None,
    minutes_skipped: bool = False,
    run_end: str | None = None,
    stop_reason: str | None = None,
    failure_check: str | None = None,
    failure_reported: str | None = None,
    rerun: str | None = None,
    notes: dict[str, str] | None = None,
    rerate: bool = False,
    preview: bool = False,
    cfg=None,
) -> dict:
    """Record one piece of feedback; returns what happened (`sent`, `target`, `local`, `note`, `summary`).

    `rating` None is feedback on a stopped or failed attempt (the run-end answers): no RFC scores are written.
    `rerate`: every answer not passed is the earlier rating's of this same attempt (a re-rating sends the whole rating
    again; an unanswered question keeps its earlier answer). `preview`: build the summary only, write and send nothing.
    """
    run_dir = run_dir.expanduser().resolve()
    trace_id = pick_trace(run_dir, stage)
    session_id = _session_id(run_dir)
    earlier = _earlier(run_dir, trace_id, session_id, cfg)
    prev = _shape_previous(earlier[-1]) if earlier else None
    if rerate and prev:
        rating = rating or prev.get("rating")
        comment = comment if comment is not None else prev.get("comment")
        notes = {**(prev.get("notes") or {}), **(notes or {})}
        verdicts, issues = dict(verdicts or {}), {k: list(v) for k, v in (issues or {}).items()}
        for dim in DIMENSIONS:
            if dim not in verdicts and dim not in issues:
                if dim in prev["verdicts"]:
                    verdicts[dim] = prev["verdicts"][dim]
                if prev.get("issues", {}).get(dim):
                    issues[dim] = list(prev["issues"][dim])
        run_speed, run_cost = run_speed or prev.get("run_speed"), run_cost or prev.get("run_cost")
        start_ready = start_ready or prev.get("start_ready")
        if minutes_saved is None and minutes_spent is None and not minutes_skipped:
            minutes_saved, minutes_spent = prev.get("minutes_saved"), prev.get("minutes_spent")
        run_end, stop_reason = run_end or prev.get("run_end"), stop_reason or prev.get("stop_reason")
        failure_check = failure_check or prev.get("failure_check")
        failure_reported = failure_reported or prev.get("failure_reported")
        rerun = rerun or prev.get("rerun")
    comment = _clean_comment(comment)
    notes = {k: t for k, v in (notes or {}).items() if k in NOTE_KEYS and (t := _clean_comment(v))}
    run_speed = run_speed if run_speed in RUN_SPEED else None
    run_cost = run_cost if run_cost in RUN_COST else None
    start_ready = start_ready if start_ready in START_READY else None
    if minutes_skipped and (minutes_saved is not None or minutes_spent is not None):
        raise ValueError("minutes_skipped cannot be combined with minutes_saved or minutes_spent")
    minutes_saved = minutes_saved if minutes_saved is not None and minutes_saved >= 0 else None
    minutes_spent = minutes_spent if minutes_spent is not None and minutes_spent >= 0 else None
    status = minutes_status(minutes_saved, minutes_spent, minutes_skipped)
    run_end = run_end if run_end in RUN_END else None
    stop_reason = stop_reason if stop_reason in STOP_REASON else None
    failure_check = failure_check if failure_check in FAILURE_CHECK else None
    failure_reported = failure_reported if failure_reported in FAILURE_REPORTED else None
    rerun = rerun if rerun in RERUN else None
    verdicts, issues = _norm_form(verdicts, issues, notes)
    state = run_end_state(run_dir)
    attempt_meta = _attempt_meta(run_dir, state)
    view = dict(
        rating=rating, verdicts=verdicts, issues=issues, notes=notes, comment=comment, run_speed=run_speed,
        run_cost=run_cost, minutes_saved=minutes_saved, minutes_spent=minutes_spent, minutes_status=status,
        run_end=run_end, stop_reason=stop_reason, failure_check=failure_check, failure_reported=failure_reported,
        rerun=rerun,
    )  # fmt: skip
    has_cause = bool(state.get("cause"))
    shown = "finished" if attempt_meta["run_state"] == "done" else attempt_meta["run_state"]
    summary = (
        ("Check before submitting" if preview else "Feedback recorded")
        + f" · attempt {attempt_meta['attempt']} ({shown})\n\n"
        + feedback_summary.table(
            feedback_summary.rows(view, has_cause), feedback_summary.rows(prev, has_cause) if prev else None
        )
    )
    if preview:
        return {"sent": False, "target": None, "local": False, "note": None, "summary": summary, "preview": True}
    prev_issues, prev_stages, prev_notes, had_comment, had_stage = _previous(
        run_dir, trace_id, session_id, cfg, entries=earlier
    )
    facts = run_facts.collect(run_dir, _prd_ref(run_dir))
    brief = run_brief(
        run_dir, facts, rating=rating, verdicts=verdicts, issues=issues, notes=notes, comment=comment,
        run_speed=run_speed, run_cost=run_cost,
    )  # fmt: skip
    with contextlib.suppress(OSError):
        (run_dir / "logs").mkdir(parents=True, exist_ok=True)
        (run_dir / "logs" / "run_brief.md").write_text(brief + "\n", encoding="utf-8")
    scores = [] if rating not in ("up", "down") else build_scores(
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
        minutes_saved=minutes_saved,
        minutes_spent=minutes_spent,
        minutes_skipped=minutes_skipped,
        notes=notes,
        cleared_issues=tuple(sorted(prev_issues)),
        cleared_stages=tuple(sorted(prev_stages)),
        cleared_notes=tuple(sorted(prev_notes)),
        had_comment=had_comment,
        had_stage=had_stage,
        attempt_meta=attempt_meta,
    )  # fmt: skip
    scores += build_run_end_scores(
        trace_id=trace_id, session_id=session_id, run_name=run_dir.name, version=_plugin_version(), facts=facts,
        attempt_meta=attempt_meta, run_end=run_end, stop_reason=stop_reason, failure_check=failure_check,
        failure_reported=failure_reported, rerun=rerun, notes=notes, cleared_notes=tuple(sorted(prev_notes)),
        brief=brief if rating not in ("up", "down") else None,
    )  # fmt: skip
    entry = {
        "ts": time.time(),
        "rating": rating,
        "run_end": run_end,
        "stop_reason": stop_reason,
        "failure_check": failure_check,
        "failure_reported": failure_reported,
        "rerun": rerun,
        "attempt": attempt_meta["attempt"],
        "attempts": attempt_meta["attempts"],
        "run_state": attempt_meta["run_state"],
        "verdicts": verdicts,
        "issues": issues,
        "stages": suspect_stages(issues),
        "run_speed": run_speed,
        "run_cost": run_cost,
        "start_ready": start_ready,
        "minutes_saved": minutes_saved,
        "minutes_spent": minutes_spent,
        "minutes_status": status,
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
    return {
        "sent": note is None, "target": "trace" if trace_id else "session", "local": local, "note": note,
        "summary": summary,
    }  # fmt: skip


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


_END_WORD = {"finished": "Finished", "stopped": "Stopped", "failed": "Failed", "unknown": "No end record"}


def recorded_line(state: dict, line: str) -> str:
    """The run's own account of how the latest attempt ended, shown at the top of the form."""
    end, stage = state["end"], state.get("stage")
    bits = [_END_WORD[end] + (f" at {stage}" if stage else "")]
    if end == "stopped":
        bits.append("you stopped it (Ctrl-C or a stop)")
    elif end == "failed" and state.get("cause"):
        bits.append(str(state["cause"]).replace("_", " "))
    elif end == "unknown":
        bits.append("the process was killed or is still running")
    if line:
        bits.append(line)
    return " · ".join(bits)


def context(run_dir: Path, cfg=None) -> dict:
    """What the rating prompt needs: the run's measured numbers in one line, and how the latest attempt ended.

    `end` is finished, stopped, failed or unknown (no end record): the form opens on that path and asks "how did it
    end?" only for unknown. `cause`/`excerpt` are the recorded failure cause. `earlier` lists the attempts before this
    one that did not finish; they keep their own feedback. `previous` is the earlier rating of this same attempt
    (None if it has none), for the form to show prefilled."""
    run_dir = run_dir.expanduser().resolve()
    line = summary_line(run_facts.collect(run_dir, _prd_ref(run_dir)))
    state = run_end_state(run_dir)
    return {"line": line, "recorded": recorded_line(state, line), **state, "previous": previous_rating(run_dir, cfg)}


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
    ap.add_argument("--run-end", choices=RUN_END,
                    help="how the run ended, by the person; only when the run has no end record")
    ap.add_argument("--stop-reason", choices=STOP_REASON, help="a stopped run: the main reason you stopped it")
    ap.add_argument("--failure-check", choices=FAILURE_CHECK, help="a failed run: was the recorded cause what happened")
    ap.add_argument("--failure-reported", choices=FAILURE_REPORTED,
                    help="a failed run with no recorded cause: what made it fail")
    ap.add_argument("--rerun", choices=RERUN, help="a stopped or failed run: would you run it again")
    ap.add_argument("--run-speed", choices=RUN_SPEED)
    ap.add_argument("--run-cost", choices=RUN_COST)
    ap.add_argument("--start-ready", choices=START_READY, help="could implementation start from this RFC")
    ap.add_argument("--minutes-saved", type=int, default=None, metavar="N",
                    help="minutes this RFC saved you versus writing it yourself (a whole number, 0 or more)")
    ap.add_argument("--minutes-spent", type=int, default=None, metavar="N",
                    help="minutes you spent reviewing and correcting the RFC after it was generated")
    ap.add_argument("--rerate", action="store_true",
                    help="re-rating this attempt: every answer not passed is the earlier rating's (an unanswered "
                         "question keeps its earlier answer)")
    ap.add_argument("--preview", action="store_true",
                    help="print the summary of what would be recorded and send and write nothing")
    ap.add_argument("--minutes-skipped", action="store_true",
                    help="the person skipped the time questions (pass only when no minutes were typed)")
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
    if args.minutes_skipped and (args.minutes_saved is not None or args.minutes_spent is not None):
        ap.error("--minutes-skipped cannot be combined with --minutes-saved or --minutes-spent")
    ended = (args.run_end, args.stop_reason, args.failure_check, args.failure_reported, args.rerun)
    if not args.rating and not any(ended) and not args.rerate:
        print("--rating is required for a finished run, or give the stopped/failed answers (or pass --context)",
              file=sys.stderr)
        return 2
    issues: dict[str, list[str]] = {}
    for dim, issue in args.issue:
        issues.setdefault(dim, []).append(issue)
    res = record(
        args.run_dir,
        args.rating,  # None for a stopped or failed attempt: only the run-end answers are recorded
        comment=args.comment,
        stage=args.stage,
        verdicts=dict(args.verdict),
        issues=issues,
        run_speed=args.run_speed,
        run_cost=args.run_cost,
        start_ready=args.start_ready,
        minutes_saved=args.minutes_saved,
        minutes_spent=args.minutes_spent,
        minutes_skipped=args.minutes_skipped,
        run_end=args.run_end,
        stop_reason=args.stop_reason,
        failure_check=args.failure_check,
        failure_reported=args.failure_reported,
        rerun=args.rerun,
        notes=dict(args.note),
        rerate=args.rerate,
        preview=args.preview,
    )
    if args.preview:
        print(res["summary"])
        print("\nNothing is sent yet.")
        return 0
    if res["sent"]:
        print(f"feedback recorded on the run's Langfuse {res['target']}")
    else:
        print(f"feedback saved locally (not sent to Langfuse: {res['note']})")
    print("\n" + res["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
