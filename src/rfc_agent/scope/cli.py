"""Scope: gather every input and settle every human decision before any model does real work.

    python -m rfc_agent.scope --prd <confluence url | page id | file> [--jira KEY] [--repos a,b] [--yes]
    python -m rfc_agent.scope cleanup <run_dir>      # remove the run's repo snapshots (git worktrees)

Repos you have not cloned (e.g. ones the context layer names) are shallow-cloned once into the repo cache
(~/.cache/rfc-agent/repos, override with RFC_AGENT_REPO_CACHE) and only fetched on later runs.

Steps (timed, written to logs/scope.json):
  1. context layer (fleetlens) health check (parallel with 2)
  2. PRD + child-page list + Jira via the claude.ai Atlassian connector (byte-exact, Haiku, no rewriting)
  3. Figma: per PRD link, open the node in the desktop app and wait for one plugin run (or reuse a cache)
     — scope candidates are computed meanwhile (a fleetlens discovery agent + a Haiku PRD summary)
  4. ONE confirm screen: repos (+branch), extra pages, Jira, Figma sections
  5. in parallel: repo snapshots pinned to the default branch, and the ticked pages/Jira
  6. scope.json + findings/scope_approved.json + raw/figma_index.md
"""

from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from .. import observe
from ..config import get_config
from ..legacy_names import migrate_run_dir
from . import atlassian as at
from . import confirm, context, figma, repos


def _say(msg: str) -> None:
    print(f"  [scope] {msg}", file=sys.stderr, flush=True)


def _resolve_added_repo(name: str, repos_root: Path) -> tuple[str, repos.RemoteInfo]:
    """A repo the user typed: if Bitbucket lacks it, try the other -/_ spelling (merch-service -> merch_service)."""
    info = repos.probe_all([name], repos_root)[name]
    alt = name.replace("-", "_") if "-" in name else name.replace("_", "-")
    if info.remote != "missing" or alt == name:
        return name, info
    alt_info = repos.probe_all([alt], repos_root)[alt]
    if alt_info.remote != "ok":
        return name, info
    _say(f"'{name}' is not on Bitbucket — using '{alt}'")
    return alt, alt_info


def _dropped_added_repos(items: list) -> list[str]:
    """Repos the user added that were left out because Bitbucket does not have them."""
    return [it.key for it in items if it.kind == "repo" and not it.include
            and it.extra.get("remote") == "missing" and it.extra.get("why") == ["added by you"]]


class _Timer:
    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.steps: dict[str, float] = {}

    @contextlib.contextmanager
    def step(self, name: str):
        t = time.monotonic()
        try:
            yield
        finally:
            self.steps[name] = round(time.monotonic() - t, 1)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60] or "page"


def _run_dir(args, cfg) -> Path:
    if args.run_dir:
        return Path(args.run_dir).expanduser()
    root = Path(args.out or cfg.get("RUNS_ROOT") or "~/rfc-runs").expanduser()
    key = at.jira_key_of(args.jira) if args.jira else None
    pid = at.page_id_of(args.prd)
    name = key.lower() if key else (f"prd-{pid}" if pid else _slugify(Path(args.prd).stem))
    return root / name


