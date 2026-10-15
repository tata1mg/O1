"""Langfuse tracing for the new pipeline (rfc_agent.observe): config, the no-keys stop, trace shape, verbose."""

from __future__ import annotations

import contextlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from rfc_agent import observe

# ---- a fake Langfuse client ---------------------------------------------------------------------------------


class Obs:
    def __init__(self, client, as_type, name, parent, **kw):
        self.client, self.type, self.name, self.parent = client, as_type, name, parent
        self.kw = dict(kw)
        self.updates: dict = {}
        self.scores: list[dict] = []
        self.ended = False
        self.thread = threading.current_thread().name
        with client.lock:
            client.all.append(self)

    def start_observation(self, *, as_type, name, **kw):
        return Obs(self.client, as_type, name, self, **kw)

    def update(self, **kw):
        self.updates.update(kw)

    def end(self, **kw):
        self.ended = True

    def score(self, **kw):
        self.scores.append(kw)

    def set_trace_io(self, **kw):
        self.client.trace_io.update(kw)

    @property
    def level(self):
        return self.updates.get("level", self.kw.get("level", "DEFAULT"))


class FakeClient:
    def __init__(self):
        self.all: list[Obs] = []
        self.trace_io: dict = {}
        self.flushed = 0
        self.lock = threading.Lock()

    @contextlib.contextmanager
    def start_as_current_observation(self, *, as_type, name, **kw):
        o = Obs(self, as_type, name, None, **kw)
        try:
            yield o
        finally:
            o.ended = True

    def flush(self):
        self.flushed += 1

    def named(self, prefix):
        return [o for o in self.all if o.name.startswith(prefix)]

    def one(self, name):
        hits = [o for o in self.all if o.name == name]
        assert len(hits) == 1, (name, [o.name for o in self.all])
        return hits[0]


class Cfg:
    def __init__(self, **values):
        self.values = {"LANGFUSE.PUBLIC_KEY": "pk-lf-test", "LANGFUSE.SECRET_KEY": "sk-lf-test", **values}

    def get(self, k, d=None):
        return self.values.get(k, d)


@pytest.fixture
def traced(monkeypatch):
    """Tracing on with keys and a fake client; returns a setter for the config values."""
    monkeypatch.delenv(observe.ENV, raising=False)
    monkeypatch.delenv(observe.VERBOSE_ENV, raising=False)
    monkeypatch.setattr(observe, "_overrides", {})
    state = {"cfg": Cfg(), "client": None}

    def make(cfg):
        state["client"] = FakeClient()
        return state["client"]

    monkeypatch.setattr(observe, "_load_cfg", lambda: state["cfg"])
    monkeypatch.setattr(observe, "_make_client", make)
    monkeypatch.setattr("rfc_agent.obs._user_id", lambda ctx: "dev@example.com")
    yield state
    assert observe._root is None  # every test closes its trace


# ---- settings -----------------------------------------------------------------------------------------------


def test_take_flags_strips_both_forms(monkeypatch):
    monkeypatch.setattr(observe, "_overrides", {})
    rest = observe.take_flags(["--run-dir", "x", "--langfuse", "false", "--langfuse-verbose=true", "--yes"])
    assert rest == ["--run-dir", "x", "--yes"]
    assert observe._overrides == {"ENABLED": False, "VERBOSE": True}


def test_take_flags_rejects_a_non_boolean(monkeypatch):
    monkeypatch.setattr(observe, "_overrides", {})
    with pytest.raises(SystemExit, match="true or false"):
        observe.take_flags(["--langfuse", "maybe"])


def test_defaults_are_on_and_not_verbose(monkeypatch):
    monkeypatch.delenv(observe.ENV, raising=False)
    monkeypatch.delenv(observe.VERBOSE_ENV, raising=False)
    monkeypatch.setattr(observe, "_overrides", {})
    assert observe.settings(Cfg()) == {"enabled": True, "verbose": False}


