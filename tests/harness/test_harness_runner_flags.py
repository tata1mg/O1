"""run_pipeline's harness seams: the engine adapter, the resolved flags, and flags.json.

Everything here must hold WITHOUT changing the legacy contract: `run_pipeline(stages, ctx)` with no
adapter and no flags behaves exactly as it did before the kernel existed, which is what
tests/test_runner.py (untouched) pins. These tests cover what the new keyword arguments add.

The fake-engine pattern is tests/test_runner.py's, deliberately: the same stage shapes, so a divergence
between the legacy path and the adapter path shows up as a difference in these manifests.
"""

from __future__ import annotations

import json

import pytest

from harness import flags as flagmod
from rfc_agent.constants import Decision
from rfc_agent.runner import (
    PipelineConfigError,
    RunContext,
    Stage,
    StageResult,
    run_pipeline,
)


def _writer(tag: str = "content"):
    def _engine(stage: Stage, ctx: RunContext) -> StageResult:
        for rel in stage.outputs:
            p = ctx.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{tag} of {rel}")
        return StageResult(final_text="done", cost_usd=0.1, usage={"input_tokens": 1}, engine_desc="fake")

    return _engine


def _stages(gate_failures: dict[str, list[str]] | None = None, own_engine=True, **kw):
    gf = gate_failures or {}
    mk = (lambda: _writer()) if own_engine else (lambda: None)
    return [
        Stage(name="a", requires=("inputs.json",), outputs=("raw/one.md",),
              build_prompt=lambda ctx: "p", gate=lambda ctx, n="a": gf.get(n, []),
              engine=mk(), **kw),
        Stage(name="b", requires=("raw/one.md",), outputs=("out/two.md",),
              build_prompt=lambda ctx: "p", gate=lambda ctx, n="b": gf.get(n, []),
              engine=mk(), **kw),
    ]


@pytest.fixture
def ctx(tmp_path):
    (tmp_path / "inputs.json").write_text("{}")
    return RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=None)


def _flags(**overrides):
    return flagmod.resolve(cli_sets=overrides, env={})


def _decisions(ctx) -> list[dict]:
    p = ctx.run_dir / "logs" / "decisions.jsonl"
    if not p.exists():
        return []
    return [json.loads(x) for x in p.read_text().splitlines() if x.strip()]


# ---- the resolved run context ---------------------------------------------------------------------

def test_engine_flags_and_workflow_land_on_the_run_context(ctx):
    """Stages and fan-out engines must read the run's engine + toggles from ctx, never os.environ."""
    seen: dict = {}

    def probe(stage, c):
        seen["engine"], seen["flags"], seen["workflow"] = c.engine, c.flags, c.workflow
        return _writer()(stage, c)

    stages = [Stage(name="a", requires=("inputs.json",), outputs=("raw/one.md",),
                    build_prompt=lambda c: "p", gate=lambda c: [], engine=probe)]
    adapter = object()
    fl = _flags()
    run_pipeline(stages, ctx, engine=adapter, flags=fl, workflow="rfc")
    assert seen["engine"] is adapter and seen["flags"] is fl and seen["workflow"] == "rfc"


def test_flags_json_records_what_was_in_effect_and_which_layer_won(ctx):
    run_pipeline(_stages(), ctx, flags=_flags(retries=False, model="claude-opus-5"))
    dumped = json.loads((ctx.run_dir / "flags.json").read_text())
    assert dumped["retries"] == {"value": False, "source": "cli",
                                 "layers": {"default": True, "cli": False}}
    assert dumped["model"]["value"] == "claude-opus-5"
    assert dumped["engine"]["source"] == "default", "untouched flags record their default too"


def test_flags_json_is_written_before_any_stage_runs(ctx):
    """A run that dies in stage one must still be reproducible from flags.json."""
    def boom(stage, c):
        raise RuntimeError("died in stage one")

    stages = [Stage(name="a", requires=("inputs.json",), outputs=("raw/one.md",),
                    build_prompt=lambda c: "p", gate=lambda c: [], engine=boom)]
    with pytest.raises(RuntimeError):
        run_pipeline(stages, ctx, flags=_flags())
    assert (ctx.run_dir / "flags.json").is_file()


def test_no_flags_no_engine_leaves_the_legacy_contract_untouched(ctx):
    """Calling `run_pipeline(stages, ctx)` directly, with no flags or adapter, keeps working exactly."""
    run_pipeline(_stages(), ctx)
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert set(manifest) == {"a", "b"} and manifest["a"]["status"] == "ok"
    assert not (ctx.run_dir / "flags.json").exists(), "no flags resolved -> no flags.json invented"


