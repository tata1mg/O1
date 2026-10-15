#!/usr/bin/env python3
"""Figma SNAPSHOT channel: the operator exports designs once with a private plugin; INGEST reads files.

Why this exists (research 2026-09-02): the official Figma MCP is the ingest bottleneck — measured
median 6.9 min / $2.42 with a design vs 1.5 min / $0.85 without — because the agent serializes
30-60 tool calls under a 15/min, 200/day quota, and `get_design_context` returns component text
instead of the instance overrides actually rendered (forum #50438). The Plugin API has no quota and
`exportAsync({format: "JSON_REST_V1"})` yields the exact REST /nodes shape from the live document
(instance `characters` included); plugins simply cannot auto-run (Figma staff, forum 39156), so the
one human click per design revision is the price. Figma-side alternatives evaluated and rejected:
bridge daemons (attended, iframe killed every 1-3 min), CDP (Figma closed it in 126.1.2).

Three subcommands, all stdlib:
  serve <out_dir> [--port 8790] [--url <figma url>]   receiver the plugin POSTs to; exits on /done
  digest <snapshot_dir>                                 writes digest.md (frame inventory + rendered text)
  install                                               copies the plugin to ~/.rfc-agent/... (import it once)

The CLI accepts the snapshot directory as `--figma <dir>`; `stage_snapshot` copies it under the run
and `snapshot_dir(ctx)` is the one predicate the engine and INGEST consult.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import secrets
import shutil
import sys
import urllib.parse
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

DEFAULT_PORT = 8790
_PLUGIN_SRC = Path(__file__).resolve().parents[2] / "tools" / "figma_snapshot_plugin"
_INSTALL_DIR = Path("~/.rfc-agent/figma_snapshot_plugin").expanduser()
_NODE_RE = re.compile(r"^I?\d+:\d+(?:;\d+:\d+)*$")  # 12:34, instance children I12:34;56:78
_TEXT_CAP = 400  # rendered-text lines per frame before the digest defers to the json


def _fname(node_id: str) -> str:
    return node_id.replace(":", "-").replace(";", "_")


# ---- receiver ------------------------------------------------------------------------------------

class SnapshotServer(HTTPServer):
    """Localhost receiver. Plugin iframes/sandboxes carry a null origin, so every answer is CORS-open;
    the only writable paths are <out_dir>/<validated node id>.{json,png} and index.json."""

    def __init__(self, out_dir: Path, *, port: int = DEFAULT_PORT, source_url: str | None,
                 targets: list[str] | None = None):
        super().__init__(("127.0.0.1", port), _Handler)
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.source_url = source_url
        # node ids the plugin should export (the PRD link's node-id). Served on /health so the export
        # matches the link even when the operator's selection or current page is somewhere else.
        self.targets = [t for t in (targets or []) if _NODE_RE.match(t)]
        self.frames: list[dict] = []
        self.meta: dict = {}
        self.done = False
        self.timeout = 0.5

    @property
    def port(self) -> int:
        return self.server_address[1]

    def serve_until_done(self) -> None:
        try:
            while not self.done:
                self.handle_request()
        finally:
            self.server_close()

    def write_index(self) -> Path:
        idx = {
            "source_url": self.source_url,
            "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "file": self.meta.get("file"),
            "page": self.meta.get("page"),
            "frames": self.frames,
        }
        p = self.out_dir / "index.json"
        p.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
        return p


class _Handler(BaseHTTPRequestHandler):
    server: SnapshotServer

    def log_message(self, *_):  # quiet — progress is printed by the handlers below
        pass

    def _reply(self, status: int, body: dict | None = None) -> None:
        payload = json.dumps(body or {}).encode()
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        if status != 204:
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        if status != 204:
            self.wfile.write(payload)

    def do_OPTIONS(self):
        self._reply(204)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, {"ok": True, "targets": self.server.targets})
        return self._reply(404, {"ok": False})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except (ValueError, TypeError):
            return self._reply(400, {"error": "bad json"})
        if self.path == "/frame":
            return self._frame(body)
        if self.path == "/done":
            self.server.meta = {"file": body.get("file"), "page": body.get("page")}
            p = self.server.write_index()
            print(f"figma_snapshot: {len(self.server.frames)} frame(s) -> {p}", file=sys.stderr)
            self._reply(200, {"frames": len(self.server.frames)})
            self.server.done = True
            return None
        return self._reply(404, {"error": "unknown path"})

    def _frame(self, body: dict) -> None:
        entry = _store_frame(self.server.out_dir, body)
        if entry is None:
            return self._reply(400, {"error": "invalid node id"})
        self.server.frames.append(entry)
        print(f"figma_snapshot: [{len(self.server.frames)}] {entry['name']} ({entry['id']})", file=sys.stderr)
        return self._reply(200, {"ok": True})


# ---- planned receiver (rfc-agent scope) -------------------------------------------------------------

class _Session:
    def __init__(self, out_dir: Path, source_url: str | None):
        self.out_dir = Path(out_dir)
        self.source_url = source_url
        self.inventory: list[dict] | None = None   # what the plugin found (no images) — phase 1
        self.file: str | None = None
        self.plan: list[str] | None = None          # node ids the operator picked — phase 2
        self.skip = False
        self.frames: list[dict] = []
        self.meta: dict = {}
        self.done = False


class PlannedServer(HTTPServer):
    """Two-phase receiver for the scope stage. The plugin first POSTs an /inventory (names, sections, sizes),
    then polls /plan until the operator has picked sections in the terminal, then exports only those.
    Several PRD links are served at once, one session per target node id ('' = a link without node-id)."""

    def __init__(self, sessions: dict[str, tuple[Path, str | None]], *, port: int = DEFAULT_PORT):
        super().__init__(("127.0.0.1", port), _PlannedHandler)
        self.sessions = {k: _Session(d, u) for k, (d, u) in sessions.items() if k == "" or _NODE_RE.match(k)}
        self.timeout = 0.5
        self.stopped = False
        self.warned_stale = False
        self.connected = False

    @property
    def port(self) -> int:
        return self.server_address[1]

    def serve_until_stopped(self) -> None:
        try:
            while not self.stopped:
                self.handle_request()
        finally:
            self.server_close()

    def session(self, target) -> _Session | None:
        t = str(target or "")
        if t in self.sessions:
            return self.sessions[t]
        # no/unknown target = the plugin ran where the linked node is not (it then uses the operator's
        # selection). With a single link there is no ambiguity about which link that selection is for.
        if len(self.sessions) == 1:
            return next(iter(self.sessions.values()))
        return None

    def write_index(self, sess: _Session) -> Path:
        sess.out_dir.mkdir(parents=True, exist_ok=True)
        idx = {"source_url": sess.source_url, "exported_at": datetime.now(UTC).isoformat(timespec="seconds"),
               "file": sess.meta.get("file") or sess.file, "page": sess.meta.get("page"),
               "inventory_count": len(sess.inventory or []), "frames": sess.frames}
        p = sess.out_dir / "index.json"
        p.write_text(json.dumps(idx, indent=2, ensure_ascii=False), encoding="utf-8")
        return p


class _PlannedHandler(_Handler):
    server: PlannedServer

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/health":
            if not self.server.connected:
                self.server.connected = True
                print("figma_snapshot: plugin connected — it is now finding the screens", file=sys.stderr, flush=True)
            return self._reply(200, {"ok": True, "plan": True,
                                     "targets": [t for t in self.server.sessions if t]})
        if path == "/plan":
            target = urllib.parse.parse_qs(query).get("target", [""])[0]
            sess = self.server.session(target)
            if sess is None or sess.skip:
                return self._reply(200, {"skip": True})
            return self._reply(200, {"ids": sess.plan} if sess.plan is not None else {"wait": True})
        return self._reply(404, {"ok": False})

    def do_POST(self):
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        except (ValueError, TypeError):
            return self._reply(400, {"error": "bad json"})
        if self.path == "/log":
            print(f"figma_snapshot: {str(body.get('text'))[:300]}", file=sys.stderr, flush=True)
            return self._reply(200, {"ok": True})
        if "target" not in body and not self.server.warned_stale:
            # a plugin window from an earlier run (pre-plan code) is still exporting into this receiver
            self.server.warned_stale = True
            print("figma_snapshot: an OLD rfc-agent plugin window is still exporting from an earlier run — close "
                  "it in Figma (✕ on the plugin panel), then press ⌥⌘P again", file=sys.stderr, flush=True)
        sess = self.server.session(body.get("target")) if "target" in body else None
        if sess is None:
            return self._reply(404, {"error": "unknown target — close this plugin window and run it again"})
        if self.path == "/inventory":
            frames = [f for f in body.get("frames") or [] if _NODE_RE.match(str(f.get("id") or ""))]
            if body.get("fallback"):
                print(f"figma_snapshot: the linked node is not in the open file ({body.get('file')!r}) — using the "
                      "operator's selection in that file instead", file=sys.stderr, flush=True)
            sess.inventory, sess.file = frames, body.get("file")
            sess.meta["page"] = body.get("page")
            print(f"figma_snapshot: inventory {len(frames)} screen(s) for {body.get('target') or 'page'}",
                  file=sys.stderr)
            return self._reply(200, {"ok": True})
        if self.path == "/frame":
            sess.out_dir.mkdir(parents=True, exist_ok=True)
            entry = _store_frame(sess.out_dir, body)
            if entry is None:
                return self._reply(400, {"error": "invalid node id"})
            sess.frames.append(entry)
            return self._reply(200, {"ok": True})
        if self.path == "/done":
            sess.meta.update(file=body.get("file"), page=body.get("page"), failures=body.get("failures"))
            self.server.write_index(sess)
            sess.done = True
            return self._reply(200, {"frames": len(sess.frames)})
        return self._reply(404, {"error": "unknown path"})


def _store_frame(out: Path, body: dict) -> dict | None:
    node = body.get("node") or {}
    nid = str(node.get("id") or "")
    if not _NODE_RE.match(nid):
        return None
    base = _fname(nid)
    (out / f"{base}.json").write_text(json.dumps(body.get("json") or {}, ensure_ascii=False), encoding="utf-8")
    png = None
    if body.get("png_b64"):
        try:
            (out / f"{base}.png").write_bytes(base64.b64decode(body["png_b64"]))
            png = f"{base}.png"
        except (ValueError, TypeError):
            png = None
    return {"id": nid, "name": node.get("name"), "type": node.get("type"), "w": node.get("w"), "h": node.get("h"),
            "scale": body.get("scale"), "page": (body.get("page") or {}).get("name"), "section": node.get("section"),
            "json": f"{base}.json", "png": png}


# ---- digest --------------------------------------------------------------------------------------

def _walk(node: dict, texts: list, instances: list, frames: list, depth: int) -> None:
    if node.get("visible") is False:
        return  # hidden subtree: never rendered, never transcribed
    t = node.get("type")
    box = node.get("absoluteBoundingBox") or {}
    if t == "TEXT" and (node.get("characters") or "").strip():
        texts.append((box.get("y", 0), box.get("x", 0), node["characters"].strip()))
    elif t == "INSTANCE":
        props = {k.split("#", 1)[0]: str(v.get("value")) for k, v in (node.get("componentProperties") or {}).items()
                 if isinstance(v, dict) and v.get("type") in ("VARIANT", "TEXT", "BOOLEAN")}
        instances.append((node.get("name") or "?", props))
    elif t in ("FRAME", "GROUP", "SECTION", "COMPONENT", "COMPONENT_SET") and 0 < depth <= 2:
        frames.append((depth, node.get("name") or "?", t, box))
    for child in node.get("children") or []:
        _walk(child, texts, instances, frames, depth + 1)


def _texts_of(entry: dict, data: dict) -> tuple[list[str], frozenset]:
    """(rendered strings in reading order, instance-variant signature) — the duplicate identity.
    Text alone is NOT enough: a frame whose only difference is a component variant with no text
    change (checkbox State=checked vs unchecked) must never collapse as 'identical'."""
    doc = data.get("document") or data
    texts: list = []
    instances: list = []
    _walk(doc, texts, instances, [], 0)
    texts.sort(key=lambda r: (round(r[0] / 8), r[1]))
    out, prev = [], None
    for _, _, s in texts:
        if s != prev:
            out.append(s)
        prev = s
    sig: dict = {}
    for name, props in instances:
        key = (name, tuple(sorted(props.items())))
        sig[key] = sig.get(key, 0) + 1
    return out, frozenset(sig.items())


def _opaque(node: dict) -> bool:
    if node.get("visible") is False or float(node.get("opacity", 1) or 1) < 0.9:
        return False
    return any(f.get("type") == "SOLID" and f.get("visible", True)
               and float((f.get("color") or {}).get("a", 1)) >= 0.9 and float(f.get("opacity", 1) or 1) >= 0.9
               for f in node.get("fills") or [])


def _visibility(doc: dict) -> dict[str, str]:
    """text -> '' | 'behind sheet' | 'off-viewport'. The export's `characters` include text under an
    opaque sheet layered on top and text scrolled outside the frame — the visual audit (2026-09-04,
    73 renders) traced the phantom screens and the cart/sheet confusion to exactly those strings being
    presented as rendered. Z-order = child order; a top-level child with an opaque solid fill
    occludes every earlier sibling's text whose centre it covers (scrims are translucent and don't)."""
    box = doc.get("absoluteBoundingBox") or {}
    fx, fy, fw, fh = box.get("x", 0), box.get("y", 0), box.get("width", 0), box.get("height", 0)
    tops = doc.get("children") or []
    occluders = []
    for i, t in enumerate(tops):
        b = t.get("absoluteBoundingBox") or {}
        if _opaque(t) and b.get("width") and b.get("height"):
            occluders.append((i, b["x"], b["y"], b["x"] + b["width"], b["y"] + b["height"]))
    flags: dict[str, str] = {}

    def visit(n: dict, top: int) -> None:
        if n.get("visible") is False:
            return
        if n.get("type") == "TEXT" and (n.get("characters") or "").strip():
            b = n.get("absoluteBoundingBox") or {}
            cx, cy = b.get("x", 0) + b.get("width", 0) / 2, b.get("y", 0) + b.get("height", 0) / 2
            text = n["characters"].strip()
            if fw and fh and not (fx <= cx <= fx + fw and fy <= cy <= fy + fh):
                flags[text] = flags.get(text) or "off-viewport"
            elif any(i > top and x0 <= cx <= x1 and y0 <= cy <= y1 for i, x0, y0, x1, y1 in occluders):
                flags[text] = flags.get(text) or "behind sheet"
            else:
                flags[text] = ""  # visible anywhere wins over hidden elsewhere
        for c in n.get("children") or []:
            visit(c, top)

    for i, t in enumerate(tops):
        visit(t, i)
    return flags


def _frame_section(entry: dict, data: dict) -> list[str]:
    doc = data.get("document") or data
    texts: list = []
    instances: list = []
    frames: list = []
    _walk(doc, texts, instances, frames, 0)
    vis = _visibility(doc)
    size = f"{round(entry.get('w') or 0)}×{round(entry.get('h') or 0)}"
    out = [f"## {entry.get('name')} (node {entry['id']}, {entry.get('type')}, {size})"
           + (f" — render: {entry['png']}" if entry.get("png") else " — render: NONE (png export failed)")]
    if frames:
        out.append("Structure (child frames, depth ≤ 2): " + "; ".join(
            f"{'  ' * (d - 1)}{n} [{t}]" for d, n, t, _ in frames[:60]) + (" …" if len(frames) > 60 else ""))
    # reading order: row bands (8px) top→bottom, then left→right; consecutive duplicates collapse
    texts.sort(key=lambda r: (round(r[0] / 8), r[1]))
    seen_prev = None
    lines = []
    hidden = 0
    for _, _, s in texts:
        if s != seen_prev:
            flag = vis.get(s, "")
            hidden += bool(flag)
            lines.append(f'- s{len(lines) + 1} "{s}"' + (f"  [{flag}]" if flag else ""))
        seen_prev = s
    out.append(f"Rendered text, reading order ({len(lines)} strings, numbered s1..s{len(lines)}; "
               f"{hidden} tagged [behind sheet]/[off-viewport] are NOT visible in the render — they belong to "
               f"the layer underneath or scrolled away; live `characters`, instance overrides included):")
    out += lines[:_TEXT_CAP]
    if len(lines) > _TEXT_CAP:
        out.append(f"- … +{len(lines) - _TEXT_CAP} more — see {entry['json']}")
    if instances:
        counts: dict[str, int] = {}
        for name, props in instances:
            key = f"`{name}`" + (" {" + ", ".join(f"{k}={v}" for k, v in props.items()) + "}" if props else "")
            counts[key] = counts.get(key, 0) + 1
        out.append("Component instances (name {properties} ×count): " + "; ".join(
            f"{k} ×{n}" if n > 1 else k for k, n in list(counts.items())[:80]))
    out.append("")
    return out


def _cluster_variants(rep_entry: dict, rep_texts: list[str], rep_sig: frozenset, variants: list) -> list[str]:
    """Group a section's variant frames into STATE CLUSTERS by the strings they add over the section
    frame, so the model reads ~10 pre-made states instead of ~70 diff lines.

    Why (benchmark v3, PROJ-3394, 2026-09-04): the designer named 73 screens "Single SKU cart", so
    (name, size) grouping produced 2 sections + 71 variant lines, and the model spent ~4.5 of its 10
    minutes re-clustering those lines into screens and assigning every node id by hand — structure
    the harness must compute. Greedy clustering on added-string overlap (Jaccard >= 0.5 against the
    cluster's shared set), deterministic in export order; frames whose text is unchanged (component
    variants only / reordered) form one layout-only cluster. Citing ANY member id in the annotations
    covers the whole cluster (figma_render), so the model never writes id lists."""
    # ponytail: O(n^2) greedy over <=~100 variants per section; revisit only if exports grow 10x
    clusters: list[dict] = []
    for e, t, sg in variants:
        added = [s for s in t if s not in rep_texts]
        removed = [s for s in rep_texts if s not in t]
        aset = set(added)
        home = None
        if aset:
            best = 0.0
            for c in clusters:
                if not c["shared"]:
                    continue
                j = len(aset & c["shared"]) / len(aset | c["shared"])
                if j >= 0.5 and j > best:
                    best, home = j, c
        else:
            home = next((c for c in clusters if c["layout_only"]), None)
        if home is None:
            home = {"shared": set(aset), "layout_only": not aset, "members": []}
            clusters.append(home)
        else:
            home["shared"] &= aset
        changed = sorted({k[0] for k, _ in sg.symmetric_difference(rep_sig)}) if sg != rep_sig else []
        home["members"].append((e, added, removed, changed))
    out = [f"State clusters of {rep_entry['id']} — {len(variants)} variant frame(s) in {len(clusters)} cluster(s). "
           f"A cluster = frames adding the same strings over the section frame (a sheet, a toast, an empty "
           f"state). Cite ANY member id as a screen's `node` to cover the whole cluster; the harness never "
           f"needs the other ids."]
    for n, c in enumerate(clusters, start=1):
        first = c["members"][0][0]
        out.append(f"### C{n} (×{len(c['members'])}) — first {first['id']} (render {first.get('png') or 'NONE'})")
        # reading order of the first member, each string once (a list row repeats "Female, 26 | Edit")
        shared_order = list(dict.fromkeys(s for s in c["members"][0][1] if s in c["shared"]))
        if c["layout_only"]:
            out.append("  shared: no text change vs the section frame — layout/component-variant differences only")
        else:
            out.append("  shared: " + "; ".join(f'+"{s}"' for s in shared_order[:20])
                       + (f" … +{len(shared_order) - 20} more" if len(shared_order) > 20 else ""))
        out.append("  frames: " + ", ".join(e["id"] for e, *_ in c["members"]))
        for e, added, removed, changed in c["members"]:
            extra = [s for s in added if s not in c["shared"]]
            bits = [f'+"{s}"' for s in extra[:6]] + [f'-"{s}"' for s in removed[:4]]
            if changed:
                bits.append("component variants differ: " + ", ".join(changed[:6]))
            if bits:
                out.append(f"  {e['id']} also: " + "; ".join(bits)
                           + (f" … +{len(extra) - 6} more" if len(extra) > 6 else ""))
    out.append("")
    return out


# ---- canvas layout: the designer's rows are the flow ------------------------------------------------

def _frame_geometry(snapshot: Path, idx: dict) -> dict[str, dict]:
    """id -> {x, y, w, h, kind: 'screen'|'band'|'caption'|'deco', caption} from each frame's exported JSON."""
    geo: dict[str, dict] = {}
    for f in idx.get("frames") or []:
        try:
            d = json.loads((snapshot / f["json"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            continue
        doc = d.get("document") or d
        b = doc.get("absoluteBoundingBox") or {}
        texts: list = []
        _walk(doc, texts, [], [], 0)
        strings = list(dict.fromkeys(t for _, _, t in texts))
        w, h = b.get("width", 0) or 0, b.get("height", 0) or 0
        if doc.get("type") == "TEXT":
            kind = "caption"
        elif doc.get("type") in ("LINE", "VECTOR", "ELLIPSE", "RECTANGLE", "STAR", "POLYGON"):
            kind = "deco"
        elif w >= 900 and h <= 200 and len(strings) <= 2:
            kind = "band"  # the wide, short header frames the designer stretches over a row of screens
        elif len(strings) <= 1:
            kind = "deco"
        else:
            kind = "screen"
        geo[f["id"]] = {"x": b.get("x", 0), "y": b.get("y", 0), "w": w, "h": h, "kind": kind,
                        "caption": strings[0] if strings else (doc.get("name") or ""), "name": f.get("name")}
    return geo


def canvas_bands(snapshot: Path) -> list[dict]:
    """The designer's rows: each wide header band with the screen frames beneath it (nearest band above
    each frame), frames in reading order (row by y, then x). Verified on PROJ-3394 against the live file
    (2026-09-04): 11 bands cover all 73 screens; the prototype has ZERO screen-to-screen links, so this
    layout IS the flow. Returns [{id, label, y, frames:[ids], rows:[[ids]]}]; a trailing '(no band)'
    entry collects frames above the first band, if any."""
    snapshot = Path(snapshot)
    idx = json.loads((snapshot / "index.json").read_text(encoding="utf-8"))
    geo = _frame_geometry(snapshot, idx)
    bands = sorted(((g["y"], fid) for fid, g in geo.items() if g["kind"] == "band"))
    screens = sorted(((g["y"], g["x"], fid) for fid, g in geo.items() if g["kind"] == "screen"))
    if not bands:
        return []
    import bisect
    band_ys = [y for y, _ in bands]
    buckets: dict[str, list] = {fid: [] for _, fid in bands}
    stray: list = []
    for y, x, fid in screens:
        i = bisect.bisect_right(band_ys, y) - 1
        (buckets[bands[i][1]] if i >= 0 else stray).append((y, x, fid))
    out = []
    for y, bid in bands:
        members = sorted(buckets[bid])
        if not members:
            continue
        rows: list[list[str]] = []
        for fy, _fx, fid in members:  # a new row when y jumps by more than a frame's height
            if rows and abs(fy - geo[rows[-1][0]]["y"]) <= 100:
                rows[-1].append(fid)
            else:
                rows.append([fid])
        out.append({"id": bid, "label": geo[bid]["caption"] or geo[bid]["name"] or bid, "y": y,
                    "frames": [f for r in rows for f in r], "rows": rows})
    if stray:
        out.append({"id": "none", "label": "(no band above)", "y": -1, "frames": [f for _, _, f in stray],
                    "rows": [[f for _, _, f in stray]]})
    return out


def prototype_edges(snapshot: Path) -> list[tuple[str, str, str, str]]:
    """(from frame, trigger node name, navigation, to frame) for prototype actions whose destination is
    another exported top-level frame — the only interactions that encode screen-to-screen flow."""
    snapshot = Path(snapshot)
    idx = json.loads((snapshot / "index.json").read_text(encoding="utf-8"))
    ids = {f["id"] for f in idx.get("frames") or []}
    edges = []
    for f in idx.get("frames") or []:
        try:
            d = json.loads((snapshot / f["json"]).read_text(encoding="utf-8"))
        except (OSError, ValueError, KeyError):
            continue

        def visit(n: dict, src: str) -> None:
            for it in n.get("interactions") or []:
                for a in it.get("actions") or []:
                    dest = a.get("destinationId")
                    if dest and dest in ids and dest != src:
                        edges.append((src, n.get("name") or "?", a.get("navigation") or a.get("type"), dest))
            for c in n.get("children") or []:
                visit(c, src)
        visit(d.get("document") or d, f["id"])
    return edges


def flow_map(snapshot: Path) -> list[str]:
    """Digest section: the canvas rows (designer's flow) + any real prototype navigation between frames."""
    bands = canvas_bands(snapshot)
    if not bands:
        return []
    idx = json.loads((Path(snapshot) / "index.json").read_text(encoding="utf-8"))
    name = {f["id"]: f.get("name") for f in idx.get("frames") or []}
    out = ["## Canvas flow — the designer's rows (read each row left→right; this IS the screen flow)", ""]
    for b in bands:
        out.append(f"- **{b['label']}** ({len(b['frames'])} frames)")
        for r in b["rows"]:
            out.append("  - " + " → ".join(f"{fid} ({name.get(fid, '?')})" for fid in r))
    edges = prototype_edges(snapshot)
    if edges:
        out.append("")
        out.append("Prototype links between exported frames:")
        out += [f"- {a} --[{nav} on '{trig}']--> {b}" for a, trig, nav, b in edges[:60]]
    else:
        out.append("")
        out.append("Prototype links between exported frames: none — the file wires no screen-to-screen "
                   "navigation; component swaps / scroll-to / destination-less overlays only.")
    out.append("")
    return out


def digest(snapshot: Path) -> str:
    """One full section per DISTINCT frame; duplicates collapse deterministically.

    Why (measured 2026-09-02, PROJ-3394): the export carries every frame — 145 for a section the MCP
    protocol sampled at 12 — and a digest with 145 full sections cost ingest 12.1 min / $4.70, WORSE
    than the MCP path it replaces. Collapse rules, in line with the skill's dedupe doctrine:
    - canvas annotations (TEXT/LINE/shape nodes exported alongside screens) go into one captions list
      — the caption text is the scenario-band signal, the nodes are not screens;
    - frames with the same (name, size) AND the same rendered strings are LAYOUT duplicates: one
      representative section + an ids line;
    - same (name, size) but different strings is a BEHAVIORAL variant (toast, expanded list, empty
      state — the thing name+size dedupe must never hide): kept, but as a diff (+/- strings) against
      the group's representative instead of a full re-listing."""
    snapshot = Path(snapshot)
    idx_path = snapshot / "index.json"
    if not idx_path.exists():
        raise FileNotFoundError(
            f"{idx_path} missing — not a figma_snapshot export (run the plugin with the receiver up)")
    idx = json.loads(idx_path.read_text(encoding="utf-8"))
    page = (idx.get("page") or {}).get("name")
    loaded: list[tuple[dict, dict | None]] = []
    for entry in idx.get("frames") or []:
        try:
            loaded.append((entry, json.loads((snapshot / entry["json"]).read_text(encoding="utf-8"))))
        except (OSError, ValueError, KeyError):
            loaded.append((entry, None))

    captions: list[str] = []
    screens: list[tuple[dict, dict]] = []
    for entry, data in loaded:
        if data is None:
            continue
        doc = data.get("document") or data
        if doc.get("type") == "TEXT":
            if (doc.get("characters") or "").strip():
                captions.append(f'- "{doc["characters"].strip()}" (node {entry["id"]})')
        elif doc.get("type") in ("LINE", "VECTOR", "ELLIPSE", "RECTANGLE", "STAR", "POLYGON"):
            pass  # divider/decoration — carries no design content
        else:
            # a FRAME wrapping nothing but ≤1 string (a caption band, a swatch label, a bare icon)
            # is an annotation, not a screen — audit 2026-09-02 found 3 such groups escaping the
            # accounting because they masqueraded as screen sections
            texts: list = []
            instances: list = []
            frames: list = []
            _walk(doc, texts, instances, frames, 0)
            if not instances and not frames and len(texts) <= 1:
                label = texts[0][2] if texts else (doc.get("name") or "?")
                captions.append(f'- "{label}" (node {entry["id"]}, annotation frame)')
            else:
                screens.append((entry, data))

    groups: dict[tuple, list[tuple[dict, dict, list[str], frozenset]]] = {}
    order: list[tuple] = []
    for entry, data in screens:
        key = (entry.get("name"), round(entry.get("w") or 0), round(entry.get("h") or 0))
        if key not in groups:
            groups[key] = []
            order.append(key)
        texts, sig = _texts_of(entry, data)
        groups[key].append((entry, data, texts, sig))

    out = [
        "# Figma snapshot digest (deterministic — generated by rfc_agent.figma_snapshot)",
        f"source_url: {idx.get('source_url')}",
        f"file: {idx.get('file')}  page: {page}  exported_at: {idx.get('exported_at')}",
        f"frames: {len(idx.get('frames') or [])} exported — {len(screens)} screen frames in "
        f"{len(groups)} (name, size) section(s) below. Identical duplicates are collapsed to ids; frames "
        f"that differ are grouped into STATE CLUSTERS under their section (see 'State clusters'). The "
        f"harness tracks coverage per section/cluster — you never list ids.",
        "",
    ]
    out += flow_map(snapshot)
    if captions:
        out.append("Canvas captions (designer annotation text near the frames — the scenario-band labels):")
        out += captions[:60]
        out.append("")
    for key in order:
        members = groups[key]
        # representative = first member of the largest identical-text cluster (the group's common
        # state), so variants read as ADDITIONS (a toast, an expanded list) — not subtractions from
        # whichever frame happened to be busiest; ties go to the more-populated frame
        clusters: dict[tuple, int] = {}
        for _, _, t, sg in members:
            clusters[(tuple(t), sg)] = clusters.get((tuple(t), sg), 0) + 1
        rep_entry, rep_data, rep_texts, rep_sig = max(
            members, key=lambda m: (clusters[(tuple(m[2]), m[3])], len(m[2]), -members.index(m)))
        out += _frame_section(rep_entry, rep_data)
        identical = [e["id"] for e, _, t, sg in members
                     if e["id"] != rep_entry["id"] and t == rep_texts and sg == rep_sig]
        variants = [(e, t, sg) for e, _, t, sg in members
                    if e["id"] != rep_entry["id"] and (t != rep_texts or sg != rep_sig)]
        if identical:
            out.append(f"Identical duplicates (same name, size, rendered text and variants — ×{len(identical)}): "
                       + ", ".join(identical))
            out.append("")
        if variants:
            out += _cluster_variants(rep_entry, rep_texts, rep_sig, variants)
    for entry, data in loaded:
        if data is None:
            out.append(f"## {entry.get('name')} (node {entry.get('id')}) — JSON UNREADABLE")
            out.append("")
    return "\n".join(out)


# ---- batches for parallel readers ----------------------------------------------------------------

_SECTION_HEAD_RE = re.compile(r"^## (.*?) \(node (I?\d+:\d+), (\w+), (\d+)×(\d+)\) — render: (\S+)$", re.MULTILINE)
_CLUSTER_HEAD_RE = re.compile(r"^### (C\d+) \(×(\d+)\) — first (I?\d+:\d+) \(render (\S+)\)$", re.MULTILINE)
_IDENT_RE = re.compile(r"^Identical duplicates[^:]*: (.*)$", re.MULTILINE)
# ---- staging + predicate ---------------------------------------------------------------------------

def stage_snapshot(src: Path, run_dir: Path) -> str:
    """Copy an exported snapshot under the run (the sandboxed agent's cwd) and write its digest.
    Returns the run-relative path stored in inputs.json. Exits loudly on a non-snapshot directory —
    a wrong --figma path must never silently degrade to 'no designs'."""
    src = Path(src).expanduser()
    if not (src / "index.json").exists():
        raise SystemExit(f"--figma {src}: no index.json — not a figma_snapshot export directory "
                         f"(start `python -m rfc_agent.figma_snapshot serve <dir>`, run the plugin, then pass <dir>)")
    dst = Path(run_dir) / "_inputs" / "figma"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)
    (dst / "digest.md").write_text(digest(dst), encoding="utf-8")
    return str(Path("_inputs") / "figma")


def snapshot_dir(ctx) -> Path | None:
    """The staged snapshot directory when this run's --figma was an export dir, else None."""
    v = ctx.inputs.get("figma")
    if not v or "://" in str(v):
        return None
    p = Path(ctx.run_dir) / str(v)
    return p if (p / "index.json").exists() else None


# ---- cli -------------------------------------------------------------------------------------------

def sync_stable() -> Path:
    """Copy the plugin into _INSTALL_DIR and return its manifest. Figma loads a development plugin from the
    folder it was imported from, and an installed rfc-agent lives in a per-version folder that an update
    replaces — so the user imports once from this fixed folder, and every run refreshes it."""
    _INSTALL_DIR.mkdir(parents=True, exist_ok=True)
    mpath = _INSTALL_DIR / "manifest.json"
    # Figma registers a development plugin under its manifest id — a reinstall must keep the id the
    # user already imported, or the menu entry silently points at a plugin that no longer exists
    prev_id = None
    if mpath.exists():
        with contextlib.suppress(ValueError, OSError):
            prev_id = json.loads(mpath.read_text(encoding="utf-8")).get("id")
    for f in _PLUGIN_SRC.iterdir():
        if f.is_file():
            shutil.copy2(f, _INSTALL_DIR / f.name)
    manifest = json.loads(mpath.read_text(encoding="utf-8"))
    # Figma requires a numeric id even for development plugins; a fresh one per first install avoids
    # colliding with any other dev plugin on the machine (the id is never published)
    manifest["id"] = prev_id or str(10**18 + secrets.randbelow(9 * 10**18))
    mpath.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return mpath


def _install() -> int:
    if not _PLUGIN_SRC.exists():
        print(f"figma_snapshot: plugin source missing at {_PLUGIN_SRC}", file=sys.stderr)
        return 1
    mpath = sync_stable()
    print(f"Plugin installed at {_INSTALL_DIR}\n"
          f"One-time: Figma desktop → Plugins → Development → Import plugin from manifest… → {mpath}\n"
          f"Per design revision: start `python -m rfc_agent.figma_snapshot serve <out_dir> --url <figma url>`,\n"
          f"open the file on the page holding the screens (select frames to scope, or select nothing for the\n"
          f"whole page), run Plugins → Development → rfc-agent Figma snapshot; pass `--figma <out_dir>` to rfc-agent.")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print(__doc__, file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "install":
        return _install()
    if cmd == "digest":
        if len(rest) != 1:
            print("usage: figma_snapshot digest <snapshot_dir>", file=sys.stderr)
            return 2
        snap = Path(rest[0]).expanduser()
        text = digest(snap)
        (snap / "digest.md").write_text(text, encoding="utf-8")
        print(f"{snap / 'digest.md'}: {len(text)} chars")
        return 0
    if cmd == "serve":
        port, url, out = DEFAULT_PORT, None, None
        i = 0
        while i < len(rest):
            if rest[i] == "--port":
                port, i = int(rest[i + 1]), i + 2
            elif rest[i] == "--url":
                url, i = rest[i + 1], i + 2
            else:
                out, i = rest[i], i + 1
        if not out:
            print("usage: figma_snapshot serve <out_dir> [--port N] [--url <figma url>]", file=sys.stderr)
            return 2
        srv = SnapshotServer(Path(out).expanduser(), port=port, source_url=url)
        print(f"figma_snapshot: receiving on http://127.0.0.1:{srv.port} -> {srv.out_dir}\n"
              f"Now run the plugin in Figma (Plugins → Development → rfc-agent Figma snapshot). Ctrl-C to abort.",
              file=sys.stderr)
        try:
            srv.serve_until_done()
        except KeyboardInterrupt:
            if srv.frames:
                srv.write_index()
                print(f"figma_snapshot: interrupted — index written for {len(srv.frames)} frame(s)", file=sys.stderr)
            return 130
        (srv.out_dir / "digest.md").write_text(digest(srv.out_dir), encoding="utf-8")
        print(f"figma_snapshot: done — pass `--figma {srv.out_dir}` to rfc-agent", file=sys.stderr)
        return 0
    print(f"figma_snapshot: unknown command {cmd!r}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
