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
        row.update(observed=kw.pop("observed", "seen on the page"), evidence=kw.pop("evidence", "shot.png"),
                   on_target=kw.pop("on_target", True))
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
    # the pilot's TC-CT-05: the sheet import wrote a note where the expectation should be
    ("FLAGGED — no Expected Result was present in the original sanity sheet for this row; needs SME/QA "
     "input before this case is executable.", ""),
    ("No expected result given.", ""),
    ("Flagged items are listed first.", "Flagged items are listed first."),
])
def test_the_expected_result_is_read_from_the_case_text(expected, found):
    text = render_text({"precondition": "p", "steps": "1. s", "test_data": "d", "expected": expected})
    assert ui_agent.expected_result(text) == found


def test_the_agent_flags_are_defined():
    for name in ("execute_ui_agent", "agent_max_actions", "agent_case_timeout_s", "agent_parallel",
                 "agent_confirm_fail"):
        assert f"testing.{name}" in NAMES


@pytest.mark.parametrize("line, kept", [
    ("- /labs home widgets do not render; reach listings through /labs/search", True),
    ("# Notes from TC-CT-07", False),
    ("- Any case that starts from these widgets will be BLOCKED the same way until this is fixed", False),
    ("- The verdict here should be a FAIL", False),
    ("", False),
])
def test_a_note_is_a_fact_about_the_environment_never_advice_about_the_outcome(line, kept):
    assert bool(ui_agent.usable_note(line)) is kept


def test_a_long_note_is_cut_at_a_sentence_or_dropped():
    long = "- The search route works. " + "x" * 600
    assert ui_agent.usable_note(long) == "- The search route works."
    assert ui_agent.usable_note("- " + "y" * 600) == ""


def test_actions_are_counted_apart_from_looking():
    calls = [{"name": "mcp__playwright__browser_snapshot"}, {"name": "mcp__playwright__browser_click"},
             {"name": "mcp__playwright__browser_take_screenshot"}, {"name": "mcp__playwright__browser_type"},
             {"name": "Read"}]
    assert ui_agent.browser_calls(calls) == 4 and ui_agent.browser_actions(calls) == 2


def test_a_fail_the_second_run_could_not_reach_says_so():
    v = ct01() | {"confirmed": False, "flaky": False}
    assert ui_agent.summary(v).startswith("FAIL (not confirmed) — 2 of 3 parts met")
    assert ui_agent.summary(v | {"flaky": True}).startswith("FAIL (flaky: passed on the second run)")


@pytest.mark.parametrize("on_target", [False, None, "yes"])
def test_a_clause_judged_on_a_substitute_is_rejected(folder, on_target):
    """Both pilots: a clause marked met from a similar element on another screen."""
    v = verdict("PASS", [{"label": "A", "expected": [clause("the home widget shows the price", on_target=on_target)]}])
    problems = ui_agent.validate_verdict(v, EXTID, folder)
    assert any("without `on_target: true`" in p and "not reached" in p for p in problems), problems


def test_running_out_is_a_cause_for_blocked_only(folder):
    waiting = clause("x", ui_agent.NOT_REACHED, blocker="not yet checked")
    running = verdict("BLOCKED", [{"label": "A", "expected": [waiting]}], cause="limit", blocked_on="not finished",
                      actions=[])
    assert ui_agent.validate_verdict(running, EXTID, folder) == []          # the file an agent starts with
    assert any("cannot be `limit`" in p for p in ui_agent.validate_verdict(ct01() | {"cause": "limit"}, EXTID, folder))
    assert ui_agent.blocked(EXTID, "limit", "out of time")["cause"] == "limit"


@pytest.mark.parametrize("spelling", ["not_reached", "Not Reached", "not-reached", "NOT_REACHED"])
def test_a_state_spelled_another_way_is_read_not_rejected(folder, spelling):
    """Third pilot: five judged clauses lost to `not_reached`."""
    v = ct02()
    for part in v["parts"][2:]:
        part["expected"][0]["state"] = spelling
    v["parts"][1]["expected"][0]["state"] = "not_met"
    assert ui_agent.validate_verdict(v | {"status": "FAILED"}, EXTID, folder) == []
    assert v["parts"][2]["expected"][0]["state"] == "not reached" and v["parts"][1]["expected"][0]["state"] == "not met"


def test_the_agent_can_check_its_own_verdict(folder):
    import json

    from workflows.testing import check_verdict

    assert check_verdict.report(folder)[0] == 2                                   # nothing written yet
    (folder / ui_agent.VERDICT).write_text(json.dumps(ct01() | {"status": "PASS"}))
    code, text = check_verdict.report(folder)
    assert code == 1 and "REJECTED" in text and "its parts give FAIL" in text
    (folder / ui_agent.VERDICT).write_text(json.dumps(ct01()))
    code, text = check_verdict.report(folder)
    assert code == 0 and text.startswith("OK — FAIL — 2 of 3 parts met")
    waiting = clause("x", ui_agent.NOT_REACHED, blocker=ui_agent.PENDING)
    start = verdict("BLOCKED", [{"label": "A", "expected": [waiting]}], cause="limit", blocked_on="not finished",
                    actions=[])
    (folder / ui_agent.VERDICT).write_text(json.dumps(start))
    code, text = check_verdict.report(folder)
    assert code == 0 and "NOT FINISHED: 1 clause(s)" in text


def test_the_results_table_always_carries_the_time_each_case_took():
    rows = [{"extid": "KIWI-180", "status": "PASS", "duration_s": 128.4, "cost_usd": 0.86, "summary": "PASS — 1 of 1"},
            {"extid": "KIWI-197", "status": "FAIL", "confirmed": True, "duration_s": 386, "cost_usd": 1.77,
             "summary": "FAIL — 0 of 1 parts met; A not met (one | line)"},
            {"extid": "KIWI-205", "status": "BLOCKED", "duration_s": 42, "cost_usd": None, "summary": "cut off"}]
    table = ui_agent.results_table(rows, [{"extid": "TC-CT-05", "reason": "no expected result"}], ["KIWI-205"])
    lines = table.splitlines()
    assert lines[0] == "| Case | Verdict | Time | Cost | What was found |"
    assert "| KIWI-180 | PASS | 2m 08s | $0.86 |" in lines[2]
    assert "| KIWI-197 | FAIL (confirmed) | 6m 26s | $1.77 |" in lines[3] and "one / line" in lines[3]
    assert "| KIWI-205 | BLOCKED | 42s | unknown |" in lines[4]
    assert "| TC-CT-05 | not executed | — | — | no expected result |" == lines[5]
    assert lines[6] == "| **Total** | 3 run | 9m 16s of agent time | $2.63 or more | |"


@pytest.mark.parametrize("seconds, text", [(0, "0s"), (59.4, "59s"), (60, "1m 00s"), (188, "3m 08s"), (None, "—")])
def test_a_duration_reads_as_minutes_and_seconds(seconds, text):
    assert ui_agent.took(seconds) == text