# ---- engine resolution ----------------------------------------------------------------------------

def test_adapter_runs_stages_that_declare_no_engine(ctx):
    calls: list[str] = []

    class Adapter:
        name = "fake-adapter"

        def run_stage(self, stage, c):
            calls.append(stage.name)
            return _writer("adapter")(stage, c)

    run_pipeline(_stages(own_engine=False), ctx, engine=Adapter(), flags=_flags())
    assert calls == ["a", "b"]
    assert (ctx.run_dir / "raw" / "one.md").read_text().startswith("adapter")
    assert json.loads((ctx.run_dir / "manifest.json").read_text())["b"]["status"] == "ok"


def test_stage_owned_engine_wins_over_the_adapter(ctx):
    """A fan-out stage keeps its own engine and receives the adapter through ctx.engine."""
    class Adapter:
        name = "should-not-run"

        def run_stage(self, stage, c):
            raise AssertionError("a stage with its own engine must not be run by the adapter")

    run_pipeline(_stages(own_engine=True), ctx, engine=Adapter(), flags=_flags())
    assert json.loads((ctx.run_dir / "manifest.json").read_text())["a"]["status"] == "ok"


def test_stage_engines_flag_overrides_per_stage(ctx, monkeypatch):
    built: list[str] = []

    class Named:
        name = "pi"

        def run_stage(self, stage, c):
            built.append(stage.name)
            return _writer("pi")(stage, c)

    class Default:
        name = "claude-code"

        def run_stage(self, stage, c):
            return _writer()(stage, c)

    monkeypatch.setattr("harness.engines.engine_from_flags", lambda name, fl: Named())
    run_pipeline(_stages(own_engine=False), ctx, engine=Default(),
                 flags=_flags(stage_engines='{"b": "pi"}'))
    assert built == ["b"], "only the overridden stage goes to the override engine"
    assert (ctx.run_dir / "out" / "two.md").read_text().startswith("pi")
    assert (ctx.run_dir / "raw" / "one.md").read_text().startswith("content")


def test_an_engine_without_run_stage_fails_with_the_fix(ctx):
    class Halfling:
        name = "halfling"

        def run(self, req):
            raise AssertionError

    with pytest.raises(PipelineConfigError, match="run_stage"):
        run_pipeline(_stages(own_engine=False), ctx, engine=Halfling(), flags=_flags())


# ---- flags that narrow the loop -------------------------------------------------------------------

def test_disabled_stages_removes_a_stage_and_records_the_decision(ctx):
    run_pipeline(_stages(), ctx, flags=_flags(disabled_stages="b"))
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert set(manifest) == {"a"}
    kinds = [d.get("decision") for d in _decisions(ctx)]
    assert Decision.STAGE_DISABLED in kinds


def test_a_disabled_stage_does_not_silently_degrade_downstream(ctx):
    """`b` requires `a`'s output; disabling `a` must fail b's requires check loudly."""
    from rfc_agent.runner import EngineFailure

    with pytest.raises(EngineFailure, match="requires"):
        run_pipeline(_stages(), ctx, flags=_flags(disabled_stages="a"))


def test_retries_off_fails_on_the_first_red_gate(ctx):
    from rfc_agent.runner import GateFailure

    stages = _stages({"b": ["nope"]}, retry_on_gate_failure=2, revise_on_retry=True)
    with pytest.raises(GateFailure):
        run_pipeline(stages, ctx, flags=_flags(retries=False))
    assert json.loads((ctx.run_dir / "manifest.json").read_text())["b"]["attempts"] == 1


def test_retries_on_by_default_spends_the_stage_budget(ctx):
    from rfc_agent.runner import GateFailure

    stages = _stages({"b": ["nope"]}, retry_on_gate_failure=2, revise_on_retry=True)
    with pytest.raises(GateFailure):
        run_pipeline(stages, ctx, flags=_flags())
    assert json.loads((ctx.run_dir / "manifest.json").read_text())["b"]["attempts"] == 3


def test_green_skip_off_reruns_a_green_stage(ctx):
    run_pipeline(_stages(), ctx, flags=_flags())
    first = json.loads((ctx.run_dir / "manifest.json").read_text())["a"]["attempts"]
    run_pipeline(_stages(), ctx, flags=_flags())
    assert json.loads((ctx.run_dir / "manifest.json").read_text())["a"]["attempts"] == first, \
        "green_skip on (default) leaves a green stage alone"
    run_pipeline(_stages(), ctx, flags=_flags(green_skip=False))
    assert json.loads((ctx.run_dir / "manifest.json").read_text())["a"]["attempts"] == first + 1