def test_precedence_flag_over_env_over_config(monkeypatch):
    monkeypatch.setattr(observe, "_overrides", {})
    cfg = Cfg(**{"LANGFUSE.ENABLED": False, "LANGFUSE.VERBOSE": True})
    monkeypatch.delenv(observe.ENV, raising=False)
    monkeypatch.delenv(observe.VERBOSE_ENV, raising=False)
    assert observe.settings(cfg) == {"enabled": False, "verbose": True}
    monkeypatch.setenv(observe.ENV, "true")
    monkeypatch.setenv(observe.VERBOSE_ENV, "0")
    assert observe.settings(cfg) == {"enabled": True, "verbose": False}
    observe.take_flags(["--langfuse", "false"])
    assert observe.settings(cfg)["enabled"] is False


def test_off_by_flag_never_loads_the_config(monkeypatch):
    monkeypatch.setattr(observe, "_overrides", {"ENABLED": False})

    def boom():
        raise AssertionError("config loaded")

    monkeypatch.setattr(observe, "_load_cfg", boom)
    assert observe.settings() == {"enabled": False, "verbose": False}


def test_new_keys_are_optional_in_an_older_config(tmp_path, monkeypatch):
    from rfc_agent.config import HARNESS, Config

    monkeypatch.delenv(observe.ENV, raising=False)

    harness = json.loads(HARNESS.read_text())
    del harness["LANGFUSE"]["ENABLED"], harness["LANGFUSE"]["VERBOSE"]
    h = tmp_path / "config.harness.json"
    h.write_text(json.dumps(harness))
    p = tmp_path / "config.json"
    p.write_text("{}")
    assert Config(local=p, harness=h).get("LANGFUSE.ENABLED") is None  # loads; observe falls back to its defaults


# ---- the run entry ------------------------------------------------------------------------------------------


def test_on_without_keys_stops_before_the_stage_runs(traced, capsys):
    traced["cfg"] = Cfg(**{"LANGFUSE.PUBLIC_KEY": "", "LANGFUSE.SECRET_KEY": ""})
    ran = []

    @observe.entry("draft")
    def main(argv=None):
        ran.append(argv)
        return 0

    assert main(["--run-dir", "/tmp/x"]) == observe.EXIT_NO_KEYS
    assert ran == [] and traced["client"] is None
    err = capsys.readouterr().err
    assert "keys are missing" in err and "--langfuse false" in err and "LANGFUSE.ENABLED" in err


def test_off_runs_untraced_with_the_flag_stripped(traced):
    traced["cfg"] = Cfg(**{"LANGFUSE.PUBLIC_KEY": "", "LANGFUSE.SECRET_KEY": ""})
    seen = []

    @observe.entry("draft")
    def main(argv=None):
        seen.append(argv)
        observe.gate("draft", ["x"], kind="draft")  # no-op: no trace open
        return 0

    assert main(["--run-dir", "/tmp/x", "--langfuse", "false"]) == 0
    assert seen == [["--run-dir", "/tmp/x"]] and traced["client"] is None


def test_scope_utilities_are_never_traced(traced):
    traced["cfg"] = Cfg(**{"LANGFUSE.PUBLIC_KEY": ""})

    @observe.entry("scope")
    def main(argv=None):
        return 0

    assert main(["run-dir", "--prd", "123"]) == 0  # would be exit 2 if it were traced


def test_standalone_stage_gets_its_own_trace(traced, tmp_path):
    @observe.entry("review")
    def main(argv=None):
        assert observe.active()
        return 0

    assert main(["--run-dir", str(tmp_path)]) == 0
    c = traced["client"]
    root, stage = c.one("rfc-run"), c.one("stage:review")
    assert stage.parent is root and stage.type == "chain" and stage.ended
    assert stage.updates["output"] == {"exit": 0} and c.trace_io["output"] == {"exit": 0}
    assert c.flushed == 1
    assert (tmp_path / "identity.json").exists()  # the session id outlives this invocation


