"""Scope: parsing, confirm input, git snapshots against a throwaway remote, receiver targets."""

from __future__ import annotations

import json
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from rfc_agent import figma_snapshot as fs
from rfc_agent.scope import atlassian as at
from rfc_agent.scope import confirm, figma, repos

# ---- atlassian ------------------------------------------------------------------------------------


def test_page_and_jira_ids():
    url = "https://example.atlassian.net/wiki/spaces/~x/pages/5541855972/PRD+Patient"
    assert at.page_id_of(url) == "5541855972"
    assert at.page_id_of("5541855972") == "5541855972"
    assert at.page_id_of("./prd.md") is None
    assert at.jira_key_of("https://example.atlassian.net/browse/proj-3394") == "PROJ-3394"


def test_parse_page_keeps_body_byte_exact():
    body = "**Resources:**  \nFigma: <https://www.figma.com/design/AbCdEfGh12/x?node-id=1-2>\n| a | b |\n"
    call = at.ToolCall("getConfluencePage", {"pageId": "42"}, json.dumps(
        {"content": {"nodes": [{"id": "42", "title": "PRD: X", "body": body, "webUrl": "https://w/42",
                                "lastModified": "Jul 08, 2026"}]}}))
    page = at.parse_page(call)
    assert page["body"] == body and page["title"] == "PRD: X" and page["id"] == "42"


def test_parse_descendants_and_links():
    call = at.ToolCall("getConfluencePageDescendants", {}, json.dumps(
        {"results": [{"id": "1", "title": "Concept Note", "type": "page", "depth": 1},
                     {"id": "2", "title": "a folder", "type": "folder"}]}))
    assert at.parse_descendants(call) == [{"id": "1", "title": "Concept Note", "depth": 1}]
    body = "see https://x.atlassian.net/wiki/spaces/A/pages/77/Other and /wiki/spaces/A/pages/42/self PROJ-1 PROJ-1"
    assert at.linked_page_ids(body, exclude={"42"}) == ["77"]
    assert at.jira_keys(body) == ["PROJ-1"]


# ---- figma links ----------------------------------------------------------------------------------


def test_figma_links_validated_and_deduped():
    text = ("Figma: <https://www.figma.com/design/4gg0nXZyvmHZW5RQVoT2A7/HIH?node-id=18525-104044&t=x>\n"
            "again https://www.figma.com/design/4gg0nXZyvmHZW5RQVoT2A7/HIH?node-id=18525-104044\n"
            "bad https://www.figma.com/design/x;rm/evil?node-id=1-2")
    links = figma.links_in(text)
    assert [(lk["key"], lk["node"]) for lk in links] == [("4gg0nXZyvmHZW5RQVoT2A7", "18525:104044")]
    assert figma.slug(links[0]) == "4gg0nXZyvmHZW5RQVoT2A7-18525-104044"
    assert figma.open_url(links[0]) == "https://www.figma.com/design/4gg0nXZyvmHZW5RQVoT2A7/HIH?node-id=18525-104044"


# ---- confirm input --------------------------------------------------------------------------------


def _items():
    return [confirm.Item("repo", "svc_a", "svc_a", extra={"branch": "master"}),
            confirm.Item("page", "1", "Concept Note", include=False),
            confirm.Item("figma", "0:Cart", "Cart (14 frames)")]


def test_confirm_toggle_add_and_branch():
    items = _items()
    assert confirm.apply(items, "2 3", lambda n: confirm.Item("repo", n, n)) is None
    assert items[1].include and not items[2].include
    confirm.apply(items, "+lab_buddy, svc_a", lambda n: confirm.Item("repo", n, n))
    assert [it.key for it in items if it.kind == "repo"] == ["svc_a", "lab_buddy"]
    assert confirm.apply(items, "b 1=feature/x", None) is None
    assert items[0].extra["branch"] == "feature/x"
    assert confirm.apply(items, "b 3=x", None) == "3 is not a repo"
    assert confirm.apply(items, "99", None).startswith("not a valid number")


# ---- git snapshots --------------------------------------------------------------------------------


