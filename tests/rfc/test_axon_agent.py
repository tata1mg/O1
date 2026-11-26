"""The Axon backend: engine selection, the Pi → Claude stream conversion, and one agent run end to end
through a fake `axon` launcher (tests/fixtures/axon/fake_axon.py)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from rfc_agent import axon_agent as ax
from rfc_agent.investigate import agent as ag

FIX = Path(__file__).parents[1] / "fixtures" / "axon"
FAKE = FIX / "fake_axon.py"
FLEETLENS = {"type": "http", "url": "https://fleetlens.example/mcp", "headers": {"Authorization": "Bearer t"}}


class Cfg:
    def __init__(self, **kv):
        self.kv = kv

    def get(self, k, d=None):
        return self.kv.get(k, d)


@pytest.fixture(autouse=True)
def _clean_engine_env(monkeypatch, tmp_path):
    # never the developer's own Axon mcp.json: a fleetlens entry there would change which tools agents get
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "no-axon-home"))
    # setenv first so monkeypatch records the original state and restores it, even after apply_engine_args
    # (which writes os.environ directly, as the CLIs do) — otherwise an engine choice leaks into later tests
    for var in (ax.ENGINE_ENV, ax.STAGE_ENGINES_ENV):
        monkeypatch.setenv(var, "")
        monkeypatch.delenv(var)


# ---- engine selection ---------------------------------------------------------------------------


def test_default_engine_is_claude_code():
    assert ax.engine_for("draft", Cfg()) == "claude-code"
    assert ax.engine_for("draft", None) == "claude-code"


def test_precedence_stage_override_then_run_engine_then_config(monkeypatch):
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.STAGE_ENGINES": {"review": "claude-code"}})
    assert ax.engine_for("draft", cfg) == "axon"
    assert ax.engine_for("review", cfg) == "claude-code"
    monkeypatch.setenv(ax.ENGINE_ENV, "claude-code")  # --engine beats config AGENT.ENGINE
    assert ax.engine_for("draft", cfg) == "claude-code"
    monkeypatch.setenv(ax.STAGE_ENGINES_ENV, json.dumps({"draft": "axon"}))  # --stage-engine beats --engine
    assert ax.engine_for("draft", cfg) == "axon"


def test_unknown_engine_is_refused():
    with pytest.raises(ValueError, match="unknown engine"):
        ax.engine_for("draft", Cfg(**{"AGENT.ENGINE": "codex"}))


def test_stage_engine_flag_parsing(monkeypatch):
    assert ax.parse_stage_engines(["review=axon", "draft=claude-code,summarize=axon"]) == {
        "review": "axon", "draft": "claude-code", "summarize": "axon"}
    with pytest.raises(ValueError):
        ax.parse_stage_engines(["review"])
    with pytest.raises(ValueError):
        ax.parse_stage_engines(["review=gpt"])
    ax.apply_engine_args("axon", ["review=claude-code"])
    assert os.environ[ax.ENGINE_ENV] == "axon"
    assert json.loads(os.environ[ax.STAGE_ENGINES_ENV]) == {"review": "claude-code"}


def test_a_typo_in_a_stage_name_is_refused_not_ignored(monkeypatch):
    with pytest.raises(ValueError, match="reveiw"):
        ax.parse_stage_engines(["reveiw=axon"])
    with pytest.raises(ValueError, match="unknown stage 'reveiw'"):
        ax.engine_for("review", Cfg(**{"AGENT.STAGE_ENGINES": {"reveiw": "axon"}}))
    with pytest.raises(ValueError, match="map"):
        ax.engine_for("review", Cfg(**{"AGENT.STAGE_ENGINES": ["review"]}))
    monkeypatch.setenv(ax.STAGE_ENGINES_ENV, "null")  # valid JSON, not a map
    with pytest.raises(ValueError, match="map"):
        ax.engine_for("review", Cfg())
    with pytest.raises(ValueError, match="map"):
        ax.apply_engine_args(None, ["review=axon"])


def test_engines_command_resolves_flags_over_config(monkeypatch, capsys):
    import rfc_agent.config as config

    monkeypatch.setattr(config, "get_config", lambda: Cfg(**{"AGENT.ENGINE": "axon"}))
    assert ax.main(["engines", "--prd", "x.md", "--stage-engine", "scope=claude-code,review=claude-code"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert got["scope"] == "claude-code" and got["review"] == "claude-code" and got["draft"] == "axon"
    assert set(got) == set(ax.STAGE_NAMES)


def test_run_json_lists_only_the_stages_that_ran(monkeypatch):
    import rfc_agent.config as config
    from rfc_agent import run as rfc_run

    monkeypatch.setattr(config, "get_config", lambda: Cfg(**{"AGENT.ENGINE": "axon"}))
    assert rfc_run._engines([{"stage": "review"}]) == {"review": "axon"}  # --from review: nothing earlier
    assert rfc_run._engines([{"stage": "revise"}]) == {"revise": "axon", "summarize": "axon"}
    assert rfc_run._engines([]) == {}


def test_every_stage_cli_and_the_pipeline_accept_the_engine_flags():
    from rfc_agent.run import build_parser

    args = build_parser().parse_args(["--prd", "x.md", "--engine", "axon", "--stage-engine", "review=claude-code"])
    assert args.engine == "axon" and args.stage_engine == ["review=claude-code"]


# ---- models -------------------------------------------------------------------------------------


@pytest.mark.parametrize("claude,gateway", [
    ("claude-opus-5-5[1m]", "anthropic/claude-opus-5.5"),
    ("claude-opus-5-5", "anthropic/claude-opus-5.5"),
    ("claude-sonnet-5-5", "anthropic/claude-sonnet-5.5"),
    ("claude-sonnet-5[1m]", "anthropic/claude-sonnet-5.5"),
    ("claude-haiku-4-5-20251001", "gemini-3.8-flash"),
    ("openai/gpt-6-luna", "openai/gpt-6-luna"),  # already a gateway id: untouched
])
def test_claude_models_map_to_gateway_models(claude, gateway):
    assert ax.gateway_model(claude, Cfg()) == gateway


def test_model_overrides_by_tier_and_by_exact_id():
    cfg = Cfg(**{"AGENT.AXON": {"MODELS": {"opus": "deepseek/deepseek-v4.1-flash",
                                           "claude-sonnet-5-5": "openai/gpt-6-luna"}}})
    assert ax.gateway_model("claude-opus-5-5", cfg) == "deepseek/deepseek-v4.1-flash"
    assert ax.gateway_model("claude-opus-5-5[1m]", cfg) == "anthropic/claude-opus-5.5"  # [1m] tier keeps its default
    assert ax.gateway_model("claude-sonnet-5-5", cfg) == "openai/gpt-6-luna"


# ---- stream conversion --------------------------------------------------------------------------


def _convert(fixture: str, cwd: Path) -> tuple[ax.PiToClaude, list[dict]]:
    shim = ax.PiToClaude(session_id="sid", model="m", cwd=cwd, mcp=["fleetlens"])
    out: list[dict] = []
    for line in (FIX / fixture).read_text().splitlines():
        out += shim.feed(json.loads(line.replace("__SID__", "sid")))
    return shim, out


def test_pi_stream_becomes_claude_stream_json(tmp_path):
    shim, out = _convert("pi.sample.jsonl", tmp_path)
    assert out[0]["type"] == "system" and out[0]["subtype"] == "init" and out[0]["session_id"] == "sid"
    uses = [b for ev in out if ev["type"] == "assistant" for b in ev["message"]["content"] if b["type"] == "tool_use"]
    assert [u["name"] for u in uses] == ["Read", "mcp__fleetlens__find_symbol", "Write"]
    assert uses[0]["input"] == {"file_path": str(tmp_path / "_snap/repo/a.py")}  # what _code_reads counts
    results = [b for ev in out if ev["type"] == "user" for b in ev["message"]["content"]]
    assert [r["tool_use_id"] for r in results] == ["c1", "c2", "c3"]
    final = shim.result()
    assert final["subtype"] == "success" and not final["is_error"]
    assert final["result"] == "done." and final["num_turns"] == 4
    assert final["total_cost_usd"] == pytest.approx(0.05)


def test_a_provider_error_is_a_failed_result_even_though_pi_settles(tmp_path):
    shim, _ = _convert("pi.error.jsonl", tmp_path)
    final = shim.result()
    assert final["is_error"] and "429 rate limit" in final["result"]


def test_a_stream_without_agent_settled_did_not_finish(tmp_path):
    shim, _ = _convert("pi.truncated.jsonl", tmp_path)
    final = shim.result()
    assert final["is_error"] and "agent_settled" in final["result"]


def test_a_disconnect_that_pi_retried_past_is_not_a_failure(tmp_path):
    # axon-1: Pi's own retry got the Draft lead through, but the error was kept and the stage restarted it
    shim, _ = _convert("pi.retried.jsonl", tmp_path)
    final = shim.result()
    assert not final["is_error"] and final["result"] == "done."


def test_a_disconnect_pi_could_not_retry_past_is_a_failure(tmp_path):
    shim, _ = _convert("pi.disconnect.jsonl", tmp_path)
    final = shim.result()
    assert final["is_error"] and ax.disconnected(final["result"])


def test_disconnected_matches_only_connection_failures():
    assert ax.disconnected("assistant stopReason=error: Connection error: the connection to Axon AI Gateway "
                           "was interrupted.")
    assert ax.disconnected("provider retries exhausted: socket hang up")
    assert not ax.disconnected("assistant stopReason=error: 429 rate limit")
    assert not ax.disconnected("timed out after 600s") and not ax.disconnected("")


def _assistant_event(stop, text="", error=""):
    return {"type": "message_end", "message": {
        "role": "assistant", "stopReason": stop, "errorMessage": error,
        "content": [{"type": "text", "text": text}] if text else [],
    }}


def _recovered_events(*, retry_end_first=False, stop="stop"):
    events = [
        {"type": "agent_start"},
        _assistant_event("error", error="502 Connection error."),
        {"type": "agent_end", "willRetry": True},
        {"type": "auto_retry_start", "attempt": 1},
    ]
    recovered = {"type": "auto_retry_end", "success": True, "attempt": 1}
    success = _assistant_event(stop, "done")
    events += [recovered, success] if retry_end_first else [success, recovered]
    return events


@pytest.mark.parametrize("retry_end_first", [False, True])
@pytest.mark.parametrize("stop", ["stop", "toolUse"])
def test_recovered_provider_error_is_not_a_failed_result(tmp_path, retry_end_first, stop):
    shim = ax.PiToClaude(session_id="sid", model="m", cwd=tmp_path)
    for event in [*_recovered_events(retry_end_first=retry_end_first, stop=stop),
                  {"type": "agent_settled", "aborted": False}]:
        shim.feed(event)
    assert not shim.result()["is_error"]
    assert shim.result()["result"] == "done"


@pytest.mark.parametrize("ending", [
    "retry_only", "truncated", "later_error", "aborted", "length", "pending", "assistant_aborted",
])
def test_recovery_does_not_hide_an_unfinished_or_failed_run(tmp_path, ending):
    shim = ax.PiToClaude(session_id="sid", model="m", cwd=tmp_path)
    if ending == "retry_only":
        events = [_assistant_event("error", error="502"), {"type": "auto_retry_end", "success": True}]
    else:
        events = _recovered_events()
    if ending == "later_error":
        events.append(_assistant_event("error", error="401 unauthorized"))
    elif ending in {"length", "pending", "assistant_aborted"}:
        # Pi can emit retry success after any non-error stop, including length/aborted.
        stop = "aborted" if ending == "assistant_aborted" else ending
        events += [_assistant_event(stop, "partial"), {"type": "auto_retry_end", "success": True}]
    if ending != "truncated":
        events.append({"type": "agent_settled", "aborted": ending == "aborted"})
    for event in events:
        shim.feed(event)
    assert shim.result()["is_error"]


@pytest.mark.parametrize("terminal", [
    {"type": "auto_retry_end", "success": False, "finalError": "502 retries exhausted"},
    {"type": "agent_settled", "aborted": True},
])
def test_a_later_assistant_message_cannot_clear_a_terminal_failure(tmp_path, terminal):
    shim = ax.PiToClaude(session_id="sid", model="m", cwd=tmp_path)
    for event in [_assistant_event("error", error="502"), terminal,
                  _assistant_event("stop", "done"), {"type": "auto_retry_end", "success": True},
                  {"type": "agent_settled", "aborted": False}]:
        shim.feed(event)
    assert shim.result()["is_error"]


# ---- one agent through the fake launcher --------------------------------------------------------


def _fake(tmp_path, monkeypatch, stream: str = "pi.sample.jsonl") -> Path:
    exe = tmp_path / "bin" / "fake_axon.py"
    exe.parent.mkdir()
    exe.write_text(FAKE.read_text())
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    dump = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_AXON_ARGV", str(dump))
    monkeypatch.setenv("FAKE_AXON_STREAM", str(FIX / stream))
    monkeypatch.setenv("FAKE_AXON_OUTPUTS", "out/x.md")
    return exe


def _spec(**kw) -> ag.AgentSpec:
    base = dict(label="lead", model="claude-opus-5-5[1m]", effort="high",
                builtin_tools=("Read", "Grep", "Glob", "Write"), write_allow=("out/*",), use_context=True,
                call_budget=30, timeout_s=60)
    base.update(kw)
    return ag.AgentSpec(**base)


def test_agent_run_on_axon_end_to_end(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)},
                 "MCP": {"mcpServers": {"fleetlens": FLEETLENS}}})
    ticks: list[tuple] = []
    res = ag.run(_spec(), "the prompt", run, cfg, on_event=lambda *a: ticks.append(a), stage="draft", step="write")

    assert res.ok, res.error
    assert res.tool_calls == 3 and res.final_text == "done." and res.session_id
    assert res.cost_usd == pytest.approx(0.05)  # summed from the session file the fake wrote
    assert (run / "out" / "x.md").exists()
    assert [t[1] for t in ticks][:2] == ["start", "tool"] and ticks[-1][1] == "end"

    dumped = json.loads((tmp_path / "argv.json").read_text())
    argv = dumped["argv"]
    assert dumped["prompt"] == "the prompt"  # on stdin, not argv
    assert "the prompt" not in argv
    assert argv[1] == "pi" and "--no-extensions" not in argv
    exts = [argv[i + 1] for i, a in enumerate(argv) if a == "-e"]
    assert [Path(e).name for e in exts] == ["harness-guard.js", "rfc-tools.js"]
    for f in ("--no-approve", "--no-skills", "--no-context-files", "--no-prompt-templates", "--no-themes"):
        assert f in argv
    assert argv[argv.index("--provider") + 1] == "litellm"
    assert argv[argv.index("--model") + 1] == "anthropic/claude-opus-5.5"
    assert argv[argv.index("--thinking") + 1] == "high"
    tools = argv[argv.index("--tools") + 1].split(",")
    assert tools[:5] == ["read", "grep", "find", "write", "ls"]
    assert "mcp__fleetlens__find_symbol" in tools and "bash" not in tools
    assert argv[argv.index("--session-id") + 1] == res.session_id

    env = dumped["env"]
    assert env["PI_OFFLINE"] is None and env["PI_CODING_AGENT_DIR"] is None
    assert env["INVESTIGATE_WRITE_ALLOW"] == "out/*" and env["INVESTIGATE_CALL_BUDGET"] == "30"
    policy = json.loads(env["HARNESS_POLICY_JSON"])
    assert policy["writable_roots"] == [str(run.resolve())]
    assert "mcp__fleetlens__find_symbol" in policy["granted_tools"]
    tools_cfg = Path(env["RFC_TOOLS_CONFIG"])
    assert stat.S_IMODE(tools_cfg.stat().st_mode) == 0o600  # it carries the fleetlens token
    body = json.loads(tools_cfg.read_text())
    assert body["mcp"]["fleetlens"] == {"url": FLEETLENS["url"], "headers": FLEETLENS["headers"]}
    assert body["hook"]["cmd"][1].endswith("investigate/hook.py")
    assert body["threshold_compaction"] is False

    stream = Path(res.stream)
    assert stream.name == "draft.lead.write.stream.jsonl"
    events = [json.loads(ln) for ln in stream.read_text().splitlines()]
    assert events[0]["type"] == "system" and events[-1]["type"] == "result"
    assert stream.with_name("draft.lead.write.pi.jsonl").exists()
    from rfc_agent.investigate.cli import _code_reads

    assert _code_reads(stream) == 1  # the Read of _snap/repo/a.py, counted exactly as on Claude


def test_resume_continues_the_saved_session(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)}})
    first = ag.run(_spec(use_context=False), "one", run, cfg, stage="draft")
    second = ag.run(_spec(use_context=False), "two", run, cfg, resume=first.session_id, stage="draft")
    assert second.ok and second.session_id == first.session_id
    argv = json.loads((tmp_path / "argv.json").read_text())["argv"]
    assert "--session-id" not in argv
    assert argv[argv.index("--session") + 1].endswith(f"_{first.session_id}.jsonl")
    assert second.cost_usd == pytest.approx(0.05)  # this invocation only, not the whole session file


def test_a_lost_gateway_resumes_the_same_session_instead_of_starting_over(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch, stream="pi.disconnect.jsonl")
    monkeypatch.setenv("FAKE_AXON_RESUME_STREAM", str(FIX / "pi.sample.jsonl"))
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)}})
    ticks: list[tuple] = []
    res = ag.run(_spec(use_context=False), "write the RFC", run, cfg, stage="draft", step="write",
                 on_event=lambda *a: ticks.append(a))
    assert res.ok, res.error
    assert res.extra["reconnects"] == 1 and res.final_text == "done."
    dumped = json.loads((tmp_path / "argv.json").read_text())
    argv = dumped["argv"]  # the last call: a resume of the first call's session, told to carry on
    assert "--session-id" not in argv and argv[argv.index("--session") + 1].endswith(f"_{res.session_id}.jsonl")
    assert dumped["prompt"] == ax.RECONNECT_PROMPT
    assert res.cost_usd == pytest.approx(0.1 + 0.05)  # both calls: the lost one and the resume
    assert res.tool_calls == 1 + 3
    assert (run / "logs" / "draft.lead.write.stream.jsonl").exists()
    assert (run / "logs" / "draft.lead.write-reconnect.stream.jsonl").exists()
    assert any(t[1] == "text" and "gateway disconnected" in t[2]["text"] for t in ticks)


def test_reconnects_stop_after_the_configured_number(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch, stream="pi.disconnect.jsonl")
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe), "RECONNECTS": 1}})
    res = ag.run(_spec(use_context=False), "p", run, cfg, stage="draft")
    assert not res.ok and ax.disconnected(res.error) and res.extra["reconnects"] == 1
    cfg0 = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe), "RECONNECTS": 0}})
    assert "reconnects" not in ag.run(_spec(use_context=False), "p", run, cfg0, stage="draft").extra


def test_resume_of_a_session_axon_never_saw_fails_without_starting(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)}})
    res = ag.run(_spec(), "p", run, cfg, resume="claude-session-from-an-earlier-run", stage="draft")
    assert not res.ok and "cannot resume" in res.error
    assert not (tmp_path / "argv.json").exists()  # nothing was started — callers rerun cold, as on Claude


def test_a_failed_agent_reports_the_provider_error(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch, stream="pi.error.jsonl")
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)}})
    res = ag.run(_spec(use_context=False), "p", run, cfg, stage="review")
    assert not res.ok and "429 rate limit" in res.error


@pytest.mark.parametrize("exit_code", [0, 3])
def test_recovered_retry_through_the_agent_runner(tmp_path, monkeypatch, exit_code):
    exe = _fake(tmp_path, monkeypatch)
    events = [*_recovered_events(), {"type": "agent_settled", "aborted": False}]
    stream = tmp_path / "recovered.jsonl"
    stream.write_text("\n".join(json.dumps(event) for event in events) + "\n")
    monkeypatch.setenv("FAKE_AXON_STREAM", str(stream))
    monkeypatch.setenv("FAKE_AXON_EXIT", str(exit_code))
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)}})
    res = ag.run(_spec(use_context=False), "p", run, cfg, stage="investigate")
    assert res.ok == (exit_code == 0), res.error
    if exit_code:
        assert "exited 3 after the turn finished" in res.error
    else:
        assert res.final_text == "done"


def test_investigate_does_not_restart_a_lead_after_a_recovered_retry(tmp_path, monkeypatch):
    from argparse import Namespace

    from rfc_agent.investigate.cli import Run
    from rfc_agent.investigate.config import resolve

    exe = _fake(tmp_path, monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)}})
    first = ag.run(_spec(use_context=False), "initial plan", run, cfg)
    assert first.ok
    stream = tmp_path / "recovered.jsonl"
    stream.write_text("\n".join(json.dumps(event) for event in [
        *_recovered_events(), {"type": "agent_settled", "aborted": False},
    ]) + "\n")
    monkeypatch.setenv("FAKE_AXON_STREAM", str(stream))
    investigation = Run(run, cfg, resolve(cfg), Namespace(fresh=False))
    investigation.state["lead_session"] = first.session_id
    original = ag.run
    calls = []

    def counted(*args, **kwargs):
        calls.append(kwargs.get("resume"))
        return original(*args, **kwargs)

    monkeypatch.setattr(ag, "run", counted)
    result = investigation.run_lead("integrate", ("work/understanding.md",), "1c")
    assert result.ok, result.error
    assert calls == [first.session_id]  # no unnecessary fresh lead and duplicate investigation spend
    assert investigation.agents["lead"]["runs"][-1]["ok"]


def test_a_failed_exit_after_a_finished_turn_is_an_error(tmp_path, monkeypatch):
    exe = _fake(tmp_path, monkeypatch)
    monkeypatch.setenv("FAKE_AXON_EXIT", "3")
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)}})
    res = ag.run(_spec(use_context=False), "p", run, cfg, stage="draft")
    assert not res.ok and "exited 3 after the turn finished" in res.error


def test_a_missing_launcher_is_a_clear_error(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(tmp_path / "nope" / "axon-missing")}})
    res = ag.run(_spec(use_context=False), "p", run, cfg, stage="review")
    assert not res.ok and "install Axon" in res.error


def test_the_default_engine_never_touches_axon(tmp_path, monkeypatch):
    """claude-code stays the default: a run with no engine chosen starts the Claude binary, not axon."""
    calls = tmp_path / "claude.calls"
    claude = tmp_path / "claude"
    claude.write_text(
        "#!/usr/bin/env python3\nimport json,sys\n"
        f"open({str(calls)!r},'a').write(json.dumps(sys.argv)+'\\n')\n"
        "sys.stdin.read()\n"
        "print(json.dumps({'type':'system','subtype':'init','session_id':'s1'}))\n"
        "print(json.dumps({'type':'result','subtype':'success','total_cost_usd':0.01,'num_turns':1}))\n")
    claude.chmod(0o755)
    run = tmp_path / "run"
    run.mkdir()
    res = ag.run(_spec(use_context=False), "p", run, Cfg(**{"AGENT.CLAUDE_BIN": str(claude)}), stage="draft")
    assert res.ok and res.session_id == "s1"
    argv = json.loads(calls.read_text().splitlines()[0])
    assert argv[1:4] == ["-p", "--output-format", "stream-json"]
    assert not (run / "logs" / "pi-sessions").exists()


# ---- the context layer behind codemode (issue #16) ------------------------------------------------------


def _axon_home(tmp_path, monkeypatch, entry: dict | None) -> Path:
    """An Axon user dir whose mcp.json has (or lacks) the operator's own fleetlens entry."""
    home = tmp_path / "axon-home"
    home.mkdir(exist_ok=True)
    servers = {"atlassian": {"url": "https://mcp.atlassian.com/v1/mcp"}}
    if entry is not None:
        servers["fleetlens"] = entry
    (home / "mcp.json").write_text(json.dumps({"mcpServers": servers}))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home))
    return home


