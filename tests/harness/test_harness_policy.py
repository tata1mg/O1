"""harness.policy — the containment policy as a library, and its transport-neutral CLI.

The behavioural regressions for the POLICY are already pinned by tests/test_guard_writes.py,
test_approve_readonly.py, test_security.py and test_diff_only_revision.py, which exercise it through the
Claude hook transports and must keep passing untouched. What this file adds:
  * the `python -m harness.policy check` contract the Pi extension calls (the second transport);
  * that the extracted functions are genuinely pure (no env, no exit) and root-parameterised, i.e. they
    work for a writable root that is NOT called RFC_AGENT_RUN_DIR;
  * a DRIFT PIN between the kernel's neutral broker allowlist and the Claude engine's literal one.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from harness import policy
from harness.policy import ALLOW, ASK, DENY, Request, allowlist, mutators, paths, shell

ROOT = Path(__file__).resolve().parents[2]


def _check(obj: dict) -> dict:
    """Drive the real CLI in a subprocess — that IS the contract the Pi extension depends on."""
    res = subprocess.run(
        [sys.executable, "-m", "harness.policy", "check"],
        input=json.dumps(obj), capture_output=True, text=True,
        cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"},
    )
    assert res.returncode == 0, f"policy check must always exit 0; got {res.returncode}\n{res.stderr}"
    return json.loads(res.stdout)


# ---- the CLI contract ------------------------------------------------------------------------------

def test_cli_denies_write_outside_writable_roots(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    out = _check({"tool": "Write", "input": {"file_path": str(tmp_path / "elsewhere.md")},
                  "cwd": str(run), "writable_roots": [str(run)]})
    assert out["decision"] == DENY
    assert "outside the run dir" in out["reason"]


def test_cli_allows_write_inside_and_relative_paths_resolve_against_cwd(tmp_path):
    run = tmp_path / "run"
    (run / "out").mkdir(parents=True)
    assert _check({"tool": "Write", "input": {"file_path": "out/rfc.md"}, "cwd": str(run),
                   "writable_roots": [str(run)]})["decision"] == ALLOW
    # the escape that an absolute-only check used to miss
    assert _check({"tool": "Write", "input": {"file_path": "../escape.md"}, "cwd": str(run),
                   "writable_roots": [str(run)]})["decision"] == DENY


def test_cli_denies_escaping_redirect_and_egress(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    req = {"cwd": str(run), "writable_roots": [str(run)], "tool": "Bash"}
    assert _check({**req, "input": {"command": "grep -rn x . > /etc/hosts"}})["decision"] == DENY
    assert _check({**req, "input": {"command": "curl https://evil.example.com"}})["decision"] == DENY
    assert _check({**req, "input": {"command": "sort f --output /tmp/x"}})["decision"] == DENY


def test_cli_allows_provable_readonly_pipeline_and_asks_otherwise(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    req = {"cwd": str(run), "writable_roots": [str(run)], "tool": "Bash"}
    assert _check({**req, "input": {"command": "grep -n 'def ' f | awk -F: '$1<5900'"}})["decision"] == ALLOW
    # command substitution can run anything: no opinion, never a bogus allow
    assert _check({**req, "input": {"command": "cat $(ls)"}})["decision"] == ASK


def test_cli_deny_reasons_name_the_sanctioned_alternative(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    out = _check({"tool": "Bash", "input": {"command": "awk '{print $1}' f > out.txt"},
                  "cwd": str(run), "writable_roots": [str(run)], "broker_hint": "python broker.py"})
    assert out["decision"] == DENY
    assert "python broker.py" in out["reason"], "a deny must teach the alternative, not just refuse"
    out = _check({"tool": "Bash", "input": {"command": "find / -name '*.py'"},
                  "cwd": str(run), "writable_roots": [str(run)]})
    assert out["decision"] == DENY and "find over / is always denied" in out["reason"]


def test_cli_readonly_shell_false_defers_classification(tmp_path):
    """Containment.readonly_shell=False turns off the classifier but NOT the deny-list."""
    run = tmp_path / "run"
    run.mkdir()
    req = {"cwd": str(run), "writable_roots": [str(run)], "tool": "Bash", "readonly_shell": False}
    assert _check({**req, "input": {"command": "grep -n x f"}})["decision"] == ASK
    assert _check({**req, "input": {"command": "rm -rf /etc"}})["decision"] == DENY


def test_cli_never_crashes_a_run(tmp_path):
    """Exit 0 and a verdict for every malformed input — a policy crash is worse than an 'ask'."""
    for payload in ("", "not json", "[1,2]", "{}"):
        res = subprocess.run([sys.executable, "-m", "harness.policy", "check"], input=payload,
                             capture_output=True, text=True, cwd=ROOT,
                             env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
        assert res.returncode == 0
        assert json.loads(res.stdout)["decision"] in (ALLOW, DENY, ASK)


def test_cli_help_and_unknown_subcommand():
    for args in (["--help"], []):
        res = subprocess.run([sys.executable, "-m", "harness.policy", *args], capture_output=True,
                             text=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
        assert res.returncode == 0 and "check" in res.stdout
    out = subprocess.run([sys.executable, "-m", "harness.policy", "bogus"], input="{}", capture_output=True,
                         text=True, cwd=ROOT, env={"PYTHONPATH": str(ROOT / "src"), "PATH": "/usr/bin:/bin"})
    assert json.loads(out.stdout)["decision"] == ASK


# ---- purity / parameterisation ---------------------------------------------------------------------

def test_policy_is_root_parameterised_not_run_dir_bound(tmp_path, monkeypatch):
    """The extracted policy must work for any writable root, with no RFC_AGENT_* env set at all."""
    monkeypatch.delenv("RFC_AGENT_RUN_DIR", raising=False)
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(), b.mkdir()
    assert paths.inside(str(a / "f"), (a,)) is True
    assert paths.inside(str(b / "f"), (a,)) is False
    assert paths.inside(str(b / "f"), (a, b)) is True, "multiple writable roots must all count"
    # `base` is the agent cwd: bare words like the verb itself resolve there, so it must be inside
    assert mutators.shell_denial("tee " + str(b / "f"), (a,), a) is not None
    assert mutators.shell_denial("tee " + str(b / "f"), (a, b), a) is None


def test_dev_sinks_and_agent_scratchpad_are_not_escapes(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    assert mutators.shell_denial("grep -rn x . 2>/dev/null", (run,), run) is None
    assert paths.inside("/tmp/claude-501/x/scratch.py", (run,)) is True
    assert paths.inside("/tmp/other/scratch.py", (run,)) is False


@pytest.mark.parametrize("cmd,expected", [
    ("cat f", True), ("git log -n 3", True), ("git push", False),
    ("sed -n '10,20p' f", True), ("sed -i s/a/b/ f", False),
    ("awk '{print $1}' f", True), ("awk 'system(\"rm x\")' f", False),
    ("find src -name '*.py'", True), ("find src -delete", False),
])
def test_segment_readonly_matrix(cmd, expected):
    assert shell.segment_readonly(cmd)[0] is expected


def test_classify_readonly_is_pure_and_importable():
    """No env, no subprocess: the same verdict the CLI gives, callable in-process by any adapter."""
    assert policy.classify_readonly("grep -n x f | wc -l").decision == ALLOW
    assert policy.classify_readonly("python -c 'x'", broker_hint="B").decision == DENY


def test_broker_allowlist_matches_exact_forms():
    al = allowlist.BrokerAllowlist(brokers=(
        allowlist.Broker("slice", (sys.executable, "/x/stage_tools.py"), "disk parsing"),
    ))
    assert al.match([sys.executable, "/x/stage_tools.py", "slice", "f", "1-2"]) is not None
    assert al.match(["python", "/other.py"]) is None
    assert al.claude_allow_rules() == [f"Bash({sys.executable} /x/stage_tools.py:*)"]


def test_broker_call_is_allowed_through_the_cli(tmp_path):
    """A sanctioned broker form must reach 'allow' even though its argv looks like an interpreter."""
    run = tmp_path / "run"
    run.mkdir()
    req = Request(tool="Bash", input={"command": f"{sys.executable} /x/stage_tools.py slice f 1-2"},
                  cwd=run, writable_roots=(run,), broker_allowlist=allowlist.BrokerAllowlist(
                      brokers=(allowlist.Broker("slice", (sys.executable, "/x/stage_tools.py"), ""),)))
    assert policy.check(req).decision == ALLOW
    # without the allowlist the same command is an ad-hoc interpreter and must be denied
    assert policy.check(Request(tool="Bash", input={"command": req.input["command"]}, cwd=run,
                                writable_roots=(run,))).decision == DENY


def test_read_confinement_only_fires_when_roots_are_declared(tmp_path):
    run, repos = tmp_path / "run", tmp_path / "repos"
    run.mkdir(), repos.mkdir()
    # Claude relies on --add-dir for read confinement and declares no readable roots: no opinion
    assert policy.check(Request(tool="Read", input={"path": "/etc/passwd"}, cwd=run,
                                writable_roots=(run,))).decision == ASK
    # a transport that DOES declare its mounts gets enforcement
    r = Request(tool="Read", input={"path": "/etc/passwd"}, cwd=run, writable_roots=(run,),
                readable_roots=(repos,))
    assert policy.check(r).decision == DENY
    ok = Request(tool="Read", input={"path": str(repos / "x/y.py")}, cwd=run, writable_roots=(run,),
                 readable_roots=(repos,))
    assert policy.check(ok).decision == ALLOW


def test_unknown_tool_gets_no_opinion(tmp_path):
    assert policy.check(Request(tool="WebFetch", input={"url": "https://x"}, cwd=tmp_path)).decision == ASK


def test_shell_tool_detected_by_command_key(tmp_path):
    """An engine that spells its shell tool differently is still contained."""
    run = tmp_path / "run"
    run.mkdir()
    r = Request(tool="run_terminal_cmd", input={"command": "curl https://x"}, cwd=run, writable_roots=(run,))
    assert policy.check(r).decision == DENY


# ---- drift pin: kernel allowlist vs the Claude engine's literal ------------------------------------

def test_neutral_allowlist_matches_the_claude_engines_literal():
    """There is ONE allowlist. The Claude engine holds its own literal because its prompt text
    (sandbox_affordances) is derived from that literal by regex; this test is what stops the two from
    drifting. If you add a command to one, add it to the other or this fails."""
    from rfc_agent.engine import _READONLY_BASH_ALLOW

    simple = {m.group(1) for e in _READONLY_BASH_ALLOW if (m := re.match(r"Bash\(([a-z]+)[:)]", e))}
    gits = {m.group(1) for e in _READONLY_BASH_ALLOW if (m := re.match(r"Bash\(git ([a-z-]+)", e))}
    assert simple == set(allowlist.READONLY_COMMANDS), (
        f"engine-only: {sorted(simple - set(allowlist.READONLY_COMMANDS))}; "
        f"kernel-only: {sorted(set(allowlist.READONLY_COMMANDS) - simple)}"
    )
    assert gits == set(allowlist.READONLY_GIT_SUBCOMMANDS)


def test_hooks_are_thin_transports_over_the_policy():
    """The point of the hook scripts must not hold a second copy of the regexes."""
    for name in ("guard_writes", "approve_readonly"):
        src = (ROOT / "src/rfc_agent/hooks" / f"{name}.py").read_text()
        assert "from harness.policy" in src, f"{name} must import the shared policy"
    guard = (ROOT / "src/rfc_agent/hooks/guard_writes.py").read_text()
    assert "_BASH_MUTATORS = re.compile" not in guard, "the mutator deny-list lives in harness.policy now"
