"""Vanilla Pi shares the RFC transport without launching Axon or borrowing its credentials."""
import json
import sys
from pathlib import Path

import pytest

from rfc_agent import axon_agent as ax
from rfc_agent.investigate import agent as ag
from rfc_agent.scope import atlassian, cli, context

FIX = Path(__file__).parents[1] / "fixtures" / "axon"


class Cfg:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


@pytest.fixture(autouse=True)
def clean_engine_env(monkeypatch):
    for name in (ax.ENGINE_ENV, ax.STAGE_ENGINES_ENV):
        monkeypatch.delenv(name, raising=False)


def test_pi_cli_and_stage_selection():
    from rfc_agent.run import build_parser

    args = build_parser().parse_args(["--prd", "prd.md", "--engine", "pi"])
    assert args.engine == "pi"
    assert ax.engine_for("draft", Cfg(**{"AGENT.ENGINE": "pi"})) == "pi"
    assert ax.parse_stage_engines(["scope=claude-code,review=pi"]) == {"scope": "claude-code", "review": "pi"}


def test_pi_settings_do_not_inherit_axon_settings(tmp_path):
    cfg = Cfg(**{"AGENT.AXON": {"BIN": "not-pi", "PROVIDER": "litellm"},
                 "AGENT.PI": {"PROVIDER": "openrouter", "AGENT_DIR": str(tmp_path)}})
    settings = ax.settings(cfg, engine="pi")
    assert settings["bin"] == "pi"
    assert settings["provider"] == "openrouter"
    assert settings["agent_dir"] == str(tmp_path)


def test_pi_provider_matches_the_shipped_model_ids_without_a_pi_block():
    assert ax.settings(Cfg(), engine="pi")["provider"] == "openrouter"
    assert ax.settings(Cfg(**{"AGENT.PI": {"PROVIDER": ""}}), engine="pi")["provider"] == ""  # Pi's own pick
    assert ax.settings(Cfg(), engine="axon")["provider"] == "litellm"


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5-5[1m]", "anthropic/claude-opus-5.5"),
    ("claude-sonnet-5-5", "anthropic/claude-sonnet-5.5"),
    ("claude-haiku-5-5", "anthropic/claude-haiku-5.5"),
    ("anthropic/claude-haiku-5.5", "anthropic/claude-haiku-5.5"),
])
def test_pi_model_tiers(model, expected):
    assert ax.gateway_model(model, Cfg(), engine="pi") == expected


def test_pi_model_override_is_separate():
    cfg = Cfg(**{"AGENT.PI": {"MODELS": {"sonnet": "openai/gpt-6.1-sol"}}})
    assert ax.gateway_model("claude-sonnet-5-5", cfg, engine="pi") == "openai/gpt-6.1-sol"
    assert ax.gateway_model("claude-sonnet-5-5", cfg) == "anthropic/claude-sonnet-5.5"


def test_native_argv_has_no_axon_subcommand_and_keeps_containment(tmp_path):
    argv = ax.build_argv(bin="pi", provider="openrouter", model="anthropic/claude-sonnet-5.5",
                         thinking="low", tools=["read", "mcp__fleetlens__get_index_info"],
                         session_dir=tmp_path, session_id="sid", resume_file=None, name="test", engine="pi")
    assert argv[:4] == ["pi", "--mode", "json", "-p"]
    assert "--no-context-files" in argv and "--no-approve" in argv
    assert str(ax.GUARD_JS) in argv and str(ax.RFC_TOOLS_JS) in argv
    assert argv[argv.index("--provider") + 1] == "openrouter"


def test_native_env_uses_own_home_and_drops_axon_relay(tmp_path, monkeypatch):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", "/axon/home")
    monkeypatch.setenv("PI_AXON_AUTH_RELAY_URL", "http://127.0.0.1:1234")
    monkeypatch.setenv("PI_AXON_AUTH_RELAY_KEY", "fake-capability")
    monkeypatch.setenv("LITELLM_API_KEY", "fake-relay-key")
    monkeypatch.setenv("LITELLM_BASE_URL", "http://127.0.0.1:1234/litellm")
    monkeypatch.setenv("PI_ORG_OTEL_MANAGED_ENDPOINT", "http://127.0.0.1:1234/otel")
    env = ax.build_env(run_dir=tmp_path, spec=None, counter=tmp_path / "counter",
                       tools_cfg=tmp_path / "tools", denials=tmp_path / "denials", granted=[],
                       hook_env={}, engine="pi", agent_dir=str(tmp_path / "native-home"))
    assert env["PI_CODING_AGENT_DIR"] == str(tmp_path / "native-home")
    assert not any(k.startswith("PI_AXON_") for k in env)
    assert "LITELLM_API_KEY" not in env and "LITELLM_BASE_URL" not in env
    assert "PI_ORG_OTEL_MANAGED_ENDPOINT" not in env