_O1 = Cfg(**{"MCP": {"mcpServers": {"fleetlens": FLEETLENS}}})
_THEIRS = {"url": "https://theirs.example/mcp", "headers": {"Authorization": "Bearer theirs"}}


def test_without_an_axon_entry_o1s_own_registration_is_used_direct(tmp_path, monkeypatch):
    _axon_home(tmp_path, monkeypatch, None)
    a = ax.context_access(_O1)
    assert a["mode"] == "direct" and a["source"] == "o1" and a["spec"] == FLEETLENS
    assert ax.context_access(Cfg())["mode"] == "off"  # nothing configured anywhere


@pytest.mark.parametrize("extra, mode", [
    ({}, "codemode"),                                   # Pi's default exposure
    ({"exposure": "codemode-deferred"}, "codemode"),    # Pi's alias
    ({"exposure": "deferred"}, "codemode"),             # codemode scripts reach deferred tools too
    ({"exposure": "direct"}, "direct"),
    ({"exposure": "direct", "toolExposure": {"get_*": "codemode"}}, "codemode"),
    ({"exposure": "hidden"}, "off"),
    ({"exposure": "hidden", "toolExposure": {"find_symbol": "direct"}}, "direct"),
    ({"exposure": "direct", "enabled": False}, "off"),
    ({"exposure": "weird"}, "off"),
])
def test_an_axon_entry_wins_and_its_exposure_decides(tmp_path, monkeypatch, extra, mode):
    _axon_home(tmp_path, monkeypatch, {**_THEIRS, **extra})
    a = ax.context_access(_O1)
    assert a["mode"] == mode and a["source"] == "axon"
    assert a["spec"] == _THEIRS  # the server the agent will really use is the one to health-check
    if mode == "off":
        assert "replaces O1's" in a["note"] or "does not support" in a["note"]


