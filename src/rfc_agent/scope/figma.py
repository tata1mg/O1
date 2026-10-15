"""Figma snapshots prepared inside scope: open the linked node in the desktop app, wait for one plugin run.

Figma does not let anything outside the app start a plugin, so the operator presses one shortcut per
link. Everything else is automated: links are parsed from the PRD, the receiver is started with the
link's node id as its export target (so the selection does not matter), the desktop app is pointed at
the node, and the terminal says what to press. Exports are cached per link so reruns can skip the click.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from .. import figma_snapshot as fs

LINK_RE = re.compile(r"https://(?:www\.)?figma\.com/(?:design|file|proto)/([A-Za-z0-9]{8,64})/[^\s)>\]]*")
_KEY_RE = re.compile(r"^[A-Za-z0-9]{8,64}$")
_NODE_RE = re.compile(r"^\d+:\d+$")
CACHE = Path("~/.cache/rfc-agent/figma").expanduser()
WAIT_S = 600


def links_in(text: str) -> list[dict]:
    """Distinct (fileKey, nodeId) pairs linked from the PRD, validated before they ever reach `open`."""
    out: OrderedDict[tuple[str, str | None], dict] = OrderedDict()
    for m in LINK_RE.finditer(text):
        url = m.group(0).rstrip(".,;")
        key = m.group(1)
        nm = re.search(r"[?&]node-id=(\d+)[-:](\d+)", url)
        node = f"{nm.group(1)}:{nm.group(2)}" if nm else None
        if not _KEY_RE.match(key) or (node and not _NODE_RE.match(node)):
            continue
        name = re.match(r"https://(?:www\.)?figma\.com/(?:design|file|proto)/[A-Za-z0-9]+/([A-Za-z0-9-]+)", url)
        out.setdefault((key, node), {"url": url, "key": key, "node": node, "name": name.group(1) if name else ""})
    return list(out.values())


def open_url(link: dict) -> str:
    name = f"/{link['name']}" if link.get("name") else ""
    node = f"?node-id={link['node'].replace(':', '-')}" if link["node"] else ""
    return f"https://www.figma.com/design/{link['key']}{name}{node}"


def slug(link: dict) -> str:
    return f"{link['key']}-{(link['node'] or 'page').replace(':', '-')}"


FIGMA_SETTINGS = Path("~/Library/Application Support/Figma/settings.json").expanduser()
PLUGIN_NAME = "rfc-agent Figma snapshot"


def registered_plugin_dirs(settings: Path | None = None) -> list[Path]:
    """Folders the Figma desktop app actually loads this plugin from. Figma reads a development plugin from
    wherever its manifest was imported (e.g. an old copy on the Desktop) — a copy outside the repo kept
    running stale code (2026-10-06)."""
    try:
        text = (settings or FIGMA_SETTINGS).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[Path] = []
    for m in re.finditer(r'"([^"]+?)/manifest\.json"', text):
        d = Path(m.group(1))
        try:
            name = json.loads((d / "manifest.json").read_text(encoding="utf-8")).get("name")
        except (OSError, ValueError):
            continue
        if name == PLUGIN_NAME and d not in out:
            out.append(d)
    return out


def ensure_plugin(settings: Path | None = None) -> tuple[bool, str, list[str]]:
    """(not_imported, manifest path to import, other copies refreshed).

    The plugin's home is a fixed folder (fs._INSTALL_DIR): an installed rfc-agent lives in a per-version
    folder that an update replaces, so an import from there would go stale. Each call refreshes the fixed
    folder from the running plugin, so Figma always runs the current code. An import from the repo (a
    developer checkout) also counts. Copies imported from anywhere else still get the current code.js /
    ui.html (their manifest id is left alone), so an old import cannot run stale code."""
    src = fs._PLUGIN_SRC.resolve()
    manifest = fs.sync_stable()
    home = manifest.parent.resolve()
    registered = registered_plugin_dirs(settings)
    synced: list[str] = []
    for d in registered:
        if d.resolve() in (src, home):
            continue
        try:
            for name in ("code.js", "ui.html"):
                shutil.copy2(src / name, d / name)
            synced.append(str(d))
        except OSError:
            pass
    imported = any(d.resolve() in (src, home) for d in registered)
    return not imported, str(manifest), synced


def cached(link: dict) -> dict | None:
    d = CACHE / slug(link)
    try:
        idx = json.loads((d / "index.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return {"dir": d, "exported_at": idx.get("exported_at"), "frames": len(idx.get("frames") or [])}


class Exporter:
    """Two-phase export over one receiver for every PRD link (fs.PlannedServer).

    Phase 1 (per link, one keypress): the plugin sends an inventory — screen names, sections, sizes, no
    images — and then waits inside Figma. Phase 2 (after the confirm screen): release() hands each plugin
    the picked frame ids and it exports only those; wait() collects them."""

    def __init__(self, links: list[dict], port: int = fs.DEFAULT_PORT):
        self.links = links
        for lk in links:
            d = CACHE / slug(lk)
            if d.exists():
                shutil.rmtree(d)
        self.srv = fs.PlannedServer({(lk["node"] or ""): (CACHE / slug(lk), lk["url"]) for lk in links}, port=port)
        self._t = threading.Thread(target=self.srv.serve_until_stopped, daemon=True)
        self._t.start()

    def sess(self, link: dict):
        return self.srv.sessions[link["node"] or ""]

    def inventory(self, link: dict, n: int, total: int, wait_s: int = WAIT_S) -> tuple[list[dict] | None, str]:
        """Open the link in the desktop app and wait for the plugin's screen list. (frames, error)."""
        if sys.platform == "darwin":
            # the link in the same form designers share it (file name included), rebuilt only from validated
            # parts — the PRD is untrusted text and this reaches the OS. Figma is brought to the FRONT (no -g):
            # in the background, the operator could switch to Figma, land on another tab and run the plugin there.
            subprocess.run(["open", "-a", "Figma", open_url(link)], check=False, capture_output=True)
        where = f"node {link['node']}" if link["node"] else "the linked page"
        msg = (f"Figma {n}/{total}: the file is open at {where}. In Figma press ⌥⌘P (Run last plugin), or "
               "Plugins → Development → rfc-agent Figma snapshot. Keep the plugin window open.")
        print(f"\n  {msg}\n  waiting up to {wait_s // 60} min… (Ctrl-C to skip this link)", file=sys.stderr)
        sess, t0 = self.sess(link), time.monotonic()
        try:
            while sess.inventory is None and time.monotonic() - t0 < wait_s:
                time.sleep(0.5)
        except KeyboardInterrupt:
            sess.skip = True
            return None, "skipped by operator"
        if sess.inventory is None:
            sess.skip = True
            return None, f"plugin did not report within {wait_s // 60} min"
        return sess.inventory, ""

    def release(self, link: dict, ids: list[str]) -> None:
        sess = self.sess(link)
        if ids:
            sess.plan = ids
        else:
            sess.skip = True

    def wait(self, timeout_s: int = 1800) -> None:
        """Wait for every released export, printing progress. Unfinished ones are written as partial."""
        live = [lk for lk in self.links if self.sess(lk).plan and not self.sess(lk).skip]
        t0, last = time.monotonic(), -1
        try:
            while any(not self.sess(lk).done for lk in live) and time.monotonic() - t0 < timeout_s:
                got = sum(len(self.sess(lk).frames) for lk in live)
                want = sum(len(self.sess(lk).plan) for lk in live)
                if got != last:
                    print(f"\r  [scope] Figma: {got}/{want} screens exported", end="", file=sys.stderr, flush=True)
                    last = got
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        print("", file=sys.stderr)
        for lk in live:
            sess = self.sess(lk)
            if not sess.done and sess.frames:
                self.srv.write_index(sess)  # partial, but usable
            if (sess.out_dir / "index.json").exists():
                (sess.out_dir / "digest.md").write_text(fs.digest(sess.out_dir), encoding="utf-8")

    def close(self) -> None:
        for lk in self.links:
            s = self.sess(lk)
            if s.plan is None:
                s.skip = True  # any plugin still waiting gets told to stop
        time.sleep(1.5)
        self.srv.stopped = True