def test_green_skip_on_records_the_skip_decision(ctx):
    run_pipeline(_stages(), ctx, flags=_flags())
    run_pipeline(_stages(), ctx, flags=_flags())
    assert Decision.GREEN_SKIP in [d.get("decision") for d in _decisions(ctx)]


def test_back_jumps_off_keeps_a_failure_in_its_own_stage(ctx):
    """With cross-stage retry disabled, a red `b` never re-runs `a`."""
    from rfc_agent.runner import GateFailure

    stages = _stages({"b": ["blocker"]}, retry_on_gate_failure=1)
    stages[1] = Stage(name="b", requires=("raw/one.md",), outputs=("out/two.md",),
                      build_prompt=lambda c: "p", gate=lambda c: ["blocker"],
                      engine=_writer(), retry_on_gate_failure=1, retry_stage="a")
    with pytest.raises(GateFailure):
        run_pipeline(stages, ctx, flags=_flags(back_jumps=False))
    manifest = json.loads((ctx.run_dir / "manifest.json").read_text())
    assert manifest["a"]["attempts"] == 1, "a must not be re-run by a back-jump that is switched off"
    assert Decision.BACK_JUMP not in [d.get("decision") for d in _decisions(ctx)]


def test_back_jumps_on_by_default_reruns_the_earlier_stage(ctx):
    from rfc_agent.runner import GateFailure

    stages = _stages()
    stages[1] = Stage(name="b", requires=("raw/one.md",), outputs=("out/two.md",),
                      build_prompt=lambda c: "p", gate=lambda c: ["blocker"],
                      engine=_writer(), retry_on_gate_failure=1, retry_stage="a")
    with pytest.raises(GateFailure):
        run_pipeline(stages, ctx, flags=_flags())
    assert Decision.BACK_JUMP in [d.get("decision") for d in _decisions(ctx)]


def test_revise_on_retry_off_wipes_outputs_between_attempts(ctx):
    """Revision mode keeps the previous outputs for targeted edits; with the flag off every attempt
    regenerates from zero, which is the from-scratch behaviour a benchmark arm may want."""
    seen: list[bool] = []

    def engine(stage, c):
        seen.append(c.path("out/two.md").exists())
        return _writer()(stage, c)

    from rfc_agent.runner import GateFailure

    stages = [
        Stage(name="a", requires=("inputs.json",), outputs=("raw/one.md",),
              build_prompt=lambda c: "p", gate=lambda c: [], engine=_writer()),
        Stage(name="b", requires=("raw/one.md",), outputs=("out/two.md",),
              build_prompt=lambda c: "p", gate=lambda c: ["nope"], engine=engine,
              retry_on_gate_failure=1, revise_on_retry=True),
    ]
    with pytest.raises(GateFailure):
        run_pipeline(stages, ctx, flags=_flags(revise_on_retry=False))
    assert seen == [False, False], "outputs wiped before every attempt"


def test_migration_state_is_false_when_the_source_cannot_be_read():
    """A warning is a courtesy; a stage must never fail to run because introspection did not work."""
    from rfc_agent.runner import _has_unmigrated_sites

    assert _has_unmigrated_sites(Stage(name="x", requires=(), outputs=(),
                                       build_prompt=lambda c: "", gate=lambda c: [],
                                       engine=None)) is False


def test_a_partly_migrated_stage_warns_when_a_non_default_engine_is_asked_for(ctx, capsys,
                                                                             monkeypatch):
    """A silent no-op would let an A/B believe a different engine ran the stage."""
    monkeypatch.setattr("rfc_agent.runner._has_unmigrated_sites", lambda stage: True)
    run_pipeline(_stages(own_engine=True), ctx, flags=_flags(stage_engines='{"b": "pi"}'))
    err = capsys.readouterr().err
    assert "[b] WARN engine='pi' is only PARTLY honoured" in err
    assert "TODO(harness-engine-seam)" in err


def test_a_fully_migrated_stage_does_not_warn(ctx, capsys, monkeypatch):
    """Warning about a stage that HAS migrated would train operators to ignore the warning."""
    monkeypatch.setattr("rfc_agent.runner._has_unmigrated_sites", lambda stage: False)
    run_pipeline(_stages(own_engine=True), ctx, flags=_flags(engine="pi"))
    assert "PARTLY honoured" not in capsys.readouterr().err


def test_no_warning_when_the_default_engine_runs_a_stage_owned_engine(ctx, capsys, monkeypatch):
    """The default IS claude-code, which is what an unmigrated call site binds — nothing to warn about."""
    monkeypatch.setattr("rfc_agent.runner._has_unmigrated_sites", lambda stage: True)
    run_pipeline(_stages(own_engine=True), ctx, flags=_flags())
    assert "PARTLY honoured" not in capsys.readouterr().err