def test_env_headers_are_resolved_like_pi_does(tmp_path, monkeypatch):
    _axon_home(tmp_path, monkeypatch, {"url": "https://theirs.example/mcp",
                                       "headers": {"Authorization": "Bearer ${FL_TOKEN}"}})
    monkeypatch.setenv("FL_TOKEN", "abc")
    a = ax.context_access(_O1)
    assert a["spec"] == {"url": "https://theirs.example/mcp", "headers": {"Authorization": "Bearer abc"}}
    monkeypatch.delenv("FL_TOKEN")
    a = ax.context_access(_O1)
    assert a["spec"] is None and "${FL_TOKEN}" in a["unverified"]


@pytest.mark.parametrize("headers", [{"Authorization": "!pass show fleetlens"}, {"Authorization": "Bearer ${NOPE_X}"}])
def test_headers_python_cannot_know_are_never_checked_against_o1s_server(tmp_path, monkeypatch, headers):
    """PR #31 review: checking O1's server in place of the one the agent will use vouches for the wrong one."""
    monkeypatch.delenv("NOPE_X", raising=False)
    _axon_home(tmp_path, monkeypatch, {"url": "https://theirs.example/mcp", "headers": headers})
    checked = []
    probe = dict(health=lambda s: (checked.append(s) or True, "ok"), spec_of=lambda cfg: FLEETLENS)
    ok, why = ax.context_health(_O1, **probe, strict=True)  # doctor: a problem to look at
    assert not ok and "could not be checked" in why and checked == []
    ok, why = ax.context_health(_O1, **probe)  # Scope: goes on, says so; the zero-lookup check still guards it
    assert ok and why.startswith("not verified") and checked == []


