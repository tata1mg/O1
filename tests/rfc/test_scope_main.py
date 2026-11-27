"""Scope's whole flow (`python -m rfc_agent.scope`) with a local PRD file and throwaway git remotes.

Nothing here touches the network or a model: no `fleetlens` is configured, so the context layer reports itself
unavailable and the discovery agent never starts.
"""

import json
import re
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


def test_prd_without_a_design_is_flagged_and_yes_goes_on(tmp_path, cfg, prd, capsys):
    root = _repos_root(tmp_path)
    run = tmp_path / "run"
    assert scope_cli.main(["--prd", str(prd), "--repos", "svc", "--repos-root", str(root), "--run-dir", str(run),
                           "--yes"]) == 0
    assert "going on without a design" in capsys.readouterr().err
    assert json.loads((run / "scope.json").read_text())["no_design"] == "the PRD links no Figma design"


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


def test_run_dir_subcommand_names_a_new_timestamped_folder_without_creating_it(tmp_path, cfg, capsys):
    assert scope_cli.main(["run-dir", "--prd", "5541855972"]) == 0
    out = capsys.readouterr().out.strip()
    assert Path(out).parent == tmp_path / "runs"
    assert re.fullmatch(r"prd-5541855972-\d{8}-\d{6}", Path(out).name)
    assert not Path(out).exists()
    # one process, one folder: tracing, the whole-run entry and Scope must all land in the same place
    assert scope_cli.main(["run-dir", "--prd", "5541855972"]) == 0
    assert capsys.readouterr().out.strip() == out


def test_run_dir_never_reuses_a_folder_that_exists(tmp_path, cfg, capsys):
    assert scope_cli.main(["run-dir", "--prd", "777"]) == 0
    first = Path(capsys.readouterr().out.strip())
    scope_cli._FRESH.clear()  # a new process
    first.mkdir(parents=True)  # the earlier run is on disk, and a second run starts in the same second
    assert scope_cli.main(["run-dir", "--prd", "777"]) == 0
    second = Path(capsys.readouterr().out.strip())
    assert second != first and second.name.startswith("prd-777-")


def test_run_dir_subcommand_echoes_the_folder_to_continue(tmp_path, cfg, capsys):
    assert scope_cli.main(["run-dir", "--run-dir", str(tmp_path / "old-run")]) == 0
    assert capsys.readouterr().out.strip() == str(tmp_path / "old-run")
    assert scope_cli.main(["run-dir"]) == 2  # neither a PRD nor a folder: nothing to name


def test_run_dir_prefers_the_jira_key_when_given(tmp_path, cfg, capsys):
    assert scope_cli.main(["run-dir", "--prd", "5541855972", "--jira", "PROJ-12"]) == 0
    assert Path(capsys.readouterr().out.strip()).name.startswith("proj-12-")


def test_cleanup_needs_exactly_one_argument(capsys):
    assert scope_cli.main(["cleanup"]) == 2
    assert "usage" in capsys.readouterr().err


# ---- instructions, --figma, the design question and what the trace records ----------------------------------


def _run_scope(tmp_path, prd, *extra):
    root = _repos_root(tmp_path)
    run = tmp_path / "run"
    code = scope_cli.main(["--prd", str(prd), "--repos", "svc", "--repos-root", str(root), "--run-dir", str(run),
                           *extra])
    return code, run


def test_instructions_go_on_top_of_the_prd_and_the_source_is_kept(tmp_path, cfg, prd, monkeypatch):
    seen = []
    monkeypatch.setattr(scope_cli.observe, "operator", lambda action, data=None: seen.append((action, data)))
    code, run = _run_scope(tmp_path, prd, "--yes", "--no-figma", "--instructions", "mWeb only. No email form.")
    assert code == 0
    assert (run / "raw" / "instructions.md").read_text().strip() == "mWeb only. No email form."
    assert PRD in (run / "raw" / "prd.source.md").read_text()
    assert "Run instructions" not in (run / "raw" / "prd.source.md").read_text()  # the source is the PRD untouched
    effective = (run / "raw" / "prd.md").read_text()
    assert effective.startswith("---\nsource_url:") and "## Run instructions from the operator" in effective
    assert effective.index("mWeb only") < effective.index("Duplicate-patient nudge")  # instructions first, PRD after
    [(action, data)] = [x for x in seen if x[0] == "run_input"]
    assert data["instructions"] == "mWeb only. No email form." and data["input_mode"] == "flags"
    assert data["figma"]["source"].startswith("skipped")


