"""The evaluator's mechanical half — every check that needs no judgment, computed in Python.

The evaluator SKILL calls its Deterministic Eval "the one thing that's never subjective" (SKILL.md:29) and
then asks a language model to perform it. That is an explicit invitation to move it into code, and survey
§3.2 lists exactly which of the 14 compliance gates are decidable from the artifact alone. This module
implements that list; everything else — the 8 rubric dimensions and gates 3, 5, 6, 7, 9, 10, 12, 13, 14 —
stays with the model, because they need to read a requirement and form an opinion.

The output mirrors the evaluator's own JSON keys so the two halves can be compared field by field. Where
the model and this module both compute a verdict (`presence_checks`), a mismatch is a gate failure listing
both, not a silent preference for one.

Every violation names an id: the gate feedback goes back into the model's guidance file, and "some case is
incomplete" cannot be acted on.
"""

from __future__ import annotations

import re

from .schema import (
    CASE_FIELDS,
    SURFACE_ENUM,
    id_module,
    swap_prefix,
    type_code,
    validate_case,
    validate_linkage,
    validate_neg_edge_per_module,
    validate_scenario,
)

#: A requirement reference: REQ-4, AC-2, a Jira key, or a contract field citation with a file:line.
RE_REQ_REF = re.compile(r"\b(?:REQ|AC|US|NFR)[-_ ]?\d+\b|\b[A-Z][A-Z0-9]{1,9}-\d+\b|\b\w+\.\w+:\d+", re.I)
#: A URL, a route, or a deep link — what a navigation step must name (SKILL.md Output Format Rule 11).
RE_LOCATOR = re.compile(r"https?://\S+|\b\w+://\S+|(?<![\w.])/[A-Za-z0-9_][\w./{}<>:-]*")
#: A navigation step. `open`/`launch` included: they land on a screen just as `navigate` does.
RE_NAV_STEP = re.compile(r"^\s*\d+\.\s*(?:navigate|go)\b|^\s*\d+\.\s*(?:open|launch|visit)\b", re.I)
#: Bare imperatives that make an "Expected Result" a restated step rather than an outcome.
RE_RESTATED = re.compile(r"^(click|tap|press|enter|type|select|submit|navigate|open|launch|scroll|swipe|"
                         r"call|send|hit|issue)\b", re.I)
#: Setup the steps imply. A row mentioning any of these needs a Precondition that says so.
SETUP_MARKERS = ("log in", "logged in", "login", "signed in", "sign in", "feature flag", "flag is",
                 "cohort", "experiment", "arm", "role", "admin", "permission", "token", "session",
                 "existing", "already has", "pre-existing", "cart contains", "subscription",
                 "test account", "seeded")
#: A field that is technically populated but says nothing.
PLACEHOLDERS = ("tbd", "todo", "to be decided", "xxx", "???", "-", "--", "placeholder", "fill in", "n/a n/a")
#: An "n/a" is an accurate answer for these fields; for the rest it is a hole (SKILL.md:101).
NA_ALLOWED_FIELDS = ("precondition", "test_data", "qa_comment", "requirement")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", str(text or "").lower())


def _words(text: str) -> str:
    return " ".join(_norm(text).split())


def _is_na(v) -> bool:
    return _words(v) in ("na", "n a", "none", "not applicable")


def _verdict(violations: list[str]) -> dict:
    return {"status": "FAIL" if violations else "PASS", "violations": violations}


# ---- the Deterministic Eval (Presence Check) ------------------------------------------------------
def presence_checks(cases: list[dict], scenarios: list[dict]) -> dict:
    """Both sub-checks over BOTH sheets, per SKILL.md:17-29.

    Scenarios carry Pre-condition/Expected Outcome and cases carry Precondition/Expected Result; the
    canonical dicts name both pairs `precondition`/`expected`, so one sweep covers both sheets.
    """
    expected_v: list[str] = []
    precondition_v: list[str] = []
    for row in [*scenarios, *cases]:
        rid = str(row.get("id") or "<no id>")
        expected = str(row.get("expected") or "").strip()
        if not expected or _is_na(expected):
            expected_v.append(f"{rid}: no Expected Result/Outcome")
        elif RE_RESTATED.match(expected):
            expected_v.append(f"{rid}: Expected Result restates the action ({expected[:60]!r}) instead of "
                              f"describing the resulting system behaviour")
        else:
            body = _words(row.get("steps") or row.get("description") or "")
            if body and _words(expected) and _words(expected) in body:
                expected_v.append(f"{rid}: Expected Result is a copy of the steps/description text")
        implied = _implied_setup(row)
        pre = str(row.get("precondition") or "").strip()
        if implied and (not pre or _is_na(pre)):
            precondition_v.append(f"{rid}: steps imply setup ({implied}) but Precondition is "
                                  f"{'empty' if not pre else 'n/a'}")
    return {"expected_result": _verdict(expected_v), "precondition": _verdict(precondition_v)}


def _implied_setup(row: dict) -> str:
    """The first setup marker the row's own text mentions, or ''."""
    blob = _words(" ".join(str(row.get(k) or "") for k in ("steps", "description", "expected", "title")))
    for marker in SETUP_MARKERS:
        if marker in blob:
            return marker
    return ""


