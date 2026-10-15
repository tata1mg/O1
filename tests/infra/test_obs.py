"""obs must be a clean no-op when unconfigured — observability never fails a run."""

from pathlib import Path

from rfc_agent import obs
from rfc_agent.runner import RunContext, StageResult


class _NoLangfuseConfig:
    def get(self, dotted, default=None):
        return "" if dotted.startswith("LANGFUSE") else default


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(run_dir=tmp_path, inputs={}, config=_NoLangfuseConfig())


def _reset():
    obs._client = None  # reset the module cache
    obs._warned = False


def test_noop_when_unconfigured(tmp_path, capsys):
    _reset()
    ctx = _ctx(tmp_path)
    with obs.run_trace(ctx) as t:
        assert t is None
        with obs.stage_span(ctx, "ingest") as s:
            assert s is None
        with obs.agent_generation(ctx, "ingest") as g:
            assert g.agent is None and g.gen is None
            h = obs.tool_started(ctx, "Read", {"file_path": "x"})
            assert h is None
            obs.tool_finished(h, output="content")
            obs.finish_generation(g, StageResult())
        obs.record_gate(ctx, "ingest", [])
        obs.finish_run(ctx, {"ingest": {"status": "ok", "cost_usd": 0.1}})
    err = capsys.readouterr().err
    assert err.count("[obs] langfuse disabled") == 1  # warned exactly once


def test_pipeline_exceptions_propagate_through_obs_spans(tmp_path):
    """GateFailure must pass through run_trace/stage_span/agent_generation untouched
    (regression: 'generator didn't stop after throw()')."""
    import pytest

    from rfc_agent.runner import GateFailure

    _reset()
    ctx = _ctx(tmp_path)
    with pytest.raises(GateFailure, match="boom"):
        with obs.run_trace(ctx):
            with obs.stage_span(ctx, "x"), obs.agent_generation(ctx, "x"):
                raise GateFailure("boom")


def test_clip_truncates_deep_strings():
    big = "a" * 5000
    out = obs._clip({"cmd": big, "nested": [{"v": big}], "n": 7}, limit=100)
    assert out["n"] == 7
    assert len(out["cmd"]) < 200 and out["cmd"].endswith("chars]")
    assert out["nested"][0]["v"].startswith("a" * 100)


def test_langfuse_api_surface():
    """The exact SDK calls obs makes must exist with the kwargs we pass — every call sits inside
    suppress(Exception), so a renamed method fails SILENTLY in prod (regression: span.update_trace
    vanished in 4.14 and session/input were null on every trace)."""
    import inspect

    from langfuse import Langfuse, propagate_attributes
    from langfuse._client.span import LangfuseGeneration, LangfuseSpan

    assert {"session_id", "metadata", "trace_name"} <= set(inspect.signature(propagate_attributes).parameters)
    assert {"input", "output"} <= set(inspect.signature(LangfuseSpan.set_trace_io).parameters)
    assert {"input", "output"} <= set(inspect.signature(Langfuse.set_current_trace_io).parameters)
    assert {"name", "input", "output", "level", "trace_context"} <= set(
        inspect.signature(Langfuse.create_event).parameters)
    assert {"name", "value", "comment"} <= set(inspect.signature(Langfuse.score_current_trace).parameters)
    # cross-process attachment: TraceContext must keep the exact keys _env_trace_context builds
    import typing as _t

    from langfuse.types import TraceContext

    assert {"trace_id", "parent_span_id"} <= set(_t.get_type_hints(TraceContext))
    gen_update = set(inspect.signature(LangfuseGeneration.update).parameters)
    assert {"model", "input", "output", "usage_details", "cost_details", "metadata"} <= gen_update

    # typed observations (agent/tool/evaluator) — the Agent-Graph/best-practice surface obs relies on
    import typing

    from langfuse._client.span import LangfuseSpan as _Span

    for cls in (Langfuse, _Span):
        so = inspect.signature(cls.start_observation)
        assert {"name", "as_type", "input"} <= set(so.parameters), cls
        literals = typing.get_args(so.parameters["as_type"].annotation)
        allowed = {a for lit in literals for a in typing.get_args(lit)} | set(literals)
        assert {"agent", "tool", "evaluator", "generation"} <= allowed, f"{cls}: {allowed}"
    from langfuse._client.span import LangfuseAgent, LangfuseEvaluator, LangfuseTool

    for cls in (LangfuseAgent, LangfuseTool, LangfuseEvaluator):
        assert hasattr(cls, "update") and hasattr(cls, "end"), cls
    assert "as_type" in inspect.signature(LangfuseSpan.start_observation).parameters


def test_release_and_user_identity_helpers(tmp_path, monkeypatch):
    """Rollout signals (2026-08-11): `release` = installed plugin version (segments gate pass-rate,
    cost, latency per code version); `user_id` = explicit OBS.USER_ID else $USER — every teammate's
    traces identifiable in the Users view with zero setup on org laptops."""
    from rfc_agent import obs
    from rfc_agent.contracts import RunContext

    rel = obs._release()
    assert rel and rel.count(".") == 2  # reads the plugin.json version

    class _Cfg:
        def __init__(self, uid): self.uid = uid
        def get(self, k, d=None): return self.uid if k == "OBS.USER_ID" else d

    monkeypatch.setenv("USER", "ambient.login")
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=_Cfg("explicit.name"))
    assert obs._user_id(ctx) == "explicit.name"          # config wins (CI/shared boxes)
    monkeypatch.setattr(obs, "_git_email", "eng@example.com")
    ctx2 = RunContext(run_dir=tmp_path, inputs={}, config=_Cfg(None))
    assert obs._user_id(ctx2) == "eng@example.com"            # git email = canonical org identity
    monkeypatch.setattr(obs, "_git_email", "")            # no git identity on this machine
    assert obs._user_id(ctx2) == "ambient.login"          # $USER last resort


def test_claude_account_is_metadata_not_identity(tmp_path, monkeypatch):
    """The shared-Max-plan account flows as trace METADATA (subscription/cost attribution), sourced
    from the CLI's own .claude.json; a missing/corrupt file degrades to 'unknown', never an error."""
    import json as _json

    from rfc_agent import obs
    from rfc_agent.contracts import RunContext

    class _Cfg:
        def __init__(self, d): self.d = d
        def get(self, k, default=None): return str(self.d) if k == "AGENT.CLAUDE_CONFIG_DIR" else default

    (tmp_path / ".claude.json").write_text(_json.dumps({"oauthAccount": {"emailAddress": "shared@example.com"}}))
    monkeypatch.setattr(obs, "_claude_email", None)
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=_Cfg(tmp_path))
    assert obs._claude_account(ctx) == "shared@example.com"
    assert obs._claude_account(ctx) == "shared@example.com"   # cached second read

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(obs, "_claude_email", None)
    ctx2 = RunContext(run_dir=tmp_path, inputs={}, config=_Cfg(empty))
    assert obs._claude_account(ctx2) == "unknown"         # no CLI record -> degrade, don't raise


def test_operator_actions_flush_is_idempotent(tmp_path, monkeypatch):
    """The human half of the signal loop: command-layer choices land as operator:* events on the next
    invocation's trace, exactly once (offset marker), and a missing file is a clean no-op."""
    import json as _json

    from rfc_agent import obs
    from rfc_agent.contracts import RunContext

    events = []

    class _Obs:
        def end(self): pass

    class _FakeClient:
        def start_observation(self, *, name, as_type=None, input=None, **kw):
            events.append((name, input, as_type))
            return _Obs()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    (tmp_path / "logs").mkdir()
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)

    obs.operator_actions_flush(ctx)  # no file -> no-op
    assert events == []

    log = tmp_path / "logs" / "operator_actions.jsonl"
    log.write_text(_json.dumps({"action": "answers_given", "detail": {"answered": 2, "asked": 4}}) + "\n")
    obs.operator_actions_flush(ctx)
    assert [e[0] for e in events] == ["operator:answers_given"]
    assert events[0][2] == "chain"  # graphable: the human half of the loop must appear in the graph

    obs.operator_actions_flush(ctx)  # marker: nothing re-emitted
    assert len(events) == 1

    with log.open("a") as f:
        f.write(_json.dumps({"action": "rfc_rating", "detail": {"rating": 4}}) + "\n")
    obs.operator_actions_flush(ctx)  # only the NEW line flushes
    assert [e[0] for e in events] == ["operator:answers_given", "operator:rfc_rating"]


def test_thinking_digest_clips_head_and_tail(tmp_path):
    """The WHY layer on the LLM node: bounded digest — opening moves and conclusions both survive a
    38KB reasoning stream; absent file is a clean ''."""
    from rfc_agent import obs

    assert obs._thinking_digest(None) == ""
    assert obs._thinking_digest(tmp_path / "missing.txt") == ""
    p = tmp_path / "generate.thinking.txt"
    p.write_text("START " + "x" * 10000 + " END")
    d = obs._thinking_digest(p, cap=200)
    assert d.startswith("START") and d.endswith("END") and "clipped" in d and len(d) < 300
    p.write_text("short reasoning")
    assert obs._thinking_digest(p) == "short reasoning"


