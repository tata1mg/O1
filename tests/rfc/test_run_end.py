"""rfc_agent.run_end: how each attempt ended (cause, kind, killed marker) and the state feedback opens on."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rfc_agent import run, run_end


@pytest.mark.parametrize(
    ("text", "cause"),
    [
        ("You've hit your 5-hour limit", "usage_or_login_limit"),
        ("Please run /login · token expired", "usage_or_login_limit"),
        ("scope: Atlassian is not connected", "atlassian_not_connected"),
        ("no Atlassian MCP server is connected (status 'needs-auth'). Authenticate one", "atlassian_not_connected"),
        (
            "tried 3 fresh connections: the Atlassian connector is not usable from headless claude",
            "atlassian_not_connected",
        ),
        ("jp.example.net did not answer after 3 attempts (VPN off?)", "context_layer_unavailable"),
        ("figma: no response from the plugin after 120s", "figma_no_response"),
        ("fleetlens unavailable for the context layer", "context_layer_unavailable"),
        ("ConnectionError: connection refused", "connection_error"),
        ("review: reviewers failed twice", "reviewers_failed"),
        ("something odd happened", "other"),
        ("", "other"),
    ],
)
def test_classify(text, cause):
    assert run_end.classify(text) == cause


def test_excerpt_scrubs_secrets_and_skips_the_stopped_banner():
    secret = "sk-ant-api03-" + "a" * 40
    out = run_end.excerpt(f"  [rfc] working\n  error: token {secret} rejected\n  [rfc] STOPPED at Scope (exit 1)")
    assert secret not in out and "rejected" in out and "STOPPED" not in out


def test_stop_kind():
    assert [run_end.stop_kind(c) for c in (0, 130, 1, 2)] == [None, "user_stop", "failure", "failure"]


def test_stderr_tail_passes_through_and_keeps_the_last_lines(capsys):
    import sys

    with run_end.capture_stderr() as tail:
        print("one", file=sys.stderr)
        print("two", file=sys.stderr)
    assert tail.text().splitlines()[-2:] == ["one", "two"]
    assert "one" in capsys.readouterr().err  # nothing swallowed
    assert sys.stderr is not tail  # restored


def _logs(tmp_path: Path) -> Path:
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def test_close_attempt_records_cause_only_for_a_failure(tmp_path):
    rd = _logs(tmp_path)
    run_end.open_attempt(rd, kind="new", start_stage="scope")
    assert (rd / "logs" / run_end.OPEN_FILE).exists()
    fail = run_end.close_attempt(rd, kind="new", start_stage="scope", code=1, status="stopped at scope", stage="scope",
                                 tail_text="Atlassian is not connected")  # fmt: skip
    assert fail["stop_kind"] == "failure" and fail["failure_cause"] == "atlassian_not_connected"
    assert not (rd / "logs" / run_end.OPEN_FILE).exists()
    stop = run_end.close_attempt(
        rd, kind="resume", start_stage="scope", code=130, status="stopped at draft", stage="draft"
    )
    assert stop["stop_kind"] == "user_stop" and "failure_cause" not in stop
    done = run_end.close_attempt(rd, kind="resume", start_stage="draft", code=0, status="done", stage=None)
    assert "stop_kind" not in done


def test_an_attempt_left_open_is_recorded_as_killed_by_the_next_start(tmp_path):
    rd = _logs(tmp_path)
    run_end.open_attempt(rd, kind="new", start_stage="scope")
    run_end.mark_stage(rd, "investigate")
    run_end.open_attempt(rd, kind="resume", start_stage="investigate")  # the first one never closed
    lines = run_end.read_attempts(rd)
    assert len(lines) == 1 and lines[0]["stop_kind"] == "killed" and lines[0]["status"] == "killed at investigate"
    assert run_end.state(rd)["end"] == "unknown" and run_end.state(rd)["attempt"] == 2


@pytest.mark.parametrize(("code", "end"), [(0, "finished"), (130, "stopped"), (1, "failed")])
def test_state_reads_the_latest_attempt(tmp_path, code, end):
    rd = _logs(tmp_path)
    run_end.close_attempt(rd, kind="new", start_stage="scope", code=code, status="x", stage="review",
                          tail_text="usage limit reached")  # fmt: skip
    st = run_end.state(rd)
    assert st["end"] == end and st["attempt"] == st["attempts"] == 1 and st["earlier"] == []
    assert st["cause"] == ("usage_or_login_limit" if end == "failed" else None)


def test_state_lists_earlier_attempts_that_did_not_finish(tmp_path):
    rd = _logs(tmp_path)
    run_end.close_attempt(rd, kind="new", start_stage="scope", code=130, status="stopped at review", stage="review")
    run_end.close_attempt(rd, kind="resume", start_stage="review", code=0, status="done", stage=None)
    st = run_end.state(rd)
    assert st["end"] == "finished" and st["attempts"] == 2
    assert st["earlier"] == [{"attempt": 1, "end": "stopped", "stage": "review"}]


def test_a_run_from_before_attempt_records_is_unknown_unless_done(tmp_path):
    rd = _logs(tmp_path)
    assert run_end.state(rd)["end"] == "unknown"
    (rd / "logs" / "run.json").write_text(json.dumps({"status": "done"}))
    assert run_end.state(rd)["end"] == "finished"


# ---- through run.main -----------------------------------------------------------------------------------------------


def _fake_stages(monkeypatch, behaviour: dict):
    def make(name):
        def stage(argv):
            rd = Path(argv[argv.index("--run-dir") + 1])
            if name == "scope":
                (rd / "raw").mkdir(parents=True, exist_ok=True)
                (rd / "raw" / "prd.md").write_text("prd")
                (rd / "scope.json").write_text("{}")
            return behaviour.get(name, lambda: 0)()

        return stage

    monkeypatch.setattr(run, "_mains", lambda: {n: make(n) for n in run.STAGES})


def test_a_failing_stage_records_its_cause_from_what_it_printed(tmp_path, monkeypatch):
    import sys

    def boom():
        print("scope: Atlassian is not connected", file=sys.stderr)
        return 1

    _fake_stages(monkeypatch, {"scope": boom})
    rd = tmp_path / "r"
    assert run.main(["--prd", "1", "--run-dir", str(rd)]) == 1
    last = run_end.read_attempts(rd)[-1]
    assert last["stop_kind"] == "failure" and last["failure_cause"] == "atlassian_not_connected"
    assert last["stage"] == "scope" and "Atlassian" in last["failure_excerpt"]
    assert not (rd / "logs" / run_end.OPEN_FILE).exists()


def test_stopped_then_resumed_keeps_both_attempts_in_order(tmp_path, monkeypatch):
    def ctrl_c():
        raise KeyboardInterrupt

    from rfc_agent.investigate import agent

    monkeypatch.setattr(agent, "kill_live_agents", lambda: 0)
    _fake_stages(monkeypatch, {"draft": ctrl_c})
    rd = tmp_path / "r"
    assert run.main(["--prd", "1", "--run-dir", str(rd)]) == 130
    _fake_stages(monkeypatch, {})
    assert run.main(["--run-dir", str(rd)]) == 0
    assert [(a["exit"], a.get("stop_kind")) for a in run_end.read_attempts(rd)] == [(130, "user_stop"), (0, None)]
    st = run_end.state(rd)
    assert st["end"] == "finished" and st["earlier"][0]["end"] == "stopped"


def test_the_feedback_hint_quotes_a_run_folder_with_spaces(tmp_path, monkeypatch, capsys):
    _fake_stages(monkeypatch, {"scope": lambda: 1})
    rd = tmp_path / "my run"
    assert run.main(["--prd", "1", "--run-dir", str(rd)]) == 1
    err = capsys.readouterr().err
    assert f"--run-dir '{rd}'" in err