def test_whole_run_one_trace_with_a_span_per_stage(traced, tmp_path):
    @observe.entry("revise")
    def revise(argv=None):
        return summarize(["--run-dir", argv[1]])  # Revise runs Summarize itself

    @observe.entry("summarize")
    def summarize(argv=None):
        return 0

    @observe.entry("draft")
    def draft(argv=None):
        raise SystemExit("gate broke")

    @observe.entry(None)
    def run(argv=None):
        assert revise(["--run-dir", argv[1]]) == 0
        with pytest.raises(SystemExit):
            draft(["--run-dir", argv[1]])
        return 1

    assert run(["--run-dir", str(tmp_path), "--langfuse-verbose", "true"]) == 1
    c = traced["client"]
    root = c.one("rfc-run")
    assert len(c.named("rfc-run")) == 1  # nested entries reuse the open trace
    assert c.one("stage:revise").parent is root
    assert c.one("stage:summarize").parent is c.one("stage:revise")
    s2 = c.one("stage:draft")
    assert s2.level == "ERROR" and s2.updates["status_message"] == "gate broke" and s2.updates["output"] == {"exit": 1}
    assert root.updates["level"] == "ERROR"


def test_sdk_failure_at_open_does_not_stop_the_run(traced, tmp_path, capsys, monkeypatch):
    def broken(cfg):
        raise RuntimeError("network down")

    monkeypatch.setattr(observe, "_make_client", broken)
    monkeypatch.setattr(observe, "_warned", False)

    @observe.entry("summarize")
    def main(argv=None):
        assert not observe.active()
        return 0

    assert main(["--run-dir", str(tmp_path)]) == 0
    assert "network down" in capsys.readouterr().err


# ---- agents -------------------------------------------------------------------------------------------------

FAKE = r"""#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-sonnet-5-5"}))
print(json.dumps({"type": "assistant", "message": {"content": [
    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "a.py"}},
    {"type": "tool_use", "id": "t2", "name": "Grep", "input": {"pattern": "x"}}]}}))
print(json.dumps({"type": "user", "message": {"content": [
    {"type": "tool_result", "tool_use_id": "t1", "content": "print(1)"},
    {"type": "tool_result", "tool_use_id": "t2", "content": "no such file", "is_error": True}]}}))
print(json.dumps({"type": "result", "subtype": "success", "session_id": "s1", "total_cost_usd": 0.25,
                  "num_turns": 3, "result": "done",
                  "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 900}}))
"""


def _agent_run(tmp_path, label="T1", stage="investigate"):
    from rfc_agent.investigate import agent as ag

    fake = tmp_path / "claude"
    if not fake.exists():
        fake.write_text(FAKE)
        fake.chmod(0o755)
    spec = ag.AgentSpec(label, "claude-sonnet-5-5[1m]", "high", ("Read",), (), use_context=False, timeout_s=30)
    return ag.run(spec, "trace flow F1", tmp_path, Cfg(**{"AGENT.CLAUDE_BIN": str(fake)}), stage=stage)


def _traced_stage(tmp_path, body, verbose=False):
    @observe.entry("investigate")
    def main(argv=None):
        body()
        return 0

    flag = ["--langfuse-verbose", "true"] if verbose else []
    assert main(["--run-dir", str(tmp_path), *flag]) == 0


def test_agent_records_model_tokens_and_cost(traced, tmp_path):
    _traced_stage(tmp_path, lambda: _agent_run(tmp_path))
    c = traced["client"]
    agent, gen = c.one("agent:T1"), c.one("llm:T1")
    assert agent.parent is c.one("stage:investigate") and gen.parent is agent
    assert agent.type == "agent" and gen.type == "generation" and gen.ended and agent.ended
    assert gen.updates["model"] == "claude-sonnet-5-5"  # bare: no [1m] suffix
    assert gen.updates["cost_details"] == {"total": 0.25}
    assert gen.updates["usage_details"]["cache_read_input_tokens"] == 900
    assert agent.updates["metadata"]["tool_calls"] == 2 and agent.updates["metadata"]["session_id"] == "s1"
    assert not c.named("tool:")  # not verbose


def test_verbose_adds_one_span_per_tool_call(traced, tmp_path):
    _traced_stage(tmp_path, lambda: _agent_run(tmp_path), verbose=True)
    c = traced["client"]
    read, grep = c.one("tool:Read"), c.one("tool:Grep")
    assert read.parent is c.one("agent:T1") and read.type == "tool"
    assert read.kw["input"] == {"file_path": "a.py"} and read.updates["output"] == "print(1)" and read.ended
    assert grep.level == "ERROR"