def test_decision_event_never_orphans(tmp_path, monkeypatch):
    """decision_event with NO ambient span and NO traceparent env must emit NOTHING — an observation
    without context mints an orphan trace with no session/user (observed 11/run from selfcheck,
    2026-08-12). decisions.jsonl remains the record."""
    from rfc_agent import obs

    calls = []

    class _Handle:
        def end(self):
            calls.append("end")

    class _FakeClient:
        def get_current_trace_id(self):
            return None

        def start_observation(self, **kw):
            calls.append(kw)
            return _Handle()

        def flush(self):
            calls.append("flush")

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.delenv(obs.TRACEPARENT_ENV, raising=False)
    obs.decision_event(_ctx(tmp_path), "generate", "qualified_bare_citations", "why", {"count": 3})
    assert calls == []  # skipped, not orphaned


def test_decision_event_attaches_via_env_traceparent(tmp_path, monkeypatch):
    """A subprocess (selfcheck) with RFC_AGENT_TRACEPARENT set attaches its decision to the LIVE
    trace via trace_context, and flushes (the process is short-lived)."""
    from rfc_agent import obs

    calls = []

    class _Handle:
        def end(self):
            calls.append("end")

    class _FakeClient:
        def get_current_trace_id(self):
            return None  # no ambient span in the subprocess

        def start_observation(self, **kw):
            calls.append(kw)
            return _Handle()

        def flush(self):
            calls.append("flush")

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setenv(obs.TRACEPARENT_ENV, "00-abc123trace-def456span-01")
    obs.decision_event(_ctx(tmp_path), "generate", "qualified_bare_citations", "why", {"count": 3})
    assert "flush" in calls
    ev = calls[0]
    assert ev["trace_context"] == {"trace_id": "abc123trace", "parent_span_id": "def456span"}
    # nested under a self-describing key: as_root promotion merges this into TRACE metadata, where a
    # bare `decision` key read as a trace-level fact and each new subprocess decision overwrote it
    assert "input" not in ev and ev["metadata"]["last_subprocess_decision"]["stage"] == "generate"
    assert ev["name"] == "decision:qualified_bare_citations"
    assert ev["as_type"] == "chain"  # graphable: Langfuse's agent graph excludes `event`


def test_decision_event_uses_ambient_span_when_present(tmp_path, monkeypatch):
    """Main process mid-run: ambient span wins — no trace_context, no forced flush."""
    from rfc_agent import obs

    calls = []

    class _Handle:
        def end(self):
            calls.append("end")

    class _FakeClient:
        def get_current_trace_id(self):
            return "livetrace"

        def start_observation(self, **kw):
            calls.append(kw)
            return _Handle()

        def flush(self):
            calls.append("flush")

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setenv(obs.TRACEPARENT_ENV, "00-other-span-01")  # ambient must still win
    obs.decision_event(_ctx(tmp_path), "review", "back_jump", "gate failed")
    assert "trace_context" not in calls[0] and calls[0]["name"] == "decision:back_jump"
    assert calls[0]["as_type"] == "chain" and "flush" not in calls  # ambient path never force-flushes


def test_tool_started_routes_through_parent_handle(tmp_path, monkeypatch):
    """Worker threads have no ambient OTEL context: an explicit parent handle must own the child
    tool observation (pre-0.22.36 review personas emitted nothing)."""
    from rfc_agent import obs

    via_client, via_parent = [], []

    class _FakeClient:
        def start_observation(self, **kw):
            via_client.append(kw)
            return "client-obs"

    class _FakeParent:
        def start_observation(self, **kw):
            via_parent.append(kw)
            return "parent-obs"

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setattr(obs, "_tool_events_left", 10)
    ctx = _ctx(tmp_path)
    assert obs.tool_started(ctx, "Read", {"f": "x"}, parent=_FakeParent()) == "parent-obs"
    assert obs.tool_started(ctx, "Read", {"f": "x"}) == "client-obs"
    assert via_parent[0]["as_type"] == "tool" and via_client[0]["as_type"] == "tool"


def test_finish_generation_normalizes_model_name(tmp_path, monkeypatch):
    """Langfuse model analytics key on EXACT names: '(3-persona parallel)' and '[1m]' decorations
    must be stripped from the model field; the full engine_desc survives in metadata."""
    from rfc_agent import obs
    from rfc_agent.runner import StageResult

    updates = {}

    class _FakeGen:
        def update(self, **kw):
            updates.update(kw)

    run = obs.AgentRun(agent=None, gen=_FakeGen(), thinking_path=None)
    res = StageResult()
    res.engine_desc = "claude:claude-opus-5 (3-persona parallel)"
    res.usage = {}
    res.tool_calls = []
    obs.finish_generation(run, res)
    assert updates["model"] == "claude-opus-5"
    assert updates["metadata"]["engine_desc"] == "claude:claude-opus-5 (3-persona parallel)"

    updates.clear()
    res.engine_desc = "claude:claude-sonnet-5[1m]"
    obs.finish_generation(run, res)
    assert updates["model"] == "claude-sonnet-5"


def test_traceparent_helpers_roundtrip(monkeypatch):
    """traceparent() emits W3C form; _env_trace_context parses it back to the TraceContext keys."""
    from rfc_agent import obs

    class _FakeClient:
        def get_current_trace_id(self):
            return "t" * 32

        def get_current_observation_id(self):
            return "s" * 16

    monkeypatch.setattr(obs, "_client", _FakeClient())
    tp = obs.traceparent()
    assert tp == f"00-{'t' * 32}-{'s' * 16}-01"
    monkeypatch.setenv(obs.TRACEPARENT_ENV, tp)
    assert obs._env_trace_context() == {"trace_id": "t" * 32, "parent_span_id": "s" * 16}
    monkeypatch.setenv(obs.TRACEPARENT_ENV, "garbage")
    assert obs._env_trace_context() is None


def test_graph_types_and_attempt_names(tmp_path, monkeypatch):
    """Langfuse builds its agent graph from every observation type EXCEPT span/event/generation, and its
    Aggregated view is one node per NAME. So the skeleton must be `chain`-typed, and anything that
    repeats per retry must carry the attempt in its name or three attempts collapse into one node."""
    from rfc_agent import obs

    opened = []

    class _Handle:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def end(self): pass
        def update(self, **kw): pass

    class _FakeClient:
        def start_as_current_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def get_current_trace_id(self): return "t"
        def score_current_trace(self, **kw): pass
        def flush(self): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)

    with obs.run_trace(ctx):
        pass
    with obs.stage_span(ctx, "generate", 2):
        pass
    with obs.agent_generation(ctx, "generate", 2):
        pass
    obs.record_gate(ctx, "generate", [], attempt=2)

    by_name = {o["name"]: o for o in opened}
    # 1. the skeleton is graphable
    assert by_name["rfc-run"]["as_type"] == "chain"
    assert by_name["stage:generate#2"]["as_type"] == "chain"
    # 2. everything that repeats per attempt is distinguishable
    assert {"stage:generate#2", "agent:generate#2", "llm:generate#2", "gate:generate#2"} <= set(by_name)
    assert by_name["stage:generate#2"]["metadata"]["attempt"] == 2
    # 3. attempt 1 stays unsuffixed — the single-attempt run reads clean
    opened.clear()
    with obs.stage_span(ctx, "ingest"):
        pass
    assert opened[0]["name"] == "stage:ingest"


def test_session_id_comes_from_identity_not_dir_name(tmp_path, monkeypatch):
    """Renaming a run dir must not split its session — session_id is read from identity.json."""
    from rfc_agent import identity, obs

    captured = {}

    class _Handle:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def set_trace_io(self, **kw): pass
        def end(self): pass

    class _FakeClient:
        def start_as_current_observation(self, **kw): return _Handle()
        def flush(self): pass

    def _fake_propagate(**kw):
        captured.update(kw)
        import contextlib as _c
        return _c.nullcontext()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setattr("langfuse.propagate_attributes", _fake_propagate)

    d = tmp_path / "PROJ-7-v1"
    d.mkdir()
    identity._cache.clear()
    with obs.run_trace(RunContext(run_dir=d, inputs={"jira": "PROJ-7"}, config=None)):
        pass
    first = captured["session_id"]

    moved = tmp_path / "PROJ-7-v1-renamed"
    d.rename(moved)
    identity._cache.clear()
    captured.clear()
    with obs.run_trace(RunContext(run_dir=moved, inputs={"jira": "PROJ-7"}, config=None)):
        pass
    assert captured["session_id"] == first == "PROJ-7-v1"
    assert captured["metadata"]["workstream_id"] == "PROJ-7"
    assert captured["metadata"]["harness_stage"] == "plan"


def test_generation_input_survives_a_large_prompt(tmp_path, monkeypatch):
    """A generation's input is the training half of every dataset item the annotate->dataset loop
    produces, so a clipped prompt corrupts the pair. Measured 18,564 chars against the old 20,000 cap."""
    from rfc_agent import obs
    from rfc_agent.runner import StageResult

    updates = {}

    class _FakeGen:
        def update(self, **kw): updates.update(kw)

    res = StageResult()
    res.engine_desc = "claude:claude-sonnet-5"
    res.usage, res.tool_calls = {}, []
    res.prompt = "P" * 60_000          # comfortably past the old cap, inside the new one
    res.final_text = "done"
    obs.finish_generation(obs.AgentRun(agent=None, gen=_FakeGen(), thinking_path=None), res)
    assert len(updates["input"]) == 60_000 and "…[+" not in updates["input"]

    updates.clear()
    res.prompt = "P" * (obs._GEN_INPUT_LIMIT + 5_000)   # past the new cap -> still bounded
    obs.finish_generation(obs.AgentRun(agent=None, gen=_FakeGen(), thinking_path=None), res)
    assert updates["input"].endswith("chars]") and len(updates["input"]) < obs._GEN_INPUT_LIMIT + 100


