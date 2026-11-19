"""What `execute_app` and `convert_flows` share: which Android cases run how, the agent's device and prompt,
and the draft flow an agent PASS leaves behind.

The verdict an Android agent writes is the web agent's (`ui_agent`): same parts, states, causes, evidence
and validation, so a reviewer reads one kind of verdict whatever the platform. What differs is here:

  * selection — CONFIRMED `exec:ui` + `surface:android` cases (a case tagged for web as well runs on both);
  * the engine per case (`plan`): its VALIDATED Maestro flow when one covers it, else the agent;
  * the agent's tools: `maestro mcp`, started per case with the case folder as its working directory, held
    by `rfc_agent.hooks.guard_device` to one device, one app and an allow-list of commands;
  * the draft: on a PASS the agent writes `draft.yaml`, the Maestro commands that worked, ending with the
    `assertVisible` that proved each clause. `lint_draft` and `flow_text` turn it into a flow file for
    `convert_flows` to run to green. Nothing here writes into the maestro-automation repo.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from rfc_agent.hooks import guard_device

from . import maestro_index, ui_agent
from .devices import maestro_bin

APP_DIR = "qa/android"
RESULTS = "results.json"
RESULTS_TABLE = "results.md"
NOTES = "notes.md"
CONVERSIONS = "conversions.json"
DRAFT = "draft.yaml"
FLOW = "flow.yaml"           # the draft as a flow file (header added), what convert_flows runs
SERVER = "maestro"
ANDROID_UI_TAGS = ("exec:ui", "surface:android")
MANUAL_ANDROID_TAGS = ("exec:manual", "surface:android")
FLOW_ENGINE, AGENT_ENGINE, NONE = "flow", "agent", "not run"
#: A case tag naming the account profile (`.maestro/users.yaml`) the case needs signed in.
ACCOUNT_TAG = "account:"


def select_cases(store, feature: str, *, include_manual: bool = False, only=()) -> list:
    cases = list(store.list_cases(feature, status="CONFIRMED", tags=ANDROID_UI_TAGS))
    if include_manual:
        seen = {c.extid for c in cases}
        cases += [c for c in store.list_cases(feature, status="CONFIRMED", tags=MANUAL_ANDROID_TAGS)
                  if c.extid not in seen]
    return [c for c in cases if not only or c.extid in only]


def case_dir(run_dir: Path, extid: str) -> Path:
    return run_dir / APP_DIR / extid


def profile_of(case, flow=None) -> str:
    """The account profile a case needs: the flow's `# REQUIRES:`, else an `account:<profile>` case tag."""
    if flow is not None and flow.requires:
        return flow.requires
    return next((t[len(ACCOUNT_TAG):] for t in case.tags if t.startswith(ACCOUNT_TAG)), "")


def plan(cases: list, flows: dict, mode: str = "auto") -> list[dict]:
    """One entry per case: {case, engine, flow, why}. `mode` is testing.android_mode.

    auto  -> the case's VALIDATED, ungated flow when it has one, else the agent
    flow  -> flows only; a case without one is `not run`
    agent -> the agent for every case

    A case whose only flows are gated (mock-required, manual-order-placing) is `not run` in every mode
    but `agent`: the gate says what the case needs, and an agent has no more of it than the flow has.
    Order-placing cases never go to the agent in v1 (decided 2026-11-17).
    """
    out = []
    for case in cases:
        mine = maestro_index.flows_for(flows, case.extid, getattr(case, "store_id", ""))
        chosen = maestro_index.choose(mine)
        order = any(maestro_index.ORDER_TAG in f.tags for f in mine)
        if not ui_agent.safe_id(case.extid):
            entry = {"engine": NONE, "flow": None,
                     "why": "the case id cannot name a folder — give it a plain extid: tag"}
        elif order:
            entry = {"engine": NONE, "flow": None,
                     "why": "its flow places a real order (manual-order-placing) — not run in v1"}
        elif mode != "agent" and chosen is not None:
            entry = {"engine": FLOW_ENGINE, "flow": chosen, "why": f"VALIDATED flow {chosen.name}"}
        elif mode == "flow":
            gated = [f for f in mine if f.validated and f.gate()]
            why = (f"its flow {gated[0].name} {gated[0].gate()}" if gated else
                   "no VALIDATED flow covers it" + (f" ({', '.join(f'{f.name}: {f.status}' for f in mine)})"
                                                     if mine else ""))
            entry = {"engine": NONE, "flow": None, "why": why}
        elif mine and all(f.gate() for f in mine if f.validated) and any(f.validated for f in mine) \
                and mode != "agent":
            f = next(f for f in mine if f.validated)
            entry = {"engine": NONE, "flow": None, "why": f"its flow {f.name} {f.gate()}"}
        elif not ui_agent.expected_result(case.text):
            entry = {"engine": NONE, "flow": None,
                     "why": "no VALIDATED flow, and the case states no expected result for an agent to judge"}
        else:
            entry = {"engine": AGENT_ENGINE, "flow": None,
                     "why": ("no flow covers it" if not mine else
                             "no VALIDATED flow (" + ", ".join(f"{f.name}: {f.status}" for f in mine) + ")")}
        out.append({"case": case, **entry})
    return out


