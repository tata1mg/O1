"""The single command (rfc_agent.run): sequencing, option passing, skipping Scope, --from, stopping."""

from __future__ import annotations

import json
from pathlib import Path

from rfc_agent import run


def _stubs(monkeypatch, fail: str | None = None, exit_with: object = None):
    calls: list[tuple[str, list[str]]] = []

    def make(name):
        def stage(argv):
            calls.append((name, argv))
            rd = Path(argv[argv.index("--run-dir") + 1])
            if name == "scope":
                (rd / "raw").mkdir(parents=True, exist_ok=True)
                (rd / "raw" / "prd.md").write_text("prd")
                (rd / "scope.json").write_text(json.dumps({"connector_cost_usd": 0.1}))
            if name == fail:
                if exit_with is not None:
                    raise SystemExit(exit_with)
                return 1
            if name == "revise":
                (rd / "review").mkdir(exist_ok=True)
                (rd / "review" / "status.json").write_text(json.dumps({"R-X.1": {"status": "unresolved"}}))
            return 0

        return stage

    monkeypatch.setattr(run, "_mains", lambda: {n: make(n) for n in run.STAGES})
    return calls


def test_full_run_passes_each_stage_its_options(tmp_path, monkeypatch):
    calls = _stubs(monkeypatch)
    rd = tmp_path / "r"
    code = run.main(
        [
            "--prd",
            "5541855972",
            "--run-dir",
            str(rd),
            "--jira",
            "PROJ-1",
            "--no-figma",
            "--preset",
            "fast",
            "--ask",
            "--ask-review",
            "--confirm-file",
        ]
    )
    assert code == 0 and [c[0] for c in calls] == list(run.STAGES)
    args = dict(calls)
    assert args["scope"][:4] == ["--run-dir", str(rd.resolve()), "--prd", "5541855972"]
    assert {"--jira", "PROJ-1", "--no-figma", "--confirm-file"} <= set(args["scope"])
    assert "--preset" in args["investigate"] and "fast" in args["investigate"] and "--ask" in args["investigate"]
    assert "--ask-review" in args["review"] and "--ask-review" not in args["investigate"]
    assert args["draft"] == ["--run-dir", str(rd.resolve())]
    summary = json.loads((rd / "logs" / "run.json").read_text())
    assert summary["status"] == "done" and summary["cost_usd_by_stage"]["scope"] == 0.1


def test_existing_inputs_skip_scope_and_from_starts_later(tmp_path, monkeypatch):
    calls = _stubs(monkeypatch)
    rd = tmp_path / "r"
    (rd / "raw").mkdir(parents=True)
    (rd / "raw" / "prd.md").write_text("x")
    (rd / "scope.json").write_text("{}")
    assert run.main(["--run-dir", str(rd)]) == 0
    assert [c[0] for c in calls] == ["investigate", "draft", "review", "revise"]
    calls.clear()
    assert run.main(["--run-dir", str(rd), "--from", "3"]) == 0
    assert [c[0] for c in calls] == ["review", "revise"]
    calls.clear()
    assert run.main(["--run-dir", str(rd), "--prd", "1", "--redo-scope"]) == 1  # a new PRD never goes into a run
    assert calls == []
    (rd / "logs").mkdir(exist_ok=True)
    (rd / "logs" / "run_args.json").write_text('{"prd": "1"}')
    assert run.main(["--run-dir", str(rd), "--redo-scope"]) == 0  # Scope again, on the PRD the run started with
    assert calls[0][0] == "scope"


def test_stops_at_a_failing_stage(tmp_path, monkeypatch):
    calls = _stubs(monkeypatch, fail="draft")
    rd = tmp_path / "r"
    assert run.main(["--prd", "1", "--run-dir", str(rd)]) == 1
    assert [c[0] for c in calls] == ["scope", "investigate", "draft"]
    assert json.loads((rd / "logs" / "run.json").read_text())["status"] == "stopped at draft"


def test_systemexit_from_a_stage_is_a_stop(tmp_path, monkeypatch):
    calls = _stubs(monkeypatch, fail="investigate", exit_with="investigate: the lead failed in 1a: boom")
    assert run.main(["--prd", "1", "--run-dir", str(tmp_path / "r")]) == 1
    assert [c[0] for c in calls] == ["scope", "investigate"]


