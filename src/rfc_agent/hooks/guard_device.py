#!/usr/bin/env python3
"""PreToolUse hook: hold the `execute_app` stage's Maestro MCP tools to one device, one app, and a short
list of commands.

Why this exists — the same reason as `guard_browser.py`: this stage's agent acts (taps and types in the
app) while reading screen text nobody vetted. Maestro MCP makes it broader than the browser: its `run`
tool executes ANY Maestro flow — inline YAML, files from disk, a whole folder — including commands that
wipe the app (`clearState`), run JavaScript that can call HTTP (`runScript`, `evalScript`), or drive
another app. So every call is checked here:

- every device tool: `device_id` must be the serial this session was given (`RFC_AGENT_DEVICE_SERIAL`); a
  tool outside `mcp_tools.READONLY_TOOLS["maestro"]` (cloud runs, the viewer) is denied;
- `run`: only inline `yaml` (no `files`, `dir`, `env`, tags); the flow's `appId` must be the run's app;
  every command must be on `ALLOWED_COMMANDS`; `runFlow` may only name a file under the maestro repo's
  `common/` sub-flow folders; `openLink` only the app's own scheme or an allowed host; `takeScreenshot`
  only into the case folder; JavaScript (`${…}`, `runScript`, `evalScript`, `assertTrue`) is refused;
- money and order taps: the same forbidden-target pattern as the browser guard (`RFC_AGENT_DEVICE_FORBID`);
- the case's action limit counts Maestro COMMANDS, not tool calls: one `run` may carry several;
- like the browser, no action is allowed before the case's verdict file exists, and again once
  `RFC_AGENT_DEVICE_VERDICT_EVERY` commands have passed since it was last written.

The YAML is read lexically, not with a YAML library (none is a dependency of this project): a command is
a list item `- name` / `- name: …`. Anything this reader cannot place is refused, so a construct it does
not understand fails closed. Exit 0 = allow, exit 2 = deny (stderr is fed back to the agent).
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

SERVER = "maestro"
SERIAL_ENV = "RFC_AGENT_DEVICE_SERIAL"
APP_ID_ENV = "RFC_AGENT_DEVICE_APP_ID"
CASE_DIR_ENV = "RFC_AGENT_DEVICE_CASE_DIR"
SUBFLOWS_ENV = "RFC_AGENT_DEVICE_SUBFLOWS"          # the maestro repo's .maestro/flows folder
LINK_HOSTS_ENV = "RFC_AGENT_DEVICE_LINK_HOSTS"      # comma-separated hosts openLink may open
LINK_SCHEMES_ENV = "RFC_AGENT_DEVICE_LINK_SCHEMES"  # comma-separated app schemes (onemg)
MAX_ACTIONS_ENV = "RFC_AGENT_DEVICE_MAX_ACTIONS"
COUNT_FILE_ENV = "RFC_AGENT_DEVICE_COUNT_FILE"
VERDICT_FILE_ENV = "RFC_AGENT_DEVICE_VERDICT_FILE"
VERDICT_EVERY_ENV = "RFC_AGENT_DEVICE_VERDICT_EVERY"
FORBID_ENV = "RFC_AGENT_DEVICE_FORBID"

#: What an agent may do in the app. Everything a validated flow in maestro-automation does to judge a
#: screen, minus what changes the app's state behind the UI's back or runs code.
ALLOWED_COMMANDS = frozenset({
    "tapOn", "doubleTapOn", "longPressOn", "inputText", "eraseText", "copyTextFrom", "pasteText",
    "scroll", "scrollUntilVisible", "swipe", "back", "pressKey", "hideKeyboard", "waitForAnimationToEnd",
    "extendedWaitUntil", "assertVisible", "assertNotVisible", "takeScreenshot", "launchApp", "stopApp",
    "openLink", "runFlow", "repeat", "retry",
})
#: Commands that only LOOK (or wait): free under the action limit.
LOOK_COMMANDS = frozenset({"assertVisible", "assertNotVisible", "takeScreenshot", "waitForAnimationToEnd",
                           "extendedWaitUntil", "repeat", "retry", "runFlow"})
#: Keys refused anywhere in a flow, nested or not.
FORBIDDEN_KEYS = ("clearState", "clearKeychain", "runScript", "evalScript", "assertTrue", "script",
                  "startRecording", "stopRecording", "addMedia", "setLocation", "travel", "killApp",
                  "setAirplaneMode", "toggleAirplaneMode")
#: Words that open a nested block of commands, not a command themselves.
_BLOCK_KEYS = frozenset({"commands"})
_RE_ITEM = re.compile(r"^(\s*)-\s+([A-Za-z_][\w]*)\s*(?::\s*(.*))?$")
_RE_KEY = re.compile(r"^\s*([A-Za-z_][\w]*)\s*:\s*(.*)$")
#: The tools whose input acts on the device. inspect_screen / take_screenshot / cheat_sheet only look.
ACTION_TOOLS = ("run",)


def split_flow(text: str) -> tuple[str, str]:
    """(config section, commands) of an inline flow: `appId: x\\n---\\n- …`; no `---` = commands only."""
    lines = text.replace("\r\n", "\n").split("\n")
    for i, line in enumerate(lines):
        if line.strip() == "---":
            return "\n".join(lines[:i]), "\n".join(lines[i + 1:])
    return "", text


def config_app_id(config: str) -> str:
    m = re.search(r"^\s*appId\s*:\s*['\"]?([^'\"\s#]+)", config, re.M)
    return m.group(1) if m else ""


def commands(body: str) -> list[tuple[str, str, int]]:
    """(command, inline value, line number) for every list item that is a command, nested ones included.

    A list item whose first word is not followed by a colon or end of line (`- "text"`) is a scalar inside
    some command's value; it is returned as ("", text, n) so the caller can refuse it unless it sits under a
    key that takes a list of strings — which no allowed command does, so it is refused.
    """
    out = []
    for n, raw in enumerate(body.split("\n"), 1):
        line = raw.split(" #", 1)[0].rstrip() if not raw.lstrip().startswith("#") else ""
        if not line.strip():
            continue
        m = _RE_ITEM.match(line)
        if m:
            out.append((m.group(2), (m.group(3) or "").strip(), n))
        elif line.lstrip().startswith("- "):
            out.append(("", line.strip()[2:], n))
    return out


def _value_of(body: str, line_no: int, key: str) -> str:
    """The value of `key` in the command starting at `line_no` (inline `- runFlow: x` or a nested
    `file: x` within its indented block)."""
    lines = body.split("\n")
    first = lines[line_no - 1]
    m = _RE_ITEM.match(first)
    if m and m.group(3) and m.group(3).strip() not in ("", "|", ">"):
        return m.group(3).strip().strip("'\"")
    indent = len(first) - len(first.lstrip())
    for line in lines[line_no:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        km = _RE_KEY.match(line)
        if km and km.group(1) == key:
            return km.group(2).strip().strip("'\"")
    return ""


def _target_words(body: str, line_no: int) -> str:
    """The text one command names (`tapOn: Pay` or its nested `text:` / `id:`), for the forbid pattern."""
    lines = body.split("\n")
    first = lines[line_no - 1]
    words = [(_RE_ITEM.match(first).group(3) or "") if _RE_ITEM.match(first) else ""]
    indent = len(first) - len(first.lstrip())
    for line in lines[line_no:]:
        if line.strip() and len(line) - len(line.lstrip()) <= indent:
            break
        km = _RE_KEY.match(line)
        if km and km.group(1) in ("text", "id", "label"):
            words.append(km.group(2))
    return " ".join(words)


def flow_denial(yaml_text: str, *, app_id: str, case_dir: str, subflows: str, link_hosts: str = "",
                link_schemes: str = "", forbid: str = "") -> tuple[str, int]:
    """Why this inline flow may not run, or "" — and how many acting commands it holds (for the limit)."""
    if not str(yaml_text or "").strip():
        return "the flow is empty: send Maestro commands as inline `yaml`", 0
    config, body = split_flow(str(yaml_text))
    if "${" in yaml_text:
        return ("`${…}` runs JavaScript inside the flow; it is not allowed here. Use literal text and "
                "selectors"), 0
    for key in FORBIDDEN_KEYS:
        # as a command (`- clearState`, `- runScript: x`) or as a key anywhere (`clearState: true`, `{script: …}`);
        # never as a word inside a value, so `tapOn: travel` is not mistaken for one
        if re.search(rf"^\s*-\s*{key}\s*(:|$)|(^|[\s{{,]){key}\s*:", yaml_text, re.M):
            return f"`{key}` is not allowed in this run (it changes the app or device behind the UI, or runs code)", 0
    named = config_app_id(config)
    if not named:
        return f"start the flow with `appId: {app_id}` and a `---` line before the commands", 0
    if named != app_id:
        return f"this run drives {app_id} only; the flow names {named}", 0
    cmds = commands(body)
    if not cmds:
        return "the flow holds no commands (`- tapOn: …` list items after the `---` line)", 0
    acting = 0
    for name, _value, n in cmds:
        if name in _BLOCK_KEYS:
            continue
        if not name or name not in ALLOWED_COMMANDS:
            what = name or f"the list item on line {n}"
            return (f"{what!r} is not a command this run may use. Allowed: {', '.join(sorted(ALLOWED_COMMANDS))}"), 0
        if name == "runFlow":
            target = _value_of(body, n, "file")
            if not target:
                return "`runFlow` may only run a shared sub-flow file (`file: <path>`), not inline commands", 0
            path = Path(target).expanduser()
            root = Path(subflows).expanduser().resolve() if subflows else None
            ok = root is not None and path.is_absolute() and path.resolve().is_relative_to(root) and \
                "common" in path.resolve().relative_to(root).parts[:-1] and path.suffix == ".yaml"
            if not ok:
                return (f"`runFlow` may only run a sub-flow under {subflows or '<maestro repo>'}/**/common/ "
                        f"(absolute path); {target!r} is not one"), 0
        if name == "openLink":
            link = _value_of(body, n, "link")
            if not _link_ok(link, link_hosts, link_schemes):
                return (f"`openLink` may only open the app's own links (schemes: {link_schemes or 'none'}; hosts: "
                        f"{link_hosts or 'none'}); {link!r} is not one"), 0
        if name in ("launchApp", "stopApp"):
            other = _value_of(body, n, "appId")
            if other and other != app_id:
                return f"`{name}` may only name this run's app ({app_id})", 0
        if name == "takeScreenshot":
            target = _value_of(body, n, "path")
            if not _inside(target, case_dir):
                return (f"`takeScreenshot` must save into your case folder ({case_dir}): give it a name in that "
                        f"folder, e.g. `takeScreenshot: a-cart`"), 0
        if forbid and name in ("tapOn", "doubleTapOn", "longPressOn", "inputText"):
            try:
                hit = re.search(forbid, _target_words(body, n), re.I)
            except re.error:
                return "the run's forbidden-action pattern is not a valid regex, so no action is allowed", 0
            if hit:
                return (f"this run may not touch that ({hit.group(0)!r}): it changes money, an order or the "
                        f"account. If a clause cannot be judged without it, the clause is `not reached` with "
                        f"the blocker 'action not allowed in this run' (cause: test data)"), 0
        if name not in LOOK_COMMANDS:
            acting += 1
    return "", acting


def _link_ok(link: str, hosts: str, schemes: str) -> bool:
    from urllib.parse import urlsplit

    parts = urlsplit(link or "")
    allowed_schemes = {s.strip().lower() for s in schemes.split(",") if s.strip()}
    allowed_hosts = {h.strip().lower() for h in hosts.split(",") if h.strip()}
    if parts.scheme.lower() in allowed_schemes:
        return True
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in allowed_hosts


def _inside(target: str, case_dir: str) -> bool:
    """A screenshot name: relative (resolved against the case folder, the server's working dir) or an
    absolute path inside it."""
    if not target or not case_dir:
        return False
    base = Path(case_dir).resolve()
    path = Path(target).expanduser()
    path = (path if path.is_absolute() else base / path).resolve()
    return path.is_relative_to(base)


def denial(tool: str, inp: dict, granted: tuple[str, ...], env: dict) -> tuple[str, int]:
    """Why this Maestro call is refused, or "" — and the acting commands it holds."""
    if tool not in granted:
        return (f"{tool} is not one of the device tools this stage may use ({', '.join(granted)})."), 0
    serial = env.get(SERIAL_ENV, "")
    if not serial:
        return (f"no device is assigned to this session ({SERIAL_ENV}), so the device may not be used. This is "
                f"the harness's to fix, not yours: stop and report it."), 0
    if tool in ("list_devices", "cheat_sheet"):
        return "", 0  # no device_id: they only read
    if not isinstance(inp, dict) or str(inp.get("device_id") or "") != serial:
        return f"use device_id {serial!r} — it is the only device this case may drive", 0
    if tool != "run":
        return "", 0
    extra = [k for k in ("files", "dir", "env", "include_tags", "exclude_tags") if inp.get(k)]
    if extra:
        return f"`run` takes inline `yaml` only here; {', '.join(extra)} is not allowed", 0
    return flow_denial(str(inp.get("yaml") or ""), app_id=env.get(APP_ID_ENV, ""),
                       case_dir=env.get(CASE_DIR_ENV, ""), subflows=env.get(SUBFLOWS_ENV, ""),
                       link_hosts=env.get(LINK_HOSTS_ENV, ""), link_schemes=env.get(LINK_SCHEMES_ENV, ""),
                       forbid=env.get(FORBID_ENV, ""))


def main() -> None:
    from rfc_agent.hooks import guard_browser

    raw = sys.stdin.read()
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError:
        ev = {}
    name = str(ev.get("tool_name") or "")
    prefix = f"mcp__{SERVER}__"
    if not name.startswith(prefix):
        sys.exit(0)
    try:
        from rfc_agent.mcp_tools import READONLY_TOOLS
        granted = READONLY_TOOLS[SERVER]
    except Exception as exc:  # FAIL CLOSED
        sys.stderr.write(f"DENIED by rfc_agent device-guard: cannot load the tool list ({exc}).\n")
        sys.exit(2)
    tool = name[len(prefix):]
    env = dict(os.environ)
    reason, acting = denial(tool, ev.get("tool_input") or {}, granted, env)
    count_file = env.get(COUNT_FILE_ENV, "")
    if not reason and tool in ACTION_TOOLS:
        reason = guard_browser.verdict_gap(tool, ACTION_TOOLS, env.get(VERDICT_FILE_ENV, ""),
                                           env.get(VERDICT_EVERY_ENV, ""),
                                           f"{count_file}.verdict" if count_file else "")
    if not reason and acting:
        reason = over_limit(acting, env.get(MAX_ACTIONS_ENV, ""), count_file)
    if reason:
        sys.stderr.write(f"DENIED by rfc_agent device-guard: {reason}\n")
        sys.exit(2)
    sys.exit(0)


def over_limit(acting: int, limit: str, count_file: str) -> str:
    """Add this call's acting commands to the case's count; refuse once it would pass the limit."""
    if not acting or not limit.isdigit() or not count_file:
        return ""
    try:
        try:
            used = int(open(count_file, encoding="utf-8").read().strip() or 0)
        except (FileNotFoundError, ValueError):
            used = 0
        if used + acting > int(limit):
            return (f"this case has used {used} of its {limit} device commands and this call needs {acting} "
                    f"more. Do not act again: write your verdict now, marking every clause you have not judged "
                    f"`not reached` with the blocker 'action limit reached'. You may still inspect the screen.")
        with open(count_file, "w", encoding="utf-8") as fh:
            fh.write(str(used + acting))
    except OSError:
        return ""
    return ""


if __name__ == "__main__":
    main()
