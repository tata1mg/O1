"""The device guard: execute_app's Maestro MCP `run` is held to one device, one app and a list of commands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from rfc_agent.hooks import guard_device as g
from rfc_agent.mcp_tools import DEVICE_ACTION_TOOLS, DEVICE_GUARD, HOOK_GUARDED_TOOLS, READONLY_TOOLS, allowed_tools

APP = "com.aranoah.healthkart.plus.dev"
SERIAL = "emulator-5554"
GRANTED = READONLY_TOOLS["maestro"]


@pytest.fixture
def env(tmp_path):
    flows = tmp_path / "repo" / ".maestro" / "flows"
    (flows / "cart" / "common").mkdir(parents=True)
    (flows / "cart" / "common" / "launch-to-home.yaml").write_text("appId: ${APP_ID}\n---\n- launchApp\n")
    case = tmp_path / "run" / "qa" / "android" / "TC-A-01"
    case.mkdir(parents=True)
    return {g.SERIAL_ENV: SERIAL, g.APP_ID_ENV: APP, g.CASE_DIR_ENV: str(case), g.SUBFLOWS_ENV: str(flows),
            g.LINK_HOSTS_ENV: "www.1mg.com", g.LINK_SCHEMES_ENV: "onemg",
            g.FORBID_ENV: r"\b(pay|place order)\b"}


def flow(*lines: str, app: str = APP) -> str:
    return f"appId: {app}\n---\n" + "\n".join(lines) + "\n"


def run(environ, yaml_text, **extra):
    return g.denial("run", {"device_id": SERIAL, "yaml": yaml_text, **extra}, GRANTED, environ)


@pytest.mark.parametrize("lines, ok", [
    (["- tapOn: Cart"], True),
    (["- tapOn:", "    id: menu_item_cart"], True),
    (["- back", "- assertVisible: Cart"], True),
    (["- scrollUntilVisible:", "    element:", "      text: Coupons", "    direction: DOWN"], True),
    (["- repeat:", "    times: 2", "    commands:", "      - swipe:", "          direction: UP"], True),
    (["- takeScreenshot: a-cart"], True),
    (["- openLink: onemg://www.1mg.com/prescription_order"], True),
    (["- openLink: https://www.1mg.com/labs/mybookings"], True),
    (["- launchApp:", "    clearState: true"], False),            # wipes the session
    (["- clearState"], False),
    (["- runScript: steal.js"], False),
    (["- evalScript: ${output.x = 1}"], False),
    (["- assertTrue: ${1 == 1}"], False),
    (["- inputText: ${maestro.copiedText}"], False),
    (["- setLocation:", "    latitude: 1", "    longitude: 2"], False),
    (["- openLink: https://evil.example/"], False),
    (["- openLink: tel:123"], False),
    (["- launchApp:", "    appId: com.android.settings"], False),
    (["- stopApp: com.android.chrome"], False),
    (["- takeScreenshot: /tmp/out"], False),                    # outside the case folder
    (["- takeScreenshot: ../../elsewhere"], False),
    (["- tapOn: Pay now"], False),                             # forbidden target
    (["- tapOn:", "    text: Place order"], False),
    (["- tapOn: travel"], True),                               # a word in a value is not a command
    (["- someNewCommand: x"], False),                          # unknown -> refused
    (["- \"just text\""], False),
    (["- runFlow:", "    commands:", "      - tapOn: x"], False),   # inline runFlow: not a sub-flow file
    (["- runFlow: relative/common/x.yaml"], False),
], ids=lambda v: str(v)[:40])
def test_commands(env, lines, ok):
    reason, _ = run(env, flow(*lines))
    assert (reason == "") is ok, reason


def test_runflow_only_into_the_repos_common_subflows(env):
    good = Path(env[g.SUBFLOWS_ENV]) / "cart" / "common" / "launch-to-home.yaml"
    assert run(env, flow(f"- runFlow: {good}"))[0] == ""
    assert run(env, flow("- runFlow:", f"    file: {good}"))[0] == ""
    outside = Path(env[g.CASE_DIR_ENV]) / "mine.yaml"
    assert "sub-flow" in run(env, flow(f"- runFlow: {outside}"))[0]
    not_common = Path(env[g.SUBFLOWS_ENV]) / "cart" / "ct001.yaml"
    assert run(env, flow(f"- runFlow: {not_common}"))[0]


def test_device_app_and_input_shape(env):
    assert "device_id" in g.denial("run", {"device_id": "emulator-5556", "yaml": flow("- back")}, GRANTED, env)[0]
    assert "device_id" in g.denial("inspect_screen", {}, GRANTED, env)[0]
    assert g.denial("inspect_screen", {"device_id": SERIAL}, GRANTED, env)[0] == ""
    assert "only" in run(env, flow("- back", app="com.other.app"))[0]
    assert "appId" in run(env, "- back\n")[0]
    assert "inline" in run(env, flow("- back"), files=["/x.yaml"])[0]
    assert "inline" in run(env, flow("- back"), env={"A": "1"})[0]
    assert "not one of" in g.denial("run_on_cloud", {"device_id": SERIAL}, GRANTED, env)[0]
    assert g.denial("list_devices", {}, GRANTED, env)[0] == ""          # read-only, takes no device_id
    assert "no device" in g.denial("run", {"device_id": SERIAL}, GRANTED, {**env, g.SERIAL_ENV: ""})[0]


def test_acting_commands_are_counted_looking_is_free(env):
    _, acting = run(env, flow("- tapOn: Cart", "- assertVisible: Cart", "- takeScreenshot: a", "- back"))
    assert acting == 2


def test_limit_counts_commands_across_calls(tmp_path):
    count = tmp_path / "count"
    assert g.over_limit(3, "4", str(count)) == ""
    assert "used 3 of its 4" in g.over_limit(2, "4", str(count))
    assert g.over_limit(1, "4", str(count)) == ""
    assert "used 4 of its 4" in g.over_limit(1, "4", str(count))


def test_registered_as_a_guarded_exception():
    assert DEVICE_ACTION_TOOLS == ("run",)
    assert all(HOOK_GUARDED_TOOLS[t] == DEVICE_GUARD for t in DEVICE_ACTION_TOOLS)
    grants = allowed_tools(("maestro",))
    assert "mcp__maestro__run" in grants and "mcp__maestro__inspect_screen" in grants
    assert not any("cloud" in x or "viewer" in x for x in grants)


def test_hook_process_denies_and_allows(env, tmp_path):
    verdict = Path(env[g.CASE_DIR_ENV]) / "verdict.json"
    verdict.write_text("{}")
    full = {**os.environ, **env, g.VERDICT_FILE_ENV: str(verdict), g.VERDICT_EVERY_ENV: "12",
            g.COUNT_FILE_ENV: str(tmp_path / "c"), g.MAX_ACTIONS_ENV: "10",
            "PYTHONPATH": str(Path(g.__file__).parents[2])}

    def call(tool, inp):
        return subprocess.run([sys.executable, g.__file__], input=json.dumps(
            {"tool_name": f"mcp__maestro__{tool}", "tool_input": inp}), capture_output=True, text=True,
            env=full).returncode

    assert call("run", {"device_id": SERIAL, "yaml": flow("- tapOn: Cart")}) == 0
    assert call("run", {"device_id": SERIAL, "yaml": flow("- clearState")}) == 2
    assert call("run_on_cloud", {"device_id": SERIAL}) == 2
    verdict.unlink()
    assert call("run", {"device_id": SERIAL, "yaml": flow("- tapOn: Cart")}) == 2   # no verdict file yet
