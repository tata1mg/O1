"""The summary table shown for a rating: before it is submitted (a re-rating is confirmed) and after every submission.

One layout for every case (finished, stopped, failed; first rating or re-rating), built from the values the command
received, so what the person sees is what was sent. `rows()` gives (question, answer) pairs; `table()` renders them as
a Markdown table, with a "Compared with last time" column when the attempt was rated before.
"""

from __future__ import annotations

MAIN_AREAS = ("coverage", "buildability", "approach", "context_layer")
AREA_TITLES = {
    "coverage": "Coverage",
    "buildability": "Buildability",
    "approach": "Approach",
    "context_layer": "Context layer use",
    "readable": "Readable summary",
    "rollout_risk": "Rollout and risk",
}
GAPS = {
    "missing_prd_flow": "Missing PRD flow",
    "missing_figma_flow_or_state": "Missing Figma flow or state",
    "marked_covered_but_not_addressed": "Marked covered, but the design doesn't address it",
    "unneeded_flow_included": "Flow not needed",
    "too_thin_to_build_from": "Too vague to start coding",
    "interface_or_contract_unclear": "Ambiguous interface or API contract",
    "build_order_or_dependency_missing": "Doesn't say what to build first",
    "citation_does_not_support_claim": "A cited source doesn't say that",
    "poor_approach": "Poor approach for our systems",
    "ignores_engineering_standards": "Ignores engineering standards",
    "tradeoff_not_justified": "Tradeoff not justified",
    "missing_alternative": "Missing alternative",
    "generic_industry_choice_over_local_fit": "Generic industry choice over local fit",
    "wrong_or_stale_code_fact": "Wrong or stale code fact",
    "missed_existing_code_or_service": "Missed existing code, service or API",
    "wrong_repos_or_pages": "Wrong repos or pages",
    "ignored_existing_pattern": "Ignored an existing pattern",
    "too_long_or_repetitive": "Too long or repetitive",
    "hard_to_navigate": "Hard to navigate",
    "leaves_out_something_important": "Leaves out something important",
    "wrong_emphasis": "Wrong emphasis",
    "missing_rollout_rollback": "Missing rollout or rollback",
    "missing_compatibility_or_migration": "Missing compatibility or migration",
    "missing_performance_or_scale": "Missing performance or scale",
    "missing_security_or_privacy": "Missing security or privacy",
    "missing_monitoring": "Missing monitoring",
    "other_see_comment": "A gap typed under Other",
}
STOP_REASONS = {
    "too_slow": "It was taking too long",
    "too_costly": "Cost or tokens were too high",
    "wrong_repos_or_pages": "Wrong repos or pages were picked",
    "bad_input": "The PRD or Figma input was wrong",
    "changed_mind": "Changed my mind, or no longer needed",
    "other": "Other (typed)",
}
FAILURE_CHECK = {"yes": "Yes, that is what happened", "no": "No, something else happened"}
FAILURE_REPORTED = {
    "usage_or_login_limit": "A usage or login limit",
    "connector": "Atlassian, Figma or another connector",
    "connection": "Connection or network",
    "other": "Other (typed)",
}
YES_NO = {"yes": "Yes", "no": "No"}
RUN_END = {"finished": "It finished", "stopped": "I stopped it", "failed": "It failed"}
RUN_SPEED = {
    "fine": "Fine",
    "too_slow_to_run": "Too slow to run",
    "too_slow_to_review": "Too slow to review",
    "too_slow_to_run_and_review": "Too slow to run and to review",
}
RUN_COST = {"worth_it": "Worth the cost", "too_token_heavy": "Too token heavy"}
_NOTE_CAP = 100
_CELL_CAP = 200


def _gap(value: str) -> str:
    return GAPS.get(value) or value.replace("_", " ").capitalize()


def _typed(answer: str, note: str | None) -> str:
    note = " ".join((note or "").split())
    if not note:
        return answer
    note = note if len(note) <= _NOTE_CAP else note[: _NOTE_CAP - 1] + "…"
    return f'{answer} · "{note}"'


def _minutes(value) -> str:
    return f"{value} min" if value is not None else "skipped"


def rows(e: dict, has_cause: bool = True) -> list[tuple[str, str]]:
    """The (question, answer) pairs for one rating. `e` is a feedback.jsonl-shaped entry; `has_cause` says whether
    the run recorded a failure cause (a failed attempt then confirms it, otherwise says what made it fail)."""
    out: list[tuple[str, str]] = []
    notes = e.get("notes") or {}
    if e.get("run_end") in RUN_END:
        out.append(("How it ended", RUN_END[e["run_end"]]))
    rating = e.get("rating")
    if rating in ("up", "down"):
        verdicts, issues = e.get("verdicts") or {}, e.get("issues") or {}
        minutes_given = e.get("minutes_status") in ("given", "partial", "skipped")
        asked = any(v != "not_rated" for v in verdicts.values())
        detailed = rating == "down" or bool(any(issues.values()) or notes or minutes_given or asked)
        thumb = "👎 Needs work" if rating == "down" else "👍 Good, with notes" if detailed else "👍 Good"
        out.append(("Overall", _typed(thumb, e.get("comment"))))
        if detailed:
            extra = [d for d in AREA_TITLES if d not in MAIN_AREAS and verdicts.get(d) not in (None, "not_rated")]
            for dim in [*MAIN_AREAS, *extra]:
                gaps = issues.get(dim) or []
                if gaps:
                    answer = ", ".join(_gap(g) for g in gaps)
                elif verdicts.get(dim) == "usable":
                    answer = "no gaps"
                else:
                    answer = "not rated"
                out.append((AREA_TITLES[dim], _typed(answer, notes.get(dim))))
            run = [RUN_SPEED.get(e.get("run_speed") or ""), RUN_COST.get(e.get("run_cost") or "")]
            if any(run):
                out.append(("The run", _typed(", ".join(x for x in run if x), notes.get("run"))))
            out.append(("Time saved", _minutes(e.get("minutes_saved"))))
            out.append(("Time spent reviewing", _minutes(e.get("minutes_spent"))))
    if e.get("stop_reason") in STOP_REASONS:
        out.append(("Main reason for stopping", _typed(STOP_REASONS[e["stop_reason"]], notes.get("stop"))))
    if e.get("failure_check") in FAILURE_CHECK:
        question = "Recorded cause is right" if has_cause else "What made it fail"
        out.append((question, _typed(FAILURE_CHECK[e["failure_check"]], notes.get("failure"))))
    if e.get("failure_reported") in FAILURE_REPORTED:
        out.append(("What made it fail", _typed(FAILURE_REPORTED[e["failure_reported"]], notes.get("failure"))))
    if e.get("rerun") in YES_NO:
        out.append(("Run it again", YES_NO[e["rerun"]]))
    return out


def _cell(text: str) -> str:
    text = " ".join(str(text).split()).replace("|", "/")
    return text if len(text) <= _CELL_CAP else text[: _CELL_CAP - 1] + "…"


def table(now: list[tuple[str, str]], before: list[tuple[str, str]] | None = None) -> str:
    """A Markdown table of the answers. With `before` (the earlier rating of the same attempt) it adds a column that
    says, per row, whether the answer was kept or changed."""
    head = "| Question | Your answer |" + (" Compared with last time |" if before is not None else "")
    lines = [head, "|---|---|" + ("---|" if before is not None else "")]
    old = dict(before or [])
    for question, answer in now:
        line = f"| {_cell(question)} | {_cell(answer)} |"
        if before is not None:
            line += " kept |" if old.get(question) == answer else " changed |"
        lines.append(line)
    return "\n".join(lines)
