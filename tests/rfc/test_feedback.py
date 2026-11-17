"""rfc_agent.feedback: trace picking, the scores built, Langfuse-off/failure fallbacks, trace-id recording."""

from __future__ import annotations

import json

import pytest

from rfc_agent import feedback, observe


class Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


@pytest.fixture(autouse=True)
def _tracing_not_forced_off(monkeypatch):
    monkeypatch.delenv("RFC_AGENT_LANGFUSE", raising=False)
    monkeypatch.setattr(observe, "_overrides", {})


@pytest.fixture(autouse=True)
def _langfuse_not_read(monkeypatch):
    """Tracing is on in this file, so a rating with nothing in the run folder would read Langfuse: stub it empty."""
    monkeypatch.setattr(feedback, "_langfuse_scores", lambda cfg, trace_id, session_id: [])


KEYS = Cfg({"LANGFUSE.PUBLIC_KEY": "pk", "LANGFUSE.SECRET_KEY": "sk", "LANGFUSE.ENABLED": True})


def _traces(run, *entries):
    (run / "logs").mkdir(parents=True, exist_ok=True)
    (run / "logs" / observe.TRACES_FILE).write_text(
        "\n".join(json.dumps({"trace_id": t, "entry": e}) for t, e in entries) + "\n"
    )


def test_record_trace_id_appends(tmp_path):
    observe._record_trace_id(tmp_path, "rfc", "t1")
    observe._record_trace_id(tmp_path, "draft", "t2")
    observe._record_trace_id(tmp_path, "draft", None)  # no id: nothing written
    lines = (tmp_path / "logs" / observe.TRACES_FILE).read_text().splitlines()
    assert [json.loads(x)["trace_id"] for x in lines] == ["t1", "t2"]


def test_pick_trace_prefers_whole_run_then_stage(tmp_path):
    _traces(tmp_path, ("a", "rfc"), ("b", "draft"), ("c", "rfc"), ("d", "draft"))
    assert feedback.pick_trace(tmp_path) == "c"
    assert feedback.pick_trace(tmp_path, "draft") == "d"
    assert feedback.pick_trace(tmp_path, "review") == "c"  # no review trace: fall back to the run


def test_pick_trace_standalone_stages_only(tmp_path):
    _traces(tmp_path, ("a", "scope"), ("b", "draft"))
    assert feedback.pick_trace(tmp_path) == "b"
    assert feedback.pick_trace(tmp_path / "nope") is None


def test_build_scores_down_with_stage():
    s = feedback.build_scores(
        rating="down",
        comment="x",
        stage="draft",
        trace_id="T",
        session_id="S",
        run_name="r",
        version="1",
    )
    names = [x["name"] for x in s]
    assert names[0] == "rfc_feedback" and names[-1] == "rfc_feedback_stage"
    assert s[0]["value"] == 0.0 and s[0]["data_type"] == "BOOLEAN" and s[0]["comment"] == "x"
    assert all(x["trace_id"] == "T" and "session_id" not in x for x in s)
    assert len({x["score_id"] for x in s}) == len(s)


def test_build_scores_up_falls_back_to_session():
    s = feedback.build_scores(
        rating="up", comment=None, stage=None, trace_id=None, session_id="S", run_name="r", version=None
    )
    assert s[0]["name"] == "rfc_feedback" and s[0]["value"] == 1.0
    assert all(x["session_id"] == "S" and "trace_id" not in x for x in s)


def test_record_sends_and_logs_locally(tmp_path, monkeypatch):
    _traces(tmp_path, ("T1", "rfc"))
    sent = []

    class C:
        def create_score(self, **kw):
            sent.append(kw)

        def flush(self):
            pass

        def shutdown(self):
            pass

    monkeypatch.setattr(observe, "_make_client", lambda cfg: C())
    secret = "sk-ant-api03-" + "a" * 40
    res = feedback.record(tmp_path, "down", comment=f"  token {secret}  ", cfg=KEYS)
    assert (res["sent"], res["target"], res["local"], res["note"]) == (True, "trace", True, None)
    assert res["summary"].startswith("Feedback recorded") and "| Overall | 👎 Needs work · " in res["summary"]
    assert len(sent) == 1 + 6 + 3 + 1  # headline, every area, speed, cost, start-ready, minutes status: always written
    assert sent[0]["name"] == "rfc_feedback"
    assert secret not in (sent[0]["comment"] or "")  # secrets scrubbed
    saved = json.loads((tmp_path / "logs" / feedback.FEEDBACK_FILE).read_text())
    assert saved["trace_id"] == "T1" and saved["rating"] == "down"


def test_record_without_keys_or_with_failure_still_saves(tmp_path, monkeypatch):
    res = feedback.record(tmp_path, "up", cfg=Cfg({"LANGFUSE.ENABLED": True}))
    assert res["sent"] is False and res["local"] is True and "keys" in res["note"]

    def boom(cfg):
        raise RuntimeError("network down")

    monkeypatch.setattr(observe, "_make_client", boom)
    res = feedback.record(tmp_path, "up", cfg=KEYS)
    assert res["sent"] is False and "network down" in res["note"]
    assert len((tmp_path / "logs" / feedback.FEEDBACK_FILE).read_text().splitlines()) == 2


def test_main_bad_run_dir(tmp_path, capsys):
    assert feedback.main(["--run-dir", str(tmp_path / "missing"), "--rating", "up"]) == 2


def test_run_facts_collects_what_exists(tmp_path):
    from rfc_agent import run_facts

    (tmp_path / "scope.json").write_text(
        json.dumps(
            {
                "prd": "/x/prd.md",
                "jira": "AB-1",
                "context_layer": {"ok": True},
                "repos": [{"repo": "api"}, {"repo": "web"}],
                "figma": [{}],
            }
        )
    )
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "rfc.md").write_text("12345")
    (tmp_path / "review").mkdir()
    (tmp_path / "review" / "status.json").write_text(
        json.dumps({"a": {"status": "unresolved"}, "b": {"status": "fixed"}})
    )
    f = run_facts.collect(tmp_path, "https://wiki/x")
    assert f == {
        "prd": "https://wiki/x",
        "jira": "AB-1",
        "repos": ["api", "web"],
        "figma_count": 1,
        "context_layer_ok": True,
        "rfc_chars": 5,
        "review_findings": 2,
        "review_unresolved": 1,
        "review_blockers": 0,  # the findings in this fixture carry no severity
        "review_majors": 0,
        "review_minors": 0,
        "review_fixed": 1,
        "review_ignored": 0,
    }
    assert run_facts.collect(tmp_path / "missing") == {}


