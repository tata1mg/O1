"""Diff-only revision protocol (0.22.59): a revision session may EDIT the deliverables but never
re-Write one whole. Revisions were measured emitting more output tokens than the composes they
revise, 60-80% of it unchanged text re-typed around a small delta."""

import json
import subprocess
import sys
from pathlib import Path

from rfc_agent.hooks.guard_writes import DIFF_ONLY_ENV

HOOK = Path(__file__).parents[2] / "src" / "rfc_agent" / "hooks" / "guard_writes.py"


def _run(tool, target, run_dir, diff_only: bool):
    env = {"RFC_AGENT_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"}
    if diff_only:
        env[DIFF_ONLY_ENV] = "1"
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": tool, "tool_input": {"file_path": str(target)}}),
        capture_output=True, text=True, timeout=10, env=env,
    )


def _run_dir(tmp_path) -> Path:
    (tmp_path / "out" / "detail").mkdir(parents=True)
    (tmp_path / "out" / "rfc.md").write_text("# RFC\n")
    (tmp_path / "out" / "detail" / "plan.md").write_text("# Plan\n")
    return tmp_path


def test_revision_write_of_an_existing_deliverable_is_denied_instructively(tmp_path):
    d = _run_dir(tmp_path)
    for target in (d / "out" / "rfc.md", d / "out" / "detail" / "plan.md"):
        res = _run("Write", target, d, diff_only=True)
        assert res.returncode == 2
        assert "targeted Edits" in res.stderr and "renumbering" in res.stderr


def test_edits_and_new_files_still_pass_while_revising(tmp_path):
    d = _run_dir(tmp_path)
    assert _run("Edit", d / "out" / "rfc.md", d, diff_only=True).returncode == 0
    assert _run("Write", d / "out" / "contract.md", d, diff_only=True).returncode == 0  # new file
    assert _run("Write", d / "findings" / "notes.md", d, diff_only=True).returncode == 0  # not a deliverable


def _run_edit(target, run_dir, old_string, diff_only: bool):
    env = {"RFC_AGENT_RUN_DIR": str(run_dir), "PATH": "/usr/bin:/bin"}
    if diff_only:
        env[DIFF_ONLY_ENV] = "1"
    return subprocess.run(
        [sys.executable, str(HOOK)],
        input=json.dumps({"tool_name": "Edit",
                          "tool_input": {"file_path": str(target), "old_string": old_string}}),
        capture_output=True, text=True, timeout=10, env=env,
    )


def test_whole_document_edit_is_denied_like_a_full_file_write(tmp_path):
    """Observed live 2026-08-18: a denied Write was rerouted as an Edit whose old_string was the
    entire document — same re-emission, different tool name. Same denial."""
    d = _run_dir(tmp_path)
    body = "# RFC\n" + "line of settled prose\n" * 50
    (d / "out" / "rfc.md").write_text(body)
    denied = _run_edit(d / "out" / "rfc.md", d, body, diff_only=True)
    assert denied.returncode == 2 and "targeted Edits" in denied.stderr
    assert _run_edit(d / "out" / "rfc.md", d, "line of settled prose\n", diff_only=True).returncode == 0
    assert _run_edit(d / "out" / "rfc.md", d, body, diff_only=False).returncode == 0  # compose: free


def test_compose_sessions_may_write_freely(tmp_path):
    """The flag is set on revision invocations only — a first compose must be able to Write."""
    d = _run_dir(tmp_path)
    assert _run("Write", d / "out" / "rfc.md", d, diff_only=False).returncode == 0


def test_containment_still_wins_over_everything(tmp_path):
    d = _run_dir(tmp_path)
    assert _run("Write", "/etc/passwd", d, diff_only=True).returncode == 2
    assert _run("Edit", "/etc/passwd", d, diff_only=False).returncode == 2


