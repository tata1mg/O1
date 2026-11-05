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
    assert res == {"sent": True, "target": "trace", "local": True, "note": None}
    assert len(sent) == 1 + 6 + 3  # the headline, then every area, speed, cost and start-ready: always written
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