def test_needs_prd_or_inputs(tmp_path, monkeypatch):
    _stubs(monkeypatch)
    assert run.main([]) == 1
    assert run.main(["--run-dir", str(tmp_path / "empty"), "--from", "2"]) == 1


# ---- the stages' old names (prep, stage1..stage5) keep working ----------------------------------------




def test_from_rejects_an_unknown_stage(capsys):
    import pytest

    with pytest.raises(SystemExit):
        run.build_parser().parse_args(["--from", "stage9"])
    assert "unknown stage" in capsys.readouterr().err






# ---- a new folder for every PRD run; --run-dir alone continues one ---------------------------------------------------


def _recorder(calls, name):
    """A stage stand-in that records its call and succeeds."""
    return lambda argv: calls.append((name, argv)) or 0


def _runs_root(tmp_path, monkeypatch):
    from rfc_agent import config
    from rfc_agent.scope import cli as scope_cli

    scope_cli._FRESH.clear()
    monkeypatch.setattr(config, "get_config", lambda: {"RUNS_ROOT": str(tmp_path / "runs")})
    return tmp_path / "runs"


def test_every_prd_run_gets_its_own_folder_even_for_the_same_prd(tmp_path, monkeypatch):
    calls = _stubs(monkeypatch)
    root = _runs_root(tmp_path, monkeypatch)
    from rfc_agent.scope import cli as scope_cli

    assert run.main(["--prd", "5541855972"]) == 0
    scope_cli._FRESH.clear()  # the second run is a new process
    (root / "prd-5541855972-19990101-000000").mkdir()  # an older run of the same PRD, untouched below
    assert run.main(["--prd", "5541855972"]) == 0
    folders = sorted(p.name for p in root.iterdir())
    assert len(folders) == 3 and all(f.startswith("prd-5541855972-") for f in folders)
    assert not any((root / "prd-5541855972-19990101-000000").iterdir())  # nothing was written into the older one
    assert sum(1 for c in calls if c[0] == "scope") == 2  # both ran Scope: neither skipped it as "already done"


def test_run_dir_alone_resumes_with_the_options_the_run_started_with(tmp_path, monkeypatch):
    calls = _stubs(monkeypatch, fail="draft")
    rd = tmp_path / "r"
    assert run.main(["--prd", "55", "--run-dir", str(rd), "--jira", "PROJ-1", "--preset", "fast", "--ask"]) == 1
    saved = json.loads((rd / "logs" / "run_args.json").read_text())
    assert saved == {"prd": "55", "jira": "PROJ-1", "preset": "fast", "ask": True}
    assert json.loads((rd / "logs" / "run.json").read_text())["status"] == "stopped at draft"
    calls.clear()
    monkeypatch.setattr(run, "_mains", lambda: {n: _recorder(calls, n) for n in run.STAGES})
    assert run.main(["--run-dir", str(rd)]) == 0  # no --prd: this is a resume
    assert [c[0] for c in calls] == ["investigate", "draft", "review", "revise"]  # its inputs exist: Scope is skipped
    # the options of the first call come back without being typed again
    assert "fast" in dict(calls)["investigate"] and "--ask" in dict(calls)["investigate"]


def test_a_run_that_stopped_in_scope_resumes_from_the_saved_prd(tmp_path, monkeypatch):
    calls = _stubs(monkeypatch, fail="scope")
    rd = tmp_path / "r"
    assert run.main(["--prd", "77", "--run-dir", str(rd)]) == 1
    (rd / "scope.json").unlink()  # a Scope that stopped before it wrote its inputs
    calls.clear()
    monkeypatch.setattr(run, "_mains", lambda: {n: _recorder(calls, n) for n in run.STAGES})
    assert run.main(["--run-dir", str(rd)]) == 0
    assert calls[0][0] == "scope" and calls[0][1][calls[0][1].index("--prd") + 1] == "77"


