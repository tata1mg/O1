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


def test_build_scores_down_with_reasons_and_stage():
    s = feedback.build_scores(
        rating="down",
        reasons=["inaccurate", "too_long"],
        comment="x",
        stage="draft",
        trace_id="T",
        session_id="S",
        run_name="r",
        version="1",
    )
    assert [x["name"] for x in s] == [
        "rfc_feedback",
        "rfc_feedback_reason",
        "rfc_feedback_reason",
        "rfc_feedback_stage",
    ]
    assert s[0]["value"] == 0.0 and s[0]["data_type"] == "BOOLEAN" and s[0]["comment"] == "x"
    assert all(x["trace_id"] == "T" and "session_id" not in x for x in s)
    assert len({x["score_id"] for x in s}) == 4


def test_build_scores_up_ignores_reasons_and_falls_back_to_session():
    s = feedback.build_scores(
        rating="up",
        reasons=["inaccurate"],
        comment=None,
        stage=None,
        trace_id=None,
        session_id="S",
        run_name="r",
        version=None,
    )
    assert len(s) == 1 and s[0]["value"] == 1.0
    assert s[0]["session_id"] == "S" and "trace_id" not in s[0]


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
    res = feedback.record(tmp_path, "down", reasons=["wrong_repos", "bogus"], comment=f"  token {secret}  ", cfg=KEYS)
    assert res == {"sent": True, "target": "trace", "local": True, "note": None}
    assert [x["name"] for x in sent] == ["rfc_feedback", "rfc_feedback_reason"]  # "bogus" dropped
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