# ---- the agent's device ------------------------------------------------------------------------------


def maestro_server(folder: Path) -> dict:
    """The MCP server for ONE case: `maestro mcp` started INSIDE the case folder, with no viewer (an open port).

    Started there, not just pointed there: on the emulator (2026-11-18, Maestro 2.12.1) a `takeScreenshot:
    <name>` sent through `run` was written to the server process's current directory, not to its
    `--working-dir`. An MCP stdio entry has no `cwd`, so the server is started through `sh -c 'cd … && exec`.
    """
    import shlex

    cmd = f"cd {shlex.quote(str(folder))} && exec {shlex.quote(maestro_bin() or 'maestro')} mcp --no-viewer " \
          f"--working-dir={shlex.quote(str(folder))}"
    return {"type": "stdio", "command": "/bin/sh", "args": ["-c", cmd]}


def device_calls(tool_calls: list[dict]) -> int:
    """How many device tool calls a session's trace holds — the proof a verdict was worked for."""
    return sum(1 for c in tool_calls or [] if isinstance(c, dict) and f"mcp__{SERVER}__" in str(c.get("name") or ""))


def device_actions(tool_calls: list[dict]) -> int:
    """Acting Maestro commands across the session's `run` calls (looking and asserting are free)."""
    total = 0
    for c in tool_calls or []:
        if isinstance(c, dict) and str(c.get("name") or "").endswith(f"mcp__{SERVER}__run"):
            text = str((c.get("input") or {}).get("yaml") or "")
            _, body = guard_device.split_flow(text)
            total += sum(1 for name, _, _ in guard_device.commands(body)
                         if name and name not in guard_device.LOOK_COMMANDS and name not in ("commands",))
    return total


# ---- the draft an agent PASS leaves ------------------------------------------------------------------


def lint_draft(text: str, app_id: str) -> tuple[list[str], list[str]]:
    """(errors, warnings) for a draft flow. Errors keep it from becoming a flow at all.

    The house rules of maestro-automation (`MAESTRO_FLOW_DRAFTER.md`): no coordinate taps (`point:`) — they
    pass while hitting the wrong element on another device — and assertions that prove the case, since a
    flow that asserts nothing is green whatever the app shows. Positional `index:` is allowed (the repo's
    validated flows use it) but flagged for the reviewer.
    """
    errors, warnings = [], []
    if not text.strip():
        return ["the draft is empty"], []
    config, body = guard_device.split_flow(text)
    named = guard_device.config_app_id(config)
    if named not in (app_id, "${APP_ID}"):
        errors.append(f"appId is {named or 'missing'}, not this run's app ({app_id})")
    if re.search(r"^\s*point\s*:", body, re.M):
        errors.append("a coordinate tap (`point:`): it can pass while hitting the wrong element — use an id, "
                      "text or a relative selector")
    cmds = [n for n, _, _ in guard_device.commands(body)]
    bad = sorted({n or "<scalar list item>" for n in cmds
                  if n not in guard_device.ALLOWED_COMMANDS and n not in ("commands",)})
    if bad:
        errors.append(f"commands this workflow does not allow: {', '.join(bad)}")
    for key in guard_device.FORBIDDEN_KEYS:
        if re.search(rf"^\s*-\s*{key}\s*(:|$)|(^|[\s{{,]){key}\s*:", body, re.M):
            errors.append(f"`{key}` is not allowed in a drafted flow")
    if "assertVisible" not in cmds and "assertNotVisible" not in cmds:
        errors.append("no assertVisible / assertNotVisible: a flow that asserts nothing proves nothing")
    if re.search(r"^\s*index\s*:", body, re.M):
        warnings.append("uses a positional `index:` selector — check it picks the same element on every device")
    return errors, warnings


