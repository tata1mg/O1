"""PiEngine adapter tests — argv, env contract, translation, timeout, failure detection, doctor.

Everything here runs against `tests/fixtures/pi/fake_pi.py` installed as the `pi` binary: this machine has
no Pi and no API key, and the fake is the proof for the adapter. The fake asserts the
argv/env contract from its own side too, so a dropped `--session-dir` fails loudly rather than silently.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import flags as F
from harness.contracts import EngineFailure, PipelineConfigError
from harness.engines import Containment, EngineRequest
from harness.engines.pi import PiEngine, _assert_known_flags
from harness.events import EventType, ToolKind, events_path, read_events

FIXTURES = Path(__file__).parents[1] / "fixtures" / "pi"
FAKE_PI = FIXTURES / "fake_pi.py"


@pytest.fixture
def fake_pi(tmp_path: Path):
    """Install the fake as an executable named `pi` and return (bin_path, argv_dump_path)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "pi"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_PI}" "$@"\n', encoding="utf-8")
    shim.chmod(0o755)
    dump = tmp_path / "argv.json"
    os.environ["FAKE_PI_ARGV"] = str(dump)
    yield shim, dump
    for k in list(os.environ):
        if k.startswith("FAKE_PI_"):
            del os.environ[k]


def make_req(tmp_path: Path, **kw) -> EngineRequest:
    run_dir = tmp_path / "run"
    (run_dir / "raw").mkdir(parents=True, exist_ok=True)
    (run_dir / "raw" / "prd.md").write_text("# PRD\nREQ-1 a\nREQ-2 b\n", encoding="utf-8")
    kw.setdefault("prompt", "Extract the requirements.")
    kw.setdefault("label", "extract")
    kw.setdefault("timeout_s", 60)
    return EngineRequest(cwd=run_dir, trace_path=events_path(run_dir, kw["label"]), **kw)


def run_engine(fake, req, **engine_kw):
    shim, dump = fake
    eng = PiEngine(bin=str(shim), **engine_kw)
    res = eng.run(req)
    return res, json.loads(dump.read_text())


def events_of(req) -> list[dict]:
    return list(read_events(req.trace_path))


def by_type(evs, t) -> list[dict]:
    return [e for e in evs if e.get("t") == t]


# ---- argv ------------------------------------------------------------------------------------------


def test_argv_carries_mode_hermeticity_guard_session_and_prompt(tmp_path, fake_pi):
    skill = tmp_path / "SKILL.md"
    skill.write_text("# persona\n", encoding="utf-8")
    req = make_req(tmp_path, model="anthropic/claude-sonnet-5", effort="high", skill_path=skill,
                   system_prompt_append="Sandbox: writes land in the run dir only.")
    _, dumped = run_engine(fake_pi, req, )
    argv = dumped["argv"]

    assert argv[:4] == ["--mode", "json", "-p", "--no-approve"]
    for flag in ("--no-skills", "--no-prompt-templates", "--no-themes", "--no-context-files",
                 "--no-extensions"):
        assert flag in argv, flag
    # exactly one extension, ours
    assert argv[argv.index("-e") + 1].endswith("harness-guard.js")
    assert argv.count("-e") == 1
    assert argv[argv.index("--model") + 1] == "anthropic/claude-sonnet-5"
    assert argv[argv.index("--thinking") + 1] == "high"
    # the SKILL.md goes in as a PATH (pi reads a --system-prompt value that exists on disk)
    assert argv[argv.index("--system-prompt") + 1] == str(skill.resolve())
    appended = Path(argv[argv.index("--append-system-prompt") + 1])
    assert appended.read_text() == "Sandbox: writes land in the run dir only."
    assert argv[argv.index("--session-dir") + 1] == str(req.cwd / "logs" / "pi-sessions")
    assert "--session-id" in argv and "--session" not in argv
    assert argv[argv.index("--name") + 1] == "extract"
    # the prompt is the last positional, always behind `--`
    assert argv[-2:] == ["--", "Extract the requirements."]


