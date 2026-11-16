"""The `execute_ui_agent` verdict contract (`workflows.testing.ui_agent`).

The three sample cases are a real hand run on mweb (2026-11-16): a category listing whose product pages
returned 400, a sort-and-filter case with one sub-check wrong and three unreachable, and a case with no
expected result.
"""

from __future__ import annotations

import copy

import pytest

from workflows.testing import ui_agent
from workflows.testing.flags import NAMES
from workflows.testing.schema import render_text

EXTID = "TC-CT-01"


def clause(text: str, state: str = ui_agent.MET, **kw) -> dict:
    row = {"text": text, "state": state}
    if state == ui_agent.NOT_REACHED:
        row["blocker"] = kw.pop("blocker", "the home carousels do not render")
    else:
        row.update(observed=kw.pop("observed", "seen on the page"), evidence=kw.pop("evidence", "shot.png"))
    return {**row, **kw}


def verdict(status: str, parts: list[dict], **kw) -> dict:
    base = {"extid": EXTID, "status": status, "reason": "because", "parts": parts,
            "actions": [{"step": "A1", "action": "open the listing", "saw": "the listing"}],
            "deviations": [], "observations": [], "blocked_on": ""}
    return {**base, **kw}


@pytest.fixture
def folder(tmp_path):
    d = ui_agent.case_dir(tmp_path, EXTID)
    d.mkdir(parents=True)
    (d / "shot.png").write_bytes(b"png")
    return d


def ct01() -> dict:
    """A met, B not met (product pages return 400), C met."""
    return verdict("FAIL", [
        {"label": "A", "expected": [clause("the category listing opens with Sort By and All filters")]},
        {"label": "B", "expected": [clause("a package opens its product page", ui_agent.NOT_MET,
                                           observed="400 Bad Request")]},
        {"label": "C", "expected": [clause("back returns to the listing")]}], cause="environment")


def ct02() -> dict:
    """A met, B not met (no labs list in the filters), C-E not reached (entry point does not render)."""
    return verdict("FAIL", [
        {"label": "A", "expected": [clause("each sort orders the list"), clause("the cross closes the sheet")]},
        {"label": "B", "expected": [clause("the filter sheet lists labs", ui_agent.NOT_MET,
                                           observed="Health Concerns, Gender, Tests, Age Group — no labs")]},
        *[{"label": x, "expected": [clause(f"part {x}", ui_agent.NOT_REACHED)]} for x in "CDE"]],
        cause="case")


# ---- the status follows from the parts -----------------------------------------------------------------


def test_the_three_sample_cases(folder):
    assert ui_agent.validate_verdict(ct01(), EXTID, folder) == []
    assert ui_agent.status_from_parts(ct01()["parts"]) == "FAIL" and ui_agent.parts_met(ct01()) == (2, 3)
    assert ui_agent.validate_verdict(ct02(), EXTID, folder) == []
    assert ui_agent.status_from_parts(ct02()["parts"]) == "FAIL" and ui_agent.parts_met(ct02()) == (1, 5)
    # TC-CT-05 has no expected result: it is never handed to an agent.
    text = render_text({"precondition": "logged in", "steps": "1. Tap 'Contains N tests'", "expected": ""})
    assert ui_agent.expected_result(text) == ""
    assert ui_agent.not_executed("TC-CT-05", "no expected result")["status"] == ui_agent.NOT_EXECUTED


@pytest.mark.parametrize("states, status", [
    (("met", "met"), "PASS"),
    (("met", "not reached"), "BLOCKED"),
    (("met", "not met", "not reached"), "FAIL"),     # FAIL outranks BLOCKED
])
def test_precedence_is_fail_then_blocked_then_pass(states, status):
    parts = [{"label": str(i), "expected": [clause("x", s)]} for i, s in enumerate(states)]
    assert ui_agent.status_from_parts(parts) == status


def test_a_mostly_passed_case_cannot_be_called_a_pass(folder):
    bad = ct01() | {"status": "PASS"}
    assert any("its parts give FAIL" in p for p in ui_agent.validate_verdict(bad, EXTID, folder))


