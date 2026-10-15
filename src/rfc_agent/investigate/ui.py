"""UI reader inputs, prepared by Python (no model).

- Which screens: the Figma sections picked in Scope (scope.json figma[].sections[].include).
- Assignment: a section holding a flow's trigger screen goes to that flow's reader; other sections are spread to
  balance; sections are never split; each reader stays within SCREENS_PER_READER where possible.
- Per screen: render path + exact text from the export with "behind sheet" / "off-viewport" flags.
- Prototype links between the reader's screens.
Writes work/ui/<reader>.md (the reader's screens file) and returns the reader plan.
"""

from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .. import figma_snapshot as fs


def _picked_frames(run_dir: Path, scope: dict) -> list[dict]:
    """[{snap_rel, section, frame entry}] for every frame in a picked section."""
    out = []
    for f in scope.get("figma") or []:
        rel = f.get("run_dir_rel")
        if not rel:
            continue
        snap = run_dir / rel
        try:
            idx = json.loads((snap / "index.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        picked = {s["name"] for s in f.get("sections") or [] if s.get("include")}
        for e in idx.get("frames") or []:
            sec = e.get("section") or e.get("page") or "(no section)"
            if not picked or sec in picked:
                out.append({"snap_rel": rel, "section": sec, "entry": e})
    return out


def plan_readers(run_dir: Path, scope: dict, flows: dict, per_reader: int) -> list[dict]:
    frames = _picked_frames(run_dir, scope)
    if not frames:
        return []
    sections: dict[tuple[str, str], list[dict]] = {}
    for fr in frames:
        sections.setdefault((fr["snap_rel"], fr["section"]), []).append(fr)
    # which flow owns a section: one of its frames' names matches a flow's trigger screen
    owner: dict[tuple[str, str], str] = {}
    for f in flows.get("flows") or []:
        names = [s.lower() for s in (f.get("trigger") or {}).get("screens") or [] if s]
        for key, frs in sections.items():
            if key in owner:
                continue
            if any(
                n and (n in (fr["entry"].get("name") or "").lower() or n in key[1].lower()) for fr in frs for n in names
            ):
                owner[key] = f.get("id")
    n_readers = max(1, math.ceil(len(frames) / max(1, per_reader)))
    readers = [{"id": f"UI{i + 1}", "sections": [], "frames": [], "flows": []} for i in range(n_readers)]
    # owned sections first, grouped by flow so a flow's screens stay with one reader
    order = sorted(sections, key=lambda k: (owner.get(k) is None, owner.get(k) or "", -len(sections[k])))
    flow_home: dict[str, dict] = {}
    for key in order:
        fid = owner.get(key)
        r = flow_home.get(fid) if fid else None
        if r is None or (len(r["frames"]) + len(sections[key]) > per_reader and len(r["frames"]) > 0):
            r = min(readers, key=lambda x: len(x["frames"]))
        r["sections"].append(key[1])
        r["frames"] += sections[key]
        if fid:
            flow_home.setdefault(fid, r)
            if fid not in r["flows"]:
                r["flows"].append(fid)
    # r["flows"] = flows whose trigger screens this reader holds (checked for a block); the reader may still
    # map any of its screens to any flow in flows.json
    return [r for r in readers if r["frames"]]


def write_reader_inputs(run_dir: Path, reader: dict) -> tuple[Path, set[str], set[str]]:
    """work/ui/<id>.md for the reader. Returns (path, node ids, texts) for the checks."""
    lines = [f"# Screens for {reader['id']}", "", f"Sections: {', '.join(reader['sections'])}", ""]
    node_ids: set[str] = set()
    texts: set[str] = set()
    ids_here = {fr["entry"]["id"] for fr in reader["frames"]}
    snaps = sorted({fr["snap_rel"] for fr in reader["frames"]})
    for fr in reader["frames"]:
        e, snap = fr["entry"], run_dir / fr["snap_rel"]
        node_ids.add(e["id"])
        png = f"{fr['snap_rel']}/{e['png']}" if e.get("png") else "NONE (render failed)"
        lines += [f"## {e.get('name')} ({e['id']}) — section {fr['section']}", f"- render: {png}"]
        try:
            data = json.loads((snap / e["json"]).read_text(encoding="utf-8"))
            visible = fs._texts_of(e, data)[0]
            flags = fs._visibility(data.get("document") or data)
        except (OSError, ValueError, KeyError):
            visible, flags = [], {}
        for t in visible:
            texts.add(t)
            flag = flags.get(t, "")
            lines.append(f'  - "{t[:200]}"' + (f"  [{flag}]" if flag else ""))
        lines.append("")
    links = []
    for rel in snaps:
        try:
            for src, trig, nav, dst in fs.prototype_edges(run_dir / rel):
                if src in ids_here and dst in ids_here:
                    links.append(f"- {src} —[{trig} · {nav}]→ {dst}")
        except (OSError, ValueError):
            continue
    lines += ["## Prototype links", *(links or ["- (none between these screens)"])]
    out = run_dir / "work" / "ui" / f"{reader['id']}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    for s in list(texts):  # node-id-ish references in texts are not node ids; keep the set as is
        if re.fullmatch(r"\s*", s):
            texts.discard(s)
    return out, node_ids, texts