def test_tool_lists_are_translated_to_pi_names_and_mcp_dropped(tmp_path, fake_pi):
    req = make_req(tmp_path, containment=Containment(
        writable_roots=(tmp_path / "run",),
        allowed_tools=("Read", "Grep", "Glob", "mcp__atlassian__search"),
        disallowed_tools=("Bash",),
    ))
    _, dumped = run_engine(fake_pi, req)
    argv = dumped["argv"]
    assert argv[argv.index("--tools") + 1] == "read,grep,find"  # Glob -> find; the MCP tool is dropped
    assert argv[argv.index("--exclude-tools") + 1] == "bash"


SESSION_ID = "11111111-2222-3333-4444-555555555555"


def seed_session_file(run_dir: Path, session_id: str = SESSION_ID) -> Path:
    """Write a session file where pi would have left it: <session-dir>/--<mangled-cwd>--/<ts>_<id>.jsonl."""
    d = run_dir / "logs" / "pi-sessions" / ("--" + str(run_dir).lstrip("/").replace("/", "-") + "--")
    d.mkdir(parents=True, exist_ok=True)
    f = d / f"20260908-100000_{session_id}.jsonl"
    f.write_text(json.dumps({"type": "session", "version": 3, "id": session_id, "cwd": str(run_dir)}) + "\n",
                 encoding="utf-8")
    return f


def test_resume_continues_the_saved_session_file(tmp_path, fake_pi):
    """Warm context: pi's headless resume primitive is `--session <path>`, and because the id was minted
    with `--session-id` on the first pass the path is derivable without parsing anything."""
    req = make_req(tmp_path, resume_session=SESSION_ID)
    saved = seed_session_file(req.cwd)
    res, dumped = run_engine(fake_pi, req)
    argv = dumped["argv"]
    assert argv[argv.index("--session") + 1] == str(saved)
    assert "--session-id" not in argv
    assert res.summary.session_id == SESSION_ID
    assert PiEngine().supports_resume() is True
    logs = [e["text"] for e in by_type(events_of(req), EventType.LOG)]
    assert any(f"resuming pi session {SESSION_ID} from {saved}" in t for t in logs)


def test_resume_falls_back_cold_with_a_log_when_the_session_file_is_gone(tmp_path, fake_pi):
    req = make_req(tmp_path, resume_session=SESSION_ID)  # nothing seeded
    res, dumped = run_engine(fake_pi, req)
    argv = dumped["argv"]
    assert argv[argv.index("--session-id") + 1] == SESSION_ID, "same id, new session"
    assert "--session" not in argv
    assert res.summary.session_id == SESSION_ID
    logs = [e["text"] for e in by_type(events_of(req), EventType.LOG)]
    assert any("starting COLD with the same id" in t for t in logs)


def test_unknown_flag_is_refused_because_pi_would_ignore_it():
    with pytest.raises(EngineFailure, match="not pinned to"):
        _assert_known_flags(["pi", "--mode", "json", "--no-skils"])


def test_a_prompt_starting_with_a_dash_is_not_mistaken_for_a_flag():
    """`--` is passed precisely so a prompt may begin with `-`; validating past the terminator rejected
    exactly the prompts it exists to protect."""
    for prompt in ("- Extract the requirements from raw/prd.md", "--- context ---\nGo", "-p is a flag"):
        _assert_known_flags(["pi", "--mode", "json", "-p", "--", prompt])  # must not raise


def test_a_dash_prompt_survives_the_whole_run(tmp_path, fake_pi):
    req = make_req(tmp_path, prompt="- Extract every REQ id")
    _, dumped = run_engine(fake_pi, req)
    assert dumped["argv"][-2:] == ["--", "- Extract every REQ id"]


def test_unknown_thinking_level_is_refused(tmp_path, fake_pi):
    req = make_req(tmp_path, effort="ultra")
    with pytest.raises(EngineFailure, match="thinking level"):
        run_engine(fake_pi, req)


def test_missing_binary_names_the_fix(tmp_path):
    req = make_req(tmp_path)
    with pytest.raises(EngineFailure, match="pi-coding-agent"):
        PiEngine(bin=str(tmp_path / "nope" / "pi")).run(req)