def test_feedback_score_metadata_carries_facts(tmp_path, monkeypatch):
    (tmp_path / "scope.json").write_text(json.dumps({"repos": [{"repo": "api"}]}))
    _traces(tmp_path, ("T1", "rfc"))
    observe._record_trace_id(tmp_path, "rfc", "T2", "https://wiki/prd")
    sent = []

    class C:
        create_score = lambda self, **kw: sent.append(kw)  # noqa: E731
        flush = shutdown = lambda self: None  # noqa: E731

    monkeypatch.setattr(observe, "_make_client", lambda cfg: C())
    feedback.record(tmp_path, "up", cfg=KEYS)
    assert sent[0]["metadata"]["repos"] == ["api"] and sent[0]["metadata"]["prd"] == "https://wiki/prd"
    assert sent[0]["trace_id"] == "T2"


# ---- the dimension form ---------------------------------------------------------------------------------------


class Sink:
    """A fake Langfuse client that keeps every score it is given, in order."""

    def __init__(self):
        self.sent = []

    def create_score(self, **kw):
        self.sent.append(kw)

    def flush(self):
        pass

    def shutdown(self):
        pass

    def by_name(self, name):
        return {x["score_id"]: x["value"] for x in self.sent if x["name"] == name}


def _finished_run(run):
    """A finished run."""
    for rel in ("out/rfc.md", "out/rfc.readable.md", "work/understanding.md"):
        (run / rel).parent.mkdir(parents=True, exist_ok=True)
        (run / rel).write_text("x")
    _traces(run, ("T1", "rfc"))


