"""Write-guard containment — quoted-span redirect parsing (v7 postmortem, 2026-08-10)."""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "src" / "rfc_agent" / "hooks" / "guard_writes.py"


def _run(cmd, run_dir):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}}),
        capture_output=True, text=True, timeout=10,
        env={"RFC_AGENT_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"},
    ).returncode


def test_quoted_comparison_is_not_a_redirect(tmp_path):
    """`awk -F: '$1>4300'` was denied — the redirect regex parsed the QUOTED `1>4300` as an fd
    redirect to file "4300". A `>` inside quotes is literal; a redirect to a quoted TARGET
    (unquoted `>`) must still be caught, in and outside the run dir alike."""
    assert _run("grep -n \"def \" x/order.py | awk -F: '$1>4300 && $1<4700'", tmp_path) == 0
    assert _run("awk 'NR>=4380 && NR<=4660' f.py", tmp_path) == 0
    assert _run(f'echo x > "{tmp_path}/ok.txt"', tmp_path) == 0       # quoted target inside run dir
    assert _run('echo x > "/somewhere/else/evil.txt"', tmp_path) == 2  # quoted target outside: denied
    assert _run("echo x > /somewhere/else/evil.txt", tmp_path) == 2    # unquoted: denied


def test_sort_write_and_exec_flags_denied(tmp_path):
    """sort is prefix-allowlisted as read-only, but -o/--output writes with no `>` for the redirect
    check to see, and --compress-program execs (GTFOBins). Flag forms deny flat; plain sort stays."""
    assert _run("sort -o /etc/evil data.txt", tmp_path) == 2
    assert _run(f"sort -o {tmp_path}/x data.txt", tmp_path) == 2      # flat deny: use `>` instead
    assert _run("sort --output=../evil data.txt", tmp_path) == 2
    assert _run("sort --compress-program=/bin/sh data.txt", tmp_path) == 2
    assert _run("sort -u data.txt | uniq -c", tmp_path) == 0
    assert _run(f"grep x f | sort > {tmp_path}/ok.txt", tmp_path) == 0


def _run_tool(tool, tool_input, run_dir):
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": tool_input}),
        capture_output=True, text=True, timeout=10,
        env={"RFC_AGENT_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"},
    )


def test_use_figma_permits_only_the_fixed_enumeration_scripts(tmp_path):
    """use_figma is arbitrary-JS-write-capable; the grant (mcp_tools) is safe only because this
    hook rejects everything but the fixed read-only scripts, node id substituted, any whitespace."""
    sys.path.insert(0, str(HOOK.parent.parent.parent))
    from rfc_agent.hooks.guard_writes import USE_FIGMA_SCRIPTS

    pages, siblings, children = USE_FIGMA_SCRIPTS
    for name in ("mcp__claude_ai_Figma__use_figma", "mcp__figma__use_figma",
                 "mcp__plugin_figma_figma__use_figma"):
        assert _run_tool(name, {"code": pages, "fileKey": "k"}, tmp_path).returncode == 0
    ok = siblings.replace("NODE_ID", "19028:132368")
    assert _run_tool("mcp__claude_ai_Figma__use_figma", {"code": ok}, tmp_path).returncode == 0
    # whitespace variance (line-wrapped copy) still passes — no deny loops over formatting
    wrapped = ok.replace("; ", ";\n").replace("{id:", "{ id:")
    assert _run_tool("mcp__claude_ai_Figma__use_figma", {"code": wrapped}, tmp_path).returncode == 0
    assert _run_tool("mcp__claude_ai_Figma__use_figma",
                     {"code": children.replace("NODE_ID", "19402:83667")}, tmp_path).returncode == 0
    # anything else is denied: improvised JS, a template with a payload appended, a write call,
    # an unsubstituted or malformed node id slot
    for bad in ("figma.currentPage.appendChild(figma.createFrame()); return 1;",
                ok + " figma.root.setPluginData('x','y');",
                siblings,  # NODE_ID left unsubstituted
                siblings.replace("NODE_ID", "19028:132368'); evil(); ('")):
        r = _run_tool("mcp__claude_ai_Figma__use_figma", {"code": bad}, tmp_path)
        assert r.returncode == 2 and "VERBATIM" in r.stderr


def _run_write(tool, target, run_dir, emit=True):
    env = {"RFC_AGENT_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"}
    if emit:
        env["RFC_AGENT_EMIT_DELIVERABLES"] = "1"
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": {"file_path": str(target)},
                          "cwd": str(run_dir)}),
        capture_output=True, text=True, timeout=10, env=env,
    )


def test_emission_mode_denies_deliverable_tool_writes_compose_included(tmp_path):
    """v7 incident (2026-09-07): the compose session rationalized a direct Write of out/contract.md
    past the prompt-only prohibition — the deny was revision-only AND existence-gated, so writing a
    not-yet-materialized deliverable sailed through. Under emission the deny is by PATH, every mode,
    existing or not; everything else in the run dir writes normally; the denial teaches the recovery."""
    (tmp_path / "out").mkdir()
    for rel in ("out/rfc.md", "out/contract.md", "out/detail/plan.md"):
        for tool in ("Write", "Edit"):
            r = _run_write(tool, tmp_path / rel, tmp_path)          # none of these files exist
            assert r.returncode == 2 and "EMISSION" in r.stderr and "<emit path=" in r.stderr
    assert _run_write("Write", tmp_path / "out" / "notes.md", tmp_path).returncode == 0
    assert _run_write("Write", tmp_path / "findings" / "x.md", tmp_path).returncode == 0
    # legacy mode (GENERATE.EMIT=0 -> env unset): compose writes stay allowed, nothing regresses
    assert _run_write("Write", tmp_path / "out" / "rfc.md", tmp_path, emit=False).returncode == 0


def test_guard_deliverables_mirror_emit_allowed():
    sys.path.insert(0, str(HOOK.parent.parent.parent))
    from rfc_agent.emit import _ALLOWED
    from rfc_agent.hooks.guard_writes import _DELIVERABLES

    assert set(_DELIVERABLES) == set(_ALLOWED)


def test_fails_closed_when_policy_package_is_missing(tmp_path):
    """Containment must never fail open: an interpreter that cannot import harness.policy denies (exit 2)."""
    import subprocess
    import sys as _sys
    from pathlib import Path as _P

    hook = _P(__file__).resolve().parents[2] / "src" / "rfc_agent" / "hooks" / "guard_writes.py"
    shadow = tmp_path / "shadow"
    (shadow / "harness").mkdir(parents=True)
    (shadow / "harness" / "__init__.py").write_text("")  # an empty `harness` shadows the real one -> ImportError
    payload = {"tool_name": "Write", "tool_input": {"file_path": str(tmp_path / "out.md"), "content": "x"}}
    env = {"PATH": "/usr/bin:/bin", "RFC_AGENT_RUN_DIR": str(tmp_path), "PYTHONPATH": str(shadow)}
    r = subprocess.run([_sys.executable, "-S", str(hook)], input=json.dumps(payload), text=True,
                       capture_output=True, env=env)
    assert r.returncode == 2, (r.returncode, r.stderr)
    assert "harness.policy" in r.stderr
