"""The `harness` CLI: the exit-code contract, the flag surface, and --json for agent callers.

Two audiences, both load-bearing. A human needs `flags --explain` to know which layer won and errors
that name the fix; an AGENT driving the harness needs --json on every read command, because the
alternative is guessing env vars. Both are tested here.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from harness import cli
from harness.constants import ExitCode

ROOT = Path(__file__).resolve().parents[2]


def _run(*args: str, env: dict | None = None) -> subprocess.CompletedProcess:
    """Drive the real console entry point in a subprocess — exit codes are the contract."""
    base = {"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin:/usr/local/bin",
            "HOME": str(Path.home()), "COLUMNS": "100"}
    return subprocess.run([sys.executable, "-m", "harness.cli", *args], capture_output=True,
                          text=True, cwd=ROOT, env={**base, **(env or {})})


# ---- discovery commands ----------------------------------------------------------------------------

def test_workflows_lists_the_discovered_workflows():
    res = _run("workflows", "--json")
    assert res.returncode == ExitCode.OK
    rows = json.loads(res.stdout)
    testing = next(r for r in rows if r["name"] == "testing")
    assert testing["default_pipeline"][0] == "sources"
    assert "execute_be" in testing["stages"] and testing["flags"] and testing["decisions"]


def test_engines_reports_names_and_doctor_status():
    res = _run("engines", "--json")
    assert res.returncode == ExitCode.OK
    rows = {r["name"]: r for r in json.loads(res.stdout)}
    assert {"claude-code", "replay", "pi"} <= set(rows)
    # replay cannot be built without a recording; that IS its doctor finding, and it must name the fix
    assert not rows["replay"]["healthy"]
    assert "harness record" in rows["replay"]["problems"][0]


def test_flags_json_is_the_machine_readable_registry():
    res = _run("flags", "--json")
    rows = {r["name"]: r for r in json.loads(res.stdout)}
    assert res.returncode == ExitCode.OK
    assert rows["engine"]["default"] == "claude-code"
    assert rows["testing.storage"]["env"] == "HARNESS_TESTING_STORAGE"
    assert rows["testing.storage"]["owner"] == "testing"
    for row in rows.values():
        assert row["help"], f"{row['name']} has no help text — an agent cannot decide to toggle it"


def test_flags_workflow_filter():
    res = _run("flags", "--json", "--workflow", "testing")
    names = {r["name"] for r in json.loads(res.stdout)}
    assert "testing.storage" in names
    assert "stage_engines" not in names, "--workflow shows what THIS workflow registered"


def test_flags_explain_shows_every_layer_and_the_winner():
    res = _run("flags", "--explain", "testing.storage", "--json",
               env={"HARNESS_TESTING_STORAGE": "kiwi"})
    payload = json.loads(res.stdout)
    assert payload["value"] == "kiwi" and payload["winning_layer"] == "env"
    assert payload["layers"]["default"] == "json" and payload["layers"]["env"] == "kiwi"


def test_flags_explain_human_output_marks_the_winner():
    res = _run("flags", "--explain", "engine")
    assert "<-- wins" in res.stdout and "claude-code" in res.stdout


def test_flags_explain_unknown_names_the_fix():
    res = _run("flags", "--explain", "nope")
    assert res.returncode == ExitCode.PIPELINE_CONFIG_ERROR
    assert "harness flags" in res.stderr


# ---- the exit-code contract ------------------------------------------------------------------------

def test_usage_errors_are_exit_1_not_2():
    """2 is GATE_FAILURE in the public contract; a typo'd subcommand must not read as a failed gate."""
    for args in (("run", "bogus"), ("bogus",), ("run", "testing", "--nope")):
        res = _run(*args)
        assert res.returncode == ExitCode.PIPELINE_CONFIG_ERROR, f"{args} -> {res.returncode}"


def test_unknown_flag_is_rejected_before_anything_runs():
    res = _run("run", "testing", "--set", "nope=1", "--feature", "x", "--no-interactive")
    assert res.returncode == ExitCode.PIPELINE_CONFIG_ERROR
    assert "unknown flag" in res.stderr and "engine" in res.stderr, "the error lists the real names"


def test_bad_stage_engine_syntax_names_the_shape():
    res = _run("run", "testing", "--stage-engine", "evaluate", "--feature", "x", "--no-interactive")
    assert res.returncode == ExitCode.PIPELINE_CONFIG_ERROR
    assert "STAGE=NAME" in res.stderr


def test_replay_without_a_recording_says_how_to_make_one():
    res = _run("run", "testing", "--engine", "replay", "--feature", "x", "--no-interactive")
    assert res.returncode == ExitCode.PIPELINE_CONFIG_ERROR
    assert "harness record" in res.stderr


