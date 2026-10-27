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
def _clean_engine_env(monkeypatch):
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


def test_every_stage_cli_and_the_pipeline_accept_the_engine_flags():
    from rfc_agent.run import build_parser

    args = build_parser().parse_args(["--prd", "x.md", "--engine", "axon", "--stage-engine", "review=claude-code"])
    assert args.engine == "axon" and args.stage_engine == ["review=claude-code"]


# ---- models -------------------------------------------------------------------------------------


@pytest.mark.parametrize("claude,gateway", [
    ("claude-opus-5-5[1m]", "Kimi-k3.1-pro"),
    ("claude-opus-5-5", "openai/gpt-6.1-sol"),
    ("claude-sonnet-5-5", "openai/gpt-6.1-terra"),
    ("claude-sonnet-5[1m]", "openai/gpt-6.1-terra"),
    ("claude-haiku-4-5-20251001", "gemini-3.8-flash"),
    ("openai/gpt-6-luna", "openai/gpt-6-luna"),  # already a gateway id: untouched
])
def test_claude_models_map_to_gateway_models(claude, gateway):
    assert ax.gateway_model(claude, Cfg()) == gateway


def test_model_overrides_by_tier_and_by_exact_id():
    cfg = Cfg(**{"AGENT.AXON": {"MODELS": {"opus": "deepseek/deepseek-v4.1-flash",
                                           "claude-sonnet-5-5": "openai/gpt-6-luna"}}})
    assert ax.gateway_model("claude-opus-5-5", cfg) == "deepseek/deepseek-v4.1-flash"
    assert ax.gateway_model("claude-opus-5-5[1m]", cfg) == "Kimi-k3.1-pro"  # [1m] tier keeps its default
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
    assert argv[argv.index("--model") + 1] == "Kimi-k3.1-pro"
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