def test_context_health_checks_the_server_the_agent_will_use(tmp_path, monkeypatch):
    _axon_home(tmp_path, monkeypatch, dict(_THEIRS))
    seen = []
    ok, why = ax.context_health(_O1, health=lambda s: (seen.append(s["url"]) or True, "ok · 15/15 tools"),
                                spec_of=lambda cfg: FLEETLENS)
    assert ok and seen == ["https://theirs.example/mcp"] and "agent access: through codemode" in why
    _axon_home(tmp_path, monkeypatch, {**_THEIRS, "exposure": "hidden"})
    ok, why = ax.context_health(_O1, health=lambda s: (True, "ok"), spec_of=lambda cfg: FLEETLENS)
    assert not ok and "hides every tool" in why  # reachable is not usable


def test_a_codemode_entry_grants_the_script_runner_and_says_how(tmp_path, monkeypatch):
    _axon_home(tmp_path, monkeypatch, dict(_THEIRS))
    exe = _fake(tmp_path, monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)},
                 "MCP": {"mcpServers": {"fleetlens": FLEETLENS}}})
    res = ag.run(_spec(), "the prompt", run, cfg, stage="scope", step="run")
    assert res.ok, res.error
    dumped = json.loads((tmp_path / "argv.json").read_text())
    tools = dumped["argv"][dumped["argv"].index("--tools") + 1].split(",")
    assert "codemode" in tools and "mcp__fleetlens__find_symbol" in tools and "bash" not in tools
    policy = json.loads(dumped["env"]["HARNESS_POLICY_JSON"])
    assert "codemode" in policy["granted_tools"]
    assert dumped["prompt"].startswith("the prompt") and "tools.mcp__fleetlens__" in dumped["prompt"]