def _write_page(path: Path, page: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(at.provenance(page["url"] or f"confluence page {page['id']}", page["title"], _now(),
                                  {"page_id": page["id"], "last_modified": page["last_modified"]})
                    + page["body"], encoding="utf-8")


def _write_jira(path: Path, key: str, call: at.ToolCall) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(at.provenance(f"jira {key}", key, _now()) + call.text, encoding="utf-8")


def _blob_images(text: str) -> int:
    return len(re.findall(r"!\[[^\]]*\]\(blob:", text))


@observe.entry("scope")
def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["run-dir"]:  # where a run for these args lands — used by /rfc-agent:scope before it starts
        rp = argparse.ArgumentParser(prog="python -m rfc_agent.scope run-dir")
        rp.add_argument("--prd", required=True)
        rp.add_argument("--jira")
        rp.add_argument("--run-dir")
        rp.add_argument("--out")
        print(_run_dir(rp.parse_known_args(argv[1:])[0], get_config()))  # ignores other stages' options
        return 0
    if argv[:1] == ["cleanup"]:
        if len(argv) != 2:
            print("usage: python -m rfc_agent.scope cleanup <run_dir>", file=sys.stderr)
            return 2
        n = repos.cleanup(Path(argv[1]).expanduser() / "_snap")
        print(f"removed {n} snapshot(s)")
        return 0

    p = argparse.ArgumentParser(prog="python -m rfc_agent.scope", description="Scope: prepare a run's inputs.")
    p.add_argument("--prd", required=True, help="Confluence url, page id, or a local markdown file")
    p.add_argument("--jira", help="Jira key or url")
    p.add_argument("--repos", default="", help="repos you already know are in scope, comma list")
    p.add_argument("--repos-root", help="local checkouts root (default: config REPOS_ROOT)")
    p.add_argument("--run-dir", help="run directory (default: RUNS_ROOT/<jira-key or prd-id>)")
    p.add_argument("--out", help="parent for the auto-named run dir (default: config RUNS_ROOT)")
    p.add_argument("--yes", action="store_true", help="accept the proposal without the confirm screen")
    p.add_argument("--confirm-file", action="store_true",
                   help="confirm through files instead of the terminal: write confirm/proposal.json and wait for "
                        "confirm/answers.json (what /rfc-agent:scope uses; automatic when there is no terminal)")
    p.add_argument("--no-figma", action="store_true", help="skip Figma entirely")
    p.add_argument("--reuse-figma", choices=("ask", "always", "never"), default="ask",
                   help="reuse a cached export of the same link (default: ask)")
    p.add_argument("--figma-wait", type=int, default=figma.WAIT_S, help="seconds to wait per Figma export")
    p.add_argument("--fresh-clone", action="store_true",
                   help="never touch your checkouts: take every repo from the repo cache "
                        "(~/.cache/rfc-agent/repos, shallow-cloned once, fetched on later runs)")
    p.add_argument("--model", default=at.DEFAULT_MODEL, help="model that makes the connector calls")
    from ..axon_agent import add_engine_args, apply_engine_args

    add_engine_args(p)
    args = p.parse_args(argv)
    apply_engine_args(args.engine, args.stage_engine)

    cfg = get_config()
    timer = _Timer()
    run_dir = _run_dir(args, cfg)
    migrate_run_dir(run_dir)
    for d in ("raw", "logs", "findings"):
        (run_dir / d).mkdir(parents=True, exist_ok=True)
    repos_root = Path(args.repos_root or cfg.get("REPOS_ROOT") or "~/Documents").expanduser()
    named = [r.strip() for r in args.repos.split(",") if r.strip()]
    interactive = not args.yes
    _say(f"run dir: {run_dir}")

    # 1 ∥ 2 — context layer (fleetlens) health while the connector warms up
    spec = context.spec_of(cfg)
    layer: dict = {}

    def _health() -> None:
        layer["ok"], layer["why"] = context.health(spec)

    t_layer = threading.Thread(target=_health, daemon=True)
    t_layer.start()

    scope: dict = {"prepared_at": _now(), "prd": args.prd, "jira": args.jira, "repos_root": str(repos_root)}
    cost = 0.0
    with contextlib.ExitStack() as stack:
        session: at.ConnectorSession | None = None
        pid = at.page_id_of(args.prd)
        jira_key = at.jira_key_of(args.jira) if args.jira else None
        descendants: list[dict] = []

        if pid or jira_key:
            with timer.step("atlassian_connect"):
                _say("starting the Atlassian connector session …")
                site = cfg.get("ATLASSIAN.SITE")
                if not site:
                    _say("STOP: ATLASSIAN.SITE is not set — add your team's site to ~/.config/rfc-agent/config.json "
                         "(or run /rfc-agent:setup)")
                    return 1
                try:
                    session, paid = at.open_session(stack, run_dir / "logs" / "scope.atlassian.stream.jsonl", site,
                                                    claude_bin=cfg.get("AGENT.CLAUDE_BIN", "claude"),
                                                    model=args.model, cwd=run_dir, say=_say)
                    cost += paid
                except at.AtlassianError as e:
                    _say(f"STOP: {e}")
                    return 1
            with timer.step("atlassian_fetch"):
                res = session.fetch(pages=[pid] if pid else [], page_descendants=[pid] if pid else [],
                                    jira=[jira_key] if jira_key else [])
                cost += res.cost_usd
                for c in res.calls:
                    if c.is_error:
                        _say(f"connector error on {c.name} {json.dumps(c.input)}: {c.text[:200]}")
                    elif c.name == "getConfluencePage" and str(c.input.get("pageId")) == pid:
                        page = at.parse_page(c)
                        if page:
                            _write_page(run_dir / "raw" / "prd.md", page)
                            scope["prd_title"] = page["title"]
                    elif c.name == "getConfluencePageDescendants":
                        descendants = at.parse_descendants(c)
                    elif c.name == "getJiraIssue" and jira_key:
                        _write_jira(run_dir / "raw" / "jira.md", jira_key, c)
        if not pid:
            src = Path(args.prd).expanduser()
            if not src.is_file():
                _say(f"STOP: --prd {args.prd} is neither a Confluence page nor a file")
                return 1
            (run_dir / "raw" / "prd.md").write_text(
                at.provenance(str(src), src.stem, _now()) + src.read_text(encoding="utf-8"), encoding="utf-8")

        prd_path = run_dir / "raw" / "prd.md"
        if not prd_path.exists() or len(prd_path.read_text(encoding="utf-8")) < 200:
            _say("STOP: the PRD came back empty — see logs/scope.atlassian.stream.jsonl")
            return 1
        prd_text = prd_path.read_text(encoding="utf-8")
        _say(f"PRD: {scope.get('prd_title') or prd_path.name} ({len(prd_text):,} chars)"
             + (f", Jira {jira_key}" if jira_key else ""))
        blobs = _blob_images(prd_text)
        if blobs:
            _say(f"WARN: {blobs} embedded image(s) in the PRD cannot be fetched through the connector")
        scope["prd_images_not_fetched"] = blobs

        linked_ids = at.linked_page_ids(prd_text, exclude={pid or ""} | {d["id"] for d in descendants})
        body_jira = [k for k in at.jira_keys(prd_text) if k != jira_key]
        figma_links = [] if args.no_figma else figma.links_in(prd_text)

        # 3 — figma exports (operator keypress) while scope candidates are computed in the background
        t_layer.join()
        if not layer.get("ok"):
            _say(f"context layer unavailable: {layer.get('why')}"
                 + ("" if named else " — add repos on the confirm screen (`+name`)"))
        cands: dict = {}

        def _gather() -> None:
            cands["meta"] = {}
            cands["list"] = context.gather(cfg, spec, bool(layer.get("ok")), prd_text, repos_root, named,
                                           meta=cands["meta"], log_dir=run_dir / "logs")
            cands["remote"] = repos.probe_all([c["repo"] for c in cands["list"]], repos_root)

        t_cands = threading.Thread(target=_gather, daemon=True)
        t0_cands = time.monotonic()
        t_cands.start()

        exports: list[dict] = []
        exporter: figma.Exporter | None = None
        if figma_links:
            with timer.step("figma_inventory"):
                not_imported, manifest, synced = figma.ensure_plugin()
                if synced:
                    _say(f"Figma plugin: refreshed the copy Figma loads ({', '.join(synced)}) with the repo's code")
                if not_imported:
                    _say("Figma plugin: import it once from its fixed folder, so it stays current across updates — "
                         "Figma → Plugins → Development → Import plugin from manifest… → " + manifest
                         + (" (then remove the old copy under Manage plugins in development)" if synced else ""))
                todo = []
                for n, link in enumerate(figma_links, 1):
                    ex = dict(link)
                    hit = figma.cached(link) if args.reuse_figma != "never" else None
                    reuse = False
                    if hit:
                        if args.reuse_figma == "always" or args.yes or args.confirm_file or not sys.stdin.isatty():
                            reuse = True
                        elif sys.stdin.isatty() and not args.confirm_file:
                            a = input(f"  Figma {n}/{len(figma_links)}: export of this link from "
                                      f"{figma.age_text(hit['exported_at'])} ({hit['frames']} screens) — reuse? [Y/n] ")
                            reuse = a.strip().lower() in ("", "y", "yes")
                    if reuse:
                        ex.update(mode="reused", run_dir_rel=figma.stage_into_run(Path(hit["dir"]), run_dir, link),
                                  exported_at=hit["exported_at"])
                        ex["sections"] = figma.sections(run_dir / ex["run_dir_rel"], prd_text)
                    else:
                        todo.append((n, link, ex))
                    exports.append(ex)
                if todo:
                    try:
                        exporter = stack.enter_context(contextlib.closing(figma.Exporter([lk for _, lk, _ in todo])))
                    except OSError as e:
                        for _, _, ex in todo:
                            ex.update(mode="failed", error=f"port 8790 busy ({e}) — is another export running?")
                        todo = []
                    for n, link, ex in todo:
                        frames, err = exporter.inventory(link, n, len(figma_links), args.figma_wait)
                        if frames is None:
                            ex.update(mode="failed", error=err)
                        else:
                            ex.update(mode="new", inventory=len(frames),
                                      sections=figma.inventory_sections(frames, prd_text))
                for n, ex in enumerate(exports, 1):
                    if ex.get("sections"):
                        count = sum(len(sec["frames"]) for sec in ex["sections"])
                        tag = " (reused export)" if ex["mode"] == "reused" else ""
                        _say(f"Figma {n}/{len(exports)}: {count} screens in {len(ex['sections'])} section(s){tag}")
                    else:
                        _say(f"Figma {n}/{len(exports)}: {ex.get('error')}")
        t_cands.join()
        timer.steps["scope_candidates_bg"] = round(time.monotonic() - t0_cands, 1)
        disc = cands.get("meta") or {}
        cost += disc.get("cost_usd") or 0
        if disc.get("discovery_error"):
            _say(f"context layer: repo discovery failed ({disc['discovery_error']}) — see "
                 "logs/scope.discovery.stream.jsonl; add repos on the confirm screen (`+name`)")
        elif disc.get("discovery"):
            d = disc["discovery"]
            _say(f"context layer: {len(d['services'])} service(s) from {d['tool_calls']} fleetlens lookups "
                 f"({d['seconds']:.0f}s, ${d['cost_usd']:.2f})")
        if disc.get("summary_error"):
            _say(f"PRD summary failed ({disc['summary_error']}) — no surfaces detected")
        if disc.get("unmapped_surfaces"):
            _say(f"the PRD changes {', '.join(disc['unmapped_surfaces'])} but the surface table names no repo for it "
                 "— add the repo on the confirm screen (`+name`), and to config SCOPE.SURFACES for next time")

        # 4 — the one confirm screen
        def repo_item(name: str, why: list[str] | None = None, info: repos.RemoteInfo | None = None) -> confirm.Item:
            if info is None:
                name, info = _resolve_added_repo(name, repos_root) if why is None else \
                    (name, repos.probe_all([name], repos_root)[name])
            local = (repos_root / name / ".git").exists()
            where = "local checkout" if local else ("in repo cache" if repos.is_cached(name) else "not cloned")
            flag, include = "", True
            if info.remote == "missing":
                # a context-layer service that maps to a name Bitbucket does not have: fix it now, not later
                flag = "⚠ NOT FOUND on Bitbucket (wrong name, or no access) — add the right name with +name · "
                include = local
            elif info.remote in ("unreachable", "unknown") and not local and not repos.is_cached(name):
                flag = "⚠ couldn't reach Bitbucket to check it (VPN?) · "
            return confirm.Item("repo", name, name, include=include,
                                note=f"{flag}{info.branch} · {where} · " + "; ".join(why or ["added by you"]),
                                extra={"branch": info.branch, "branch_from": info.how, "remote": info.remote,
                                       "why": why or ["added by you"]})

        items = [repo_item(c["repo"], c["why"], cands["remote"].get(c["repo"])) for c in cands["list"]]
        items += [confirm.Item("page", d["id"], d["title"], include=False) for d in descendants]
        items += [confirm.Item("page", i, f"linked page {i}", include=False) for i in linked_ids]
        items += [confirm.Item("jira", k, k, include=False) for k in body_jira]
        for xi, ex in enumerate(exports):
            for s in ex.get("sections") or []:
                items.append(confirm.Item("figma", f"{xi}:{s['name']}", f"{s['name']} ({len(s['frames'])} screens)",
                                          note=f"PRD overlap {s['overlap']}", include=s["include"]))
        header = (f"── Scope · confirm ── PRD: {scope.get('prd_title') or prd_path.name}\n"
                  f"  context layer: {'ok' if layer.get('ok') else 'UNAVAILABLE — ' + str(layer.get('why'))}"
                  + (f"\n  surfaces: {', '.join(disc['surfaces'])}" if disc.get("surfaces") else ""))
        with timer.step("confirm_wait"):
            if interactive and (args.confirm_file or not sys.stdin.isatty()):
                items = confirm.via_file(items, repo_item, header, run_dir)
            elif interactive:
                items = confirm.ask(items, repo_item, header)
            else:
                print(f"\n{header}{confirm.render(items)}\n", file=sys.stderr)
        for name in _dropped_added_repos(items):  # never silent: file mode has no second screen to show it
            _say(f"WARNING: '{name}' is NOT on Bitbucket (wrong name, or no access) — dropped. "
                 "Rerun scope with the exact repo name.")
        observe.operator("confirm", {"accepted_defaults": not interactive,
                                     "selected": [f"{it.kind}:{it.key}" for it in items if it.include],
                                     "not_selected": [f"{it.kind}:{it.key}" for it in items if not it.include]})
        chosen = [it for it in items if it.kind == "repo" and it.include]
        if not chosen:
            _say("STOP: no repos in scope (context layer down and none named) — pass --repos")
            return 1
        for it in items:
            if it.kind == "figma":
                xi, name = it.key.split(":", 1)
                for s in exports[int(xi)]["sections"]:
                    if s["name"] == name:
                        s["include"] = it.include

        # 5 — snapshots ∥ extra pages ∥ Figma export of the picked sections
        for ex in exports:
            if ex.get("mode") == "new":
                exporter.release(ex, [f["id"] for sec in ex["sections"] if sec["include"] for f in sec["frames"]])
        with timer.step("snapshots_pages_figma"), ThreadPoolExecutor(max_workers=3) as pool:
            _say(f"snapshotting {len(chosen)} repo(s) …")
            fut_snap = pool.submit(repos.snapshot_all, [it.key for it in chosen], repos_root, run_dir / "_snap",
                                   branches={it.key: (it.extra["branch"], it.extra["branch_from"]) for it in chosen},
                                   fresh_clone=args.fresh_clone)
            fut_fig = pool.submit(exporter.wait) if exporter else None
            want_pages = [it for it in items if it.kind == "page" and it.include]
            want_jira = [it.key for it in items if it.kind == "jira" and it.include]
            fetched_pages: list[dict] = []
            if (want_pages or want_jira) and session:
                res = session.fetch(pages=[it.key for it in want_pages], jira=want_jira)
                cost += res.cost_usd
                for c in res.calls:
                    if c.is_error:
                        _say(f"connector error on {c.name}: {c.text[:200]}")
                    elif c.name == "getConfluencePage":
                        page = at.parse_page(c)
                        if page:
                            _write_page(run_dir / "raw" / "linked" / f"{page['id']}-{_slugify(page['title'])}.md", page)
                            fetched_pages.append({"id": page["id"], "title": page["title"]})
                    elif c.name == "getJiraIssue":
                        k = str(c.input.get("issueIdOrKey"))
                        _write_jira(run_dir / "raw" / "linked" / f"jira-{k}.md", k, c)
            snaps = fut_snap.result()
            if fut_fig:
                fut_fig.result()
        for ex in exports:
            if ex.get("mode") != "new":
                continue
            sess = exporter.sess(ex)
            if (sess.out_dir / "index.json").exists():
                ex["run_dir_rel"] = figma.stage_into_run(sess.out_dir, run_dir, ex)
                idx = json.loads((sess.out_dir / "index.json").read_text(encoding="utf-8"))
                ex["exported_at"] = idx.get("exported_at")
                picked = {sec["name"] for sec in ex["sections"] if sec["include"]}
                ex["sections"] = figma.sections(run_dir / ex["run_dir_rel"], prd_text)
                for sec in ex["sections"]:
                    sec["include"] = sec["name"] in picked or not picked
            elif any(sec["include"] for sec in ex["sections"]):
                ex["error"] = "export did not finish"

    # 6 — artifacts
    for it, rec in zip(chosen, snaps, strict=True):
        rec["why"] = it.extra.get("why")
    figma_out = []
    for ex in exports:
        figma_out.append({k: v for k, v in ex.items() if k in ("url", "key", "node", "mode", "inventory", "error",
                                                                "run_dir_rel", "exported_at")}
                         | {"sections": [{"name": s["name"], "frames": len(s["frames"]), "include": s["include"]}
                                         for s in ex.get("sections") or []]})
    if any(ex.get("run_dir_rel") for ex in exports):
        figma.write_index(run_dir, exports)
    links_md = ["# Linked context", "", "## Child pages"] + [f"- {d['title']} — page id {d['id']}" for d in descendants]
    links_md += ["", "## Linked pages"] + [f"- page id {i}" for i in linked_ids]
    links_md += ["", "## Referenced Jira"] + [f"- {k}" for k in body_jira]
    links_md += ["", "## Figma links in PRD"] + [f"- {lk['url']}" for lk in figma.links_in(prd_text)]
    (run_dir / "raw" / "links.md").write_text("\n".join(links_md) + "\n", encoding="utf-8")

    scope.update({
        "context_layer": {"ok": bool(layer.get("ok")), "why": layer.get("why")},
        "repo_discovery": disc,
        "repos": snaps,
        "pages_included": fetched_pages,
        "jira_included": want_jira,
        "figma": figma_out,
        "connector_cost_usd": round(cost, 4),
        "timings_s": timer.steps | {"total": round(time.monotonic() - timer.t0, 1)},
    })
    (run_dir / "scope.json").write_text(json.dumps(scope, indent=2), encoding="utf-8")
    (run_dir / "findings" / "scope_approved.json").write_text(
        json.dumps({"repos": [r["repo"] for r in snaps]}, indent=2), encoding="utf-8")
    (run_dir / "logs" / "scope.json").write_text(json.dumps(scope["timings_s"], indent=2), encoding="utf-8")

    _summary(run_dir, scope)
    return 0


def _summary(run_dir: Path, scope: dict) -> None:
    print("\n── Scope done ─────────────────────────────", file=sys.stderr)
    for r in scope["repos"]:
        when = (r.get("commit_date") or "")[:10]
        extra = f" · fetch {r['fetch']}" if r.get("fetch", "ok") != "ok" else ""
        err = f" · {r['error']}" if r.get("error") and r["source"] == "context-index-only" else ""
        via = f" · via {r['via']}" + (" (new clone)" if r.get("cache") == "cloned" else "") if r.get("via") else ""
        print(f"  {r['repo']:<28} {r['source']:<24} {r.get('branch', '?')}@{(r.get('sha') or '')[:8]} "
              f"{when}{via}{extra}{err}", file=sys.stderr)
    for f in scope["figma"]:
        inc = sum(s["frames"] for s in f["sections"] if s["include"])
        listed = f.get("inventory") or sum(s["frames"] for s in f["sections"])
        print(f"  figma {f.get('node') or 'page'}: {inc} screens exported of {listed} found"
              + (f" · {f['error']}" if f.get("error") else ""), file=sys.stderr)
    if scope["pages_included"]:
        print(f"  extra pages: {', '.join(p['title'] for p in scope['pages_included'])}", file=sys.stderr)
    t = scope["timings_s"]
    print(f"  time: {t['total']}s total ({', '.join(f'{k} {v}s' for k, v in t.items() if k != 'total')})"
          f" · connector ${scope['connector_cost_usd']}", file=sys.stderr)
    print(f"  → {run_dir / 'scope.json'}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