def test_gate_verdict_lands_on_the_stage_span(tmp_path, monkeypatch):
    """Annotating a single observation hides the rest of the trace (documented Langfuse limitation),
    so the attempt's verdict must travel with the attempt — on the stage span, which is the current
    observation when the gate runs (agent and generation have already closed)."""
    from rfc_agent import obs

    current, scores, trace_scores = {}, [], []

    class _Ev:
        def update(self, **kw): pass
        def end(self): pass
        def score(self, **kw): scores.append(kw)  # anchored to the gate observation (2026-08-20)

    class _FakeClient:
        def start_observation(self, **kw): return _Ev()
        def update_current_span(self, **kw): current.update(kw)
        def score_current_trace(self, **kw): trace_scores.append(kw)

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)

    obs.record_gate(ctx, "generate", ["blocker one", "blocker two"], attempt=2)
    md = current["metadata"]
    assert md["gate_passed"] is False and md["gate_failure_count"] == 2
    assert md["gate_failures"][0] == "blocker one"
    # 0.24.32 score-format audit: verdict is BOOLEAN (Langfuse's binary standard), and the
    # per-evaluation failure COUNT is its own score — the convergence gauge pass/fail flattened.
    # 0.24.33: plus the attempt TERMINAL STATE (xUnit failure≠error) — record_gate covers the
    # evaluated outcomes; the runner's crash paths emit engine_failed into the same family.
    assert scores == [
        {"name": "gate_generate_passed", "value": 0.0, "data_type": "BOOLEAN"},
        {"name": "gate_generate_failure_count", "value": 2.0},
        {"name": "generate_attempt_outcome", "value": "gate_failed", "data_type": "CATEGORICAL"},
    ]

    current.clear()
    scores.clear()
    obs.record_gate(ctx, "ingest", [])
    assert current["metadata"] == {"gate_passed": True, "gate_failure_count": 0, "gate_failures": []}
    assert scores == [
        {"name": "gate_ingest_passed", "value": 1.0, "data_type": "BOOLEAN"},
        {"name": "gate_ingest_failure_count", "value": 0.0},  # zeros charted, deliberately
        {"name": "ingest_attempt_outcome", "value": "passed", "data_type": "CATEGORICAL"},
    ]
    assert trace_scores == []  # anchored on the evaluator, never doubled at bare trace level

    # a crashed attempt never reaches record_gate — record_attempt_outcome is the availability
    # signal (previously NOTHING was emitted: pass-rate averages silently excluded crashes —
    # survivorship bias, 5 of radiology-v3's 7 review attempts were invisible)
    obs.record_attempt_outcome(ctx, "review", "engine_failed", attempt=7)
    assert trace_scores == [{
        "name": "review_attempt_outcome", "value": "engine_failed", "data_type": "CATEGORICAL",
        "comment": "attempt 7 ended without a gate evaluation: engine_failed"}]
    # ...and gate_review_passed was deliberately NOT emitted: a crash is an error, not a
    # gate verdict (emitting 0 would poison the gate-quality trend whenever the harness is flaky)
    assert not any(k.get("name") == "gate_review_passed" for k in trace_scores)


def test_run_tags_ticket_and_context_window(tmp_path):
    """Tags are Langfuse's filter chips (metadata is not filterable the same way). The ticket tag must
    survive the case that breaks any derived test: a run dir NAMED after its own ticket."""
    from rfc_agent import obs

    class _Cfg:
        def __init__(self, default="", stage=None): self.d, self.s = default, stage or {}
        def get(self, k, default=None):
            return {"AGENT.DEFAULT_MODEL": self.d, "AGENT.STAGE_MODELS": self.s}.get(k, default)

    ctx = RunContext(run_dir=tmp_path / "PROJ-3394", inputs={}, config=_Cfg("claude-sonnet-5[1m]"))
    # the collision case: workstream_id == session_id, yet it IS a real ticket
    tags = obs._run_tags(ctx, {"workstream_id": "PROJ-3394", "workstream_source": "jira",
                               "session_id": "PROJ-3394"})
    assert tags == ["ticket:PROJ-3394", "ctx:1m"]

    # a label-derived workstream is not a ticket and must not be tagged as one
    tags = obs._run_tags(ctx, {"workstream_id": "2026-08-12-ai-overview", "workstream_source": "label"})
    assert tags == ["ctx:1m"]

    # 200K config -> no ctx tag; a per-investigateM override -> tagged
    plain = RunContext(run_dir=tmp_path, inputs={}, config=_Cfg("claude-sonnet-5"))
    assert obs._run_tags(plain, {"workstream_source": "label"}) == []
    staged = RunContext(run_dir=tmp_path, inputs={}, config=_Cfg("claude-sonnet-5", {"review": "claude-opus-5[1m]"}))
    assert obs._run_tags(staged, {"workstream_source": "label"}) == ["ctx:1m"]

    # CLI --model override is honoured, and a missing config never raises
    cli = RunContext(run_dir=tmp_path, inputs={"model": "claude-opus-5[1m]"}, config=None)
    assert obs._run_tags(cli, {"workstream_source": "label"}) == ["ctx:1m"]


def test_ctx1m_tag_respects_cli_model_precedence(tmp_path):
    """engine.run_claude gives CLI --model absolute precedence over DEFAULT_MODEL/STAGE_MODELS, so a
    200K --model run must NOT be tagged ctx:1m just because the ignored default carries [1m]
    (2026-08-16 audit: OR-ing across all sources produced exactly that false positive)."""
    from rfc_agent import obs

    class _Cfg:
        def get(self, k, d=None):
            return {"AGENT.DEFAULT_MODEL": "claude-sonnet-5[1m]",
                    "AGENT.STAGE_MODELS": {"review": "claude-opus-5[1m]"}}.get(k, d)

    ident = {"workstream_source": "label"}
    # CLI overrides to 200K -> config's [1m] is dead config, no tag
    ctx = RunContext(run_dir=tmp_path, inputs={"model": "claude-sonnet-5"}, config=_Cfg())
    assert obs._run_tags(ctx, ident) == []
    # CLI itself is 1m -> tagged
    ctx = RunContext(run_dir=tmp_path, inputs={"model": "claude-opus-5[1m]"}, config=_Cfg())
    assert obs._run_tags(ctx, ident) == ["ctx:1m"]
    # no CLI override -> config judged as before
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=_Cfg())
    assert obs._run_tags(ctx, ident) == ["ctx:1m"]


def test_finish_run_scores_rework_only_for_stages_this_invocation_ran(tmp_path, monkeypatch):
    """The manifest is cumulative across resumes: without the `ran` filter every later invocation
    re-emitted every earlier stage's rework score (audit: ingest_rework_attempts 5x on one lineage),
    so any dashboard SUM/AVG double-counted."""
    from rfc_agent import obs

    scores = []

    class _FakeClient:
        def set_current_trace_io(self, **kw): pass
        def score_current_trace(self, **kw): scores.append(kw["name"])

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)
    manifest = {
        "ingest": {"status": "ok", "attempts": 2, "cost_usd": 1.0, "duration_s": 10},   # earlier invocation
        "generate": {"status": "ok", "attempts": 3, "cost_usd": 2.0, "duration_s": 20}, # ran now
    }
    obs.finish_run(ctx, manifest, ran={"generate"})
    assert "generate_rework_attempts" in scores
    assert "ingest_rework_attempts" not in scores      # resume must not re-score untouched stages
    assert "run_total_cost_usd" in scores              # per-trace snapshot stays

    scores.clear()
    obs.finish_run(ctx, manifest, ran=None)            # legacy path: emit all
    assert "ingest_rework_attempts" in scores


def test_subagent_inherits_ambient_attempt_and_reports_outcome(tmp_path, monkeypatch):
    """Fan-out workers repeat per stage attempt; without the ambient suffix the Aggregated view
    collapsed `shard:lab_buddy` from attempts 1 and 2 into one (1/2) node (HEA-TAX12 smoke, live).
    And an ended agent node with no output reads as unfinished — subagent_finished closes with one."""
    from rfc_agent import obs

    opened, updated = [], []

    class _Handle:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def update(self, **kw): updated.append(kw)
        def end(self): pass

    class _FakeClient:
        def start_as_current_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)

    with obs.agent_generation(ctx, "ground", attempt=2):
        obs.subagent_span(ctx, "shard:lab_buddy", "one shard")
    names = [o["name"] for o in opened]
    assert "shard:lab_buddy#2" in names            # inherits the ambient attempt
    assert obs._current_attempt == 1               # reset on exit — never leaks to the next stage

    opened.clear()
    with obs.agent_generation(ctx, "review", attempt=1):
        obs.subagent_span(ctx, "persona:skeptic")
    assert "persona:skeptic" in [o["name"] for o in opened]  # attempt 1 stays unsuffixed

    h = _Handle()
    obs.subagent_finished(h, "wrote review/persona_skeptic.md (4,213 bytes)")
    assert updated == [{"output": "wrote review/persona_skeptic.md (4,213 bytes)"}]
    obs.subagent_finished(None, "ignored")         # None handle: clean no-op