def test_a_direct_setup_is_unchanged(tmp_path, monkeypatch):
    _axon_home(tmp_path, monkeypatch, None)
    exe = _fake(tmp_path, monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)},
                 "MCP": {"mcpServers": {"fleetlens": FLEETLENS}}})
    assert ag.run(_spec(), "the prompt", run, cfg, stage="scope", step="run").ok
    dumped = json.loads((tmp_path / "argv.json").read_text())
    assert "codemode" not in dumped["argv"][dumped["argv"].index("--tools") + 1].split(",")
    assert dumped["prompt"] == "the prompt"


@pytest.mark.parametrize("extra, tools", [
    ({"exposure": "hidden", "toolExposure": {"find_symbol": "direct"}}, ["find_symbol"]),
    ({"exposure": "direct", "toolExposure": {"get_*": "hidden"}}, None),
    ({"exposure": "codemode", "toolExposure": {"get_source": "hidden", "find_*": "direct"}}, None),
])
def test_only_the_tools_the_entry_shows_are_reachable(tmp_path, monkeypatch, extra, tools):
    """PR #31 review: the grant listed all 15 even when the operator's entry hid some."""
    _axon_home(tmp_path, monkeypatch, {**_THEIRS, **extra})
    a = ax.context_access(_O1)
    every = list(ag.CONTEXT_TOOLS)
    if tools is None:
        hidden = {"get_*": [t for t in every if t.startswith("get_")], "get_source": ["get_source"]}
        gone = hidden["get_source"] if "get_source" in extra.get("toolExposure", {}) else hidden["get_*"]
        tools = [t for t in every if t not in gone]
    assert a["tools"] == tools and a["mode"] != "off"
    assert "of 15 tools" in a["note"]