def test_prd_with_a_folder_that_already_holds_a_run_is_refused(tmp_path, monkeypatch, capsys):
    calls = _stubs(monkeypatch)
    rd = tmp_path / "r"
    assert run.main(["--prd", "1", "--run-dir", str(rd)]) == 0
    calls.clear()
    assert run.main(["--prd", "1", "--run-dir", str(rd)]) == 1
    assert "already holds a run" in capsys.readouterr().err and calls == []
    assert run.main(["--run-dir", str(tmp_path / "nothing-here")]) == 1  # nothing to resume
    assert "nothing to resume" in capsys.readouterr().err


def test_one_run_however_many_attempts_it_took(tmp_path, monkeypatch):
    _stubs(monkeypatch, fail="review")
    rd = tmp_path / "r"
    assert run.main(["--prd", "9", "--run-dir", str(rd)]) == 1
    first = json.loads((rd / "logs" / "run.json").read_text())
    assert (first["attempts"], first["resumes"]) == (1, 0)
    _stubs(monkeypatch)  # the cause is fixed
    assert run.main(["--run-dir", str(rd)]) == 0
    last = json.loads((rd / "logs" / "run.json").read_text())
    assert (last["attempts"], last["resumes"], last["status"]) == (2, 1, "done")
    assert last["seconds_all_attempts"] >= last["seconds_total"] + first["seconds_total"]
    kinds = [json.loads(x)["kind"] for x in (rd / "logs" / "attempts.jsonl").read_text().splitlines()]
    assert kinds == ["new", "resume"]


# ---- stopping a run -------------------------------------------------------------------------------------------------


def test_ctrl_c_stops_the_agents_and_records_the_attempt_as_stopped(tmp_path, monkeypatch, capsys):
    from rfc_agent.investigate import agent

    stopped = []
    monkeypatch.setattr(agent, "kill_live_agents", lambda: stopped.append(1) or 2)

    def stage(argv):
        rd = Path(argv[argv.index("--run-dir") + 1])
        (rd / "raw").mkdir(parents=True, exist_ok=True)
        (rd / "raw" / "prd.md").write_text("prd")
        (rd / "scope.json").write_text("{}")
        return 0

    def interrupted(argv):
        raise KeyboardInterrupt

    mains = {n: stage for n in run.STAGES} | {"draft": interrupted}
    monkeypatch.setattr(run, "_mains", lambda: mains)
    rd = tmp_path / "r"
    assert run.main(["--prd", "5", "--run-dir", str(rd)]) == 130
    assert stopped == [1] and "stopped 2 running agent(s)" in capsys.readouterr().err
    summary = json.loads((rd / "logs" / "run.json").read_text())
    assert summary["status"] == "stopped at draft" and (summary["attempts"], summary["resumes"]) == (1, 0)
    last = json.loads((rd / "logs" / "attempts.jsonl").read_text().splitlines()[-1])
    assert last["exit"] == 130 and last["status"] == "stopped at draft"
    # and it can be continued from the same folder, starting where it stopped
    mains["draft"] = stage
    assert run.main(["--run-dir", str(rd)]) == 0
    assert json.loads((rd / "logs" / "run.json").read_text())["resumes"] == 1


def test_a_plain_stop_is_handled_like_ctrl_c():
    import signal

    old = signal.getsignal(signal.SIGTERM)
    try:
        run._stop_on_sigterm()
        handler = signal.getsignal(signal.SIGTERM)
        try:
            handler(signal.SIGTERM, None)
            raise AssertionError("the stop signal should raise KeyboardInterrupt")
        except KeyboardInterrupt:
            pass
    finally:
        signal.signal(signal.SIGTERM, old)


def test_a_resume_also_gets_yes_and_confirm_file_back(tmp_path, monkeypatch):
    calls = _stubs(monkeypatch, fail="draft")
    rd = tmp_path / "r"
    assert run.main(["--prd", "55", "--run-dir", str(rd), "--yes", "--confirm-file"]) == 1
    saved = json.loads((rd / "logs" / "run_args.json").read_text())
    assert saved["yes"] is True and saved["confirm_file"] is True
    calls.clear()
    monkeypatch.setattr(run, "_mains", lambda: {n: _recorder(calls, n) for n in run.STAGES})
    assert run.main(["--run-dir", str(rd)]) == 0
    assert "--yes" in dict(calls)["investigate"] and "--confirm-file" in dict(calls)["investigate"]