def test_form_writes_every_dimension_and_tags_issues_with_stages(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    feedback.record(
        tmp_path,
        "up",  # an approved RFC can still have gaps
        verdicts={"coverage": "usable_with_gaps", "approach": "usable", "readable": "usable"},
        issues={"coverage": ["missing_figma_flow_or_state", "bogus"], "approach": ["ignores_engineering_standards"]},
        run_speed="too_slow_to_review",
        start_ready="after_a_few_clarifications",
        cfg=KEYS,
    )
    assert sink.by_name("rfc_feedback") == {"fb:T1:overall": 1.0}
    assert sink.by_name("rfc_coverage") == {"fb:T1:dim:coverage": "usable_with_gaps"}
    assert sink.by_name("rfc_approach") == {"fb:T1:dim:approach": "usable_with_gaps"}  # a gap beats "usable"
    assert sink.by_name("rfc_buildability") == {"fb:T1:dim:buildability": "not_rated"}  # not asked
    assert sink.by_name("rfc_run_speed") == {"fb:T1:run_speed": "too_slow_to_review"}
    assert sink.by_name("rfc_run_cost") == {"fb:T1:run_cost": "not_rated"}
    assert sink.by_name("rfc_start_ready") == {"fb:T1:start_ready": "after_a_few_clarifications"}
    # "bogus" is dropped; "usable" ticked together with a gap: the gap wins
    assert sink.by_name("rfc_coverage_issue") == {
        "fb:T1:issue:coverage:missing_figma_flow_or_state": "missing_figma_flow_or_state"
    }
    assert sink.by_name("rfc_approach_issue") == {
        "fb:T1:issue:approach:ignores_engineering_standards": "ignores_engineering_standards"
    }
    issue = next(x for x in sink.sent if x["name"] == "rfc_coverage_issue")
    assert issue["metadata"]["likely_stages"] == ["investigate", "scope"]


def test_rating_again_replaces_the_earlier_answers(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    feedback.record(
        tmp_path,
        "down",
        verdicts={"coverage": "not_usable"},
        issues={"coverage": ["missing_prd_flow", "unneeded_flow_included"]},
        cfg=KEYS,
    )
    sink.sent.clear()
    feedback.record(
        tmp_path,
        "up",
        verdicts={"coverage": "usable_with_gaps"},
        issues={"coverage": ["unneeded_flow_included"]},
        cfg=KEYS,
    )
    # the dimension is overwritten by id, the issue that is gone is cleared, the one kept is still set
    assert sink.by_name("rfc_coverage") == {"fb:T1:dim:coverage": "usable_with_gaps"}
    assert sink.by_name("rfc_coverage_issue") == {
        "fb:T1:issue:coverage:unneeded_flow_included": "unneeded_flow_included",
        "fb:T1:issue:coverage:missing_prd_flow": "none",
    }
    assert sink.by_name("rfc_feedback") == {"fb:T1:overall": 1.0}


def test_issues_without_a_verdict_imply_gaps():
    v, i = feedback._norm_form({}, {"coverage": ["missing_prd_flow"]})
    assert v["coverage"] == "usable_with_gaps" and v["approach"] == "not_rated"
    assert i == {"coverage": ["missing_prd_flow"]}


def test_rating_without_trace_clears_against_the_session(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    (tmp_path / "logs" / observe.TRACES_FILE).unlink()  # no recorded trace: the session is the target
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    feedback.record(
        tmp_path, "down", verdicts={"coverage": "not_usable"}, issues={"coverage": ["unneeded_flow_included"]}, cfg=KEYS
    )
    sink.sent.clear()
    feedback.record(tmp_path, "up", cfg=KEYS)
    sid = f"fb:session:{tmp_path.name}:issue:coverage:unneeded_flow_included"
    assert sink.by_name("rfc_coverage_issue") == {sid: "none"}


def test_main_parses_the_form_and_rejects_bad_values(tmp_path, monkeypatch, capsys):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    monkeypatch.setattr(observe, "_load_cfg", lambda: KEYS)
    argv = [
        "--run-dir",
        str(tmp_path),
        "--rating",
        "down",
        "--verdict",
        "coverage=not_usable",
        "--issue",
        "coverage=missing_prd_flow",
        "--run-cost",
        "too_token_heavy",
    ]
    assert feedback.main(argv) == 0
    assert sink.by_name("rfc_coverage_issue") and sink.by_name("rfc_run_cost") == {"fb:T1:run_cost": "too_token_heavy"}
    for bad in (["--issue", "coverage=nope"], ["--issue", "nodim=x"], ["--verdict", "coverage=maybe"]):
        with pytest.raises(SystemExit) as e:
            feedback.main(["--run-dir", str(tmp_path), "--rating", "up", *bad])
        assert e.value.code == 2
    assert feedback.main(["--run-dir", str(tmp_path)]) == 2  # a rating, or --context, is required


def test_context_gives_the_numbers_for_the_prompt(tmp_path, capsys):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "rfc.md").write_text("x")
    (tmp_path / "raw").mkdir()
    (tmp_path / "raw" / "prd.md").write_text("word " * 3200)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "run.json").write_text(
        json.dumps({"cost_usd_total": 18.4, "stages": [{"seconds": 2400}, {"seconds": 120}]})
    )
    (tmp_path / "logs" / "investigate.json").write_text(json.dumps({"pause_wait_s": {"pause1_wait_s": 300.0}}))
    assert feedback.main(["--run-dir", str(tmp_path), "--context"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["line"] == "42 min (5 min waiting on a person) · $18.40 · PRD 3,200 words"


# ---- what is typed under Other, kept against its own question ---------------------------------------------------


def _comments(sink):
    return {
        x["name"] + ("=" + x["value"] if x["name"].endswith("_issue") else ""): x["comment"]
        for x in sink.sent
        if x.get("comment")
    }


def test_typed_text_is_stored_against_its_own_question(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    feedback.record(
        tmp_path,
        "up",
        comment="overall: good but see notes",
        verdicts={"coverage": "usable_with_gaps", "approach": "usable_with_gaps"},
        issues={"coverage": ["missing_prd_flow"]},  # a ticked gap AND typed text on the same question: both kept
        notes={
            "coverage": "no retry flow for failed payments",
            "approach": "skipped the queue option",
            "run": "slow in Figma",
        },
        cfg=KEYS,
    )
    got = _comments(sink)
    assert got["rfc_feedback"] == "overall: good but see notes"
    assert got["rfc_coverage"] == "no retry flow for failed payments"  # ticked gap lives in rfc_coverage_issue
    assert sink.by_name("rfc_coverage_issue") == {"fb:T1:issue:coverage:missing_prd_flow": "missing_prd_flow"}
    # approach had no listed gap ticked: the text is itself the gap, `other_see_comment`, and carries the text too
    assert got["rfc_approach"] == got["rfc_approach_issue=other_see_comment"] == "skipped the queue option"
    assert got["rfc_run_speed"] == got["rfc_run_cost"] == "slow in Figma"
    assert len([k for k in got if k.startswith("rfc_coverage_issue")]) == 0  # the text is not copied onto listed gaps


def test_a_note_means_the_area_has_a_gap():
    v, i = feedback._norm_form({"coverage": "usable"}, {}, {"coverage": "no retry flow"})
    assert v["coverage"] == "usable_with_gaps" and i == {"coverage": ["other_see_comment"]}
    v, i = feedback._norm_form({"coverage": "not_usable"}, {}, {"coverage": "x"})
    assert v["coverage"] == "not_usable" and i == {"coverage": ["other_see_comment"]}  # a harsher verdict is kept
    v, i = feedback._norm_form({}, {}, {"run": "slow"})  # a run note is not an area gap
    assert i == {} and v["coverage"] == "not_rated"


def test_notes_are_scrubbed_filtered_and_saved_locally(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    secret = "sk-ant-api03-" + "a" * 40
    feedback.record(
        tmp_path, "down", notes={"coverage": f"token {secret}", "nope": "ignored", "approach": "   "}, cfg=KEYS
    )
    got = _comments(sink)
    assert secret not in got["rfc_coverage"] and "approach" not in " ".join(got)
    saved = json.loads((tmp_path / "logs" / feedback.FEEDBACK_FILE).read_text())
    assert list(saved["notes"]) == ["coverage"] and secret not in saved["notes"]["coverage"]


def test_main_takes_notes_and_the_catch_all_gap(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    monkeypatch.setattr(observe, "_load_cfg", lambda: KEYS)
    argv = [
        "--run-dir",
        str(tmp_path),
        "--rating",
        "up",
        "--issue",
        "coverage=other_see_comment",
        "--note",
        "coverage=a typed gap",
        "--note",
        "run=too slow",
    ]
    assert feedback.main(argv) == 0
    got = _comments(sink)
    assert got["rfc_coverage"] == "a typed gap" and got["rfc_run_speed"] == "too slow"
    for bad in (["--note", "nope=x"], ["--note", "coverage="], ["--note", "coverage"]):
        with pytest.raises(SystemExit) as e:
            feedback.main(["--run-dir", str(tmp_path), "--rating", "up", *bad])
        assert e.value.code == 2


def test_both_slow_options_are_a_value_of_their_own(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    monkeypatch.setattr(observe, "_load_cfg", lambda: KEYS)
    feedback.record(tmp_path, "up", run_speed="too_slow_to_run_and_review", cfg=KEYS)
    assert sink.by_name("rfc_run_speed") == {"fb:T1:run_speed": "too_slow_to_run_and_review"}
    assert (
        feedback.main(["--run-dir", str(tmp_path), "--rating", "up", "--run-speed", "too_slow_to_run_and_review"]) == 0
    )


# ---- which stage to look at ---------------------------------------------------------------------------------------


def test_gaps_point_at_stages_and_a_rerate_clears_them(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    feedback.record(
        tmp_path,
        "down",
        verdicts={"coverage": "usable_with_gaps", "approach": "usable_with_gaps"},
        issues={"coverage": ["missing_figma_flow_or_state"], "approach": ["ignores_engineering_standards"]},
        cfg=KEYS,
    )
    # a figma flow gap names investigate and scope; the standards gap names draft
    assert sink.by_name("rfc_suspect_stage") == {
        "fb:T1:suspect:draft": "draft",
        "fb:T1:suspect:investigate": "investigate",
        "fb:T1:suspect:scope": "scope",
    }
    sink.sent.clear()
    feedback.record(
        tmp_path,
        "up",
        verdicts={"approach": "usable_with_gaps"},
        issues={"approach": ["ignores_engineering_standards"]},
        cfg=KEYS,
    )
    assert sink.by_name("rfc_suspect_stage") == {
        "fb:T1:suspect:draft": "draft",
        "fb:T1:suspect:investigate": "none",
        "fb:T1:suspect:scope": "none",
    }


def test_a_typed_gap_points_at_no_stage_and_every_gap_names_one():
    assert feedback.suspect_stages({"coverage": ["other_see_comment"]}) == []
    for dim, gaps in feedback.DIMENSIONS.items():
        assert all(stages for gap, stages in gaps.items() if gap != "other_see_comment"), dim


# ---- the brief that travels with a rating ---------------------------------------------------------------------------


def test_the_brief_tells_the_run_the_review_and_what_the_reviewer_said(tmp_path):
    facts = {
        "status": "done",
        "attempts": 2,
        "resumes": 1,
        "duration_s": 2580,
        "cost_usd": 63.78,
        "tokens_total": 49_100_000,
        "prd_words": 2864,
        "repos_proposed": 7,
        "repos_kept": 1,
        "figma_frames_in_scope": 5,
        "figma_sections_skipped": 2,
        "flows": 5,
        "reqs": 32,
        "req_covered": 11,
        "req_assumes": 18,
        "req_open": 1,
        "open_questions": 14,
        "alternatives": 3,
        "review_findings": 51,
        "review_blockers": 5,
        "review_majors": 19,
        "review_minors": 27,
        "review_fixed": 24,
        "review_unresolved": 0,
        "revise_rounds": 2,
        "revise_lines_changed": 140,
    }
    text = feedback.run_brief(
        tmp_path / "prd-1-20261030-101500",
        facts,
        rating="up",
        verdicts={"coverage": "usable_with_gaps", "buildability": "usable"},
        issues={"coverage": ["marked_covered_but_not_addressed"]},
        notes={"coverage": "a few missed flows"},
        comment="good overall",
        run_speed="too_slow_to_review",
        run_cost="worth_it",
    )
    for expected in (
        "prd-1-20261030-101500",
        "2 attempts (1 resumes)",
        "43 min",
        "$63.78",
        "49.1M tokens",
        "PRD 2,864 words",
        "1 of 7 proposed repos kept",
        "2 Figma sections skipped",
        "32 requirements (11 covered, 18 assumes, 1 open)",
        "51 findings (5 blocker, 19 major, 27 minor)",
        "24 fixed",
        "0 unresolved",
        "2 revise round(s), 140 lines changed",
        "gaps in coverage (marked_covered_but_not_addressed)",
        "fine: buildability",
        "look at: draft, review",
        "speed too_slow_to_review",
        'coverage: "a few missed flows"',
        'overall: "good overall"',
        "review/R-*.confirm*.md",
    ):
        assert expected in text, expected
    assert len(text) <= 1800


def test_the_brief_says_when_a_run_had_no_figma(tmp_path):
    kw = dict(rating="up", verdicts={}, issues={}, notes={}, comment=None, run_speed=None, run_cost=None)
    assert "no Figma exported" in feedback.run_brief(tmp_path / "r", {"figma_count": 0, "prd_words": 10}, **kw)
    assert "Figma" not in feedback.run_brief(tmp_path / "r", {"prd_words": 10}, **kw)  # unknown is not "none"
    assert "no Figma" not in feedback.run_brief(tmp_path / "r", {"figma_count": 2, "figma_frames_in_scope": 5}, **kw)


def test_the_brief_of_a_bare_run_is_short_and_never_fails(tmp_path):
    text = feedback.run_brief(
        tmp_path / "r", {}, rating="down", verdicts={}, issues={}, notes={}, comment=None, run_speed=None, run_cost=None
    )
    assert text.splitlines()[0] == "Run: r" and "Reviewer: 👎" in text and "Files:" in text


def test_the_brief_rides_on_the_headline_score_only_and_is_saved(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    (tmp_path / "scope.json").write_text(json.dumps({"repos": [{"repo": "api"}]}))
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    feedback.record(tmp_path, "up", verdicts={"coverage": "usable"}, cfg=KEYS)
    with_brief = [x["name"] for x in sink.sent if "run_brief" in x["metadata"]]
    assert with_brief == ["rfc_feedback"]
    brief = (tmp_path / "logs" / "run_brief.md").read_text()
    assert brief.strip() == next(x for x in sink.sent if x["name"] == "rfc_feedback")["metadata"]["run_brief"]
    assert "fine: coverage" in brief


def test_rating_again_clears_the_comments_the_new_rating_does_not_repeat(tmp_path, monkeypatch):
    _finished_run(tmp_path)
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    feedback.record(
        tmp_path,
        "up",
        comment="first overall",
        verdicts={"coverage": "usable_with_gaps", "approach": "usable_with_gaps"},
        issues={"coverage": ["missing_prd_flow"]},
        notes={"coverage": "no retry flow", "approach": "skipped the queue option", "run": "slow in Figma"},
        cfg=KEYS,
    )
    sink.sent.clear()
    # the same gap again, but nothing typed: Langfuse keeps an old comment unless it is sent an empty one
    feedback.record(
        tmp_path, "up", verdicts={"coverage": "usable_with_gaps"}, issues={"coverage": ["missing_prd_flow"]}, cfg=KEYS
    )
    sent = {(x["name"], x["score_id"].split(":", 2)[-1]): x for x in sent_all(sink)}
    assert sent[("rfc_feedback", "overall")]["comment"] == ""
    assert sent[("rfc_coverage", "dim:coverage")]["comment"] == ""
    assert sent[("rfc_run_speed", "run_speed")]["comment"] == "" == sent[("rfc_run_cost", "run_cost")]["comment"]
    gone = sent[("rfc_approach_issue", "issue:approach:other_see_comment")]  # approach lost its typed gap: set to none
    assert gone["value"] == "none" and gone["comment"] == ""
    assert "comment" not in sent[("rfc_buildability", "dim:buildability")]  # never had one: left alone


def sent_all(sink):
    return sink.sent


def test_run_summary_for_a_run_nobody_rated(tmp_path):
    """The end-of-run summary has the run's numbers and says 'not rated yet', with no typed text or thumb."""
    facts = {"status": "done", "duration_s": 3600, "cost_usd": 12.5, "prd_words": 900, "review_findings": 4,
             "langfuse_export_failures": 2}  # fmt: skip
    text = feedback.run_summary(tmp_path / "prd-1", facts)
    assert "Reviewer: not rated yet" in text and "Typed" not in text and "👍" not in text and "👎" not in text
    assert "$12.50" in text and "4 findings" in text and "2 failed Langfuse uploads" in text
    assert "failed Langfuse" not in feedback.run_summary(tmp_path / "prd-1", {"status": "done"})


def test_rating_again_without_a_stage_clears_the_stage_of_the_earlier_rating(tmp_path, monkeypatch):
    _finished_run(tmp_path)  # only a whole-run trace: a stage rating lands on it too
    sink = Sink()
    monkeypatch.setattr(observe, "_make_client", lambda cfg: sink)
    feedback.record(tmp_path, "down", stage="review", cfg=KEYS)
    assert sink.by_name("rfc_feedback_stage") == {"fb:T1:stage": "review"}
    sink.sent.clear()
    feedback.record(tmp_path, "down", cfg=KEYS)
    assert sink.by_name("rfc_feedback_stage") == {"fb:T1:stage": "none"}


def test_minutes_are_numeric_scores_only_when_typed():
    base = dict(rating="up", comment=None, stage=None, trace_id="T1", session_id="s", run_name="r", version=None)
    typed = feedback.build_scores(**base, minutes_saved=45, minutes_spent=20)
    got = {s["name"]: (s["value"], s["data_type"]) for s in typed if s["name"] in ("rfc_minutes_saved", "rfc_minutes_spent")}
    assert got == {"rfc_minutes_saved": (45.0, "NUMERIC"), "rfc_minutes_spent": (20.0, "NUMERIC")}
    untyped = feedback.build_scores(**base)
    assert not [s for s in untyped if s["name"] in ("rfc_minutes_saved", "rfc_minutes_spent")]


def test_approach_gap_is_poor_approach_and_infra_is_folded_into_it():
    gaps = feedback.DIMENSIONS["approach"]
    assert "poor_approach" in gaps and "ignores_existing_infra" not in gaps
    assert "ignores_engineering_standards" in gaps  # standards stay a separate checklist


def test_record_keeps_minutes_in_the_local_log(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    feedback.record(tmp_path, "up", minutes_saved=-3, minutes_spent=12, cfg=KEYS)  # negative is dropped
    entry = json.loads((tmp_path / "logs" / feedback.FEEDBACK_FILE).read_text().splitlines()[-1])
    assert entry["minutes_saved"] is None and entry["minutes_spent"] == 12


def _status(**kw):
    base = dict(rating="up", comment=None, stage=None, trace_id="T1", session_id="s", run_name="r", version=None)
    got = feedback.build_scores(**base, **kw)
    return next(s["value"] for s in got if s["name"] == "rfc_minutes_status")


def test_minutes_status_says_what_the_latest_rating_had():
    assert _status(minutes_saved=10, minutes_spent=5) == "given"
    assert _status(minutes_saved=10) == "partial"
    assert _status(minutes_skipped=True) == "skipped"
    assert _status() == "not_rated"


def test_record_sets_status_and_a_skip_replaces_earlier_minutes(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: sent.append(scores) or None)
    feedback.record(tmp_path, "up", minutes_saved=30, minutes_spent=10, cfg=KEYS)
    feedback.record(tmp_path, "up", minutes_skipped=True, cfg=KEYS)  # the re-rate skips the time questions
    first, second = sent
    assert next(s["value"] for s in first if s["name"] == "rfc_minutes_status") == "given"
    assert next(s["value"] for s in second if s["name"] == "rfc_minutes_status") == "skipped"
    assert any(s["name"] == "rfc_minutes_saved" for s in first)
    assert not any(s["name"] == "rfc_minutes_saved" for s in second)  # no number written when skipped
    log = [json.loads(x) for x in (tmp_path / "logs" / feedback.FEEDBACK_FILE).read_text().splitlines()]
    assert [e["minutes_status"] for e in log] == ["given", "skipped"]


# ---- feedback on a stopped or failed attempt ------------------------------------------------------------------------


def _attempts(run, *lines):
    (run / "logs").mkdir(parents=True, exist_ok=True)
    (run / "logs" / "attempts.jsonl").write_text("\n".join(json.dumps(x) for x in lines) + "\n")


def test_run_end_scores_write_only_what_was_answered():
    base = dict(trace_id="T1", session_id="s", run_name="r", version=None)
    assert feedback.build_run_end_scores(**base) == []
    got = {s["name"]: s["value"] for s in feedback.build_run_end_scores(**base, stop_reason="too_slow", rerun="yes")}
    assert got == {"rfc_run_stop_reason": "too_slow", "rfc_rerun": "yes"}  # no "not_rated" placeholders
    sc = feedback.build_run_end_scores(**base, failure_check="no", notes={"failure": "VPN was off"})
    assert sc[0]["name"] == "rfc_failure_cause_confirmed" and sc[0]["comment"] == "VPN was off"
    assert sc[0]["score_id"] == "fb:T1:failure_confirmed"


def test_a_stopped_attempt_is_rated_without_a_thumb(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: sent.append(scores) or None)
    _traces(tmp_path, ("T1", "rfc"))
    _attempts(tmp_path, {"exit": 130, "stop_kind": "user_stop", "stage": "review", "status": "stopped at review"})
    note = {"stop": "cost, and slow too"}
    res = feedback.record(tmp_path, None, stop_reason="too_costly", rerun="no", notes=note, cfg=KEYS)
    assert res["sent"] and res["target"] == "trace"
    by = {s["name"]: s for s in sent[0]}
    assert set(by) == {"rfc_run_stop_reason", "rfc_rerun"}  # no headline, no areas
    assert by["rfc_run_stop_reason"]["comment"] == "cost, and slow too"
    meta = by["rfc_rerun"]["metadata"]
    assert meta["attempt"] == 1 and meta["run_state"] == "stopped at review"
    entry = json.loads((tmp_path / "logs" / feedback.FEEDBACK_FILE).read_text().splitlines()[-1])
    assert entry["rating"] is None and entry["stop_reason"] == "too_costly" and entry["attempt"] == 1


def test_a_finished_attempt_does_not_override_the_stopped_attempts_feedback(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: sent.append(scores) or None)
    _traces(tmp_path, ("T1", "rfc"))
    _attempts(tmp_path, {"exit": 130, "stop_kind": "user_stop", "stage": "review"})
    feedback.record(tmp_path, None, stop_reason="changed_mind", rerun="yes", cfg=KEYS)
    _traces(tmp_path, ("T1", "rfc"), ("T2", "rfc"))  # the resume is a new trace
    _attempts(tmp_path, {"exit": 130, "stop_kind": "user_stop", "stage": "review"}, {"exit": 0})
    feedback.record(tmp_path, "down", cfg=KEYS)
    stopped_ids = {s["score_id"] for s in sent[0]}
    finished_ids = {s["score_id"] for s in sent[1]}
    assert all(i.startswith("fb:T1:") for i in stopped_ids) and all(i.startswith("fb:T2:") for i in finished_ids)
    assert not stopped_ids & finished_ids  # nothing of attempt 1 can be replaced by attempt 2
    meta = next(s for s in sent[1] if s["name"] == "rfc_feedback")["metadata"]
    assert (meta["attempt"], meta["attempts"], meta["run_state"]) == (2, 2, "done")


def test_rating_the_same_stopped_attempt_again_replaces_only_that_attempt(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: sent.append(scores) or None)
    _traces(tmp_path, ("T1", "rfc"))
    _attempts(tmp_path, {"exit": 1, "stop_kind": "failure", "stage": "scope", "failure_cause": "other"})
    feedback.record(tmp_path, None, failure_check="no", rerun="yes", notes={"failure": "first"}, cfg=KEYS)
    feedback.record(tmp_path, None, failure_check="yes", rerun="yes", cfg=KEYS)
    first, second = ({s["name"]: s for s in x} for x in sent)
    assert first["rfc_failure_cause_confirmed"]["score_id"] == second["rfc_failure_cause_confirmed"]["score_id"]
    assert second["rfc_failure_cause_confirmed"]["comment"] == ""  # the old note is cleared


def test_unknown_values_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    bad = dict(run_end="bogus", stop_reason="bogus", failure_check="maybe", rerun="sometimes")
    feedback.record(tmp_path, None, cfg=KEYS, **bad)
    entry = json.loads((tmp_path / "logs" / feedback.FEEDBACK_FILE).read_text().splitlines()[-1])
    assert entry["run_end"] is entry["stop_reason"] is entry["failure_check"] is entry["rerun"] is None


def test_context_says_how_the_run_ended_and_whether_earlier_attempts_did_not_finish(tmp_path):
    _attempts(tmp_path, {"exit": 1, "stop_kind": "failure", "stage": "scope", "failure_cause": "usage_or_login_limit"})
    ctx = feedback.context(tmp_path)
    assert ctx["end"] == "failed" and ctx["cause"] == "usage_or_login_limit"
    assert ctx["recorded"].startswith("Failed at scope · usage or login limit")
    _attempts(tmp_path, {"exit": 130, "stop_kind": "user_stop", "stage": "review"}, {"exit": 0})
    ctx = feedback.context(tmp_path)
    assert ctx["end"] == "finished" and ctx["earlier"] == [{"attempt": 1, "end": "stopped", "stage": "review"}]


def test_cli_accepts_run_end_answers_without_a_rating(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    monkeypatch.setattr(observe, "_load_cfg", lambda: KEYS)
    assert feedback.main(["--run-dir", str(tmp_path), "--stop-reason", "too_slow", "--rerun", "yes"]) == 0
    assert feedback.main(["--run-dir", str(tmp_path)]) == 2  # nothing at all is still a usage error


def test_skip_cannot_be_combined_with_typed_minutes(tmp_path, capsys):
    with pytest.raises(ValueError):
        feedback.record(tmp_path, "up", minutes_skipped=True, minutes_saved=10, cfg=KEYS)
    with pytest.raises(SystemExit):
        feedback.main(["--run-dir", str(tmp_path), "--rating", "up", "--minutes-skipped", "--minutes-spent", "5"])
    assert "cannot be combined" in capsys.readouterr().err


# ---- prefill from the earlier rating of the same attempt ---------------------------------------------------------


def _rate(run, rating="down", **kw):
    feedback.record(run, rating, cfg=KEYS, **kw)


FORM = dict(
    verdicts={"coverage": "usable_with_gaps", "approach": "usable"},
    issues={"coverage": ["missing_prd_flow"]},
    notes={"coverage": "a Figma state is missing"},
    comment="needs another pass",
    run_speed="too_slow_to_review",
    start_ready="after_a_few_clarifications",
    minutes_saved=30,
    minutes_spent=10,
)


def test_previous_is_none_until_the_attempt_was_rated(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    _traces(tmp_path, ("T1", "rfc"))
    assert feedback.context(tmp_path, KEYS)["previous"] is None


def test_previous_returns_the_whole_earlier_rating_of_this_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    _traces(tmp_path, ("T1", "rfc"))
    _rate(tmp_path, **FORM)
    prev = feedback.context(tmp_path, KEYS)["previous"]
    assert prev["rating"] == "down" and prev["source"] == "run folder"
    assert prev["verdicts"] == {"coverage": "usable_with_gaps", "approach": "usable"}  # not_rated ones are left out
    assert prev["issues"] == {"coverage": ["missing_prd_flow"]}
    assert prev["notes"] == {"coverage": "a Figma state is missing"} and prev["comment"] == "needs another pass"
    assert prev["run_speed"] == "too_slow_to_review" and prev["start_ready"] == "after_a_few_clarifications"
    assert (prev["minutes_saved"], prev["minutes_spent"]) == (30, 10)


def test_previous_is_the_latest_rating_and_a_skip_leaves_no_old_minutes(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    _traces(tmp_path, ("T1", "rfc"))
    _rate(tmp_path, **FORM)
    _rate(tmp_path, "up", minutes_skipped=True)
    prev = feedback.context(tmp_path, KEYS)["previous"]
    assert prev["rating"] == "up" and "minutes_saved" not in prev and "issues" not in prev


def test_another_attempt_starts_blank_and_keeps_its_own_feedback(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    _traces(tmp_path, ("T1", "rfc"))
    _rate(tmp_path, None, stop_reason="too_slow", rerun="yes")  # the stopped attempt, rated
    assert feedback.context(tmp_path, KEYS)["previous"]["stop_reason"] == "too_slow"
    _traces(tmp_path, ("T1", "rfc"), ("T2", "rfc"))  # resumed: a new attempt, a new trace
    assert feedback.context(tmp_path, KEYS)["previous"] is None


def test_a_stopped_attempt_rated_twice_is_prefilled_and_replaced(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: sent.append(scores))
    _traces(tmp_path, ("T1", "rfc"))
    _rate(tmp_path, None, stop_reason="too_slow", rerun="yes", notes={"stop": "took ages"})
    prev = feedback.context(tmp_path, KEYS)["previous"]
    assert (prev["stop_reason"], prev["rerun"], prev["notes"]) == ("too_slow", "yes", {"stop": "took ages"})
    _rate(tmp_path, None, stop_reason="too_costly", rerun="yes")
    first = {s["score_id"] for s in sent[0]}
    second = {s["score_id"] for s in sent[1]}
    assert any("stop_reason" in i for i in first) and first & second  # same ids: the second rating replaces


LANGFUSE_SCORES = [
    {"name": "rfc_feedback", "value": True, "comment": "needs another pass"},
    {"name": "rfc_coverage", "value": "usable_with_gaps", "comment": "a Figma state is missing"},
    {"name": "rfc_approach", "value": "usable"},
    {"name": "rfc_readable", "value": "not_rated"},
    {"name": "rfc_coverage_issue", "value": "missing_prd_flow"},
    {"name": "rfc_coverage_issue", "value": "none"},  # cleared by an even earlier re-rate
    {"name": "rfc_run_speed", "value": "too_slow_to_review", "comment": "slow to read"},
    {"name": "rfc_run_cost", "value": "not_rated"},
    {"name": "rfc_start_ready", "value": "not_rated"},
    {"name": "rfc_minutes_status", "value": "given"},
    {"name": "rfc_minutes_saved", "value": 30.0},
    {"name": "rfc_minutes_spent", "value": 10.0},
    {"name": "rfc_suspect_stage", "value": "investigate"},
]


def _langfuse_on(monkeypatch, scores):
    monkeypatch.setenv("RFC_AGENT_LANGFUSE", "true")
    monkeypatch.setattr(feedback, "_langfuse_scores", lambda cfg, trace_id, session_id: scores)


def test_entry_from_scores_rebuilds_the_rating():
    e = feedback._entry_from_scores(LANGFUSE_SCORES)
    assert e["rating"] == "up" and e["comment"] == "needs another pass"
    assert e["verdicts"] == {"coverage": "usable_with_gaps", "approach": "usable"}
    assert e["issues"] == {"coverage": ["missing_prd_flow"]}
    assert e["notes"] == {"coverage": "a Figma state is missing", "run": "slow to read"}
    assert (e["minutes_saved"], e["minutes_spent"]) == (30, 10) and e["run_speed"] == "too_slow_to_review"
    assert feedback._entry_from_scores([]) is None


def test_entry_from_scores_drops_minutes_the_status_calls_stale():
    stale = [{"name": "rfc_feedback", "value": True}, {"name": "rfc_minutes_status", "value": "skipped"},
             {"name": "rfc_minutes_saved", "value": 30.0}]  # fmt: skip
    assert feedback._entry_from_scores(stale).get("minutes_saved") is None


def test_previous_falls_back_to_langfuse_only_when_the_run_folder_has_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    _traces(tmp_path, ("T1", "rfc"))
    _langfuse_on(monkeypatch, LANGFUSE_SCORES)
    prev = feedback.context(tmp_path, KEYS)["previous"]
    assert prev["source"] == "langfuse" and prev["issues"] == {"coverage": ["missing_prd_flow"]}
    # once the run folder has a rating, Langfuse is not read and nothing is mixed
    _rate(tmp_path, "up", verdicts={"approach": "usable"})
    monkeypatch.setattr(feedback, "_langfuse_scores", lambda *a: pytest.fail("should not be read"))
    prev = feedback.context(tmp_path, KEYS)["previous"]
    assert prev["source"] == "run folder" and "issues" not in prev and prev["verdicts"] == {"approach": "usable"}


def test_a_rerate_clears_gaps_langfuse_remembers_when_the_log_is_gone(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: sent.append(scores))
    _traces(tmp_path, ("T1", "rfc"))
    _langfuse_on(monkeypatch, LANGFUSE_SCORES)
    _rate(tmp_path, "up", verdicts={"coverage": "usable"})  # the gap is no longer picked
    cleared = [s for s in sent[0] if s["name"] == "rfc_coverage_issue"]
    assert [(s["value"], s["score_id"]) for s in cleared] == [("none", "fb:T1:issue:coverage:missing_prd_flow")]


def test_langfuse_scores_pages_and_keeps_only_this_targets_feedback(monkeypatch):
    import io

    pages = [
        {"data": [{"id": "fb:T1:overall"}, {"id": "other"}], "meta": {"cursor": "c2"}},
        {"data": [{"id": "fb:T1:dim:coverage"}, {"id": "fb:T2:overall"}], "meta": {}},
    ]
    urls = []

    def fake(req, timeout):
        urls.append(req.full_url)
        return io.BytesIO(json.dumps(pages[len(urls) - 1]).encode())

    monkeypatch.undo()  # the autouse stub of _langfuse_scores
    monkeypatch.setattr("urllib.request.urlopen", fake)
    got = feedback._langfuse_scores(Cfg({**KEYS, "LANGFUSE.HOST": "https://lf.example/"}), "T1", "S")
    assert [x["id"] for x in got] == ["fb:T1:overall", "fb:T1:dim:coverage"]
    assert "traceId=T1" in urls[0] and "fields=core%2Cdetails" in urls[0] and "cursor=c2" in urls[1]


def test_langfuse_trouble_means_no_prefill_not_an_error(monkeypatch):
    def boom(req, timeout):
        raise OSError("network down")

    monkeypatch.undo()
    monkeypatch.setattr("urllib.request.urlopen", boom)
    assert feedback._langfuse_scores(KEYS, "T1", "S") == []


# ---- the summary table, --rerate and --preview -------------------------------------------------------------------


def _log(run):
    return [json.loads(x) for x in (run / "logs" / feedback.FEEDBACK_FILE).read_text().splitlines()]


def test_record_returns_the_summary_table(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    _traces(tmp_path, ("T1", "rfc"))
    res = feedback.record(tmp_path, "down", cfg=KEYS, **FORM)
    assert res["summary"].startswith("Feedback recorded · attempt 1 (")
    assert "| Coverage | Missing PRD flow" in res["summary"] and "Compared with last time" not in res["summary"]
    res = feedback.record(tmp_path, "down", cfg=KEYS, **FORM)  # the same again: every row kept
    assert "Compared with last time" in res["summary"] and "changed" not in res["summary"].split("\n", 2)[2]


def test_rerate_fills_every_unanswered_question_from_the_earlier_rating(tmp_path, monkeypatch):
    sent = []
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: sent.append(scores))
    _traces(tmp_path, ("T1", "rfc"))
    _rate(tmp_path, **FORM)
    res = feedback.record(tmp_path, None, rerate=True, issues={"buildability": ["too_thin_to_build_from"]}, cfg=KEYS)
    entry = _log(tmp_path)[-1]
    assert entry["rating"] == "down" and entry["comment"] == "needs another pass"
    assert entry["issues"] == {"coverage": ["missing_prd_flow"], "buildability": ["too_thin_to_build_from"]}
    assert entry["verdicts"]["approach"] == "usable" and entry["notes"] == {"coverage": "a Figma state is missing"}
    assert (entry["minutes_saved"], entry["minutes_spent"]) == (30, 10) and entry["run_speed"] == "too_slow_to_review"
    rows = {ln.split(" | ")[0].strip("| "): ln for ln in res["summary"].splitlines()[4:]}
    assert rows["Coverage"].endswith("| kept |") and rows["Buildability"].endswith("| changed |")
    assert any(s["name"] == "rfc_minutes_saved" and s["value"] == 30.0 for s in sent[-1])  # the number is sent again


def test_rerate_with_a_typed_answer_replaces_that_one_only(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    _traces(tmp_path, ("T1", "rfc"))
    _rate(tmp_path, **FORM)
    feedback.record(tmp_path, None, rerate=True, minutes_saved=45, cfg=KEYS)
    e = _log(tmp_path)[-1]
    assert (e["minutes_saved"], e["minutes_spent"]) == (45, None)  # a typed minute replaces the pair: nothing stale
    feedback.record(tmp_path, None, rerate=True, minutes_skipped=True, cfg=KEYS)
    assert _log(tmp_path)[-1]["minutes_status"] == "skipped"


def test_rerate_of_a_stopped_attempt(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    _traces(tmp_path, ("T1", "rfc"))
    _attempts(tmp_path, {"exit": 130, "stop_kind": "user_stop", "stage": "review"})
    feedback.record(tmp_path, None, stop_reason="too_slow", rerun="yes", cfg=KEYS)
    res = feedback.record(tmp_path, None, rerate=True, stop_reason="too_costly", cfg=KEYS)
    e = _log(tmp_path)[-1]
    assert (e["stop_reason"], e["rerun"]) == ("too_costly", "yes")
    assert "| Main reason for stopping | Cost or tokens were too high | changed |" in res["summary"]
    assert "| Run it again | Yes | kept |" in res["summary"]


def test_preview_writes_and_sends_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: pytest.fail("nothing may be sent"))
    _traces(tmp_path, ("T1", "rfc"))
    res = feedback.record(tmp_path, "down", preview=True, cfg=KEYS, **FORM)
    assert res["preview"] and res["summary"].startswith("Check before submitting")
    assert not (tmp_path / "logs" / feedback.FEEDBACK_FILE).exists()
    assert not (tmp_path / "logs" / "run_brief.md").exists()


def test_cli_prints_the_table_after_recording_and_for_a_preview(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(feedback, "_send", lambda cfg, scores: None)
    monkeypatch.setattr(observe, "_load_cfg", lambda: KEYS)
    _traces(tmp_path, ("T1", "rfc"))
    args = ["--run-dir", str(tmp_path), "--rating", "down", "--verdict", "coverage=usable_with_gaps",
            "--issue", "coverage=missing_prd_flow"]  # fmt: skip
    assert feedback.main([*args, "--preview"]) == 0
    out = capsys.readouterr().out
    assert "Check before submitting" in out and "Nothing is sent yet." in out and "feedback recorded" not in out
    assert feedback.main(args) == 0
    out = capsys.readouterr().out
    assert out.startswith("feedback recorded on the run's Langfuse trace") and "| Coverage | Missing PRD flow |" in out
    assert feedback.main(["--run-dir", str(tmp_path), "--rerate", "--minutes-saved", "20"]) == 0  # needs no rating
    assert "| Overall | 👎 Needs work | kept |" in capsys.readouterr().out
