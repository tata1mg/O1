"""The `axon` harness adapter: the deltas from `pi` measured against Axon 0.2.6 / Pi 1.4.0 (decisions.md)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from harness.engines import Containment, EngineRequest, get_engine, names
from harness.engines.axon import AxonEngine, map_model, model_tier

FIX = Path(__file__).parents[1] / "fixtures" / "axon"


@pytest.fixture
def fake(tmp_path, monkeypatch) -> Path:
    exe = tmp_path / "bin" / "fake_axon.py"
    exe.parent.mkdir()
    exe.write_text((FIX / "fake_axon.py").read_text())
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("FAKE_AXON_ARGV", str(tmp_path / "argv.json"))
    monkeypatch.setenv("FAKE_AXON_STREAM", str(FIX / "pi.sample.jsonl"))
    return exe


def test_axon_is_a_registered_engine():
    assert "axon" in names()
    assert isinstance(get_engine("axon"), AxonEngine)


def test_model_tiers():
    assert model_tier("claude-opus-5-5[1m]") == "opus[1m]"
    assert model_tier("claude-haiku-4-5-20251001") == "haiku"
    assert model_tier("gemini-3.8-flash") is None
    assert map_model("claude-sonnet-5-5") == "openai/gpt-6.1-terra"
    assert map_model("claude-sonnet-5-5", {"sonnet": "x"}) == "x"
    assert map_model(None) is None


def _req(tmp_path: Path, **containment) -> EngineRequest:
    run = tmp_path / "run"
    run.mkdir(exist_ok=True)
    return EngineRequest(prompt="go", cwd=run, label="stage", trace_path=run / "logs" / "stage.events.jsonl",
                         timeout_s=60, model="claude-opus-5-5", effort="medium",
                         containment=Containment(**containment))


def test_run_keeps_axons_extensions_and_registers_mcp(tmp_path, fake):
    eng = AxonEngine(bin=str(fake))
    spec = {"type": "http", "url": "https://fleetlens.example/mcp", "headers": {"X": "1"}}
    res = eng.run(_req(tmp_path, mcp_servers={"fleetlens": spec},
                       allowed_tools=("Read", "mcp__fleetlens__find_symbol")))
    assert res.summary.got_result and not res.summary.is_error, res.summary
    assert res.summary.final_text == "done."
    assert res.engine_desc.startswith("axon:")
    dumped = json.loads((tmp_path / "argv.json").read_text())
    argv = dumped["argv"]
    assert argv[1] == "pi" and "--no-extensions" not in argv
    assert [Path(argv[i + 1]).name for i, a in enumerate(argv) if a == "-e"] == ["harness-guard.js", "rfc-tools.js"]
    assert argv[argv.index("--provider") + 1] == "litellm"
    assert argv[argv.index("--model") + 1] == "openai/gpt-6.1-sol"
    assert argv[argv.index("--tools") + 1] == "read,mcp__fleetlens__find_symbol"
    env = dumped["env"]
    assert env["PI_OFFLINE"] is None and env["PI_CODING_AGENT_DIR"] is None
    assert json.loads(env["HARNESS_POLICY_JSON"])["granted_tools"] == ["mcp__fleetlens__find_symbol"]
    cfg = Path(env["RFC_TOOLS_CONFIG"])
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600
    assert json.loads(cfg.read_text())["mcp"] == {"fleetlens": {"url": spec["url"], "headers": {"X": "1"}}}
    assert not (tmp_path / "run" / ".pi-home").exists()


def test_plain_pi_still_refuses_mcp(tmp_path):
    from harness.contracts import PipelineConfigError

    with pytest.raises(PipelineConfigError, match="no MCP support"):
        get_engine("pi", bin="pi").run(_req(tmp_path, mcp_servers={"x": {"url": "u"}}))


def test_doctor_passes_on_a_healthy_launcher(fake):
    assert AxonEngine(bin=str(fake)).doctor() == []


def test_doctor_names_the_fix_when_axon_is_missing(tmp_path):
    problems = AxonEngine(bin=str(tmp_path / "no-axon")).doctor()
    assert problems and "install Axon" in problems[0]


def test_doctor_flags_a_signed_out_session(fake, monkeypatch):
    monkeypatch.setenv("FAKE_AXON_AUTH_EXIT", "1")
    assert any("not signed in" in p for p in AxonEngine(bin=str(fake)).doctor())


NODE = __import__("shutil").which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH (Axon requires Node >= 22.19 anyway)")
def test_rfc_tools_extension_decisions_and_the_real_hook(tmp_path):
    import subprocess
    import sys

    from harness.engines.axon import RFC_TOOLS_JS
    from harness.engines.pi import GUARD_JS
    from rfc_agent.investigate.agent import HOOK

    for f in (RFC_TOOLS_JS, FIX / "rfc_tools_cases.mjs"):
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
    run = tmp_path / "run"
    r = subprocess.run([NODE, str(FIX / "rfc_tools_cases.mjs"), str(RFC_TOOLS_JS), str(GUARD_JS), sys.executable,
                        str(HOOK), str(run)], capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["ok"], out["failures"]