# ---- env / policy contract -------------------------------------------------------------------------


def test_pi_flags_reach_argv_through_the_flags_object(tmp_path, fake_pi):
    """every pi.* knob must be reachable from `--set`. The adapter reads them off the resolved Flags
    object it is constructed with, so a flag set on the CLI has to appear in the argv it builds."""
    flags = F.resolve(cli_sets={"pi.provider": "anthropic", "pi.thinking": "xhigh",
                                "pi.session_dir": "logs/sessions-alt", "pi.hermetic": False})
    req = make_req(tmp_path)
    _, dumped = run_engine(fake_pi, req, flags=flags)
    argv = dumped["argv"]
    assert argv[argv.index("--provider") + 1] == "anthropic"
    assert argv[argv.index("--thinking") + 1] == "xhigh"
    assert argv[argv.index("--session-dir") + 1] == str(req.cwd / "logs" / "sessions-alt")
    assert "--no-skills" not in argv, "pi.hermetic=False must drop the hermeticity flags"


def test_pi_bin_and_guard_path_come_from_flags_too(tmp_path, fake_pi):
    shim, _ = fake_pi
    guard = tmp_path / "alt-guard.js"
    guard.write_text("export default function () {}\n", encoding="utf-8")
    flags = F.resolve(cli_sets={"pi.bin": str(shim), "pi.guard_path": str(guard),
                                "pi.denials_echo": True, "pi.telemetry": True})
    eng = PiEngine(flags=flags)
    assert eng.bin == str(shim)
    req = make_req(tmp_path)
    eng.run(req)
    dumped = json.loads((tmp_path / "argv.json").read_text())
    assert dumped["argv"][dumped["argv"].index("-e") + 1] == str(guard)
    assert dumped["env"]["HARNESS_DENIALS_ECHO"] == "1"
    assert "PI_TELEMETRY" not in dumped["env"]


def test_env_carries_the_policy_contract_and_pins_pi_to_the_run(tmp_path, fake_pi):
    repos = tmp_path / "repos"
    repos.mkdir()
    req = make_req(tmp_path, containment=Containment(
        writable_roots=(tmp_path / "run",), readable_roots=(repos,), readonly_shell=False,
        disallowed_tools=("Bash",)))
    _, dumped = run_engine(fake_pi, req)
    env = dumped["env"]

    policy = json.loads(env["HARNESS_POLICY_JSON"])
    assert policy["writable_roots"] == [str(tmp_path / "run")]
    assert policy["readable_roots"] == [str(repos)]
    assert policy["readonly_shell"] is False
    assert policy["disabled_tools"] == ["bash"]
    assert policy["cwd"] == str(tmp_path / "run")
    assert json.loads(env["HARNESS_POLICY_CMD"]) == [sys.executable, "-m", "harness.policy", "check"]
    # the workflow-only fields are absent unless a workflow declares them (see the two tests below)
    for key in ("broker_hint", "repos_root", "exempt_marker"):
        assert key not in policy
    assert env["HARNESS_DENIALS_PATH"].endswith("extract.denials.jsonl")
    assert env["PI_OFFLINE"] == "1" and env["PI_TELEMETRY"] == "0" and env["PI_SKIP_VERSION_CHECK"] == "1"
    assert env["PI_CODING_AGENT_DIR"] == str(tmp_path / "run" / ".pi-home")
    settings = json.loads((tmp_path / "run" / ".pi-home" / "settings.json").read_text())
    assert settings == {"compaction": {"enabled": False}}  # lossy compaction off by default