def test_engine_call_falls_back_to_the_primitive_without_an_adapter(tmp_path, monkeypatch):
    """Every legacy caller and every hand-built RunContext keeps working: no adapter -> run_claude."""
    from rfc_agent import engine as rfc_engine

    seen: list = []
    monkeypatch.setattr(rfc_engine, "run_claude",
                        lambda *a, **k: seen.append((a[2], k.get("label"))) or "primitive")
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)
    stage = Stage(name="s", requires=(), outputs=(), build_prompt=lambda c: "", gate=lambda c: [])
    assert rfc_engine.engine_call(stage, ctx, "p", tmp_path / "l.jsonl", 10, label="s.child") == "primitive"
    assert seen == [("p", "s.child")]


def test_engine_call_routes_through_the_adapter_when_one_is_resolved(tmp_path):
    """The migrated call sites' contract: one EngineRequest per child, on the run's adapter."""
    from harness.engines import EngineResult
    from harness.events import TraceSummary
    from rfc_agent import engine as rfc_engine
    from rfc_agent.streams import StreamResult

    reqs: list = []

    class Adapter:
        name = "fake"

        def run(self, req):
            reqs.append(req)
            return EngineResult(summary=TraceSummary(final_text="from adapter"), duration_s=1.0,
                                engine_desc="fake:m", native=StreamResult(final_text="native"))

    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None, engine=Adapter())
    stage = Stage(name="s", requires=(), outputs=(), build_prompt=lambda c: "", gate=lambda c: [])
    out = rfc_engine.engine_call(stage, ctx, "prompt", tmp_path / "logs" / "s.stream.jsonl", 42,
                                 label="s.child", model="m1", effort="low", live=False)
    assert out.final_text == "native", "a claude-code adapter hands back the identical StreamResult"
    req = reqs[0]
    assert req.prompt == "prompt" and req.timeout_s == 42 and req.label == "s.child"
    assert req.model == "m1" and req.effort == "low" and req.live is False
    assert req.raw_trace_path == tmp_path / "logs" / "s.stream.jsonl"
    assert req.trace_path == tmp_path / "logs" / "s.child.events.jsonl"
    assert req.native_ctx == {"stage": stage, "ctx": ctx}


def test_engine_call_bridges_an_adapter_with_no_stream_result(tmp_path):
    """replay and pi have no StreamResult; the canonical summary is translated so the nine unmigrated
    call-site BODIES (which read .session_id, .is_error, .cost_usd) work unchanged after migration."""
    from harness.engines import EngineResult
    from harness.events import TraceSummary
    from rfc_agent import engine as rfc_engine

    summary = TraceSummary(final_text="done", cost_usd=None, session_id="sess-1", denials=2,
                           compactions=1, got_result=True, num_turns=4,
                           tool_calls=[{"name": "Read", "kind": "read", "input": {"path": "a"}}])

    class Adapter:
        name = "replay"

        def run(self, req):
            return EngineResult(summary=summary, duration_s=1.0, engine_desc="replay:rec",
                                native={"not": "a StreamResult"})

    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None, engine=Adapter())
    stage = Stage(name="s", requires=(), outputs=(), build_prompt=lambda c: "", gate=lambda c: [])
    out = rfc_engine.engine_call(stage, ctx, "p", tmp_path / "l.jsonl", 10, label="s.c")
    assert out.final_text == "done" and out.session_id == "sess-1"
    assert out.denials == 2 and out.compactions == 1 and out.num_turns == 4
    assert out.cost_usd is None, "no reported cost must stay None, never 0.0"
    assert out.tool_calls == [{"tool_name": "Read", "input": {"path": "a"}}], \
        "re-keyed to tool_name, which is what the drain and obs readers use"


def test_engine_stage_falls_back_and_routes(tmp_path, monkeypatch):
    from rfc_agent import engine as rfc_engine
    from rfc_agent.contracts import StageResult

    monkeypatch.setattr(rfc_engine, "claude_engine", lambda s, c: StageResult(engine_desc="primitive"))
    stage = Stage(name="s", requires=(), outputs=(), build_prompt=lambda c: "", gate=lambda c: [])
    bare = RunContext(run_dir=tmp_path, inputs={}, config=None)
    assert rfc_engine.engine_stage(stage, bare).engine_desc == "primitive"

    class Adapter:
        name = "fake"

        def run_stage(self, s, c):
            return StageResult(engine_desc="adapter")

    routed = RunContext(run_dir=tmp_path, inputs={}, config=None, engine=Adapter())
    assert rfc_engine.engine_stage(stage, routed).engine_desc == "adapter"