def inventory_sections(frames: list[dict], prd_text: str) -> list[dict]:
    """Inventory grouped by Figma SECTION with a PRD-overlap pre-tick (names only — no text yet)."""
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for f in frames:
        groups.setdefault(f.get("section") or f.get("page") or "(no section)", []).append(f)
    prd = _tokens(prd_text)
    out = []
    for name, fr in groups.items():
        words = _tokens(name)
        for f in fr:
            words |= _tokens(f.get("name") or "")
        out.append({"name": name, "frames": fr, "overlap": len(words & prd)})
    for s in out:
        s["include"] = len(out) <= 3 or s["overlap"] >= 2
    return out


# ---- sections for the confirm screen + the compact index -------------------------------------------

_STOP = set("the and for with that this from your will have when then than into only also each which what "
            "their there they them been were being should would could about after before other these those "
            "user users page screen click button show shown flow".split())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z][a-z0-9]{3,}", text.lower()) if w not in _STOP}


def sections(snap: Path, prd_text: str) -> list[dict]:
    """Frames grouped by Figma SECTION, with a PRD-overlap pre-tick. [{name, frames: [entry], include}]."""
    idx = json.loads((snap / "index.json").read_text(encoding="utf-8"))
    groups: OrderedDict[str, list[dict]] = OrderedDict()
    for e in idx.get("frames") or []:
        groups.setdefault(e.get("section") or e.get("page") or "(no section)", []).append(e)
    prd = _tokens(prd_text)
    out = []
    for name, frames in groups.items():
        words = set(_tokens(name))
        for e in frames:
            words |= _tokens(e.get("name") or "")
            try:
                data = json.loads((snap / e["json"]).read_text(encoding="utf-8"))
                words |= _tokens(" ".join(fs._texts_of(e, data)[0]))
            except (OSError, ValueError, KeyError):
                pass
        overlap = len(words & prd)
        out.append({"name": name, "frames": frames, "overlap": overlap})
    for s in out:  # few sections: keep all; many: tick the ones that share real vocabulary with the PRD
        s["include"] = len(out) <= 3 or s["overlap"] >= 8
    return out