def test_the_workflow_only_policy_fields_are_forwarded_from_the_environment(tmp_path, fake_pi):
    """`broker_hint` / `repos_root` / `exempt_marker` are what make a pinned broker exempt and make the
    policy's teaching strings name a real alternative. The kernel may not derive them (naming a
    workflow's broker script here is a layering violation), so they ride engine-neutral env keys."""
    req = make_req(tmp_path, env={
        "HARNESS_POLICY_BROKER_HINT": "/venv/bin/python /pkg/hooks/stage_tools.py",
        "HARNESS_POLICY_REPOS_ROOT": "/abs/repos",
        "HARNESS_POLICY_EXEMPT_MARKER": "rfc_agent",
    })
    _, dumped = run_engine(fake_pi, req)
    policy = json.loads(dumped["env"]["HARNESS_POLICY_JSON"])
    assert policy["broker_hint"].endswith("stage_tools.py")
    assert policy["repos_root"] == "/abs/repos"
    assert policy["exempt_marker"] == "rfc_agent"


def test_the_policy_extra_flag_beats_the_environment(tmp_path, fake_pi):
    flags = F.resolve(cli_sets={"pi.policy_extra": '{"exempt_marker": "from_flag"}'})
    req = make_req(tmp_path, env={"HARNESS_POLICY_EXEMPT_MARKER": "from_env"})
    _, dumped = run_engine(fake_pi, req, flags=flags)
    policy = json.loads(dumped["env"]["HARNESS_POLICY_JSON"])
    assert policy["exempt_marker"] == "from_flag"


def test_no_declared_writable_roots_defaults_to_the_run_dir_and_says_so(tmp_path, fake_pi):
    req = make_req(tmp_path)  # default Containment(): writable_roots == ()
    _, dumped = run_engine(fake_pi, req)
    assert json.loads(dumped["env"]["HARNESS_POLICY_JSON"])["writable_roots"] == [str(tmp_path / "run")]
    logs = [e["text"] for e in by_type(events_of(req), EventType.LOG)]
    assert any("defaulting to the run dir" in t for t in logs)


def test_hooks_disabled_means_no_guard_extension_and_a_loud_note(tmp_path, fake_pi):
    req = make_req(tmp_path, containment=Containment(writable_roots=(tmp_path / "run",), hooks_enabled=False))
    _, dumped = run_engine(fake_pi, req)
    assert "-e" not in dumped["argv"]
    logs = [e["text"] for e in by_type(events_of(req), EventType.LOG)]
    assert any("NO containment is in force" in t for t in logs)


def test_a_stage_needing_mcp_is_refused_before_any_spend(tmp_path, fake_pi):
    """pi has no MCP and no upstream MCP extension, so such a stage cannot succeed. Refusing up front
    beats running it and failing its output check after paying for the turn."""
    req = make_req(tmp_path, containment=Containment(
        writable_roots=(tmp_path / "run",), mcp_servers={"atlassian": {}, "figma": {}}))
    with pytest.raises(PipelineConfigError) as exc:
        run_engine(fake_pi, req)
    msg = str(exc.value)
    assert "extract" in msg and "atlassian" in msg and "figma" in msg
    assert "stage_engines" in msg, "the error must name the override that fixes it"
    assert not req.trace_path.exists() or not by_type(events_of(req), EventType.RESULT), "nothing ran"


def test_readable_roots_and_steering_are_recorded_as_gaps(tmp_path, fake_pi):
    req = make_req(tmp_path, containment=Containment(
        writable_roots=(tmp_path / "run",), readable_roots=(tmp_path,)), steer_epoch=123.0)
    run_engine(fake_pi, req)
    logs = " | ".join(e["text"] for e in by_type(events_of(req), EventType.LOG))
    assert "no --add-dir equivalent" in logs
    assert "no steering channel" in logs


def test_mcp_tool_names_are_still_dropped_from_the_tool_lists(tmp_path, fake_pi):
    """A Claude-authored allowlist carries mcp__* names even when the stage declares no servers."""
    req = make_req(tmp_path, containment=Containment(
        writable_roots=(tmp_path / "run",), allowed_tools=("Read", "mcp__atlassian__search")))
    _, dumped = run_engine(fake_pi, req)
    assert dumped["argv"][dumped["argv"].index("--tools") + 1] == "read"
    logs = " | ".join(e["text"] for e in by_type(events_of(req), EventType.LOG))
    assert "dropped 1 MCP tool name" in logs


# ---- translation of a full run ---------------------------------------------------------------------