def test_native_stream_is_labelled_pi():
    shim = ax.PiToClaude(session_id="sid", model="m", engine="pi")
    assert shim._init()["engine"] == "pi"
    assert shim.result()["engine"] == "pi"


def test_scope_uses_pi_transport_and_does_not_fall_back_to_claude(tmp_path, monkeypatch):
    monkeypatch.setenv(ax.ENGINE_ENV, "pi")
    seen = []
    stream = tmp_path / "stream.jsonl"
    stream.write_text(json.dumps({"type": "result", "is_error": False, "result": "ok"}) + "\n")

    def fake_run(*args, **kwargs):
        seen.append(kwargs["engine"])
        return ag.AgentResult(label="summary", ok=True, stream=str(stream))

    monkeypatch.setattr(ax, "run", fake_run)
    assert context._on_pi_runtime(Cfg())
    _, result = context._axon_agent("prompt", "claude-haiku-5-5", Cfg(), tmp_path / "logs",
                                    label="summary", use_context=False, timeout_s=10)
    assert seen == ["pi"] and not result["is_error"]
    with pytest.raises(atlassian.AtlassianError, match="local Markdown"):
        cli._axon_atlassian(Cfg(), "model", tmp_path)


@pytest.mark.parametrize("engine,hint", [
    ("axon", "https://deputydev.ai/axon/install.sh | sh"),
    ("pi", "npm install -g --ignore-scripts @earendil-works/pi-coding-agent"),
])
def test_missing_binary_reports_engine_specific_install_command(tmp_path, engine, hint):
    cfg = Cfg(**{"AGENT.ENGINE": engine, f"AGENT.{engine.upper()}": {"BIN": str(tmp_path / "missing-agent")}})
    spec = ag.AgentSpec("lead", "model", None, (), (), use_context=False, timeout_s=10)
    res = ag.run(spec, "synthetic", tmp_path, cfg)
    assert not res.ok and hint in res.error and f"AGENT.{engine.upper()}.BIN" in res.error


@pytest.mark.parametrize("engine", ["claude-code", "axon", "pi"])
def test_atlassian_resolves_the_engine_once(tmp_path, monkeypatch, engine):
    calls = []

    def select(stage, cfg):
        calls.append(stage)
        return engine

    monkeypatch.setattr(ax, "engine_for", select)
    if engine == "pi":
        with pytest.raises(atlassian.AtlassianError, match="local Markdown"):
            cli._axon_atlassian(Cfg(), "model", tmp_path)
    else:
        cli._axon_atlassian(Cfg(), "model", tmp_path)
    assert calls == ["scope"]


def test_native_agent_and_resume_use_the_shared_stream_transport(tmp_path, monkeypatch):
    # Adapt the existing stream-replay fixture, without a real launcher/model call.
    exe = tmp_path / "native-pi"
    fixture = FIX / "fake_axon.py"
    exe.write_text(f"#!{sys.executable}\nimport os, sys\nos.execv(sys.executable, "
                   f"[sys.executable, {str(fixture)!r}, 'pi', *sys.argv[1:]])\n")
    exe.chmod(0o700)
    monkeypatch.setenv("FAKE_AXON_STREAM", str(FIX / "pi.sample.jsonl"))
    monkeypatch.setenv("FAKE_AXON_OUTPUTS", "out/x.md")
    monkeypatch.setenv("FAKE_AXON_ARGV", str(tmp_path / "dump.json"))
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cfg = Cfg(**{"AGENT.ENGINE": "pi", "AGENT.PI": {"BIN": str(exe), "PROVIDER": "openrouter",
                                                         "AGENT_DIR": str(tmp_path / "native-home")},
                 "MCP": {"mcpServers": {"fleetlens": {"url": "http://localhost:9999/mcp"}}}})
    spec = ag.AgentSpec("lead", "claude-opus-5-5[1m]", "low", ("Read", "Write"), ("out/*",), timeout_s=20)
    first = ag.run(spec, "prompt", run_dir, cfg)
    assert first.ok, first.error
    assert first.extra["engine"] == "pi" and first.tool_calls == 3
    assert (run_dir / "out/x.md").exists()
    dumped = json.loads((tmp_path / "dump.json").read_text())
    assert dumped["env"]["PI_CODING_AGENT_DIR"] == str(tmp_path / "native-home")
    assert dumped["argv"][dumped["argv"].index("--provider") + 1] == "openrouter"
    second = ag.run(spec, "continue", run_dir, cfg, resume=first.session_id)
    assert second.ok, second.error
    assert second.session_id == first.session_id and second.extra["engine"] == "pi"