def test_tool_duration_is_stream_true(tmp_path, monkeypatch):
    """The CLI stamps every assistant/user event with a ms timestamp; the tool span must end at
    otel_start + (ts_result - ts_use) so its DURATION is exact regardless of the drain poll."""
    from rfc_agent import obs

    ended = []

    class _Otel:
        start_time = 5_000_000_000_000  # ns

    class _Handle:
        _otel_span = _Otel()
        def update(self, **kw): pass
        def end(self, **kw): ended.append(kw)

    class _FakeClient:
        def start_observation(self, **kw): return _Handle()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setattr(obs, "_tool_events_left", 10)
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=None)

    h = obs.tool_started(ctx, "Read", {"f": "x"}, event_ts="2026-08-14T16:53:13.699Z")
    obs.tool_finished(h, output="ok", event_ts="2026-08-14T16:53:13.713Z")  # 14ms later
    assert ended == [{"end_time": 5_000_000_000_000 + 14_000_000}]  # exactly 14ms after otel start

    # missing result timestamp (run-end sweep of unanswered calls) -> plain end, drain clock
    ended.clear()
    h = obs.tool_started(ctx, "Read", {"f": "x"}, event_ts="2026-08-14T16:53:13.699Z")
    obs.tool_finished(h, output=None)
    assert ended == [{}]

    # garbled start timestamp -> plain end
    ended.clear()
    h = obs.tool_started(ctx, "Read", {"f": "x"}, event_ts="not-a-time")
    obs.tool_finished(h, output="ok", event_ts="2026-08-14T16:53:13.713Z")
    assert ended == [{}]

    # negative delta (clock weirdness) must never emit a lying span
    ended.clear()
    h = obs.tool_started(ctx, "Read", {"f": "x"}, event_ts="2026-08-14T16:53:13.713Z")
    obs.tool_finished(h, output="ok", event_ts="2026-08-14T16:53:13.699Z")
    assert ended == [{}]


def test_event_ns_parses_cli_timestamps():
    from rfc_agent import obs

    assert obs._event_ns("2026-08-14T16:53:13.699Z") == 1786726393699000000
    assert obs._event_ns(None) is None and obs._event_ns("") is None and obs._event_ns("garbage") is None


def test_decision_event_attempt_suffix(tmp_path, monkeypatch):
    """Decisions are attempt-suffixed like stages/gates: unsuffixed repeating decisions were the
    graph's time-fusion anchors (one revise_in_place node at steps 2 AND 195, T3 replay 2026-08-17).
    Explicit attempt wins over ambient; attempt 1 stays unsuffixed."""
    from rfc_agent import obs

    calls = []

    class _Handle:
        def end(self):
            pass

    class _FakeClient:
        def get_current_trace_id(self):
            return "livetrace"

        def start_observation(self, **kw):
            calls.append(kw)
            return _Handle()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    obs.decision_event(_ctx(tmp_path), "generate", "revise_in_place", "w", attempt=3)
    obs.decision_event(_ctx(tmp_path), "generate", "revise_in_place", "w", attempt=1)
    monkeypatch.setattr(obs, "_current_attempt", 2)  # ambient fallback (engine/stage call sites)
    obs.decision_event(_ctx(tmp_path), "ground", "ground_fanout", "w")
    assert [c["name"] for c in calls] == [
        "decision:revise_in_place#3", "decision:revise_in_place", "decision:ground_fanout#2"]


def test_gate_started_handle_carries_true_latency(tmp_path, monkeypatch):
    """gate_started opens the evaluator BEFORE evaluation; record_gate(handle=) finishes it with the
    verdict — the observation's window covers real gate work instead of rendering 0.00s."""
    from rfc_agent import obs

    events = []

    class _Handle:
        def update(self, **kw):
            events.append(("update", kw))

        def end(self):
            events.append(("end", {}))

    class _FakeClient:
        def start_observation(self, **kw):
            events.append(("start", kw))
            return _Handle()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    h = obs.gate_started(_ctx(tmp_path), "ground", attempt=2)
    assert events[0][0] == "start" and events[0][1]["name"] == "gate:ground#2"
    assert events[0][1]["as_type"] == "evaluator"
    obs.record_gate(_ctx(tmp_path), "ground", ["f1"], attempt=2, handle=h)
    # no second start_observation: the handle is reused, so start-time = pre-evaluation
    assert [e[0] for e in events] == ["start", "update", "end"]
    upd = events[1][1]
    assert upd["input"] == {"failure_count": 1, "checks_inert": []}
    assert upd["level"] == "ERROR" and upd["output"] == ["f1"]


def test_stage_span_carries_identifying_input(tmp_path, monkeypatch):
    """Stage nodes showed Input/Output: undefined to annotators (census 2026-08-17: 13/13 empty).
    stage_span now sets input {stage, attempt, mode}."""
    from contextlib import nullcontext

    from rfc_agent import obs

    calls = []

    class _FakeClient:
        def start_as_current_observation(self, **kw):
            calls.append(kw)
            return nullcontext()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    with obs.stage_span(_ctx(tmp_path), "ground", attempt=3, mode="revision"):
        pass
    assert calls[0]["input"] == {"stage": "ground", "attempt": 3, "mode": "revision"}
    assert calls[0]["name"] == "stage:ground#3"


def test_record_gate_sets_stage_output_verdict(tmp_path, monkeypatch):
    """The gate merge stamps a one-line verdict as the STAGE span's output — self-describing for
    annotators; previously only metadata."""
    from rfc_agent import obs

    merged = {}

    class _Handle:
        def update(self, **kw): pass
        def end(self): pass

    class _FakeClient:
        def start_observation(self, **kw): return _Handle()
        def update_current_span(self, **kw): merged.update(kw)
        def score_current_trace(self, **kw): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    obs.record_gate(_ctx(tmp_path), "ground", ["findings.md: citation NOT witnessed"], attempt=2)
    assert merged["output"].startswith("gate_failed: 1 failure(s) — findings.md: citation NOT")
    merged.clear()
    obs.record_gate(_ctx(tmp_path), "ground", [], attempt=3)
    assert merged["output"] == "gate passed"


def test_subagent_span_detail_becomes_input(tmp_path, monkeypatch):
    """Shards were the one agent class with input:null — detail (scope) now rides as input."""
    from rfc_agent import obs

    calls = []

    class _Handle:
        pass

    class _FakeClient:
        def start_observation(self, **kw):
            calls.append(kw)
            return _Handle()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    obs.subagent_span(_ctx(tmp_path), "shard:svc_a", "repos=svc_a; reqs=REQ-1,REQ-9")
    assert calls[0]["input"] == {"detail": "repos=svc_a; reqs=REQ-1,REQ-9"}


def test_every_observation_carries_stage_metadata(tmp_path, monkeypatch):
    """0.22.64: an observation seen in isolation (annotation queue, API, dataset provenance) must be
    self-describing — tools, subagents, generations and decisions all stamp their own
    metadata.{stage, attempt}; outside a stage the ambient is None and nothing is fabricated."""
    from rfc_agent import obs

    opened = []

    class _Handle:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def update(self, **kw): pass
        def end(self): pass

    class _FakeClient:
        def start_as_current_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def get_current_trace_id(self): return "abc123"

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setattr(obs, "_tool_events_left", 10)
    ctx = _ctx(tmp_path)

    with obs.agent_generation(ctx, "generate", attempt=3):
        obs.tool_started(ctx, "Bash", {"command": "ls"})
        obs.subagent_span(ctx, "shard:svc_a")
        obs.decision_event(ctx, "generate", "patch_revision", "mechanical failures only")

    by_name = {o["name"]: o for o in opened}
    md = {"stage": "generate", "attempt": 3}
    # >= : graph identity (langgraph_node/step) legitimately rides along since 0.22.67
    assert md.items() <= by_name["llm:generate#3"]["metadata"].items()
    assert md.items() <= by_name["tool:Bash"]["metadata"].items()
    assert md.items() <= by_name["shard:svc_a#3"]["metadata"].items()
    assert md.items() <= by_name["decision:patch_revision#3"]["metadata"].items()
    assert by_name["decision:patch_revision#3"]["input"]["stage"] == "generate"  # payload keeps it too

    # outside any stage: ambient is cleared, tools carry no fabricated stage
    obs.tool_started(ctx, "Read", {"file_path": "x"})
    assert opened[-1]["name"] == "tool:Read" and opened[-1]["metadata"] is None


def test_worker_thread_tools_inherit_main_thread_otel_context(tmp_path, monkeypatch):
    """0.22.66: propagated identity (run_id/session/user) lives in the MAIN thread's OTel context and
    does not cross into fan-out worker threads — persona/shard tool spans exported without it (UI
    showed 17 metadata items on a generate tool vs 9 on a persona tool; the public API hides the
    difference by stripping trace-scoped keys). tool_started must attach the stage's context
    snapshot around worker-side creation."""
    import threading

    from opentelemetry import context as otel_ctx

    from rfc_agent import obs

    seen = {}

    class _Handle:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def start_observation(self, **kw): return _Handle()
        def update(self, **kw): pass
        def end(self): pass

    class _FakeParent:
        def start_observation(self, **kw):
            seen["in_worker"] = otel_ctx.get_value("rfc-test-identity")
            return "h"

    class _FakeClient:
        def start_as_current_observation(self, **kw): return _Handle()
        def start_observation(self, **kw): return _Handle()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setattr(obs, "_tool_events_left", 10)
    ctx = _ctx(tmp_path)

    token = otel_ctx.attach(otel_ctx.set_value("rfc-test-identity", "RUN-XYZ"))
    try:
        with obs.agent_generation(ctx, "review", attempt=2):
            t = threading.Thread(target=lambda: obs.tool_started(ctx, "Bash", {"c": "ls"}, parent=_FakeParent()))
            t.start()
            t.join()
    finally:
        otel_ctx.detach(token)

    assert seen["in_worker"] == "RUN-XYZ"     # worker saw the main thread's propagated context
    assert obs._stage_otel_ctx is None        # snapshot cleared on stage exit — never leaks