def _sh(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _make_remote(tmp: Path) -> tuple[Path, Path]:
    """bare origin with default branch `main` + an operator checkout sitting on a stale feature branch."""
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
    _sh("git", "checkout", "-qb", "test-branch", cwd=root / "svc")
    (root / "svc" / "dirty.txt").write_text("uncommitted\n")
    # origin moves on after the operator's last fetch
    (work / "a.py").write_text("v2\n")
    _sh("git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "v2", cwd=work)
    _sh("git", "push", "-q", str(origin), "main", cwd=work)
    return root, origin




def test_snapshot_reads_fresh_origin_tip_and_leaves_checkout_alone(tmp_path):
    root, _ = _make_remote(tmp_path)
    [rec] = repos.snapshot_all(["svc"], root, tmp_path / "snap", cache_root=tmp_path / "cache")
    assert rec["source"] == "snapshot" and rec["fetch"] == "ok" and rec["branch"] == "main"
    assert rec["via"] == "your checkout" and not (tmp_path / "cache").exists()  # a local repo is never cloned
    assert (tmp_path / "snap" / "svc" / "a.py").read_text() == "v2\n"  # origin's tip, not the stale checkout
    head = subprocess.run(["git", "branch", "--show-current"], cwd=root / "svc", capture_output=True, text=True)
    assert head.stdout.strip() == "test-branch" and (root / "svc" / "dirty.txt").exists()
    assert repos.cleanup(tmp_path / "snap") == 1
    wt = subprocess.run(["git", "worktree", "list"], cwd=root / "svc", capture_output=True, text=True)
    assert len(wt.stdout.strip().splitlines()) == 1  # the worktree registration is gone too


def test_missing_repo_cloned_once_into_cache_then_reused(tmp_path):
    root, origin = _make_remote(tmp_path)
    # template derived from the existing checkout's origin -> a repo that is not checked out locally
    _sh("git", "clone", "-q", "--bare", str(origin), str(origin.parent / "other.git"))
    cache = tmp_path / "cache"
    recs = repos.snapshot_all(["other", "ghost"], root, tmp_path / "snap", cache_root=cache)
    by = {r["repo"]: r for r in recs}
    assert by["other"]["source"] == "snapshot" and by["other"]["cache"] == "cloned"
    assert (tmp_path / "snap" / "other" / "a.py").exists() and (cache / "other" / ".git").exists()
    assert not (root / "other").exists()  # never cloned into the operator's workspace
    assert by["ghost"]["source"] == "context-index-only" and "not found" in by["ghost"]["error"]
    assert not any(p.name.startswith(".tmp-") for p in cache.iterdir())  # failed clone left nothing behind
    repos.cleanup(tmp_path / "snap")
    # second run: the cache is fetched, not re-cloned
    [again] = repos.snapshot_all(["other"], root, tmp_path / "snap2", cache_root=cache)
    assert again["source"] == "snapshot" and again["fetch"] == "ok" and "cache" not in again
    assert again["via"] == "repo cache"


def test_fresh_clone_ignores_the_operators_checkout_and_uses_the_cache(tmp_path):
    root, _ = _make_remote(tmp_path)
    cache = tmp_path / "cache"
    [rec] = repos.snapshot_all(["svc"], root, tmp_path / "snap", fresh_clone=True, cache_root=cache)
    assert rec["via"] == "repo cache" and rec["cache"] == "cloned" and rec["source"] == "snapshot"
    assert (cache / "svc" / ".git").exists()
    assert (tmp_path / "snap" / "svc" / "a.py").read_text() == "v2\n"
    head = subprocess.run(["git", "branch", "--show-current"], cwd=root / "svc", capture_output=True, text=True)
    assert head.stdout.strip() == "test-branch" and (root / "svc" / "dirty.txt").exists()  # checkout untouched
    wt = subprocess.run(["git", "worktree", "list"], cwd=root / "svc", capture_output=True, text=True)
    assert len(wt.stdout.strip().splitlines()) == 1  # no worktree was added to it


def test_config_path_comes_from_the_env_var(tmp_path, monkeypatch):
    from rfc_agent import config

    monkeypatch.setenv("RFC_AGENT_CONFIG", str(tmp_path / "mine.json"))
    assert config._find_local() == tmp_path / "mine.json"
    monkeypatch.delenv("RFC_AGENT_CONFIG")
    assert config._find_local().name == "config.json" and config._find_local().parent.name == "rfc-agent"


def test_probe_flags_wrong_names_before_confirm(tmp_path):
    root, origin = _make_remote(tmp_path)
    tpl = repos.remote_template(root)
    assert repos.probe(None, tpl.format(repo="svc")).remote == "ok"
    missing = repos.probe(None, tpl.format(repo="no_such_repo"))
    assert missing.remote in ("missing", "unknown")  # local file remotes word it differently from Bitbucket
    bitbucket = ("You may not have access to this repository or it no longer exists in this workspace.\n"
                 "fatal: Could not read from remote repository.")
    assert repos.classify_remote_error(bitbucket) == "missing"
    assert repos.classify_remote_error("To access this repository, an admin must whitelist your IP.") == "unreachable"
    infos = repos.probe_all(["svc", "nope"], root, cache_root=tmp_path / "cache")
    assert infos["svc"].branch == "main" and infos["svc"].remote == "ok"


def test_snapshot_falls_back_to_last_fetch_when_remote_is_down(tmp_path):
    root, origin = _make_remote(tmp_path)
    origin.rename(origin.with_suffix(".gone"))
    [rec] = repos.snapshot_all(["svc"], root, tmp_path / "snap", cache_root=tmp_path / "cache")
    assert rec["source"] == "snapshot-of-last-fetch" and rec["fetch"].startswith("failed")
    assert (tmp_path / "snap" / "svc" / "a.py").read_text() == "v1\n"


# ---- receiver -------------------------------------------------------------------------------------


def test_receiver_serves_targets_and_records_sections(tmp_path):
    srv = fs.SnapshotServer(tmp_path / "out", port=0, source_url="u", targets=["12:34", "bad;rm"])
    t = threading.Thread(target=srv.serve_until_done, daemon=True)
    t.start()
    base = f"http://127.0.0.1:{srv.port}"
    assert json.load(urllib.request.urlopen(base + "/health"))["targets"] == ["12:34"]

    def post(path, body):
        req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        return urllib.request.urlopen(req).read()

    post("/frame", {"node": {"id": "12:35", "name": "Sheet", "section": "Patient selection"},
                    "json": {"document": {"type": "FRAME", "children": []}}, "page": {"name": "P"}})
    post("/done", {"file": "F", "page": {"name": "P"}})
    t.join(timeout=5)
    idx = json.loads((tmp_path / "out" / "index.json").read_text())
    assert idx["frames"][0]["section"] == "Patient selection"
    secs = figma.sections(tmp_path / "out", "patient selection sheet")
    assert secs[0]["name"] == "Patient selection" and secs[0]["include"]


# ---- two-phase Figma export ----------------------------------------------------------------------


def test_planned_export_lists_first_then_exports_only_picked(tmp_path, monkeypatch):
    monkeypatch.setattr(figma, "CACHE", tmp_path / "cache")
    monkeypatch.setattr(figma.subprocess, "run", lambda *a, **k: None)  # never launch the real Figma app
    link = {"url": "https://www.figma.com/design/AbCdEfGh12/x?node-id=1-2", "key": "AbCdEfGh12", "node": "1:2"}
    ex = figma.Exporter([link], port=0)
    base = f"http://127.0.0.1:{ex.srv.port}"
    inventory = [{"id": "1:10", "name": "Patient sheet", "section": "Patient selection", "w": 360, "h": 800},
                 {"id": "1:11", "name": "Add patient", "section": "Patient selection", "w": 360, "h": 800},
                 {"id": "1:20", "name": "Single SKU cart", "section": "Cart", "w": 360, "h": 800}]
    exported: list[str] = []

    def plugin():  # what code.js does in plan mode
        def post(path, body):
            req = urllib.request.Request(base + path, data=json.dumps(body).encode(),
                                         headers={"Content-Type": "application/json"})
            return json.load(urllib.request.urlopen(req))

        health = json.load(urllib.request.urlopen(base + "/health"))
        assert health["plan"] and health["targets"] == ["1:2"]
        post("/inventory", {"target": "1:2", "file": "F", "frames": inventory})
        while True:
            plan = json.load(urllib.request.urlopen(base + "/plan?target=1:2"))
            if "ids" in plan or plan.get("skip"):
                break
        for fid in plan.get("ids", []):
            exported.append(fid)
            post("/frame", {"target": "1:2", "node": {"id": fid, "name": fid, "section": "Patient selection"},
                            "json": {"document": {"type": "FRAME", "children": []}}, "page": {"name": "P"}})
        post("/done", {"target": "1:2", "file": "F", "page": {"name": "P"}, "failures": 0})

    t = threading.Thread(target=plugin, daemon=True)
    t.start()
    frames, err = ex.inventory(link, 1, 1, wait_s=10)
    assert err == "" and len(frames) == 3
    secs = figma.inventory_sections(frames, "patient selection add patient")
    assert [s["name"] for s in secs] == ["Patient selection", "Cart"]
    ex.release(link, [f["id"] for s in secs if s["name"] == "Patient selection" for f in s["frames"]])
    ex.wait(timeout_s=10)
    t.join(timeout=5)
    ex.close()
    assert exported == ["1:10", "1:11"]  # the cart section was never exported
    idx = json.loads((tmp_path / "cache" / figma.slug(link) / "index.json").read_text())
    assert idx["inventory_count"] == 3 and len(idx["frames"]) == 2


def test_confirm_via_file_applies_selection(tmp_path):
    items = _items() + [confirm.Item("repo", "torpedo", "torpedo", extra={"branch": "master"})]
    answers = tmp_path / confirm.ANSWERS

    def answer():
        prop = tmp_path / confirm.PROPOSAL
        while not prop.exists():
            time.sleep(0.01)
        data = json.loads(prop.read_text())
        assert [i["n"] for i in data["items"]] == [1, 2, 3, 4] and data["items"][1]["include"] is False
        answers.write_text(json.dumps({"selected": [2, 4], "add_repos": ["lab_buddy"],
                                       "branches": {"torpedo": "feature/x"}}))

    t = threading.Thread(target=answer, daemon=True)
    t.start()
    out = confirm.via_file(items, lambda n: confirm.Item("repo", n, n, extra={}), "hdr", tmp_path, timeout_s=10)
    kept = {(it.kind, it.key) for it in out if it.include}
    assert kept == {("page", "1"), ("repo", "torpedo"), ("repo", "lab_buddy")}
    assert next(it for it in out if it.key == "torpedo").extra["branch"] == "feature/x"


def test_plugin_code_synced_into_the_folder_figma_loads(tmp_path, monkeypatch):
    home = tmp_path / ".rfc-agent" / "figma_snapshot_plugin"
    monkeypatch.setattr(fs, "_INSTALL_DIR", home)
    stale = tmp_path / "Desktop" / "rfc-agent-figma-plugin"
    stale.mkdir(parents=True)
    (stale / "manifest.json").write_text(json.dumps({"name": figma.PLUGIN_NAME, "id": "679"}))
    (stale / "code.js").write_text("// old")
    other = tmp_path / "other-plugin"
    other.mkdir()
    (other / "manifest.json").write_text(json.dumps({"name": "something else"}))
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"plugins": [f"{stale}/manifest.json", f"{other}/manifest.json"]}))
    first, manifest, synced = figma.ensure_plugin(settings)
    assert synced == [str(stale)] and first  # still imported from elsewhere -> operator is told
    assert manifest == str(home / "manifest.json")  # the fixed folder, not the per-version plugin folder
    assert "inventory" in (stale / "code.js").read_text()
    assert json.loads((stale / "manifest.json").read_text())["id"] == "679"  # the imported id is kept
    settings.write_text(json.dumps({"plugins": [f"{fs._PLUGIN_SRC}/manifest.json"]}))
    assert figma.ensure_plugin(settings) == (False, manifest, [])  # imported from the repo: nothing to do


