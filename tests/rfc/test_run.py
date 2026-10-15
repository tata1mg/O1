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
    assert run.main(["--run-dir", str(rd), "--prd", "1", "--redo-scope"]) == 0
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