def test_declared_graph_identity(tmp_path, monkeypatch):
    """0.22.67/68: the Aggregated graph is DECLARED via langgraph_node/step. Node-rich lanes: every
    observation keeps its own box with lane-unique names (tool nodes are per-name PER LANE, so
    nothing merges across stages — the arrow-spaghetti cause), sub-steps order the lane
    stage->agent->llm->workers->tools->gate, and runner decisions between lanes get their own
    sequential steps so control flow reads gate -> decision:back_jump -> next stage."""
    import json as _json

    from rfc_agent import obs

    opened = []

    class _Handle:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def update(self, **kw): pass
        def end(self): pass

    class _FakeClient:
        def start_as_current_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def get_current_trace_id(self): return "abc"
        def update_current_span(self, **kw): pass
        def score_current_trace(self, **kw): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setattr(obs, "_tool_events_left", 50)
    monkeypatch.setattr(obs, "_step_counter", 0)
    obs._node_steps.clear()
    ctx = _ctx(tmp_path)

    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "operator_actions.jsonl").write_text(_json.dumps({"action": "answers_given"}) + "\n")
    obs.operator_actions_flush(ctx)

    with obs.stage_span(ctx, "review", 1):
        with obs.agent_generation(ctx, "review", 1):
            obs.subagent_span(ctx, "persona:skeptic")
            obs.tool_started(ctx, "Bash", {"c": "ls"})
            obs.decision_event(ctx, "review", "patch_revision", "mid-lane engine decision")
        obs.record_gate(ctx, "review", ["blocker"], attempt=1)
    obs.decision_event(ctx, "review", "back_jump", "between lanes", attempt=1)
    with obs.stage_span(ctx, "generate", 2):
        pass

    ns = {o["name"]: o["metadata"].get("langgraph_node") for o in opened if o.get("metadata")}
    st = {o["name"]: o["metadata"].get("langgraph_step") for o in opened if o.get("metadata")}
    # per-action operator nodes (0.24.5): the collapsed "operator" box hid WHAT the human did
    assert (ns["operator:answers_given"], st["operator:answers_given"]) == ("operator:answers_given", 1)
    assert (ns["stage:review"], st["stage:review"]) == ("stage:review", 2)
    assert (ns["agent:review"], st["agent:review"]) == ("agent:review", 3)
    assert (ns["llm:review"], st["llm:review"]) == ("llm:review", 4)
    assert (ns["persona:skeptic"], st["persona:skeptic"]) == ("persona:skeptic", 5)   # shared worker row
    assert (ns["tool:Bash"], st["tool:Bash"]) == ("tool:Bash@review", 6)        # first use
    assert (ns["decision:patch_revision"], st["decision:patch_revision"]) == ("decision:patch_revision", 7)
    assert (ns["gate:review"], st["gate:review"]) == ("gate:review", 8)
    assert (ns["decision:back_jump"], st["decision:back_jump"]) == ("decision:back_jump", 9)   # between lanes
    assert (ns["stage:generate#2"], st["stage:generate#2"]) == ("stage:generate#2", 10)  # next lane
    # start marker (STAGE only): a zero-duration observation that makes the lane visible MID-RUN,
    # while the real spans (which export only on close) are still open
    assert (ns["start:stage:review"], st["start:stage:review"]) == ("stage:review", 2)
    assert "start:agent:review" not in ns and "start:llm:review" not in ns


def test_thinking_digest_is_per_attempt_and_per_worker(tmp_path, monkeypatch):
    """0.22.73: the thinking tap APPENDS across attempts/invocations, so digests must start at the
    attempt's own byte offset (generate#8 and #9 shared an attempt-1 head, 356KB file). And fan-out
    workers' thinking (548KB across 3 persona files while llm:review carried a 0-char digest) is
    attached to the persona/shard span at subagent_finished via a breadcrumb from subagent_span."""
    from rfc_agent import obs

    logs = tmp_path / "logs"
    logs.mkdir()

    # offset-aware digest primitive
    f = logs / "generate.thinking.txt"
    f.write_bytes(b"OLD-ATTEMPT-THINKING\n")
    off = f.stat().st_size
    f.write_bytes(f.read_bytes() + b"NEW ATTEMPT REASONING")
    assert obs._thinking_digest(f, start=off) == "NEW ATTEMPT REASONING"
    assert obs._thinking_digest(f, start=10**9).startswith("OLD")  # shrunk/recreated -> whole file

    # agent_generation captures the offset at open
    class _Handle:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def start_observation(self, **kw): return _Handle()
        def update(self, **kw): pass
        def end(self): pass

    class _FakeClient:
        def start_as_current_observation(self, **kw): return _Handle()
        def start_observation(self, **kw): return _Handle()

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = _ctx(tmp_path)
    with obs.agent_generation(ctx, "generate", 2) as run:
        assert run.thinking_offset == f.stat().st_size

    # worker breadcrumb: span captures (path, offset); finished attaches ONLY the new digest
    updated = []

    class _Recorder(_Handle):
        def update(self, **kw): updated.append(kw)

    monkeypatch.setattr(obs, "_get_client", lambda ctx: type("C", (), {
        "start_observation": lambda self, **kw: _Recorder()})())
    wf = logs / "review.skeptic.thinking.txt"
    wf.write_text("stale round one thinking\n")
    with obs.agent_generation(ctx, "review", 1):
        h = obs.subagent_span(ctx, "persona:skeptic", thinking_path=wf)
    wf.write_text(wf.read_text() + "round two: verified CHANGE-13 drops [:default]")
    obs.subagent_finished(h, "wrote findings")
    assert updated[-1]["output"] == "wrote findings"
    assert updated[-1]["metadata"]["thinking_digest"] == "round two: verified CHANGE-13 drops [:default]"


def test_finish_generation_never_writes_cache_as_model(tmp_path):
    """All-cache ground runs have engine_desc 'cache (…)' — 'cache' must not enter model analytics."""
    _reset()
    updates = []

    class _Gen:
        def update(self, **kw): updates.append(kw)
        def end(self): pass

    run = obs.AgentRun(agent=None, gen=_Gen())
    obs.finish_generation(run, StageResult(engine_desc="cache (5-shard reuse, no invocations)"))
    assert "model" not in updates[-1]
    obs.finish_generation(run, StageResult(engine_desc="claude:claude-sonnet-5[1m]"))
    assert updates[-1]["model"] == "claude-sonnet-5"


def test_finish_generation_carries_denials_and_compactions(tmp_path):
    _reset()
    updates = []

    class _Gen:
        def update(self, **kw): updates.append(kw)
        def end(self): pass

    run = obs.AgentRun(agent=None, gen=_Gen())
    obs.finish_generation(run, StageResult(engine_desc="claude:m", denials=3, compactions=1,
                                           context_tokens=170002))
    md = updates[-1]["metadata"]
    assert (md["denials"], md["compactions"], md["context_tokens"]) == (3, 1, 170002)


def test_record_gate_reports_inert_checks_and_failure_classes(tmp_path, monkeypatch):
    """A PASS with disabled checks must be distinguishable from a policed PASS (the audit's worst
    false green), and failures carry machine classes for taxonomy dashboards."""
    _reset()
    ev_updates, scores = [], []

    class _Ev:
        def update(self, **kw): ev_updates.append(kw)
        def end(self): pass
        def score(self, **kw): scores.append(kw)  # gate scores anchor to the evaluator observation

    class _FakeClient:
        def start_observation(self, **kw): return _Ev()
        def update_current_span(self, **kw): pass
        def score_current_trace(self, **kw): scores.append(kw)

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = _ctx(tmp_path)
    obs.record_gate(ctx, "ground", [], attempt=1, inert=["citation_witness", "req_sufficiency"])
    assert ev_updates[-1]["input"] == {"failure_count": 0,
                                       "checks_inert": ["citation_witness", "req_sufficiency"]}
    inert_scores = [s for s in scores if s["name"] == "gate_ground_inert_checks"]
    assert inert_scores and inert_scores[0]["value"] == 2.0

    obs.record_gate(ctx, "review", ["review BLOCKER: x", "review BLOCKER: y"], attempt=2)
    assert ev_updates[-1]["metadata"]["failure_classes"] == {"review_blocker": 2}
    assert ev_updates[-1]["input"]["checks_inert"] == []