def test_plugin_imported_from_fixed_folder_gets_current_code(tmp_path, monkeypatch):
    """An update replaces the plugin's per-version folder; the fixed folder Figma loads must follow it."""
    home = tmp_path / ".rfc-agent" / "figma_snapshot_plugin"
    monkeypatch.setattr(fs, "_INSTALL_DIR", home)
    settings = tmp_path / "settings.json"
    assert figma.ensure_plugin(settings)[0]  # nothing imported yet
    settings.write_text(json.dumps({"localFileExtensions": [{"manifestPath": f"{home}/manifest.json"}]}))
    plugin_id = json.loads((home / "manifest.json").read_text())["id"]
    (home / "code.js").write_text("// code from an older version")
    assert figma.ensure_plugin(settings) == (False, str(home / "manifest.json"), [])
    assert (home / "code.js").read_text() == (fs._PLUGIN_SRC / "code.js").read_text()
    assert json.loads((home / "manifest.json").read_text())["id"] == plugin_id  # Figma's entry stays valid


def test_connector_session_reports_claude_refusal_not_connector(tmp_path):
    """A usage-limit reply used to surface as 'connector not usable'; it must name the real cause."""
    fake = tmp_path / "claude"
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        'print(json.dumps({"type": "system", "subtype": "init", "mcp_servers": '
        '[{"name": "claude.ai Atlassian", "status": "connected"}]}), flush=True)\n'
        "for line in sys.stdin:\n"
        '    print(json.dumps({"type": "result", "subtype": "success", "is_error": True, '
        '"result": "You\'ve hit your weekly limit"}), flush=True)\n'
    )
    fake.chmod(0o755)
    import pytest

    with at.ConnectorSession(tmp_path / "s.jsonl", claude_bin=str(fake)) as s, pytest.raises(at.AtlassianError) as e:
        s.connect("https://example.atlassian.net")
    assert "weekly limit" in str(e.value) and "not a connector problem" in str(e.value)