def test_stages_subset_requires_a_run_dir():
    res = _run("run", "testing", "--stages", "evaluate", "--feature", "x", "--no-interactive")
    assert res.returncode == ExitCode.PIPELINE_CONFIG_ERROR
    assert "--run-dir" in res.stderr


def test_record_on_a_dir_that_is_not_a_run():
    res = _run("record", "/nonexistent-run-dir", "/tmp/rec-should-not-exist")
    assert res.returncode == ExitCode.PIPELINE_CONFIG_ERROR
    assert "manifest.json" in res.stderr


def test_record_builds_a_recording_and_prints_the_replay_command(tmp_path):
    run = tmp_path / "run"
    (run / "out").mkdir(parents=True)
    (run / "out" / "rfc.md").write_text("# RFC")
    (run / "manifest.json").write_text(json.dumps(
        {"generate": {"status": "ok", "outputs": ["out/rfc.md"], "duration_s": 1.0}}))
    res = _run("record", str(run), str(tmp_path / "rec"), "--json")
    assert res.returncode == ExitCode.OK
    payload = json.loads(res.stdout)
    assert payload["labels"] == ["generate"]
    assert (Path(payload["recording_dir"]) / "generate" / "outputs" / "out" / "rfc.md").is_file()
    plain = _run("record", str(run), str(tmp_path / "rec2"))
    assert "--engine replay" in plain.stdout and "replay.recording=" in plain.stdout


# ---- flag plumbing (in-process, no subprocess needed) ----------------------------------------------

def test_cli_sugar_folds_into_the_one_flag_dict():
    args = _ns(engine="pi", model="claude-opus-5", stage_engine=["review=claude-code"],
               disable_stage=["review", "generate"], set=["retries=false"], enable=["hooks"],
               disable=["green_skip"])
    sets = cli.cli_sets(args)
    assert sets["engine"] == "pi" and sets["model"] == "claude-opus-5"
    assert json.loads(sets["stage_engines"]) == {"review": "claude-code"}
    assert sets["disabled_stages"] == "review,generate"
    assert sets["retries"] == "false" and sets["hooks"] is True and sets["green_skip"] is False


def test_cli_sets_tolerates_a_frozen_alias_surface():
    """A workflow parser with no --set/--engine must not crash here."""
    assert cli.cli_sets(_ns()) == {}


def test_cli_sets_rejects_bad_stage_engine_syntax():
    with pytest.raises(ValueError, match="STAGE=NAME"):
        cli.cli_sets(_ns(stage_engine=["review"]))


def test_resolve_run_dir_precedence(tmp_path):
    hooks = cli.RunHooks(slug=lambda a: "slug")

    class Cfg:
        def get(self, key, default=None):
            return str(tmp_path / "configured") if key == "RUNS_ROOT" else default

    assert cli.resolve_run_dir(_ns(run_dir=str(tmp_path / "explicit")), Cfg(), hooks) == \
        tmp_path / "explicit"
    out = cli.resolve_run_dir(_ns(out=str(tmp_path / "out")), Cfg(), hooks)
    assert out.parent == tmp_path / "out" and out.name.endswith("-slug")
    cfg_rooted = cli.resolve_run_dir(_ns(), Cfg(), hooks)
    assert cfg_rooted.parent == tmp_path / "configured"
    assert cli.resolve_run_dir(_ns(), None, cli.RunHooks()).parent == Path("~/rfc-runs").expanduser()


def test_run_hooks_default_to_doing_nothing():
    """a harness must not launch desktop apps, write memory or notify on someone else's behalf."""
    hooks = cli.RunHooks()
    assert hooks.early_exit is hooks.prepare is hooks.before_run is hooks.after_run is None
    assert hooks.slug is None and hooks.compose is None and hooks.run is None


def _ns(**kw):
    import argparse

    base = {"no_interactive": False, "force": False, "json_out": False}
    return argparse.Namespace(**{**base, **kw})


# ---- the run flow, driven in-process with stub hooks -----------------------------------------------

class _Cfg:
    def __init__(self, **overrides):
        self._o = {"LANGFUSE.PUBLIC_KEY": "pk", "LANGFUSE.SECRET_KEY": "sk", **overrides}

    def get(self, key, default=None):
        return self._o.get(key, default)

    config: dict = {}


def _stub_hooks(tmp_path, cfg=None, ran=None, **extra):
    from harness.contracts import Stage, StageResult

    def engine(stage, ctx):
        for rel in stage.outputs:
            ctx.path(rel).parent.mkdir(parents=True, exist_ok=True)
            ctx.path(rel).write_text("x")
        return StageResult(engine_desc="stub")

    stages = [Stage(name="ingest", requires=(), outputs=("raw/prd.md",),
                    build_prompt=lambda c: "", gate=lambda c: [], engine=engine)]

    def run(s, ctx, **kw):
        if ran is not None:
            ran.append(kw)
        from rfc_agent.runner import run_pipeline

        run_pipeline(s, ctx, **kw)

    return cli.RunHooks(load_config=lambda: cfg or _Cfg(), compose=lambda names: stages,
                        run=run, slug=lambda a: "t", **extra)


