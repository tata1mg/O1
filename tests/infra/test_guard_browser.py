"""The browser guard: the one stage whose MCP tools act (execute_ui_agent) is held to its run's origin."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rfc_agent.hooks import guard_browser
from rfc_agent.mcp_tools import (
    BROWSER_ACTION_TOOLS,
    BROWSER_GUARD,
    HOOK_GUARDED_TOOLS,
    READONLY_TOOLS,
    allowed_tools,
)

ORIGIN = "https://prestag.1mg.test"
GRANTED = READONLY_TOOLS["playwright"]
HOOK = Path(guard_browser.__file__)


@pytest.mark.parametrize("tool, inp, origin, allowed", [
    ("browser_navigate", {"url": f"{ORIGIN}/labs"}, ORIGIN, True),
    ("browser_navigate", {"url": "/labs?x=1"}, ORIGIN, True),
    ("browser_navigate", {"url": "https://PRESTAG.1mg.test:443/labs"}, ORIGIN, True),   # same origin, spelled out
    ("browser_navigate", {"url": "https://www.1mg.com/labs"}, ORIGIN, False),          # production
    ("browser_navigate", {"url": "http://prestag.1mg.test/labs"}, ORIGIN, False),      # other scheme
    ("browser_navigate", {"url": "https://prestag.1mg.test.evil.example/"}, ORIGIN, False),
    ("browser_navigate", {"url": f"https://evil.example/?next={ORIGIN}"}, ORIGIN, False),
    ("browser_navigate", {"url": "javascript:alert(1)"}, ORIGIN, False),
    ("browser_navigate", {"url": "file:///etc/hosts"}, ORIGIN, False),
    ("browser_navigate", {"url": "labs"}, ORIGIN, False),                              # not a path
    ("browser_tabs", {"action": "new", "url": "https://evil.example/"}, ORIGIN, False),
    ("browser_click", {"element": "Add to cart", "ref": "e12"}, ORIGIN, True),
    ("browser_snapshot", {}, ORIGIN, True),
    ("browser_click", {"element": "Add to cart", "ref": "e12"}, "", False),            # no origin: nothing runs
    ("browser_snapshot", {}, "not a url", False),
    ("browser_evaluate", {"function": "() => document.cookie"}, ORIGIN, False),        # never granted
    ("browser_file_upload", {"paths": ["/etc/hosts"]}, ORIGIN, False),
    ("browser_run_code_unsafe", {"code": "1"}, ORIGIN, False),
])
def test_a_browser_call_is_held_to_the_runs_origin(tool, inp, origin, allowed):
    reason = guard_browser.denial(tool, inp, origin, GRANTED)
    assert (reason == "") is allowed, reason


def test_every_action_tool_is_a_named_guarded_exception():
    assert BROWSER_ACTION_TOOLS and all(HOOK_GUARDED_TOOLS[t] == BROWSER_GUARD for t in BROWSER_ACTION_TOOLS)
    assert set(BROWSER_ACTION_TOOLS) <= set(GRANTED)
    grants = allowed_tools(("playwright",))
    assert "mcp__playwright__browser_click" in grants and "mcp__playwright" not in grants
    for never in ("evaluate", "run_code", "file_upload", "cookie", "route", "storage", "install", "drag"):
        assert not any(never in t for t in GRANTED), f"{never} must stay out of the browser grant"


def _hook(event: dict, origin: str | None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k != guard_browser.ORIGIN_ENV}
    if origin is not None:
        env[guard_browser.ORIGIN_ENV] = origin
    return subprocess.run([sys.executable, str(HOOK)], input=json.dumps(event), capture_output=True, text=True,
                          env=env)


def test_the_hook_denies_with_exit_2_and_a_reason():
    off = _hook({"tool_name": "mcp__playwright__browser_navigate", "tool_input": {"url": "https://evil.example"}},
                ORIGIN)
    assert off.returncode == 2 and "outside this run's test environment" in off.stderr
    ok = _hook({"tool_name": "mcp__playwright__browser_navigate", "tool_input": {"url": f"{ORIGIN}/labs"}}, ORIGIN)
    assert ok.returncode == 0
    unset = _hook({"tool_name": "mcp__playwright__browser_snapshot", "tool_input": {}}, None)
    assert unset.returncode == 2 and guard_browser.ORIGIN_ENV in unset.stderr


def test_the_hook_ignores_every_other_tool():
    assert _hook({"tool_name": "Bash", "tool_input": {"command": "ls"}}, None).returncode == 0
    assert _hook({"tool_name": "mcp__fleetlens__get_source", "tool_input": {"url": "https://x.example"}},
                 None).returncode == 0


def test_the_child_settings_route_browser_tools_to_the_guard(tmp_path):
    from rfc_agent.engine import _write_guard_settings

    settings = json.loads(_write_guard_settings(tmp_path, tmp_path / "s.json").read_text())
    entry = next(e for e in settings["hooks"]["PreToolUse"] if e["matcher"] == "mcp__playwright__.*")
    assert entry["hooks"][0]["command"].endswith("guard_browser.py")
    assert settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"].endswith("guard_writes.py")  # order kept