def test_connector_session_accepts_either_atlassian_server(tmp_path):
    """The user-scope `atlassian` server counts; one connected server wins over another needing auth."""
    import pytest

    def session(servers):
        fake = tmp_path / "claude"
        fake.write_text(
            "#!/usr/bin/env python3\n"
            "import json, sys\n"
            f'print(json.dumps({{"type": "system", "subtype": "init", "mcp_servers": {servers!r}}}), flush=True)\n'
            "for line in sys.stdin:\n"
            '    print(json.dumps({"type": "result", "subtype": "success", "result": "ok"}), flush=True)\n'
        )
        fake.chmod(0o755)
        return at.ConnectorSession(tmp_path / "s.jsonl", claude_bin=str(fake))

    both = [{"name": "claude.ai Atlassian", "status": "needs-auth"}, {"name": "atlassian", "status": "connected"}]
    for servers in (both, both[::-1], [{"name": "atlassian", "status": "connected"}]):
        with session(servers) as s:
            s._turn("ok")
            assert s.connector_status == "connected"
    with session([{"name": "atlassian", "status": "needs-auth"}]) as s, pytest.raises(at.AtlassianError) as e:
        s.connect("https://example.atlassian.net")
    assert "no Atlassian MCP server is connected" in str(e.value)


def test_added_repo_uses_the_spelling_bitbucket_has_and_drops_are_reported(tmp_path, monkeypatch, capsys):
    from rfc_agent.scope import cli as scope_cli

    on_bitbucket = {"merch_service"}
    monkeypatch.setattr(scope_cli.repos, "probe_all", lambda names, root: {
        n: repos.RemoteInfo("master", "remote HEAD" if n in on_bitbucket else "guessed",
                            "ok" if n in on_bitbucket else "missing") for n in names})
    name, info = scope_cli._resolve_added_repo("merch-service", tmp_path)
    assert (name, info.remote) == ("merch_service", "ok") and "using 'merch_service'" in capsys.readouterr().err
    assert scope_cli._resolve_added_repo("nope-repo", tmp_path)[0] == "nope-repo"  # neither spelling exists
    items = [confirm.Item("repo", "nope-repo", "nope-repo", include=False,
                          extra={"remote": "missing", "why": ["added by you"]}),
             confirm.Item("repo", "svc", "svc", include=False, extra={"remote": "missing", "why": ["context layer"]})]
    assert scope_cli._dropped_added_repos(items) == ["nope-repo"]