def test_operator_flush_scores_rating_and_flushes_skipped_asks(tmp_path, monkeypatch):
    """rfc_rating becomes a chartable score (human_rfc_rating), and a non-interactive run's owed-but-
    unasked questions land as operator:ask_skipped events. Both flushes are idempotent via markers."""
    import json as _json

    _reset()
    opened, scores = [], []

    class _Obs:
        def end(self): pass

    class _FakeClient:
        def start_observation(self, **kw):
            opened.append(kw)
            return _Obs()
        def create_score(self, **kw): scores.append(kw)

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    obs._node_steps.clear()
    ctx = _ctx(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / ".last_trace_id").write_text("rated-trace-1")  # the run the rating refers to (0.23.1)
    (logs / "operator_actions.jsonl").write_text(
        _json.dumps({"action": "rfc_rating", "detail": {"rating": 4, "note": "solid"}}) + "\n")
    (logs / "asks_skipped.jsonl").write_text(
        _json.dumps({"note": "ground open questions not asked", "questions": ["Q1", "Q2"]}) + "\n")
    obs.operator_actions_flush(ctx)
    names = [o["name"] for o in opened]
    assert "operator:rfc_rating" in names and "operator:ask_skipped" in names
    assert scores and scores[0]["name"] == "human_rfc_rating" and scores[0]["value"] == 4.0
    assert scores[0]["trace_id"] == "rated-trace-1"
    n = len(opened)
    obs.operator_actions_flush(ctx)  # idempotent — markers advanced
    assert len(opened) == n


def test_finish_run_marks_invocation_truth_and_denominators(tmp_path, monkeypatch):
    """The trace output block is cumulative manifest state — entries must say whether THIS invocation
    ran them (verified misleading live on PROJ-3394-v5 run 6); repo HEADs and the REQ-count
    denominator ride along when their artifacts exist."""
    import json as _json

    _reset()
    scores, io = [], {}

    class _FakeClient:
        def set_current_trace_io(self, **kw): io.update(kw)
        def update_current_span(self, **kw): pass
        def score_current_trace(self, **kw): scores.append(kw)

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = _ctx(tmp_path)
    (tmp_path / "findings").mkdir()
    (tmp_path / "findings" / "evidence.json").write_text(
        _json.dumps({"heads": {"svc_a": "abc123"}}))
    (tmp_path / "extract").mkdir()
    (tmp_path / "extract" / "requirements.json").write_text(
        _json.dumps({"requirements": [{"id": f"REQ-{i}"} for i in range(1, 8)]}))
    manifest = {
        "ingest": {"status": "ok", "cost_usd": 1.0, "duration_s": 10, "inputs_digest": "aa"},
        "review": {"status": "gate_failed", "cost_usd": 2.0, "duration_s": 20, "attempts": 2},
    }
    obs.finish_run(ctx, manifest, ran={"review"})
    out = io["output"]
    assert out["ingest"]["ran_this_invocation"] is False
    assert out["review"]["ran_this_invocation"] is True
    assert out["ingest"]["inputs_digest"] == "aa"
    assert out["repo_heads"] == {"svc_a": "abc123"}
    by_name = {s["name"]: s["value"] for s in scores}
    assert by_name["req_count"] == 7.0
    assert "ingest_rework_attempts" not in by_name  # resume must not re-score untouched stages


def test_subagent_finished_attaches_worker_stream_facts(tmp_path, monkeypatch):
    """Per-worker truth (audit 2026-08-19): each persona/shard node carries its OWN cost/usage/model
    and the exact prompt it received — warm-resume prompts included."""
    _reset()
    updated = []

    class _H:
        def update(self, **kw): updated.append(kw)
        def end(self): pass

    from rfc_agent.streams import StreamResult

    st = StreamResult(cost_usd=0.5, model="claude-sonnet-5",
                      usage={"input_tokens": 1, "output_tokens": 2,
                             "cache_read_input_tokens": 3, "cache_creation_input_tokens": 4},
                      num_turns=7, denials=1)
    st.prompt_sent = "You are RESUMED in your own previous review session" + "x" * 5000
    obs.subagent_finished(_H(), "wrote review/persona_skeptic.md (3,800 bytes)", stream=st)
    md = updated[-1]["metadata"]
    assert md["cost_usd"] == 0.5 and md["model"] == "claude-sonnet-5" and md["num_turns"] == 7
    assert md["usage"]["cache_read_input_tokens"] == 3 and md["denials"] == 1
    assert md["prompt"].startswith("You are RESUMED") and len(md["prompt"]) < 4200


def test_crashed_sentinel_never_becomes_a_model(tmp_path):
    """Review finding #5: 'crashed (no stream recovered)' parsed to model='crashed' — the same
    analytics-poisoning class the 'cache' guard fixed."""
    _reset()
    updates = []

    class _Gen:
        def update(self, **kw): updates.append(kw)
        def end(self): pass

    run = obs.AgentRun(agent=None, gen=_Gen())
    obs.finish_generation(run, StageResult(engine_desc="crashed (no stream recovered)"))
    assert "model" not in updates[-1]
    obs.finish_generation(run, StageResult(engine_desc="claude:claude-test (crashed)"))
    assert updates[-1]["model"] == "claude-test"


def test_rating_scores_the_rated_trace_never_the_flushing_one(tmp_path, monkeypatch):
    """Review finding #2: the rating is collected post-run and flushed on the NEXT invocation —
    score_current_trace attached it to the improved rerun, or nowhere. It must target the stored
    .last_trace_id, and skip (event only) when no stored id exists."""
    import json as _json

    _reset()
    created, current_scored = [], []

    class _Obs:
        def end(self): pass

    class _FakeClient:
        def start_observation(self, **kw): return _Obs()
        def create_score(self, **kw): created.append(kw)
        def score_current_trace(self, **kw): current_scored.append(kw)

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    obs._node_steps.clear()
    ctx = _ctx(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "operator_actions.jsonl").write_text(
        _json.dumps({"action": "rfc_rating", "detail": {"rating": 2}}) + "\n")
    obs.operator_actions_flush(ctx)  # no stored trace id -> event yes, score NO
    assert created == [] and current_scored == []

    (logs / ".operator_actions.flushed").unlink()  # replay with a stored id
    (logs / ".last_trace_id").write_text("abc123traceid")
    obs.operator_actions_flush(ctx)
    assert current_scored == []
    assert created and created[0]["trace_id"] == "abc123traceid" and created[0]["value"] == 2.0


def test_deferred_rating_targets_its_own_stamped_trace(tmp_path, monkeypatch):
    """Feedback-timing redesign (2026-08-20): ratings are engagement-gated, so deferral past
    intermediate invocations is the NORM — and .last_trace_id is overwritten by every one of them
    (a green-skip flush would mis-attribute). The record's delivery-stamped trace_id must win."""
    import json as _json

    _reset()
    created = []

    class _Obs:
        def end(self): pass

    class _FakeClient:
        def start_observation(self, **kw): return _Obs()
        def create_score(self, **kw): created.append(kw)
        def score_current_trace(self, **kw): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    obs._node_steps.clear()
    ctx = _ctx(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / ".last_trace_id").write_text("greenskip-STALE")  # a later invocation moved it
    (logs / "operator_actions.jsonl").write_text(_json.dumps({
        "ts": "2026-08-20T12:10:00+00:00", "action": "rfc_rating",
        "detail": {"ship_asis": False, "direction_ok": True, "worst_problem": "CHANGE-6 wrong path",
                   "trace_id": "delivered-RATED", "delivered_at": "2026-08-20T12:00:00+00:00",
                   "engagement": {"published": True, "opened": True, "discussed": False}}}) + "\n")
    obs.operator_actions_flush(ctx)
    names = {c["name"]: c for c in created}
    # binary per-dimension verdicts land even with NO numeric rating volunteered
    assert names["human_ship_asis"]["value"] == 0.0
    assert names["human_direction_ok"]["value"] == 1.0
    assert "human_rfc_rating" not in names
    for c in created:
        assert c["trace_id"] == "delivered-RATED"          # stamped id wins over the stale file
        assert "CHANGE-6 wrong path" in c["comment"]        # worst-problem note travels
        assert "opened, published" in c["comment"]          # grounding evidence on the score
        assert "rated 600s after delivery" in c["comment"]  # latency for calibration filtering


def test_ungrounded_rating_is_labeled_as_such(tmp_path, monkeypatch):
    import json as _json

    _reset()
    created = []

    class _Obs:
        def end(self): pass

    class _FakeClient:
        def start_observation(self, **kw): return _Obs()
        def create_score(self, **kw): created.append(kw)
        def score_current_trace(self, **kw): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    obs._node_steps.clear()
    ctx = _ctx(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / ".last_trace_id").write_text("tid-1")
    (logs / "operator_actions.jsonl").write_text(_json.dumps(
        {"ts": "t", "action": "rfc_rating", "detail": {"rating": 4}}) + "\n")
    obs.operator_actions_flush(ctx)
    assert created and "NONE RECORDED — ungrounded label" in created[0]["comment"]


def test_finish_run_persists_trace_id_and_flushes_operator_actions(tmp_path, monkeypatch):
    """Review findings #2 + #4: finish_run must store this trace's id (the rating's future target)
    and flush mid-run operator actions onto THIS invocation's trace."""
    import json as _json

    _reset()
    opened = []

    class _Obs:
        def end(self): pass

    class _FakeClient:
        def get_current_trace_id(self): return "tid-999"
        def start_observation(self, **kw):
            opened.append(kw)
            return _Obs()
        def set_current_trace_io(self, **kw): pass
        def update_current_span(self, **kw): pass
        def score_current_trace(self, **kw): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    obs._node_steps.clear()
    ctx = _ctx(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "operator_actions.jsonl").write_text(
        _json.dumps({"ts": "t", "action": "checkpoint_rerun", "detail": {"note": "tighten §4"}}) + "\n")
    obs.finish_run(ctx, {"a": {"status": "ok"}}, ran={"a"})
    assert (logs / ".last_trace_id").read_text() == "tid-999"
    assert any(o["name"] == "operator:checkpoint_rerun" for o in opened), \
        "mid-run actions must land on the invocation that produced them"