def test_run_writes_flags_json_and_returns_ok(tmp_path):
    args = _ns(run_dir=str(tmp_path / "run"), no_interactive=True, set=["green_skip=false"],
               enable=[], disable=[], pipeline="ingest")
    ran: list = []
    code = cli.run_workflow("testing", args, _stub_hooks(tmp_path, ran=ran))
    assert code == ExitCode.OK
    dumped = json.loads((tmp_path / "run" / "flags.json").read_text())
    assert dumped["green_skip"]["value"] is False and dumped["green_skip"]["source"] == "cli"
    assert ran[0]["workflow"] == "testing" and ran[0]["flags"] is not None


def test_run_halts_before_spend_without_an_observability_sink(tmp_path):
    ran: list = []
    args = _ns(run_dir=str(tmp_path / "run"), no_interactive=True, pipeline="ingest")
    args.enable = ["obs_required"]
    cfg = _Cfg(**{"LANGFUSE.SECRET_KEY": ""})
    code = cli.run_workflow("testing", args, _stub_hooks(tmp_path, cfg=cfg, ran=ran))
    assert code == ExitCode.PIPELINE_CONFIG_ERROR and not ran


def test_obs_required_can_be_disabled_explicitly(tmp_path):
    ran: list = []
    args = _ns(run_dir=str(tmp_path / "run"), no_interactive=True, pipeline="ingest",
               set=[], enable=[], disable=["obs_required"])
    cfg = _Cfg(**{"LANGFUSE.PUBLIC_KEY": "", "LANGFUSE.SECRET_KEY": ""})
    assert cli.run_workflow("testing", args, _stub_hooks(tmp_path, cfg=cfg, ran=ran)) == ExitCode.OK
    assert ran, "an explicit opt-out runs untraced"


def test_early_exit_short_circuits_before_the_config_is_loaded(tmp_path):
    """`--steer` at a live run must work on a machine with no config.json."""
    loaded: list = []

    def boom():
        loaded.append(1)
        raise RuntimeError("config.json not found")

    hooks = cli.RunHooks(early_exit=lambda a: 0, load_config=boom)
    assert cli.run_workflow("testing", _ns(), hooks) == ExitCode.OK
    assert not loaded, "the config must not be touched on a path that never runs the pipeline"


def test_exit_codes_map_the_runner_failures(tmp_path):
    from harness.contracts import EngineFailure, GateFailure, PipelineConfigError, RunStopped

    cases = [(GateFailure("bad"), ExitCode.GATE_FAILURE, "gate_failed"),
             (EngineFailure("boom"), ExitCode.ENGINE_FAILURE, "engine_failed"),
             (PipelineConfigError("nope"), ExitCode.PIPELINE_CONFIG_ERROR, "config_error"),
             (RunStopped("ingest"), ExitCode.OK, "stopped")]
    for exc, expected, outcome in cases:
        seen: list = []

        def raiser(*a, exc=exc, **k):
            raise exc

        hooks = _stub_hooks(tmp_path)
        hooks.run = raiser
        hooks.after_run = lambda a, d, o, seen=seen: seen.append(o)
        args = _ns(run_dir=str(tmp_path / f"run-{outcome}"), no_interactive=True, pipeline="ingest")
        assert cli.run_workflow("testing", args, hooks) == expected
        assert seen == [outcome], "after_run must be told the real outcome"


def test_json_out_gives_an_agent_the_whole_result(tmp_path, capsys):
    args = _ns(run_dir=str(tmp_path / "run"), no_interactive=True, pipeline="ingest", json_out=True)
    code = cli.run_workflow("testing", args, _stub_hooks(tmp_path))
    payload = json.loads(capsys.readouterr().out)
    assert code == ExitCode.OK
    assert payload["outcome"] == "ok" and payload["exit_code"] == 0
    assert payload["workflow"] == "testing" and payload["pipeline"] == ["ingest"]
    assert payload["stages"] == {"ingest": "ok"} and payload["run_dir"].endswith("run")


def test_hooks_fire_in_the_documented_order(tmp_path):
    order: list = []
    hooks = _stub_hooks(tmp_path)
    hooks.early_exit = lambda a: order.append("early_exit") or None
    hooks.prepare = lambda a, p: order.append("prepare")
    hooks.before_run = lambda a, d: order.append("before_run")
    hooks.after_run = lambda a, d, o: order.append("after_run")
    args = _ns(run_dir=str(tmp_path / "run"), no_interactive=True, pipeline="ingest")
    cli.run_workflow("testing", args, hooks)
    assert order == ["early_exit", "prepare", "before_run", "after_run"]