def _flaky_claude(tmp_path, works_from: int, status: str = "pending"):
    """A fake headless claude: process n (counted in a file) gets the Atlassian tools only from `works_from` on,
    as when Atlassian's server is too slow to list them on a new connection."""
    fake, count = tmp_path / "claude", tmp_path / "starts"
    count.write_text("0")
    sites = json.dumps([{"id": "cloud-1", "url": "https://example.atlassian.net"}])
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"c = pathlib.Path({str(count)!r}); n = int(c.read_text()) + 1; c.write_text(str(n))\n"
        f"ok = n >= {works_from}\n"
        f'print(json.dumps({{"type": "system", "subtype": "init", "mcp_servers": '
        f'[{{"name": "atlassian", "status": "connected" if ok else {status!r}}}]}}), flush=True)\n'
        "for line in sys.stdin:\n"
        "    if ok and 'getAccessibleAtlassianResources' in line:\n"
        '        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", '
        '"name": "mcp__atlassian__getAccessibleAtlassianResources", "input": {}}]}}), flush=True)\n'
        '        print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", '
        f'"tool_use_id": "t1", "content": {sites!r}}}]}}}}), flush=True)\n'
        '    print(json.dumps({"type": "result", "subtype": "success", "result": "done"}), flush=True)\n'
    )
    fake.chmod(0o755)
    return fake, count