def test_run_translates_the_recorded_stream_into_canonical_events(tmp_path, fake_pi):
    os.environ["FAKE_PI_OUTPUTS"] = "out/extract.json"
    req = make_req(tmp_path, model="claude-sonnet-5")
    res, _ = run_engine(fake_pi, req)
    evs = events_of(req)

    assert len(by_type(evs, EventType.INIT)) == 1
    init = by_type(evs, EventType.INIT)[0]
    assert init["engine"] == "pi" and init["extra"]["session_version"] == 3
    assert by_type(evs, EventType.USER)[0]["text"] == "Extract the requirements."
    assert len(by_type(evs, EventType.THINKING)) == 1
    assert len(by_type(evs, EventType.TEXT)) == 4
    assert len(by_type(evs, EventType.TOOL_START)) == 4
    assert len(by_type(evs, EventType.TOOL_END)) == 4
    assert [e["kind"] for e in by_type(evs, EventType.TOOL_START)] == [
        ToolKind.READ, ToolKind.SEARCH, ToolKind.SHELL, ToolKind.WRITE]
    assert len(by_type(evs, EventType.RESULT)) == 1

    # the raw pi stream is kept beside the canonical trace as evidence
    assert (req.cwd / "logs" / "extract.pi.jsonl").read_text().count('"type":"session"') == 1
    assert (req.cwd / "out" / "extract.json").exists()

    s = res.summary
    assert s.got_result and not s.is_error
    assert s.final_text == "Extracted 2 requirements (REQ-1, REQ-2) into out/extract.json."
    assert s.num_turns == 4
    assert s.model == "claude-sonnet-5"
    assert s.session_id and len(s.session_id) == 36  # the uuid the adapter minted
    assert res.engine_desc == "pi:claude-sonnet-5"
    assert res.exit_code == 0
    assert s.reads == ["raw/prd.md"]
    assert s.searches == [{"pattern": "REQ-[0-9]+", "path": "raw"}]
    assert s.writes == ["out/extract.json"]


def test_cost_is_summed_from_the_session_jsonl_with_the_category_breakdown(tmp_path, fake_pi):
    req = make_req(tmp_path)
    res, _ = run_engine(fake_pi, req)
    # 0.008025 + 0.00846 + 0.01452 + 0.01038, from the recorded per-message Usage.cost.total
    assert res.summary.cost_usd == pytest.approx(0.041385)
    result = by_type(events_of(req), EventType.RESULT)[0]
    assert result["extra"]["cost_source"] == "session_file"
    breakdown = result["extra"]["cost"]
    assert breakdown["total"] == pytest.approx(0.041385)
    assert breakdown["input"] == pytest.approx(0.0276)
    assert breakdown["output"] == pytest.approx(0.008925)
    assert breakdown["cacheRead"] == pytest.approx(0.00186)
    assert breakdown["cacheWrite"] == pytest.approx(0.003)


def test_cost_is_none_not_zero_when_no_message_reported_a_cost(tmp_path, fake_pi):
    """pi emits no terminal totals record and some providers report usage only at completion, so an
    unmeasured run must not be indistinguishable from a free one."""
    os.environ["FAKE_PI_STREAM"] = str(FIXTURES / "session.nocost.jsonl")
    req = make_req(tmp_path)
    res, _ = run_engine(fake_pi, req)
    assert res.summary.cost_usd is None
    result = by_type(events_of(req), EventType.RESULT)[0]
    assert "cost_usd" not in result, "the RESULT event omits an unknown cost rather than claiming 0.0"
    assert result["extra"]["cost_source"] == "none"
    assert "cost" not in result["extra"]


def test_denials_come_from_the_guards_side_channel(tmp_path, fake_pi):
    os.environ["FAKE_PI_DENIALS"] = json.dumps([
        {"tool": "write", "toolCallId": "call-9", "reason": "Write targets /etc/hosts, outside the run dir"},
        {"tool": "bash", "toolCallId": "call-10", "reason": "command mutates outside the run dir"},
    ])
    req = make_req(tmp_path)
    res, _ = run_engine(fake_pi, req)
    denials = by_type(events_of(req), EventType.DENIAL)
    assert len(denials) == 2 and res.summary.denials == 2
    assert denials[0]["tool"] == "write" and "outside the run dir" in denials[0]["reason"]
    assert denials[0]["call_id"] == "call-9"