def test_env_vars_alone_never_activate_obs(tmp_path, monkeypatch):
    """0.23.3 revert guard: env credentials reach obs ONLY via config._ENV_MAP (env-wins at config
    load). A direct env read in _get_client made the TEST SUITE construct real clients whenever a
    developer had LANGFUSE_* exported (fixtures pass config=None), shipping synthetic traces to the
    production project. With no config keys, exported env vars must stay inert here."""
    _reset()

    class _Boom:
        def __init__(self, **kw):
            raise AssertionError("a client must never be constructed from bare env vars")

    import langfuse as _lf
    monkeypatch.setattr(_lf, "Langfuse", _Boom)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")
    assert obs._get_client(_ctx(tmp_path)) is None            # empty-key config double
    _reset()
    from rfc_agent.contracts import RunContext
    assert obs._get_client(RunContext(run_dir=tmp_path, inputs={}, config=None)) is None


def test_operator_flush_places_actions_between_stages(monkeypatch, tmp_path):
    """0.24.5: a steer posted DURING a stage flushes at that stage's boundary and gets its own
    graph node at its real position in the flow (… stage:extract → operator:steer → …), instead
    of one collapsed 'operator' box at __start__ flushed only when the invocation exits."""
    import json as _json

    from rfc_agent import obs

    opened = []

    class _Handle:
        def update(self, **kw): pass
        def end(self, **kw): pass

    class _FakeClient:
        def start_as_current_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def get_current_trace_id(self): return "abc"
        def update_current_span(self, **kw): pass
        def score_current_trace(self, **kw): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setattr(obs, "_step_counter", 0)
    obs._node_steps.clear()
    ctx = _ctx(tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    oa = logs / "operator_actions.jsonl"

    with obs.stage_span(ctx, "extract", 1):
        pass
    oa.write_text(_json.dumps({"action": "steer", "detail": {"lane": "extract"}}) + "\n")
    obs.operator_actions_flush(ctx)  # the runner's stage-boundary flush
    with obs.stage_span(ctx, "ground", 1):
        pass
    oa.write_text(oa.read_text() + _json.dumps({"action": "steer", "detail": {"lane": "ground"}}) + "\n")
    obs.operator_actions_flush(ctx)

    ns = [(o["name"], o["metadata"].get("langgraph_node"), o["metadata"].get("langgraph_step"))
          for o in opened if o.get("metadata") and str(o.get("name", "")).startswith(("stage:", "operator:"))]
    names = [n for n, _, _ in ns]
    assert names.count("operator:steer") == 2, "idempotent cursor: each record flushed exactly once"
    # the steer sits between the two stage lanes, with its own named node
    steer_idx = names.index("operator:steer")
    assert names.index("stage:extract") < steer_idx < names.index("stage:ground")
    assert ns[steer_idx][1] == "operator:steer"


def test_operator_flush_forces_sdk_export_only_when_records_emitted(tmp_path, monkeypatch):
    """0.24.11: measured 2026-08-19 — the OTEL batch delay was ~4s of the ~5s steer→Langfuse
    latency; client.flush() after an emitting batch cuts server createdAt to ~0.2s. No-op flush
    calls (stage boundaries with nothing new) must NOT trigger a blocking network flush."""
    import json as _json

    from rfc_agent import obs

    calls = {"obs": 0, "flush": 0}

    class _Obs:
        def end(self): pass

    class _FakeClient:
        def start_observation(self, **kw):
            calls["obs"] += 1
            return _Obs()
        def flush(self):
            calls["flush"] += 1

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = _ctx(tmp_path)
    (tmp_path / "logs").mkdir()
    oa = tmp_path / "logs" / "operator_actions.jsonl"
    oa.write_text(_json.dumps({"action": "steer"}) + "\n")

    obs.operator_actions_flush(ctx)
    assert calls == {"obs": 1, "flush": 1}, "an emitting batch must force the exporter"
    obs.operator_actions_flush(ctx)  # nothing new behind the marker
    assert calls == {"obs": 1, "flush": 1}, "a no-op flush must not add a blocking network call"


def test_operator_actions_are_backdated_to_their_own_ts(tmp_path, monkeypatch):
    """0.24.12: the observation is positioned at the moment the human ACTED (the record's ts), not
    when the batcher ran — flushed-later actions sorted AFTER their consequences (the delivery
    decision rendered before the steer that caused it). Best-effort: a handle without the OTEL
    span degrades to creation-time position and must NOT pass end_time (negative duration)."""
    import json as _json

    from rfc_agent import obs

    ended = []

    class _Span:  # stands in for the OTEL SDK span whose _start_time the backdate writes
        _start_time = None

    class _Obs:
        def __init__(self, with_span):
            if with_span:
                self._otel_span = _Span()
        def end(self, **kw):
            ended.append((getattr(getattr(self, "_otel_span", None), "_start_time", None),
                          kw.get("end_time")))

    class _FakeClient:
        def __init__(self, with_span): self.with_span = with_span
        def start_observation(self, **kw): return _Obs(self.with_span)
        def flush(self): pass

    (tmp_path / "logs").mkdir()
    oa = tmp_path / "logs" / "operator_actions.jsonl"
    oa.write_text(_json.dumps({"ts": "2026-08-19T18:25:31+00:00", "action": "steer"}) + "\n")
    ctx = _ctx(tmp_path)

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient(with_span=True))
    obs.operator_actions_flush(ctx)
    want_ns = obs._event_ns("2026-08-19T18:25:31+00:00")
    assert ended == [(want_ns, want_ns)], "backdated: OTEL start rewritten AND end_time at the same ts"

    ended.clear()
    (tmp_path / "logs" / ".operator_actions.flushed").unlink()
    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient(with_span=False))
    obs.operator_actions_flush(ctx)
    assert ended == [(None, None)], "no OTEL span -> creation-time position, NO end_time"


class _KnobConfig:
    """Config carrying enough AGENT knobs to make the snapshot exceed the 200-char cap."""
    def get(self, dotted, default=None):
        vals = {
            "AGENT.DEFAULT_MODEL": "claude-opus-5[1m]",
            "AGENT.STAGE_MODELS": {"generate_cases": "claude-opus-5[1m]", "evaluate": "claude-sonnet-5[1m]"},
            "AGENT.RESUME_CONTEXT_CAP": 300_000,
            "AGENT.RUN_TIMEOUT": 5400,
            "AGENT.THINKING_TAP": True,
        }
        return vals.get(dotted, default)


def test_config_snapshot_never_rides_propagated_attributes(tmp_path):
    # 0.24.19 field bug: propagate_attributes caps values at 200 chars and silently DROPS longer
    # ones — the 231-char config_snapshot never reached a single trace. The snapshot must therefore
    # be absent from the propagated provenance dict (root-observation metadata carries it instead),
    # and everything that DOES ride propagation must fit under the cap.
    import json as _json
    ctx = RunContext(run_dir=tmp_path, inputs={}, config=_KnobConfig())
    snap = obs._config_snapshot(ctx)
    assert snap and len(snap) > 200          # the shape that used to get dropped
    assert _json.loads(snap)["RUN_TIMEOUT"] == 5400  # parseable, real knobs
    prov = obs._provenance(ctx)
    assert "config_snapshot" not in prov
    assert len(prov["config_hash"]) == 12    # short comparability signal survives kills
    assert all(len(str(v)) <= 200 for v in prov.values())


def test_tool_reason_rides_tool_metadata(tmp_path, monkeypatch):
    """P0 (0.24.50): each tool observation carries the thinking that governed it — `reason` (clipped,
    whitespace-normalized) + `reason_scope` in METADATA (never input: tool input must stay a faithful
    copy of the tool args for the annotate->dataset loop). No reason -> no keys."""
    from rfc_agent import obs

    opened = []

    class _Handle:
        def update(self, **kw): pass
        def end(self, **kw): pass

    class _FakeClient:
        def start_as_current_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def get_current_trace_id(self): return "abc123"

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    monkeypatch.setattr(obs, "_tool_events_left", 10)
    ctx = _ctx(tmp_path)
    with obs.agent_generation(ctx, "generate"):
        obs.tool_started(ctx, "Read", {"file_path": "x"},
                         reason="  the guard\n belongs in update_patients  ", reason_scope="same_message")
        obs.tool_started(ctx, "Read", {"file_path": "y"}, reason="z" * 2000, reason_scope="carried")
        obs.tool_started(ctx, "Bash", {"command": "ls"})
    tools = [o for o in opened if o["name"].startswith("tool:")]
    assert tools[0]["metadata"]["reason"] == "the guard belongs in update_patients"
    assert tools[0]["metadata"]["reason_scope"] == "same_message"
    assert tools[0]["input"] == {"file_path": "x"}  # input untouched
    assert len(tools[1]["metadata"]["reason"]) <= 800 + 20  # clipped (+ clip suffix)
    assert tools[1]["metadata"]["reason_scope"] == "carried"
    assert "reason" not in (tools[2]["metadata"] or {})