def test_slow_atlassian_gets_a_fresh_connection(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "PENDING_WAIT_S", 0)
    import contextlib

    fake, count = _flaky_claude(tmp_path, works_from=2)
    said = []
    with contextlib.ExitStack() as stack:
        s, _ = at.open_session(stack, tmp_path / "s.jsonl", "https://example.atlassian.net", claude_bin=str(fake),
                               say=said.append, wait_s=0)
        assert s.cloud_id == "cloud-1" and s.proc.poll() is None  # the working session stays open for fetches
    assert count.read_text() == "2" and "try 1 of 3" in said[0]


def test_slow_atlassian_gives_up_after_three_connections(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "PENDING_WAIT_S", 0)
    import contextlib

    import pytest

    fake, count = _flaky_claude(tmp_path, works_from=99)
    with contextlib.ExitStack() as stack, pytest.raises(at.AtlassianError) as e:
        at.open_session(stack, tmp_path / "s.jsonl", "https://example.atlassian.net", claude_bin=str(fake), wait_s=0)
    assert count.read_text() == "3"
    assert "tried 3 fresh connections" in str(e.value) and "rerun in a minute" in str(e.value)


def test_missing_sign_in_is_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(at, "PENDING_WAIT_S", 0)
    import contextlib

    import pytest

    fake, count = _flaky_claude(tmp_path, works_from=99, status="needs-auth")
    with contextlib.ExitStack() as stack, pytest.raises(at.AtlassianError) as e:
        at.open_session(stack, tmp_path / "s.jsonl", "https://example.atlassian.net", claude_bin=str(fake), wait_s=0)
    assert count.read_text() == "1" and "no Atlassian MCP server is connected" in str(e.value)


def test_a_pending_connector_is_waited_for_on_the_same_connection(tmp_path, monkeypatch):
    """The common case: the server connects 10-30 s in, after the warm-up turn. No fresh connection needed."""
    import contextlib

    monkeypatch.setattr(at, "PENDING_POLL_S", 0)
    fake, count = tmp_path / "claude", tmp_path / "starts"
    count.write_text("0")
    sites = json.dumps([{"id": "cloud-1", "url": "https://example.atlassian.net"}])
    fake.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys, pathlib\n"
        f"c = pathlib.Path({str(count)!r}); c.write_text(str(int(c.read_text()) + 1))\n"
        "turn = 0\n"
        "for line in sys.stdin:\n"
        "    turn += 1\n"
        "    ok = turn >= 3\n"
        '    print(json.dumps({"type": "system", "subtype": "init", "mcp_servers": '
        '[{"name": "atlassian", "status": "connected" if ok else "pending"}]}), flush=True)\n'
        "    if ok and 'getAccessibleAtlassianResources' in line:\n"
        '        print(json.dumps({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "t1", '
        '"name": "mcp__atlassian__getAccessibleAtlassianResources", "input": {}}]}}), flush=True)\n'
        '        print(json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", '
        f'"tool_use_id": "t1", "content": {sites!r}}}]}}}}), flush=True)\n'
        '    print(json.dumps({"type": "result", "subtype": "success", "result": "ok", "total_cost_usd": 0.001}), '
        "flush=True)\n"
    )
    fake.chmod(0o755)
    with contextlib.ExitStack() as stack:
        s, cost = at.open_session(stack, tmp_path / "s.jsonl", "https://example.atlassian.net", claude_bin=str(fake),
                                  wait_s=0)
        assert s.cloud_id == "cloud-1" and s.connector_status == "connected"
    assert count.read_text() == "1", "one connection: it waited instead of starting over"
    assert cost == 0.004  # two pending checks, the turn that saw it connected, and the call