def test_the_grant_follows_the_exposed_tools(tmp_path, monkeypatch):
    _axon_home(tmp_path, monkeypatch, {**_THEIRS, "exposure": "hidden", "toolExposure": {"find_symbol": "direct"}})
    exe = _fake(tmp_path, monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)},
                 "MCP": {"mcpServers": {"fleetlens": FLEETLENS}}})
    ag.run(_spec(), "the prompt", run, cfg, stage="scope", step="run")
    dumped = json.loads((tmp_path / "argv.json").read_text())
    tools = dumped["argv"][dumped["argv"].index("--tools") + 1].split(",")
    assert [t for t in tools if t.startswith("mcp__")] == ["mcp__fleetlens__find_symbol"]
    assert "codemode" not in tools
    assert json.loads(dumped["env"]["HARNESS_POLICY_JSON"])["granted_tools"] == ["mcp__fleetlens__find_symbol"]


def test_an_unusable_entry_grants_no_context_tools(tmp_path, monkeypatch):
    _axon_home(tmp_path, monkeypatch, {**_THEIRS, "enabled": False})
    exe = _fake(tmp_path, monkeypatch)
    run = tmp_path / "run"
    run.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "axon", "AGENT.AXON": {"BIN": str(exe)},
                 "MCP": {"mcpServers": {"fleetlens": FLEETLENS}}})
    ag.run(_spec(), "the prompt", run, cfg, stage="scope", step="run")
    dumped = json.loads((tmp_path / "argv.json").read_text())
    tools = dumped["argv"][dumped["argv"].index("--tools") + 1].split(",")
    assert not any(t.startswith("mcp__") or t == "codemode" for t in tools)