def write_index(run_dir: Path, exports: list[dict]) -> Path:
    """raw/figma_index.md — included frames only: id, name, render path, rendered text. Investigate reads it."""
    lines = ["# Figma index (scope)", ""]
    for ex in exports:
        if not ex.get("run_dir_rel"):
            continue
        lines += [f"## {ex['url']}", f"Snapshot: `{ex['run_dir_rel']}` · exported {ex.get('exported_at', '?')}", ""]
        snap = run_dir / ex["run_dir_rel"]
        for s in ex.get("sections") or []:
            if not s["include"]:
                continue
            lines += [f"### {s['name']} ({len(s['frames'])} frames)", ""]
            for e in s["frames"]:
                texts: list[str] = []
                try:
                    texts = fs._texts_of(e, json.loads((snap / e["json"]).read_text(encoding="utf-8")))[0]
                except (OSError, ValueError, KeyError):
                    pass
                render = f"{ex['run_dir_rel']}/{e['png']}" if e.get("png") else "NONE"
                lines.append(f"- **{e.get('name')}** (`{e['id']}`) — render: `{render}`")
                if texts:
                    lines.append("  - text: " + " · ".join(t.replace("\n", " ")[:80] for t in texts[:40]))
            lines.append("")
    p = run_dir / "raw" / "figma_index.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    return p


def stage_into_run(src: Path, run_dir: Path, link: dict) -> str:
    rel = Path("_inputs") / "figma" / slug(link)
    dst = run_dir / rel
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    return str(rel)


def age_text(iso: str | None) -> str:
    try:
        then = datetime.fromisoformat(iso)
        hours = (datetime.now(UTC) - then).total_seconds() / 3600
    except (TypeError, ValueError):
        return "unknown age"
    return f"{hours:.0f} h ago" if hours < 48 else f"{hours / 24:.0f} days ago"