def flow_text(draft: str, case, *, run_label: str, profile: str = "", when: datetime | None = None) -> str:
    """The draft as a flow file in the repo's house style: a header naming the case it covers (by extid and,
    for a Kiwi case, its number, so `maestro_index` matches it either way), DRAFT status, the account it
    needs, `appId: ${APP_ID}`, the `agent-draft` tag, and the draft's commands with screenshots dropped
    (they were the agent's evidence, not checks)."""
    _, body = guard_device.split_flow(draft)
    kept = [line for line in body.split("\n") if not re.match(r"^\s*-\s*takeScreenshot\s*:", line)]
    when = (when or datetime.now(UTC)).strftime("%Y-%m-%d")
    kiwi = f" (Kiwi #{case.store_id})" if getattr(case, "store_id", "") else ""
    title = " ".join(str(case.summary or "").split())[:100]
    header = [f"# Covers: {case.extid}{kiwi} — {title}" if title else f"# Covers: {case.extid}{kiwi}",
              "#",
              f"# STATUS: DRAFT — from an execute_app agent PASS ({when}, {run_label}); not yet validated on a device.",
              "#   Written from the Maestro commands the agent ran; review every selector before freezing it."]
    if profile:
        header.append(f"# REQUIRES: {profile}")
    return "\n".join([*header, "appId: ${APP_ID}", "tags:", "  - agent-draft", "---",
                      "\n".join(kept).strip("\n"), ""])


# ---- the prompt --------------------------------------------------------------------------------------


def build_prompt(case, rel: str, settings: dict, serial: str, notes: str, check_cmd: str,
                 verdict_every: int) -> str:
    lines = [
        f"Execute test case {case.extid} in the Android app on device {serial} and return a verdict.",
        "",
        f"Case: {case.summary}" if case.summary else "",
        "",
        case.text,
        "",
        f"App: {settings['app_id']} ({settings['variant']} build {settings['build']}), environment "
        f"{settings['environment']}. Every device call takes device_id \"{serial}\"; no other device and no "
        f"other app may be used.",
        (f"The app is signed in as the `{settings['profile']}` test account." if settings.get("profile") else
         "The app is in whatever state the device holds; do not sign in or out."),
        f"`list_devices` will show {serial} among others; use only {serial}. If the app is not on screen, start it "
        "once with `- launchApp`; after that do not launch it again (it restarts the app) unless the case says so. "
        "Look with `inspect_screen` (the view hierarchy). Act with `run`, passing inline `yaml` that starts "
        f"`appId: {settings['app_id']}`, a `---` line, then one or a few Maestro commands. `cheat_sheet` lists "
        "the command syntax.",
        f"Limits: {settings['max_actions']} acting commands (inspecting and asserting are free) and "
        f"{settings['case_timeout_s'] // 60} minutes. A session that runs out is cut off without warning — "
        f"write the verdict file BEFORE your first `run` and rewrite it after each part. The device refuses to "
        f"act until the file exists, and again after {verdict_every} commands without a rewrite.",
        (f"Check your verdict whenever you rewrite it, and always before you stop: run `{check_cmd} {rel}`."
         if check_cmd else
         "Your verdict is checked every time you write it: the result of the write ends with OK, or with exactly "
         "what will be rejected. Fix what it names and write the file again until it says OK. You have no shell "
         "and need none."),
        "",
        f"Your case folder is {rel}/, and it is the device server's working directory. Prove every clause you "
        f"mark met or not met by sending, through `run`, an `assertVisible` (or `assertNotVisible`) for what "
        f"you saw followed by `- takeScreenshot: <name>` — a bare name lands in your folder as <name>.png; give "
        f"that file as the clause's `evidence`.",
        "Prefer selectors in this order: `id`, then exact text, then a relative selector (below, childOf, …). "
        "Avoid `point:` taps: a coordinate can hit the wrong element.",
        f"When the case PASSES, also write {rel}/{DRAFT}: one Maestro flow (`appId: {settings['app_id']}`, "
        f"`---`, commands) holding, in order, the commands that got you there and the asserts that proved "
        f"each clause — not the attempts that failed, not the screenshots. It becomes a draft flow for this "
        f"case. Do not write it for a FAIL or a BLOCKED.",
        f"Write the verdict to {rel}/{ui_agent.VERDICT} — the object the skill's 'Harness output contract' "
        f"describes, with \"extid\": \"{case.extid}\".",
        f"If you learn something about this app or environment the next case would need, write it as short "
        f"bullet lines to {rel}/{NOTES}.",
    ]
    if settings.get("forbid"):
        lines += ui_agent.allow_lines(settings.get("allow") or ())
    if settings.get("production"):
        lines += ["This is the PRODUCTION app on a test account: " + ("an order or a payment" if "coupons" in
                  (settings.get("allow") or ()) else "an order, a payment or a coupon") + " here is real."]
    if notes.strip():
        lines += ["", "Notes from earlier cases in this run:", "", notes.strip()]
    return "\n".join(line for line in lines if line is not None)