def test_calls_a_codemode_script_made_become_their_own_tool_calls(tmp_path):
    shim, out = _convert("pi.codemode.jsonl", tmp_path)
    uses = [b for ev in out if ev["type"] == "assistant" for b in ev["message"]["content"] if b["type"] == "tool_use"]
    assert [u["name"] for u in uses] == ["codemode", "mcp__fleetlens__get_service_graph",
                                         "mcp__fleetlens__find_symbol"]
    nested = [ev for ev in out if ev.get("parent_tool_use_id") == "cm1"]
    assert len(nested) == 4  # two tool_use + two tool_result, each marked with the script's call
    results = {b["tool_use_id"]: b for ev in out if ev["type"] == "user" for b in ev["message"]["content"]}
    assert results["cm1/1"]["content"] == "graph" and not results["cm1/1"]["is_error"]
    assert results["cm1/2"]["is_error"] and results["cm1"]["content"] == "Script completed"
    assert shim.turns == 2  # the script's calls are not model turns
    assert shim.result()["result"] == "done."


def test_run_metrics_and_discovery_count_calls_made_from_a_script(tmp_path):
    from rfc_agent import run_metrics
    from rfc_agent.scope import context

    _, out = _convert("pi.codemode.jsonl", tmp_path)
    stream = tmp_path / "s.stream.jsonl"
    stream.write_text("\n".join(json.dumps(e) for e in out) + "\n")
    calls: dict = {}
    for ev in out:
        for b in (ev.get("message") or {}).get("content") or []:
            if b.get("type") == "tool_use":
                calls[b["id"]] = {"name": b["name"]}
            elif b.get("type") == "tool_result" and b["tool_use_id"] in calls:
                calls[b["tool_use_id"]].update(output=b["content"], is_error=b["is_error"])
    assert context.successful_lookups(calls) == 1  # the failed find_symbol and the codemode call don't count
    text = stream.read_text()
    assert text.count('"mcp__fleetlens__') == 2
    assert run_metrics.CONTEXT_SERVER == "fleetlens"
