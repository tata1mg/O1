from __future__ import annotations

from rfc_agent import feedback_summary as fs


def _a(rows):
    return dict(rows)


def test_a_plain_thumb_is_one_row():
    assert fs.rows({"rating": "up"}) == [("Overall", "👍 Good")]


def test_a_down_rating_lists_the_four_areas_and_both_times():
    e = {
        "rating": "down",
        "verdicts": {"coverage": "usable_with_gaps", "approach": "usable"},
        "issues": {"coverage": ["missing_prd_flow", "poor_gap_name"]},
        "notes": {"coverage": "a Figma state is missing"},
        "comment": "needs another pass",
        "minutes_saved": 30,
        "minutes_status": "partial",
    }
    r = _a(fs.rows(e))
    assert r["Overall"] == '👎 Needs work · "needs another pass"'
    assert r["Coverage"] == 'Missing PRD flow, Poor gap name · "a Figma state is missing"'
    assert r["Approach"] == "no gaps" and r["Buildability"] == "not rated" and r["Context layer use"] == "not rated"
    assert r["Time saved"] == "30 min" and r["Time spent reviewing"] == "skipped"


def test_a_thumb_up_with_a_gap_is_good_with_notes_and_other_areas_appear_when_rated():
    e = {"rating": "up", "verdicts": {"readable": "usable_with_gaps"}, "issues": {"readable": ["hard_to_navigate"]}}
    r = _a(fs.rows(e))
    assert r["Overall"] == "👍 Good, with notes" and r["Readable summary"] == "Hard to navigate"


def test_stopped_and_failed_rows():
    stopped = _a(fs.rows({"stop_reason": "too_slow", "rerun": "yes", "notes": {"stop": "ages"}}))
    assert stopped == {"Main reason for stopping": 'It was taking too long · "ages"', "Run it again": "Yes"}
    failed = fs.rows({"failure_check": "no", "rerun": "no", "notes": {"failure": "it was the VPN"}})
    assert failed[0] == ("Recorded cause is right", 'No, something else happened · "it was the VPN"')
    killed = fs.rows({"failure_reported": "connection", "rerun": "yes"}, has_cause=False)
    assert killed[0] == ("What made it fail", "Connection or network")
    assert fs.rows({"run_end": "stopped", "stop_reason": "too_costly"})[0] == ("How it ended", "I stopped it")


def test_the_table_marks_kept_and_changed_against_the_earlier_rating():
    before = [("Overall", "👎 Needs work"), ("Coverage", "no gaps")]
    now = [("Overall", "👎 Needs work"), ("Coverage", "Missing PRD flow"), ("Time saved", "5 min")]
    t = fs.table(now, before).splitlines()
    assert t[0] == "| Question | Your answer | Compared with last time |"
    assert t[2].endswith("| kept |") and t[3].endswith("| changed |") and t[4].endswith("| changed |")
    assert fs.table(now).splitlines()[0] == "| Question | Your answer |"


def test_cells_cannot_break_the_table():
    t = fs.table([("Q", "a | b\nc " + "x" * 400)])
    assert t.count("\n") == 2 and "a / b c" in t and "…" in t
