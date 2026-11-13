"""`/rfc` inside Axon: the Pi extension (rfc_agent/axon_ext/rfc-command.js) and its installer."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from rfc_agent import axon_agent

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures" / "axon"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node not on PATH (Axon requires Node >= 22.19 anyway)")
def test_rfc_command_end_to_end(tmp_path):
    """Preflight, launch, all three pause kinds answered through the dialogs, finish — against fake_uv.py."""
    (tmp_path / "run").mkdir()
    (tmp_path / "cwd").mkdir()
    fake_uv = FIX / "fake_uv.py"
    fake_uv.chmod(0o755)
    out = subprocess.run(
        [NODE, str(FIX / "rfc_command_cases.mjs"), str(axon_agent.RFC_COMMAND_JS), str(fake_uv), str(ROOT),
         str(tmp_path / "run"), str(tmp_path / "cwd")],
        capture_output=True, text=True, timeout=90,
    )
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["ok"], json.dumps(res["failures"], indent=2)


@pytest.mark.skipif(NODE is None, reason="node not on PATH (Axon requires Node >= 22.19 anyway)")
def test_test_command_end_to_end(tmp_path):
    """/test: phase A, the review handoff, phase B (web), /test execute, /test evaluate, test_status — against
    fake_uv_testing.py. Engines default to axon, and no PI_ variable reaches the children."""
    for d in ("run", "cwd"):
        (tmp_path / d).mkdir()
    fake_uv = FIX / "fake_uv_testing.py"
    fake_uv.chmod(0o755)
    out = subprocess.run(
        [NODE, str(FIX / "test_command_cases.mjs"), str(axon_agent.TEST_COMMAND_JS), str(fake_uv), str(ROOT),
         str(tmp_path / "run"), str(tmp_path / "cwd"), str(tmp_path / "uv.log")],
        capture_output=True, text=True, timeout=150,
    )
    assert out.returncode == 0, out.stderr
    res = json.loads(out.stdout.strip().splitlines()[-1])
    assert res["ok"], json.dumps(res["failures"], indent=2)


def test_install_writes_stub(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: "/bin/uv" if name == "uv" else None)
    path = axon_agent.install(tmp_path)
    assert path == tmp_path / "extensions" / "rfc-agent.js"
    text = path.read_text()
    assert json.dumps(str(axon_agent.RFC_COMMAND_JS)) in text
    assert f'project: {json.dumps(str(ROOT))}' in text and 'uv: "/bin/uv"' in text
    assert axon_agent.RFC_COMMAND_JS.exists()
    test_stub = (tmp_path / "extensions" / "test-agent.js").read_text()
    assert json.dumps(str(axon_agent.TEST_COMMAND_JS)) in test_stub and 'uv: "/bin/uv"' in test_stub
    assert axon_agent.TEST_COMMAND_JS.exists()


def test_install_needs_uv(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(SystemExit):
        axon_agent.install(tmp_path)


def test_agent_dir_honours_env(monkeypatch, tmp_path):
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path))
    assert axon_agent.axon_agent_dir() == tmp_path
    monkeypatch.delenv("PI_CODING_AGENT_DIR")
    assert axon_agent.axon_agent_dir() == Path.home() / ".axon" / "pi"