def test_verbose_from_config(traced, tmp_path):
    traced["cfg"] = Cfg(**{"LANGFUSE.VERBOSE": True})
    _traced_stage(tmp_path, lambda: _agent_run(tmp_path))
    assert len(traced["client"].named("tool:")) == 2


def test_parallel_agents_nest_under_the_stage(traced, tmp_path):
    def body():
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda lab: _agent_run(tmp_path, lab), ["T1", "T2", "UI1"]))

    _traced_stage(tmp_path, body, verbose=True)
    c = traced["client"]
    stage = c.one("stage:investigate")
    for lab in ("T1", "T2", "UI1"):
        a = c.one(f"agent:{lab}")
        assert a.parent is stage and a.thread != threading.main_thread().name
    assert len(c.named("tool:")) == 6


def test_failed_agent_is_an_error(traced, tmp_path):
    from rfc_agent.investigate import agent as ag

    bad = tmp_path / "claude"
    bad.write_text("#!/bin/sh\necho boom >&2\nexit 3\n")
    bad.chmod(0o755)
    spec = ag.AgentSpec("lead", "claude-opus-5-5", None, ("Read",), (), use_context=False, timeout_s=30)

    def body():
        r = ag.run(spec, "x", tmp_path, Cfg(**{"AGENT.CLAUDE_BIN": str(bad)}), step="1a")
        assert not r.ok

    _traced_stage(tmp_path, body)
    a = traced["client"].one("agent:lead:1a")
    assert a.level == "ERROR" and "exit 3" in a.updates["status_message"]


def test_agent_untraced_when_off(monkeypatch, tmp_path):
    monkeypatch.setattr(observe, "_overrides", {"ENABLED": False})
    r = _agent_run(tmp_path)
    assert r.ok and r.cost_usd == 0.25 and r.extra["usage"]["output_tokens"] == 5


# ---- gates, operator, Scope calls -------------------------------------------------------------------------


def test_gate_scores_and_verdict(traced, tmp_path):
    def body():
        observe.gate("draft", {"citations": ["a.py:9 past EOF"], "mermaid": []}, ["size"], kind="draft")
        observe.gate("T1", [], [], kind="investigate_tracer", round_label="after-fix")

    _traced_stage(tmp_path, body)
    c = traced["client"]
    bad, ok = c.one("gate:draft"), c.one("gate:T1:after-fix")
    assert bad.type == "evaluator" and bad.level == "ERROR"
    assert bad.updates["output"]["must_fix"] == ["citations: a.py:9 past EOF"]
    assert bad.kw["metadata"]["failures_by_check"] == {"citations": 1}
    assert {"name": "gate_draft_passed", "value": 0.0, "data_type": "BOOLEAN"} in bad.scores
    assert ok.updates["output"] == "PASS" and ok.scores[0]["name"] == "gate_investigate_tracer_passed"


def test_pause_answers_are_recorded(traced, tmp_path):
    from rfc_agent.investigate import pauses
    from rfc_agent.scope import confirm

    items = [confirm.Item("repo", "svc", "svc"), confirm.Item("repo", "web", "web", include=False)]
    _traced_stage(
        tmp_path,
        lambda: pauses.ask(1, tmp_path, "── Investigate · Pause 1 ──\nmore", items, file_mode=False,
                           accept_defaults=True),
    )
    op = traced["client"].one("operator:pause1")
    assert op.kw["input"]["selected"] == ["repo:svc"] and op.kw["input"]["not_selected"] == ["repo:web"]
    assert op.kw["input"]["header"] == "── Investigate · Pause 1 ──"


def test_scope_summary_call_is_recorded(traced, tmp_path, monkeypatch):
    from rfc_agent.scope import context

    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\nimport json,sys\nsys.stdin.read()\n"
        'print(json.dumps({"total_cost_usd": 0.01, "usage": {"input_tokens": 7},'
        ' "result": json.dumps({"summary": "adds a field", "surfaces": []})}))\n'
    )
    fake.chmod(0o755)

    @observe.entry("scope")
    def main(argv=None):
        assert context.summarise("# PRD", Cfg(**{"AGENT.CLAUDE_BIN": str(fake)}))["ok"]
        return 0

    assert main(["--run-dir", str(tmp_path)]) == 0
    c = traced["client"]
    gen = c.one("llm:prd-summary")
    assert c.one("agent:prd-summary").parent is c.one("stage:scope")
    assert gen.updates["output"]["summary"] == "adds a field" and gen.updates["usage_details"]["input"] == 7


