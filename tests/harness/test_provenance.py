"""Every run records the O1 commit it ran on, and says when the checkout had uncommitted code."""

from __future__ import annotations

import subprocess
from pathlib import Path

from harness.provenance import code_version, label


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def _repo(tmp_path: Path) -> Path:
    root = tmp_path / "o1"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.py").write_text("x = 1\n")
    _git(root, "init", "-q", "-b", "main")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    _git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    return root


def test_a_clean_checkout(tmp_path):
    root = _repo(tmp_path)
    (root / "Design notes.md").write_text("untracked notes outside src/ cannot change a run")
    v = code_version(root)
    assert len(v["commit"]) == 40 and v["branch"] == "main" and v["dirty"] is False and v["changed"] == []
    assert label(v) == f"O1 {v['commit'][:10]} (main)"


def test_uncommitted_code_makes_the_run_dirty(tmp_path):
    root = _repo(tmp_path)
    (root / "src" / "a.py").write_text("x = 2\n")
    (root / "src" / "new.py").write_text("")
    v = code_version(root)
    assert v["dirty"] is True and sorted(v["changed"]) == ["src/a.py", "src/new.py"]
    assert label(v).endswith("+ 2 uncommitted change(s)")


def test_outside_git(tmp_path):
    v = code_version(tmp_path)
    assert v["commit"] is None and label(v) == "O1 (not a git checkout)"