# ---- failure detection -----------------------------------------------------------------------------


def test_exit_zero_with_an_errored_stream_is_a_failure(tmp_path, fake_pi):
    os.environ["FAKE_PI_STREAM"] = str(FIXTURES / "session.error.jsonl")
    req = make_req(tmp_path)
    res, _ = run_engine(fake_pi, req)
    assert res.exit_code == 0, "pi --mode json exits 0 even on agent error — that is the trap under test"
    s = res.summary
    assert s.is_error
    assert "stopReason=error" in s.error_reason
    assert "provider retries exhausted" in s.error_reason
    assert s.compactions == 1


def test_a_stream_without_agent_settled_is_a_failure(tmp_path, fake_pi):
    os.environ["FAKE_PI_NO_STREAM"] = "1"
    req = make_req(tmp_path)
    res, _ = run_engine(fake_pi, req)
    assert res.summary.is_error
    assert "without agent_settled" in res.summary.error_reason


def test_extension_error_on_stderr_fails_the_stage(tmp_path, fake_pi):
    os.environ["FAKE_PI_STDERR"] = "Extension error (harness-guard.js): boom"
    req = make_req(tmp_path)
    res, _ = run_engine(fake_pi, req)
    assert res.summary.is_error
    assert "guard extension errored" in res.summary.error_reason
    assert "Extension error" in (req.cwd / "logs" / "extract.pi.stderr").read_text()


def assert_dead(pid: int, within_s: float = 25.0) -> None:
    deadline = time.monotonic() + within_s
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    pytest.fail(f"process {pid} survived — the process GROUP was not killed")


def test_timeout_kills_the_whole_process_group(tmp_path, fake_pi):
    pid_file = tmp_path / "child.pid"
    os.environ["FAKE_PI_SLEEP"] = "30"
    os.environ["FAKE_PI_CHILD_PID"] = str(pid_file)
    req = make_req(tmp_path, timeout_s=1)
    started = time.monotonic()
    with pytest.raises(EngineFailure, match="timed out after 1s"):
        run_engine(fake_pi, req)
    assert time.monotonic() - started < 40, "the deadline was not enforced"
    assert_dead(int(pid_file.read_text()))

    # the trace is still complete evidence: a terminal RESULT marked not-ok, then the typed failure
    result = by_type(events_of(req), EventType.RESULT)[0]
    assert result["ok"] is False and "timed out" in result["error"]


def test_a_timeout_cannot_hang_on_a_grandchild_that_ignores_sigterm(tmp_path, fake_pi):
    """The real shape of pi's bash tool: the leader dies politely on SIGTERM while a node server or `npm`
    it spawned ignores it AND holds the inherited stdout pipe open. A reader that waits on the pipe, or an
    escalation conditional on the LEADER surviving, wedges the run past its own timeout forever."""
    pid_file = tmp_path / "child.pid"
    os.environ["FAKE_PI_SLEEP"] = "30"
    os.environ["FAKE_PI_CHILD_PID"] = str(pid_file)
    os.environ["FAKE_PI_CHILD_TRAPS_TERM"] = "1"
    req = make_req(tmp_path, timeout_s=1)
    started = time.monotonic()
    with pytest.raises(EngineFailure, match="timed out after 1s"):
        run_engine(fake_pi, req)
    elapsed = time.monotonic() - started
    # the SIGTERM grace is KILL_GRACE_S; anything near 60s is the old hang
    assert elapsed < 40, f"took {elapsed:.1f}s — the timeout hung on the inherited pipe"
    assert_dead(int(pid_file.read_text()))
    result = by_type(events_of(req), EventType.RESULT)[0]
    assert result["ok"] is False and "timed out" in result["error"]