# ---- the decidable compliance gates ----------------------------------------------------------------
def gate_traceable_requirement(cases: list[dict]) -> dict:
    """Gate 1: every case cites a requirement line, ticket, PRD section or contract field."""
    out = []
    for c in cases:
        blob = " ".join(str(c.get(k) or "") for k in ("requirement", "qa_comment", "title", "expected"))
        if not RE_REQ_REF.search(blob):
            out.append(f"{c.get('id')}: no requirement reference in `requirement` or `qa_comment` "
                       f"(expected a REQ-n, an AC-n, a ticket key, or a contract field citation)")
    return _verdict(out)


def gate_structural_completeness(cases: list[dict]) -> dict:
    """Gate 2, mechanical half: blank/placeholder mandatory fields, and vague navigation steps."""
    out = []
    for c in cases:
        cid = c.get("id")
        for field in CASE_FIELDS:
            val = str(c.get(field) or "").strip()
            if not val:
                out.append(f"{cid}: {field} is empty")
            elif _words(val) in PLACEHOLDERS or val.strip("-_ ") == "":
                out.append(f"{cid}: {field} is a placeholder ({val[:40]!r})")
            elif _is_na(val) and field not in NA_ALLOWED_FIELDS:
                out.append(f"{cid}: {field} is 'n/a', which is not an accurate value for this column")
        for line in str(c.get("steps") or "").splitlines():
            if RE_NAV_STEP.match(line) and not RE_LOCATOR.search(line):
                flagged = "url" in _words(c.get("qa_comment")) or "route" in _words(c.get("qa_comment"))
                if not flagged:
                    out.append(f"{cid}: navigation step names no URL/route/deep link and the QA Comment "
                               f"does not flag that one was unavailable — {line.strip()[:70]!r}")
    return _verdict(out)


def gate_surface_coverage(cases: list[dict], scenarios: list[dict]) -> dict:
    """Gate 4, mechanical half: every surface the suite declares must actually be covered by a case."""
    declared = {str(s.get("surface") or "").lower() for s in scenarios if s.get("surface")}
    declared = {s for s in declared if s in SURFACE_ENUM}
    covered = {str(c.get("surface") or "").lower() for c in cases}
    out = [f"surface {s!r} is declared on a scenario but no test case covers it"
           for s in sorted(declared - covered)]
    return _verdict(out)


def gate_duplicate_consolidation(cases: list[dict]) -> dict:
    """Gate 8, mechanical half: identical (title, surface) pairs. Different surface is NOT a duplicate."""
    seen: dict[tuple[str, str], list[str]] = {}
    for c in cases:
        key = (_words(c.get("title")), str(c.get("surface") or "").lower())
        seen.setdefault(key, []).append(str(c.get("id")))
    out = [f"{', '.join(ids)}: identical title on the same surface — consolidate or differentiate"
           for key, ids in sorted(seen.items()) if len(ids) > 1 and key[0]]
    return _verdict(out)


def gate_id_convention(cases: list[dict], scenarios: list[dict]) -> dict:
    """Not one of the SKILL's 14, but the ID convention is mandatory and fully mechanical."""
    out: list[str] = []
    for c in cases:
        out += [v for v in validate_case(c) if "id does not match" in v or "TYPE code" in v]
    for s in scenarios:
        out += [v for v in validate_scenario(s) if "id does not match" in v or "TYPE code" in v]
    return _verdict(out)


def gate_linkage(cases: list[dict], scenarios: list[dict], *, require_case_per_scenario: bool = True) -> dict:
    """TS<->TC prefix-swap linkage, no orphans, no duplicate ids."""
    out = validate_linkage(cases, scenarios, require_case_per_scenario=require_case_per_scenario)
    for c in cases:
        sid = str(c.get("scenario_id") or "")
        want = swap_prefix(str(c.get("id") or ""))
        if sid and want and sid != want:
            out.append(f"{c.get('id')}: linked scenario {sid} is not the prefix swap {want}")
    return _verdict(sorted(set(out)))


def gate_neg_edge_per_module(scenarios: list[dict]) -> dict:
    """Every module needs a Negative and an Edge Case scenario (SKILL.md Output Format Rule 8)."""
    return _verdict(validate_neg_edge_per_module(scenarios))


def evaluate(cases: list[dict], scenarios: list[dict], *, require_case_per_scenario: bool = True) -> dict:
    """The whole mechanical scorecard — what the stage writes to `qa/eval.mechanical.json`."""
    checks = {
        "traceable_requirement_reference": gate_traceable_requirement(cases),
        "structural_completeness": gate_structural_completeness(cases),
        "surface_coverage": gate_surface_coverage(cases, scenarios),
        "duplicate_consolidation": gate_duplicate_consolidation(cases),
        "id_convention": gate_id_convention(cases, scenarios),
        "scenario_case_linkage": gate_linkage(cases, scenarios,
                                              require_case_per_scenario=require_case_per_scenario),
        "negative_and_edge_per_module": gate_neg_edge_per_module(scenarios),
    }
    presence = presence_checks(cases, scenarios)
    all_pass = (all(v["status"] == "PASS" for v in checks.values())
                and all(v["status"] == "PASS" for v in presence.values()))
    return {
        "computed_by": "python",
        "counts": {"scenarios": len(scenarios), "cases": len(cases),
                   "modules": len({str(s.get("module") or id_module(str(s.get("id") or "")) or "")
                                   for s in scenarios}),
                   "http_cases": len([c for c in cases
                                      if str(c.get("execution") or "").lower() == "http"]),
                   "types": sorted({type_code(str(c.get("type") or "")) or "?" for c in cases})},
        "presence_checks": presence,
        "mechanical_checks": checks,
        "compliance_status": "PASS" if all_pass else "FAIL",
    }
