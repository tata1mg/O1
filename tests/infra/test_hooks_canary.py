"""End-to-end feedback-channel canary (v2 run 2026-09-03: hooks passed every unit test while the CLI
side of the PostToolUse contract had silently changed — a 25-minute compose ran blind)."""

import json
import subprocess

from rfc_agent import hooks_canary
from rfc_agent.contracts import RunContext


def _ctx(tmp_path):
    return RunContext(run_dir=tmp_path, inputs={"repos_root": str(tmp_path)}, config=None)


def _fake_run(stdout: str, calls: list):
    def run(cmd, **kw):
        calls.append(cmd)
        if cmd[1:2] == ["--version"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="9.9.9 (Claude Code)\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr="")
    return run


def test_alive_is_cached_per_cli_version_and_never_reprobed(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks_canary, "_CACHE", tmp_path / "cache")
    calls: list = []
    stream = '{"type":"result","result":"BYTE BUDGET pacing — out/contract.md is 4,800 bytes: 46% ..."}\n'
    monkeypatch.setattr(subprocess, "run", _fake_run(stream, calls))
    settings = tmp_path / "s.json"
    settings.write_text("{}")
    assert hooks_canary.ensure_hooks_alive(_ctx(tmp_path), settings) is True
    marker = tmp_path / "cache" / "9.9.9.json"
    assert marker.exists() and json.loads(marker.read_text())["alive"] is True
    spawned = [c for c in calls if "-p" in c]
    assert len(spawned) == 1 and "--settings" in spawned[0], "probed under the stage's own settings"
    decisions = (tmp_path / "logs" / "decisions.jsonl").read_text()
    assert '"hook_canary"' in decisions and '"alive": true' in decisions
    # second call: cached, nothing spawned
    assert hooks_canary.ensure_hooks_alive(_ctx(tmp_path), settings) is None
    assert len([c for c in calls if "-p" in c]) == 1


def test_dead_channel_warns_records_and_is_not_cached(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(hooks_canary, "_CACHE", tmp_path / "cache")
    calls: list = []
    monkeypatch.setattr(subprocess, "run", _fake_run('{"type":"result","result":"NONE"}\n', calls))
    settings = tmp_path / "s.json"
    settings.write_text("{}")
    assert hooks_canary.ensure_hooks_alive(_ctx(tmp_path), settings) is False
    assert not (tmp_path / "cache" / "9.9.9.json").exists(), "failures re-probe next invocation"
    assert "WARNING" in capsys.readouterr().err
    assert "hook_canary" in (tmp_path / "logs" / "decisions.jsonl").read_text()
    # a re-probe happens (not cached)
    hooks_canary.ensure_hooks_alive(_ctx(tmp_path), settings)
    assert len([c for c in calls if "-p" in c]) == 2