def test_there_is_no_partial_status(folder):
    bad = ct01() | {"status": "PARTIAL"}
    assert any("no partial pass" in p for p in ui_agent.validate_verdict(bad, EXTID, folder))


# ---- every clause is held to its evidence ---------------------------------------------------------------


@pytest.mark.parametrize("change, expect", [
    ({"evidence": ""}, "evidence is missing"),
    ({"evidence": "nope.png"}, "does not exist"),
    ({"evidence": "../../../etc/hosts"}, "outside the case's folder"),
    ({"observed": ""}, "with no `observed`"),
    ({"state": "not checked"}, "must be checked"),
])
def test_a_pass_needs_evidence_for_every_clause(folder, change, expect):
    v = verdict("PASS", [{"label": "A", "expected": [clause("the listing opens", **change)]}])
    problems = ui_agent.validate_verdict(v, EXTID, folder)
    assert any(expect in p for p in problems), problems


def test_a_fail_needs_what_was_seen_and_a_cause(folder):
    v = ct01()
    v["parts"][1]["expected"][0]["observed"] = ""
    del v["cause"]
    problems = ui_agent.validate_verdict(v, EXTID, folder)
    assert any("with no `observed`" in p for p in problems) and any("needs a `cause`" in p for p in problems)


def test_a_clause_not_reached_names_its_blocker(folder):
    v = verdict("BLOCKED", [{"label": "A", "expected": [clause("x", ui_agent.NOT_REACHED, blocker="")]}],
                cause="environment", blocked_on="home page")
    assert any("names no `blocker`" in p for p in ui_agent.validate_verdict(v, EXTID, folder))


def test_blocked_needs_blocked_on(folder):
    v = verdict("BLOCKED", [{"label": "A", "expected": [clause("x", ui_agent.NOT_REACHED)]}],
                cause="environment")
    assert any("no `blocked_on`" in p for p in ui_agent.validate_verdict(v, EXTID, folder))
    assert ui_agent.validate_verdict(v | {"blocked_on": "home carousels"}, EXTID, folder) == []


def test_only_a_blocked_verdict_may_have_no_parts(folder):
    assert ui_agent.validate_verdict(ui_agent.blocked(EXTID, "limit", "40 actions used"), EXTID, folder) == []
    assert any("no `parts`" in p for p in ui_agent.validate_verdict(verdict("PASS", []), EXTID, folder))


def test_a_verdict_needs_the_actions_that_led_to_it(folder):
    v = verdict("PASS", [{"label": "A", "expected": [clause("x")]}], actions=[])
    assert any("no `actions`" in p for p in ui_agent.validate_verdict(v, EXTID, folder))


def test_a_verdict_for_another_case_is_named(folder):
    v = copy.deepcopy(ct01()) | {"extid": "TC-OTHER-01"}
    assert any("names 'TC-OTHER-01'" in p for p in ui_agent.validate_verdict(v, EXTID, folder))
    assert ui_agent.validate_verdict([], EXTID, folder) == [f"{EXTID}: verdict.json is not an object"]


# ---- the breakdown and the case text --------------------------------------------------------------------


def test_the_summary_shows_how_close_the_case_came():
    line = ui_agent.summary(ct01())
    assert line.startswith("FAIL — 2 of 3 parts met") and "B not met (400 Bad Request)" in line
    assert line.endswith("cause: environment")


@pytest.mark.parametrize("expected, found", [
    ("The listing opens.", "The listing opens."), ("", ""), ("N/A", ""), ("TBD.", ""),
])
def test_the_expected_result_is_read_from_the_case_text(expected, found):
    text = render_text({"precondition": "p", "steps": "1. s", "test_data": "d", "expected": expected})
    assert ui_agent.expected_result(text) == found


def test_the_agent_flags_are_defined():
    for name in ("execute_ui_agent", "agent_max_actions", "agent_case_timeout_s", "agent_parallel",
                 "agent_confirm_fail"):
        assert f"testing.{name}" in NAMES