def test_a_non_positive_timeout_is_a_config_error_not_an_instant_death(tmp_path, fake_pi):
    for bad in (0, -5, None):
        req = make_req(tmp_path, timeout_s=bad)
        with pytest.raises(PipelineConfigError, match="timeout_s must be a positive"):
            run_engine(fake_pi, req)


def test_budget_kill_is_best_effort_but_terminal(tmp_path, fake_pi):
    req = make_req(tmp_path, max_budget_usd=0.01)  # the recorded stream passes 0.01 on its second message
    with pytest.raises(EngineFailure, match="exceeded max_budget_usd"):
        run_engine(fake_pi, req)
    result = by_type(events_of(req), EventType.RESULT)[0]
    assert result["ok"] is False and "max_budget_usd" in result["error"]


def test_a_zero_budget_means_spend_nothing_not_no_cap(tmp_path, fake_pi):
    """`or None` turned a legitimate "spend nothing" into the opposite instruction."""
    req = make_req(tmp_path, max_budget_usd=0.0)
    with pytest.raises(EngineFailure, match="exceeded max_budget_usd"):
        run_engine(fake_pi, req)


def test_a_negative_budget_is_refused(tmp_path, fake_pi):
    req = make_req(tmp_path, max_budget_usd=-1.0)
    with pytest.raises(PipelineConfigError, match="must not be negative"):
        run_engine(fake_pi, req)


def test_an_unreachable_policy_cli_is_a_startup_error_not_a_stage_of_denials(tmp_path, fake_pi, monkeypatch):
    """The guard fails safe, so an unreachable policy CLI presents as "no tool worked" AFTER the spend.
    One probe before Popen turns it into a named startup failure."""
    monkeypatch.setattr("harness.engines.pi.policy_cmd",
                        lambda: [sys.executable, "-c", "import sys; sys.exit(3)"])
    req = make_req(tmp_path)
    with pytest.raises(EngineFailure, match="containment policy CLI gave no verdict"):
        run_engine(fake_pi, req)
    assert not (req.cwd / "logs" / "extract.pi.jsonl").exists(), "pi must never have been spawned"


def test_a_relative_pythonpath_is_absolutised_for_the_guard(tmp_path, fake_pi, monkeypatch):
    """A relative PYTHONPATH resolves against the GUARD's cwd (the run dir), where `src` does not exist —
    every policy call would fail and the guard would block every tool."""
    monkeypatch.setenv("PYTHONPATH", f"src{os.pathsep}/already/absolute")
    req = make_req(tmp_path)
    _, dumped = run_engine(fake_pi, req)
    parts = dumped["env"]["PYTHONPATH"].split(os.pathsep)
    assert all(os.path.isabs(p) for p in parts), parts
    assert "/already/absolute" in parts, "an absolute entry must be left exactly as the operator wrote it"


def test_a_torn_non_json_stdout_line_is_recorded_not_fatal(tmp_path, fake_pi):
    os.environ["FAKE_PI_STDOUT_NOISE"] = "not json at all"
    req = make_req(tmp_path)
    res, _ = run_engine(fake_pi, req)
    logs = " | ".join(e.get("text") or "" for e in by_type(events_of(req), EventType.LOG))
    assert "__nonjson__" in logs or "unknown pi event type" in logs
    assert res.summary.got_result


# ---- doctor ----------------------------------------------------------------------------------------


def test_doctor_reports_a_missing_binary_with_an_install_command(tmp_path):
    problems = PiEngine(bin=str(tmp_path / "absent-pi")).doctor()
    assert any("npm install -g --ignore-scripts @earendil-works/pi-coding-agent" in p for p in problems)


def test_doctor_flags_an_old_pi_and_an_old_node(tmp_path, fake_pi, monkeypatch):
    shim, _ = fake_pi
    monkeypatch.setenv("FAKE_PI_VERSION", "0.74.2")
    node = tmp_path / "bin" / "node"
    node.write_text('#!/bin/sh\necho v20.11.0\n', encoding="utf-8")
    node.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    problems = PiEngine(bin=str(shim), node_bin=str(node)).doctor()
    assert any("pi 0.74.2 is older than 0.85.0" in p for p in problems)
    assert any("Node 20.11.0 is older than pi's requirement 22.19.0" in p for p in problems)
    assert not any("no provider credential" in p for p in problems)


