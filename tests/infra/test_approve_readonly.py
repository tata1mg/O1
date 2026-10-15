"""approve_readonly hook — the environment tooth from the v5 postmortem (2026-08-10).

Three runs of memory notes ("avoid piping/chaining", "never search from root") demonstrably never
changed model behavior; this hook absorbs the recurring read-only denial classes into the harness.
Cases below replay v5's real denied commands. Contract: ALLOW only what is provably read-only,
DENY only root-find (with a substitution hint), NO OPINION otherwise.
"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).parents[2] / "src" / "rfc_agent" / "hooks" / "approve_readonly.py"


def run_hook(command: str, env: dict | None = None) -> dict | None:
    """None = no opinion (defer); dict = the hook's permission decision."""
    p = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Bash", "tool_input": {"command": command}}),
        capture_output=True, text=True, timeout=10, env=env,
    )
    assert p.returncode == 0, p.stderr
    if not p.stdout.strip():
        return None
    return json.loads(p.stdout)["hookSpecificOutput"]


def decision(command: str) -> str:
    out = run_hook(command)
    return out["permissionDecision"] if out else "defer"


# ---- v5's real denied commands (from logs/activity.log) ----

def test_v5_grep_awk_pipeline_allowed():
    assert decision(
        'grep -n "^    async def \\|^    def " svc_a/app/managers/order.py | awk -F: \'$1<5900\''
    ) == "allow"


def test_v5_awk_range_scan_allowed():
    assert decision(
        "awk 'NR<=3373 && /^    async def |^    def /{name=$0; line=NR} END{print line, name}' "
        "svc_a/app/managers/order.py"
    ) == "allow"


def test_v5_for_loop_sed_context_allowed():
    assert decision(
        'for ln in 3373 5911 5913; do echo "=== around $ln ==="; '
        'sed -n "$((ln-15)),$((ln+15))p" svc_a/app/managers/order.py; done'
    ) == "allow"


def test_v5_for_loop_grep_docs_allowed():
    assert decision(
        'for p in "presigned_report_url" "CHANGE-3" "Not planned"; do grep -rn "$p" out/; done'
    ) == "allow"


def test_v5_cd_and_for_sed_allowed():
    assert decision(
        "cd /Users/x/Desktop/code && for f in svc_e/app/service_clients/svc_a_admin.py; "
        'do sed -n "140,150p" "$f"; done'
    ) == "allow"


def test_v5_find_root_denied_with_hint():
    out = run_hook('find / -maxdepth 4 -iname "state_feeder_service" -type d 2>/dev/null')
    assert out and out["permissionDecision"] == "deny"
    assert "scope it" in out["permissionDecisionReason"]


def test_v5_inline_python_defers():
    # interpreters are unauditable — the hook must stay silent, not guess
    assert decision('cd /Users/x/run && python3 -c "import json; print(1)"') == "defer"
    assert decision("python3 /private/tmp/claude-x/scratchpad/check.py") == "defer"


# ---- precision: write-shaped commands must never be allowed ----

def test_writes_and_escapes_never_allowed():
    for cmd in (
        "sed -i 's/a/b/' file.py",                             # in-place write
        "sed -n '1,5p' file.py > out.txt",                     # redirect
        "awk '{print > \"owned.txt\"}' file.py",               # awk file write
        "awk 'BEGIN{system(\"rm -rf /\")}' file.py",           # awk exec
        "find . -name '*.pyc' -delete",                        # find mutation
        "find svc_a -name '*.py' -exec rm {} \\;",            # find exec
        "grep -rn foo $(cat targets.txt)",                     # command substitution
        "git push origin main",                                # git write
        "cat file.py | tee copy.py",                           # tee (unknown head -> defer)
        "FOO=bar grep x file",                                 # env-prefix -> defer
    ):
        assert decision(cmd) != "allow", cmd


def test_git_read_subcommands_allowed_write_not():
    assert decision("cd svc_a && git log --oneline -5 | head -3") == "allow"
    assert decision("git grep -n presign | cut -d: -f1 | sort | uniq -c") == "allow"
    assert decision("git commit -m x") != "allow"


def test_scoped_find_allowed():
    assert decision("find svc_a/app -name 'order*.py'") == "allow"
    assert decision("find . -maxdepth 2 -type d") == "allow"


def test_non_bash_and_garbage_defer():
    p = subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Write", "tool_input": {"file_path": "x"}}),
        capture_output=True, text=True, timeout=10,
    )
    assert p.returncode == 0 and not p.stdout.strip()
    p = subprocess.run([sys.executable, str(HOOK)], input="not json", capture_output=True, text=True)
    assert p.returncode == 0 and not p.stdout.strip()


# ---- instructive denies inside the pipeline sandbox (PROJ-3394-figma-v2 denial census) ----
# Outcome-identical to headless auto-deny; the win is the reason string, relayed to the child as a
# tool_result (verified live 2026-08-11: the find-/ hint arrived verbatim, 1-turn recovery).

_SANDBOX = {"RFC_AGENT_RUN_DIR": "/tmp/run", "RFC_AGENT_REPOS_ROOT": "/repos"}


def test_hea3394_awk_redirect_denied_with_broker_hint():
    # the real denied shape: awk + `>` (unparseable to the segmenter — previously a silent defer)
    out = run_hook("awk -F: '{print $1}' frames.txt > starts.txt", env=_SANDBOX)
    assert out and out["permissionDecision"] == "deny"
    assert "stage_tools.py slice" in out["permissionDecisionReason"]


def test_unsafe_awk_segment_denied_with_hint():
    out = run_hook('awk \'BEGIN{system("id")}\' f.py', env=_SANDBOX)
    assert out and out["permissionDecision"] == "deny" and "slice" in out["permissionDecisionReason"]


def test_adhoc_python_denied_with_broker_hint():
    out = run_hook("python3 /private/tmp/claude-x/scratchpad/check.py", env=_SANDBOX)
    assert out and out["permissionDecision"] == "deny" and "help" in out["permissionDecisionReason"]


def test_find_deny_names_actual_repos_root():
    out = run_hook("find . -name '*.pyc' -delete", env=_SANDBOX)
    assert out and out["permissionDecision"] == "deny"
    assert "/repos" in out["permissionDecisionReason"] and "rg --files" in out["permissionDecisionReason"]


def test_pinned_broker_invocation_is_not_denied():
    # the sanctioned alternative must never be caught by the interpreter class it rides on
    cmd = f"{sys.executable} /x/src/rfc_agent/hooks/stage_tools.py slice meta.xml 100-200"
    assert run_hook(cmd, env=_SANDBOX) is None


def test_quoted_class_words_are_data_not_commands():
    # `grep 'awk' f > out` must not trip the awk class — the word sits in a quoted span
    assert run_hook("grep 'awk system' notes.md > hits.txt", env=_SANDBOX) is None


def test_never_classes_stay_silent_outside_sandbox():
    # interactive sessions (no RFC_AGENT_RUN_DIR): the original inert precision contract holds
    assert run_hook("awk -F: '{print $1}' f > s.txt") is None
    assert run_hook("python3 x.py") is None