def test_drain_pairs_thinking_with_tool_calls(tmp_path, monkeypatch):
    """The live drain hands each tool_use its governing thinking block: same_message when they share
    the assistant message, carried when the reasoning is the most recent earlier block."""
    import json as _json

    from rfc_agent import engine, obs

    calls = []
    monkeypatch.setattr(obs, "tool_started",
                        lambda ctx, name, inp, parent=None, event_ts=None, reason=None,
                        reason_scope=None: calls.append((name, reason, reason_scope)))
    monkeypatch.setattr(obs, "tool_finished", lambda *a, **kw: None)
    monkeypatch.setattr(obs, "operator_actions_flush", lambda ctx: None)
    (tmp_path / "logs").mkdir()
    sp = tmp_path / "logs" / "generate.stream.jsonl"
    evs = [
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "thinking", "thinking": "read the builder"},
            {"type": "tool_use", "id": "t1", "name": "Read", "input": {}}]}},
        {"type": "assistant", "message": {"id": "m2", "content": [
            {"type": "tool_use", "id": "t2", "name": "Grep", "input": {}}]}},
    ]
    sp.write_text("".join(_json.dumps(e) + "\n" for e in evs))

    class _Proc:
        args = ["claude"]
        def poll(self): return 0
    engine._wait_with_progress(_Proc(), sp, timeout=5, ctx=_ctx(tmp_path), quiet=True)
    assert calls == [("Read", "read the builder", "same_message"),
                     ("Grep", "read the builder", "carried")]


def test_generation_digest_prefers_deliberation(tmp_path, monkeypatch):
    """P1 (0.24.50): the generation digest = whole non-tool thinking turns from the stream (source
    'deliberation'), with the tap-file head+tail only as fallback ('tap_headtail'). Head+tail of the
    whole stream covered 4.6% and deleted the mid-stream decisions."""
    from rfc_agent import obs
    from rfc_agent.contracts import StageResult

    updates = {}

    class _Gen:
        def update(self, **kw): updates.update(kw)
        def end(self): pass

    tp = tmp_path / "gen.thinking.txt"
    tp.write_text("TAPHEAD " + "x" * 9000 + " TAPTAIL")
    run = obs.AgentRun(agent=None, gen=_Gen(), thinking_path=tp, thinking_offset=0)
    res = StageResult(final_text="ok", engine_desc="claude:m", usage={},
                      deliberation=["plan A", "conclusion: ship the flag"])
    obs.finish_generation(run, res)
    md = updates["metadata"]
    assert md["thinking_digest"] == "plan A\n---\nconclusion: ship the flag"
    assert md["thinking_digest_source"] == "deliberation"

    updates.clear()
    run = obs.AgentRun(agent=None, gen=_Gen(), thinking_path=tp, thinking_offset=0)
    obs.finish_generation(run, StageResult(final_text="ok", engine_desc="claude:m", usage={}))
    md = updates["metadata"]
    assert md["thinking_digest"].startswith("TAPHEAD") and md["thinking_digest"].endswith("TAPTAIL")
    assert md["thinking_digest_source"] == "tap_headtail"


def test_worker_digest_prefers_stream_deliberation(tmp_path, monkeypatch):
    """Fan-out workers get the same preference: their own stream's deliberation beats the tap file."""
    from rfc_agent import obs
    from rfc_agent.streams import StreamResult

    updates = {}

    class _Handle:
        def update(self, **kw): updates.update(kw)
        def end(self): pass

    h = _Handle()
    tp = tmp_path / "w.thinking.txt"
    tp.write_text("tap text")
    h._rfc_thinking = (tp, 0)
    s = StreamResult(deliberation=["skeptic verdict: the flag cannot reach the clear path"])
    obs.subagent_finished(h, output="wrote fragment", stream=s)
    md = updates["metadata"]
    assert md["thinking_digest"] == "skeptic verdict: the flag cannot reach the clear path"
    assert md["thinking_digest_source"] == "deliberation"


def test_deliberation_digest_keeps_recent_whole_blocks_and_names_elisions():
    from rfc_agent import obs

    blocks = ["b1 " + "a" * 50, "b2 " + "b" * 50, "b3 final decision"]
    out = obs._deliberation_digest(blocks, cap=80)
    assert "b3 final decision" in out and out.startswith("[")  # elision note names the loss
    assert "b1" not in out
    assert obs._deliberation_digest([], cap=80) == ""


def test_gate_metadata_carries_blocker_anchors(tmp_path, monkeypatch):
    """0.24.54: review's failure_classes is the single degenerate class review_blocker, so the
    CHANGE/REQ ids the blockers name are emitted as blocker_anchors — the anchor-level grouping
    key and the same-anchor-across-rounds (blind-retry/descent) join. Sorted, unique, absent when
    no failure names an id."""
    from rfc_agent import obs

    opened = []

    class _Handle:
        def __init__(self): self.kw = {}
        def update(self, **kw): self.kw.update(kw)
        def end(self): pass
        def score(self, **kw): pass

    class _FakeClient:
        def start_observation(self, **kw):
            h = _Handle()
            h.meta = kw
            opened.append(h)
            return h
        def get_current_trace_id(self): return "abc"
        def update_current_span(self, **kw): pass
        def score_current_trace(self, **kw): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    ctx = _ctx(tmp_path)
    obs.record_gate(ctx, "review", [
        "review BLOCKER: CHANGE-10 builds only records; REQ-38 fail-open missing (see CHANGE-8)",
        "review BLOCKER: CHANGE-2 seed exists but CHANGE-10 no-match branch never saves (REQ-30)",
    ])
    md = opened[0].kw["metadata"]
    assert md["failure_classes"] == {"review_blocker": 2}
    assert md["blocker_anchors"] == ["CHANGE-2", "CHANGE-8", "CHANGE-10", "REQ-30", "REQ-38"]

    opened.clear()
    obs.record_gate(ctx, "ground", ["findings.md: citation line out of range: a.py:9 (file has 8)"])
    assert "blocker_anchors" not in opened[0].kw["metadata"]  # no ids named -> key absent


def test_passing_ground_gate_ships_the_evidence_pack(tmp_path, monkeypatch):
    """0.24.57: the GROUND->GENERATE handoff reads findings.md from disk, so the trace held only a
    pointer + 2K-clipped tool reads — 'did GENERATE receive the fact' (Q2 row 6) needed the machine
    that ran the pipeline. A PASSING ground evaluation now ships the assembled findings.md whole as
    artifact:findings.md under the stage span. Red ground gates and other stages ship nothing."""
    from rfc_agent import obs

    opened = []

    class _Handle:
        def update(self, **kw): pass
        def end(self, **kw): pass
        def score(self, **kw): pass

    class _FakeClient:
        def start_observation(self, **kw):
            opened.append(kw)
            return _Handle()
        def get_current_trace_id(self): return "abc"
        def update_current_span(self, **kw): pass
        def score_current_trace(self, **kw): pass

    monkeypatch.setattr(obs, "_get_client", lambda ctx: _FakeClient())
    (tmp_path / "findings").mkdir()
    (tmp_path / "findings" / "findings.md").write_text("# findings\n- svc_a/app/x.py:12 pattern")
    ctx = _ctx(tmp_path)

    obs.record_gate(ctx, "ground", [])  # PASS -> artifact
    arts = [o for o in opened if o.get("name") == "artifact:findings.md"]
    assert len(arts) == 1
    assert arts[0]["output"] == "# findings\n- svc_a/app/x.py:12 pattern"
    assert arts[0]["metadata"]["path"] == "findings/findings.md"
    assert "langgraph_node" not in arts[0]["metadata"]  # a snapshot is not a control-flow step

    opened.clear()
    obs.record_gate(ctx, "ground", ["findings.md: absence claim looks FALSE"])  # red -> nothing
    assert not [o for o in opened if o.get("name") == "artifact:findings.md"]
    opened.clear()
    obs.record_gate(ctx, "generate", [])  # other stages -> nothing
    assert not [o for o in opened if o.get("name") == "artifact:findings.md"]


def test_clip_replaces_base64_data_uris_before_truncation():
    """Clipping mid-blob made the Langfuse media parser traceback on every screenshot Read
    (22x per snapshot ingest, 2026-09-02) — the blob is replaced by its length, never cut."""
    from rfc_agent.obs import _clip
    blob = "data:image/png;base64," + "A" * 5000
    out = _clip({"tool_result": "before " + blob + " after"}, limit=2000)
    assert out["tool_result"] == f"before data:image/png;base64,[{len(blob)} chars omitted] after"
    # short data URIs (< 64 chars of payload) are left alone — they clip safely and may be real content
    tiny = "data:image/png;base64,iVBORw0KGgo="
    assert _clip(tiny) == tiny


def test_clip_drops_data_from_inline_base64_media_blocks():
    """The Read tool's image result is an Anthropic image block, not a data URI; Langfuse's exporter
    rebuilds a data URI from any dict carrying `data` + a media-type key, so a clipped `data` string
    still tracebacked (2026-09-03, second sighting). No `data` key -> nothing for it to rebuild."""
    from rfc_agent.obs import _clip
    block = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A" * 140000}}]
    out = _clip(block)
    src = out[0]["source"]
    assert "data" not in src and src["media_type"] == "image/png" and src["omitted_base64_chars"] == 140000
    import json
    assert '"data"' not in json.dumps(out)