def test_doctor_reports_missing_credentials_without_printing_any(tmp_path, fake_pi, monkeypatch):
    shim, _ = fake_pi
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "OPENROUTER_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(tmp_path / "empty-home"))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))  # never consult the operator's real ~/.pi
    problems = PiEngine(bin=str(shim)).doctor()
    creds = [p for p in problems if "no provider credential" in p]
    assert len(creds) == 1
    assert "ANTHROPIC_API_KEY" in creds[0] and "sk-" not in creds[0]


def test_doctor_ignores_an_empty_auth_json(tmp_path, fake_pi, monkeypatch):
    """pi writes `auth.json` as `{}` on its first run, so existence is not a credential."""
    shim, _ = fake_pi
    for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY",
              "OPENROUTER_API_KEY"):
        monkeypatch.delenv(v, raising=False)
    home = tmp_path / "pi-home"
    home.mkdir()
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(home))
    monkeypatch.setenv("HOME", str(tmp_path / "fake-home"))

    (home / "auth.json").write_text("{}", encoding="utf-8")
    assert any("no provider credential" in p for p in PiEngine(bin=str(shim)).doctor())

    (home / "auth.json").write_text('{"anthropic": {"key": "sk-secret"}}', encoding="utf-8")
    problems = PiEngine(bin=str(shim)).doctor()
    assert not any("no provider credential" in p for p in problems)
    assert not any("sk-secret" in p for p in problems), "doctor must never surface a credential value"


def test_doctor_is_healthy_when_everything_checks_out(tmp_path, fake_pi, monkeypatch):
    shim, _ = fake_pi
    node = tmp_path / "bin" / "node"
    node.write_text('#!/bin/sh\necho v22.19.0\n', encoding="utf-8")
    node.chmod(0o755)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-a-real-key")
    assert PiEngine(bin=str(shim), node_bin=str(node)).doctor() == []


def test_kill_group_escalates_to_sigkill_even_after_the_leader_is_reaped(tmp_path):
    """The escalation must not be conditional on the LEADER outliving the grace period. pi dies politely
    on SIGTERM while something it spawned ignores it; by then the leader is reaped and `os.getpgid` raises,
    so a lookup at escalation time finds no group to kill. The pgid is captured at spawn for this reason."""
    from harness.engines.pi import _kill_group, _pgid_of

    marker = tmp_path / "survivor.pid"
    # a leader that dies on SIGTERM, having spawned a grandchild that ignores it and holds our stdout pipe
    grandchild = "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(300)"
    script = "\n".join([
        "import subprocess, sys, time",
        f"c = subprocess.Popen([sys.executable, '-c', {grandchild!r}])",
        f"open({str(marker)!r}, 'w').write(str(c.pid))",
        "time.sleep(300)",
    ])
    proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE,
                            start_new_session=True)
    pgid = _pgid_of(proc)
    deadline = time.monotonic() + 20
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert marker.exists(), "the fixture's grandchild never started"
    survivor = int(marker.read_text())

    _kill_group(proc, pgid)

    assert_dead(proc.pid)
    assert_dead(survivor), "a SIGTERM-ignoring grandchild must still be SIGKILLed"
    assert signal.SIGTERM != signal.SIGKILL


def test_the_cli_construction_path_forwards_flags_to_the_engine():
    # was xfail for review finding 1.2 (engine_from_flags dropped `flags`); the kernel fix landed, so this
    # is a plain regression test now.
    """The contract says `--set pi.provider=anthropic` must reach the engine. It reaches PiEngine when a Flags
    object is passed, and both real construction sites go through engine_from_flags — which does not
    pass one. Pinned here so the seam is visible from the pi suite rather than only in a review doc."""
    from harness.engines import engine_from_flags

    flags = F.resolve(cli_sets={"pi.provider": "anthropic"})
    assert engine_from_flags("pi", flags)._flag("pi.provider") == "anthropic"
