"""Scope's whole flow (`python -m rfc_agent.scope`) with a local PRD file and throwaway git remotes.

Nothing here touches the network or a model: no `fleetlens` is configured, so the context layer reports itself
unavailable, and the PRD summary refuses to start a real `claude` while RFC_AGENT_NO_REAL_CLAUDE is set.
"""

import json
import subprocess
from pathlib import Path

import pytest

from rfc_agent.config import Config
from rfc_agent.scope import cli as scope_cli

LINE = "When a user adds a patient whose name matches an existing one, show a nudge.\n"
PRD = "# Duplicate-patient nudge\n\n" + LINE * 4


def _sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _repos_root(tmp: Path) -> Path:
    """A checkouts root holding one repo, `svc`, cloned from a bare origin whose default branch is `main`."""
    work = tmp / "seed"
    work.mkdir()
    _sh("git", "init", "-q", "-b", "main", cwd=work)
    (work / "a.py").write_text("v1\n")
    _sh("git", "add", ".", cwd=work)
    _sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "v1", cwd=work)
    origin = tmp / "remotes" / "svc.git"
    _sh("git", "clone", "-q", "--bare", str(work), str(origin))
    root = tmp / "repos"
    root.mkdir()
    _sh("git", "clone", "-q", str(origin), str(root / "svc"))
    return root


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A config with no context layer and no Atlassian site; RUNS_ROOT inside tmp."""
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"RUNS_ROOT": str(tmp_path / "runs")}), encoding="utf-8")
    config = Config(local=path)
    monkeypatch.setattr(scope_cli, "get_config", lambda: config)
    return config


@pytest.fixture
def prd(tmp_path):
    p = tmp_path / "prd.md"
    p.write_text(PRD, encoding="utf-8")
    return p


def test_local_prd_runs_end_to_end_and_writes_every_artifact(tmp_path, cfg, prd):
    root = _repos_root(tmp_path)
    run = tmp_path / "run"
    code = scope_cli.main(["--prd", str(prd), "--repos", "svc", "--repos-root", str(root), "--run-dir", str(run),
                           "--yes", "--no-figma"])
    assert code == 0

    scope = json.loads((run / "scope.json").read_text(encoding="utf-8"))
    assert scope["context_layer"]["ok"] is False  # no fleetlens configured
    [repo] = scope["repos"]
    assert repo["repo"] == "svc" and repo["source"] == "snapshot" and repo["branch"] == "main"
    assert repo["why"] == ["named by you (--repos)"]
    assert (run / "_snap" / "svc" / "a.py").read_text() == "v1\n"

    assert json.loads((run / "findings" / "scope_approved.json").read_text()) == {"repos": ["svc"]}
    assert "Duplicate-patient nudge" in (run / "raw" / "prd.md").read_text(encoding="utf-8")
    assert (run / "raw" / "links.md").read_text().startswith("# Linked context")
    assert "total" in json.loads((run / "logs" / "scope.json").read_text())


def test_local_prd_gets_a_provenance_header(tmp_path, cfg, prd):
    root = _repos_root(tmp_path)
    run = tmp_path / "run"
    scope_cli.main(["--prd", str(prd), "--repos", "svc", "--repos-root", str(root), "--run-dir", str(run),
                    "--yes", "--no-figma"])
    head = (run / "raw" / "prd.md").read_text(encoding="utf-8").split("\n\n", 1)[0]
    assert str(prd) in head and "fetched_at" in head


def test_snapshot_is_cleaned_up_without_touching_the_run(tmp_path, cfg, prd, capsys):
    root = _repos_root(tmp_path)
    run = tmp_path / "run"
    scope_cli.main(["--prd", str(prd), "--repos", "svc", "--repos-root", str(root), "--run-dir", str(run),
                    "--yes", "--no-figma"])
    assert scope_cli.main(["cleanup", str(run)]) == 0
    assert "removed 1 snapshot" in capsys.readouterr().out
    assert not (run / "_snap" / "svc").exists()
    assert (run / "scope.json").exists()


def test_prd_that_is_neither_a_page_nor_a_file_stops(tmp_path, cfg, capsys):
    code = scope_cli.main(["--prd", str(tmp_path / "missing.md"), "--run-dir", str(tmp_path / "run"), "--yes"])
    assert code == 1
    assert "neither a Confluence page nor a file" in capsys.readouterr().err


def test_no_repos_named_and_no_context_layer_stops(tmp_path, cfg, prd, capsys):
    run = tmp_path / "run"
    code = scope_cli.main(["--prd", str(prd), "--repos-root", str(_repos_root(tmp_path)), "--run-dir", str(run),
                           "--yes", "--no-figma"])
    assert code == 1
    assert "no repos in scope" in capsys.readouterr().err
    assert not (run / "scope.json").exists()  # a stop leaves no half-written approval behind


def test_confluence_page_without_a_site_stops_before_any_connector_call(tmp_path, cfg, capsys):
    code = scope_cli.main(["--prd", "5541855972", "--run-dir", str(tmp_path / "run"), "--yes"])
    assert code == 1
    assert "ATLASSIAN.SITE is not set" in capsys.readouterr().err


def test_a_named_repo_that_is_not_on_the_remote_is_dropped_loudly(tmp_path, cfg, prd, capsys):
    root = _repos_root(tmp_path)
    code = scope_cli.main(["--prd", str(prd), "--repos", "svc,ghost", "--repos-root", str(root),
                           "--run-dir", str(tmp_path / "run"), "--yes", "--no-figma"])
    assert code == 0
    scope = json.loads((tmp_path / "run" / "scope.json").read_text(encoding="utf-8"))
    by = {r["repo"]: r for r in scope["repos"]}
    assert by["svc"]["source"] == "snapshot"
    assert by["ghost"]["source"] == "context-index-only" and "not found" in by["ghost"]["error"]


def test_run_dir_subcommand_names_the_folder_without_creating_it(tmp_path, cfg, capsys):
    assert scope_cli.main(["run-dir", "--prd", "5541855972"]) == 0
    out = capsys.readouterr().out.strip()
    assert out == str(tmp_path / "runs" / "prd-5541855972")
    assert not Path(out).exists()


def test_run_dir_prefers_the_jira_key_when_given(tmp_path, cfg, capsys):
    assert scope_cli.main(["run-dir", "--prd", "5541855972", "--jira", "PROJ-12"]) == 0
    assert capsys.readouterr().out.strip().endswith("proj-12")


def test_cleanup_needs_exactly_one_argument(capsys):
    assert scope_cli.main(["cleanup"]) == 2
    assert "usage" in capsys.readouterr().err