def test_preflight(traced, capsys, monkeypatch):
    assert observe.preflight() == 0 and "langfuse=on keys=ok verbose=false" in capsys.readouterr().out
    traced["cfg"] = Cfg(**{"LANGFUSE.SECRET_KEY": ""})
    assert observe.preflight() == observe.EXIT_NO_KEYS
    out = capsys.readouterr().out
    assert "keys=MISSING" in out and "sk-lf" not in out and "pk-lf" not in out
    monkeypatch.setenv(observe.ENV, "false")
    assert observe.preflight() == 0 and capsys.readouterr().out.strip() == "langfuse=off"


def test_investigate_end_to_end_trace_shape(traced, tmp_path, monkeypatch):
    """The real Investigate (fake claude) under tracing: lead steps named, tracers parallel under the stage, gates."""
    import test_investigate as t1

    run = tmp_path / "run"
    (run / "raw").mkdir(parents=True)
    (run / "raw" / "prd.md").write_text("# PRD\nIf similar…")
    t1._snapshot(run)
    flows = t1._flows()
    flows["flows"][0]["hops"] = flows["flows"][0]["hops"][:1]
    (run / "scope.json").write_text(json.dumps(t1._scope(run, t1._export(run))))
    fake = tmp_path / "claude"
    fake.write_text(
        t1.FAKE_CLAUDE.replace("%REQS%", repr(t1._reqs()))
        .replace("%FLOWS%", repr(flows))
        .replace("%FACTS%", repr(t1.FACTS + "- svc/app/patient.py:99 off by a lot\n"))
    )
    fake.chmod(0o755)
    cfg = Cfg(**{"AGENT.CLAUDE_BIN": str(fake)})
    traced["cfg"] = cfg
    monkeypatch.setattr("rfc_agent.investigate.cli.get_config", lambda: cfg)
    monkeypatch.setattr("rfc_agent.investigate.repo_info.index_info", lambda *a, **k: {})
    from rfc_agent.investigate import cli

    assert cli.main(["--run-dir", str(run), "--yes"]) == 0
    c = traced["client"]
    stage = c.one("stage:investigate")
    assert c.one("agent:lead:1a").parent is stage and c.one("agent:lead:1c").parent is stage
    assert c.one("agent:T1").parent is stage and c.one("agent:UI1").parent is stage
    assert c.one("gate:plan").scores[0] == {"name": "gate_investigate_plan_passed", "value": 1.0,
                                            "data_type": "BOOLEAN"}
    assert c.one("gate:T1").level == "ERROR" and c.one("gate:T1:after-fix").level != "ERROR"  # the fix round
    assert c.one("gate:understanding").updates["output"] == "PASS"
    assert stage.updates["output"] == {"exit": 0} and all(o.ended for o in c.all)


def _client_kwargs(monkeypatch, cfg: dict) -> dict:
    import langfuse

    seen: dict = {}
    monkeypatch.setenv("OTEL_SERVICE_NAME", "rfc-agent")  # _make_client setdefaults it; pytest restores it

    class Recorder:
        def __init__(self, **kw):
            seen.update(kw)

    class Cfg:
        def get(self, k, d=None):
            return {"LANGFUSE.PUBLIC_KEY": "pk", "LANGFUSE.SECRET_KEY": "sk", **cfg}.get(k, d)

    monkeypatch.setattr(langfuse, "Langfuse", Recorder)
    observe._make_client(Cfg())
    return seen


def test_secret_masking_is_on_by_default(monkeypatch):
    assert "mask_otel_spans" in _client_kwargs(monkeypatch, {})


def test_mask_secrets_false_turns_the_scrubber_off(monkeypatch):
    assert "mask_otel_spans" not in _client_kwargs(monkeypatch, {"LANGFUSE.MASK_SECRETS": False})