def test_instructions_can_come_from_a_file(tmp_path, cfg, prd):
    f = tmp_path / "overrides.md"
    f.write_text("Share a PDF through the native share sheet.\n")
    code, run = _run_scope(tmp_path, prd, "--yes", "--no-figma", "--instructions", str(f))
    assert code == 0
    assert "native share sheet" in (run / "raw" / "prd.md").read_text()


def test_no_instructions_leaves_the_prd_alone(tmp_path, cfg, prd):
    code, run = _run_scope(tmp_path, prd, "--yes", "--no-figma")
    assert code == 0
    assert not (run / "raw" / "instructions.md").exists() and not (run / "raw" / "prd.source.md").exists()
    assert "Run instructions" not in (run / "raw" / "prd.md").read_text()


def test_a_figma_value_that_is_not_a_figma_link_stops(tmp_path, cfg, prd, capsys):
    code, _ = _run_scope(tmp_path, prd, "--yes", "--figma", "https://example.com/not-figma")
    assert code == 1 and "not a Figma design link" in capsys.readouterr().err


def test_a_prompt_run_records_the_prompt_clipped_to_300_words(tmp_path, cfg, prd, monkeypatch):
    seen = []
    monkeypatch.setattr(scope_cli.observe, "operator", lambda action, data=None: seen.append((action, data)))
    long_prompt = tmp_path / "prompt.md"
    long_prompt.write_text("Build an rfc " + " ".join(f"w{i}" for i in range(400)))
    code, run = _run_scope(tmp_path, prd, "--yes", "--no-figma", "--input-mode", "prompt",
                           "--prompt-text", str(long_prompt))
    assert code == 0
    data = dict(seen)["run_input"]
    assert data["input_mode"] == "prompt"
    assert "w296" in data["prompt"] and "w297" not in data["prompt"]  # 3 words + w0…w296
    assert (run / "raw" / "prompt.md").read_text().startswith("Build an rfc")


def test_no_figma_anywhere_asks_the_design_question_before_figma_runs(tmp_path, cfg, prd, monkeypatch):
    """No --figma, none in the instructions, none in the PRD: ask first; proceeding skips the late question."""
    asked = {}

    def fake_ask(reason, run_dir=None, use_file=False, **_):
        asked["reason"] = reason
        return None

    def fake_via_file(items, add_repo, header, run_dir, timeout_s=3600, no_design=None):
        asked["late_no_design"] = no_design
        return items

    monkeypatch.setattr(scope_cli.confirm, "ask_design", fake_ask)
    monkeypatch.setattr(scope_cli.confirm, "via_file", fake_via_file)
    code, run = _run_scope(tmp_path, prd, "--confirm-file")
    assert code == 0
    assert "no Figma design" in asked["reason"] or "links no Figma" in asked["reason"]
    assert asked["late_no_design"] is None  # decided once: not asked again at the confirm screen
    assert json.loads((run / "scope.json").read_text())["figma_source"].startswith("none")


def test_a_link_added_at_the_design_question_is_used_and_recorded(tmp_path, cfg, prd, monkeypatch):
    link = "https://www.figma.com/design/uwvYvW3cBm2DNKXgfIUXC5?node-id=0-1"
    monkeypatch.setattr(scope_cli.confirm, "ask_design", lambda *a, **k: link)
    monkeypatch.setattr(scope_cli.figma, "ensure_plugin", lambda: (False, "m", []))

    class NoPort:  # the exporter cannot start: the link is still used and its failure is reported
        def __init__(self, *a, **k):
            raise OSError("busy")

    monkeypatch.setattr(scope_cli.figma, "Exporter", NoPort)
    monkeypatch.setattr(scope_cli.confirm, "via_file", lambda items, add_repo, header, run_dir, **k: items)
    code, run = _run_scope(tmp_path, prd, "--confirm-file")
    assert code == 0
    scope = json.loads((run / "scope.json").read_text())
    assert scope["figma_source"] == "added at the prompt"
    assert scope["figma"][0]["key"] == "uwvYvW3cBm2DNKXgfIUXC5" and scope["figma"][0]["mode"] == "failed"
    assert link in (run / "raw" / "links.md").read_text()


def test_added_repos_are_reported_one_by_one(tmp_path, cfg, prd, capsys):
    code, _ = _run_scope(tmp_path, prd, "--yes", "--no-figma")
    assert code == 0  # `svc` was named with --repos, so it is not an "added by you" repo: no "added" line
    assert "added repo" not in capsys.readouterr().err
