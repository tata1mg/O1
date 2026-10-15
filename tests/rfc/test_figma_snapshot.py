"""figma_snapshot: the quota-free Figma channel. A private dev plugin (tools/figma_snapshot_plugin)
exports JSON_REST_V1 + PNG per frame and POSTs them to the localhost receiver; the receiver writes a
snapshot directory; `digest` renders it into the compact inventory INGEST reads instead of calling
the Figma MCP. Properties pinned here: the receiver's on-disk contract, the digest's reading-order
text + variant extraction, CLI staging of a snapshot dir, and the run-level consequence — a
snapshot run binds no figma MCP server and its kickoff prompt forbids Figma tools."""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from rfc_agent import figma_snapshot
from rfc_agent.figma_snapshot import SnapshotServer, digest, snapshot_dir, stage_snapshot

# a JSON_REST_V1 export: the per-node entry of GET /v1/files/:key/nodes (document + components)
_FRAME = {
    "document": {
        "id": "19028:146037", "name": "Select patient", "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 360, "height": 800},
        "children": [
            {"id": "1:2", "name": "Header", "type": "FRAME",
             "absoluteBoundingBox": {"x": 0, "y": 0, "width": 360, "height": 56},
             "children": [
                 {"id": "1:3", "name": "title", "type": "TEXT", "characters": "Select patient",
                  "absoluteBoundingBox": {"x": 16, "y": 16, "width": 200, "height": 24}},
             ]},
            {"id": "1:4", "name": "Suggestion chip_standard", "type": "INSTANCE",
             "componentId": "c1",
             "componentProperties": {"State": {"value": "Selected", "type": "VARIANT"},
                                     "Label#1:0": {"value": "Sort by", "type": "TEXT"}},
             "absoluteBoundingBox": {"x": 16, "y": 100, "width": 80, "height": 32},
             "children": [
                 {"id": "I1:4;2:1", "name": "label", "type": "TEXT", "characters": "Sort by",
                  "absoluteBoundingBox": {"x": 20, "y": 106, "width": 60, "height": 20}},
             ]},
            {"id": "1:5", "name": "hidden toast", "type": "FRAME", "visible": False,
             "absoluteBoundingBox": {"x": 0, "y": 700, "width": 360, "height": 40},
             "children": [
                 {"id": "1:6", "name": "t", "type": "TEXT", "characters": "NEVER SHOWN",
                  "absoluteBoundingBox": {"x": 0, "y": 700, "width": 100, "height": 20}},
             ]},
            {"id": "1:7", "name": "cta", "type": "TEXT", "characters": "Proceed to slot selection",
             "absoluteBoundingBox": {"x": 16, "y": 740, "width": 300, "height": 24}},
            {"id": "1:8", "name": "row2", "type": "TEXT", "characters": "You have 14 patients.",
             "absoluteBoundingBox": {"x": 16, "y": 60, "width": 300, "height": 20}},
        ],
    },
    "components": {"c1": {"name": "Suggestion chip_standard", "key": "k"}},
}


def _post(port: int, path: str, body: dict) -> int:
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("POST", path, json.dumps(body), {"Content-Type": "application/json"})
    status = conn.getresponse().status
    conn.close()
    return status


def _export(out: Path, url: str | None = "https://www.figma.com/design/ABC/x?node-id=1-2") -> Path:
    """Drive the receiver exactly as the plugin does: /frame per node, then /done."""
    srv = SnapshotServer(out, port=0, source_url=url)
    t = threading.Thread(target=srv.serve_until_done, daemon=True)
    t.start()
    assert _post(srv.port, "/frame", {
        "page": {"id": "0:1", "name": "Patient management"},
        "node": {"id": "19028:146037", "name": "Select patient", "type": "FRAME", "w": 360, "h": 800},
        "scale": 2, "json": _FRAME, "png_b64": "iVBORw0KGgo=",
    }) == 200
    assert _post(srv.port, "/done", {"file": "HIH-X", "page": {"id": "0:1", "name": "Patient management"}}) == 200
    t.join(timeout=5)
    assert not t.is_alive(), "receiver must exit after /done"
    return out


def test_receiver_writes_frames_and_index(tmp_path):
    out = _export(tmp_path / "snap")
    idx = json.loads((out / "index.json").read_text())
    assert idx["source_url"].startswith("https://www.figma.com/design/ABC")
    assert idx["file"] == "HIH-X" and idx["page"]["name"] == "Patient management"
    (frame,) = idx["frames"]
    assert frame["id"] == "19028:146037" and frame["json"] == "19028-146037.json" and frame["png"] == "19028-146037.png"
    assert json.loads((out / "19028-146037.json").read_text())["document"]["name"] == "Select patient"
    assert (out / "19028-146037.png").read_bytes().startswith(b"\x89PNG")


def test_receiver_answers_cors_preflight_and_health(tmp_path):
    srv = SnapshotServer(tmp_path, port=0, source_url=None)
    t = threading.Thread(target=srv.serve_until_done, daemon=True)
    t.start()
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5)
    conn.request("OPTIONS", "/frame")
    r = conn.getresponse()
    assert r.status == 204 and r.getheader("Access-Control-Allow-Origin") == "*"
    conn.request("GET", "/health")
    assert conn.getresponse().status == 200
    conn.close()
    _post(srv.port, "/done", {})
    t.join(timeout=5)


def test_receiver_rejects_unsafe_node_ids(tmp_path):
    srv = SnapshotServer(tmp_path, port=0, source_url=None)
    t = threading.Thread(target=srv.serve_until_done, daemon=True)
    t.start()
    assert _post(srv.port, "/frame", {"node": {"id": "../../evil", "name": "x", "type": "FRAME"},
                                       "json": {}, "png_b64": ""}) == 400
    assert not list(tmp_path.glob("*evil*"))
    _post(srv.port, "/done", {})
    t.join(timeout=5)


def test_digest_reading_order_variants_and_hidden_pruning(tmp_path):
    out = _export(tmp_path / "snap")
    text = digest(out)
    (out / "digest.md").write_text(text)
    # one section per frame, pointing at the render the agent should Read
    assert "## Select patient (node 19028:146037, FRAME, 360×800) — render: 19028-146037.png" in text
    # rendered text in reading order (top→bottom), from live `characters` — instance override included
    order = [text.index(s) for s in ('"Select patient"', '"You have 14 patients."', '"Sort by"',
                                     '"Proceed to slot selection"')]
    assert order == sorted(order)
    # hidden subtrees are pruned, not transcribed
    assert "NEVER SHOWN" not in text
    # component instances surface with their variant/text properties (state enumeration comes free)
    assert "`Suggestion chip_standard` {State=Selected, Label=Sort by}" in text


def test_digest_reports_missing_index_loudly(tmp_path):
    with pytest.raises(FileNotFoundError):
        digest(tmp_path)


def _mini_frame(nid: str, texts: list[str], name: str = "Single SKU cart") -> dict:
    return {"document": {
        "id": nid, "name": name, "type": "FRAME",
        "absoluteBoundingBox": {"x": 0, "y": 0, "width": 360, "height": 800},
        "children": [{"id": f"{nid}-t{i}", "name": "t", "type": "TEXT", "characters": s,
                      "absoluteBoundingBox": {"x": 16, "y": 40 * i, "width": 200, "height": 20}}
                     for i, s in enumerate(texts)],
    }}


def test_digest_collapses_duplicates_keeps_variants_and_captions(tmp_path):
    """The 145-frame lesson (2026-09-02 benchmark: 12.1m/$4.70, worse than MCP): layout duplicates
    collapse to ids, behavioral variants survive as diffs, canvas captions become the band list."""
    srv = SnapshotServer(tmp_path / "snap", port=0, source_url=None)
    t = threading.Thread(target=srv.serve_until_done, daemon=True)
    t.start()
    base = ["Cart", "Select patient", "Proceed to slot selection"]
    frames = [
        ("10:1", _mini_frame("10:1", base)),
        ("10:2", _mini_frame("10:2", base)),                              # identical duplicate
        ("10:3", _mini_frame("10:3", [*base, "Successfully added one more patient."])),  # toast variant
        ("10:4", {"document": {"id": "10:4", "name": "USE CASES", "type": "TEXT",
                               "characters": "No patient exists",
                               "absoluteBoundingBox": {"x": 0, "y": 0, "width": 200, "height": 20}}}),
        ("10:5", {"document": {"id": "10:5", "name": "Line 1078", "type": "LINE",
                               "absoluteBoundingBox": {"x": 0, "y": 0, "width": 500, "height": 1}}}),
    ]
    for nid, payload in frames:
        node = payload["document"]
        box = node.get("absoluteBoundingBox") or {}
        assert _post(srv.port, "/frame", {"node": {"id": nid, "name": node["name"], "type": node["type"],
                                                    "w": box.get("width"), "h": box.get("height")},
                                           "json": payload, "png_b64": "iVBORw0KGgo="}) == 200
    _post(srv.port, "/done", {"file": "f", "page": {"id": "0:1", "name": "p"}})
    t.join(timeout=5)

    text = digest(tmp_path / "snap")
    assert text.count("## Single SKU cart") == 1  # one representative section for the whole group
    assert "Identical duplicates (same name, size, rendered text and variants — ×1): 10:2" in text
    # the toast frame is a STATE CLUSTER of the section, pre-grouped by the harness
    assert "State clusters of 10:1 — 1 variant frame(s) in 1 cluster(s)" in text
    assert "### C1 (×1) — first 10:3 (render 10-3.png)" in text
    assert '  shared: +"Successfully added one more patient."' in text and "  frames: 10:3" in text
    assert '- "No patient exists" (node 10:4)' in text  # caption listed, not a screen section
    assert "## USE CASES" not in text and "Line 1078" not in text


def test_digest_never_collapses_frames_differing_only_by_component_variant(tmp_path):
    """Text alone must not decide 'identical': a checked-vs-unchecked checkbox changes no string."""
    srv = SnapshotServer(tmp_path / "snap", port=0, source_url=None)
    t = threading.Thread(target=srv.serve_until_done, daemon=True)
    t.start()
    def frame(nid, state):
        f = _mini_frame(nid, ["Select patient"])
        f["document"]["children"].append({
            "id": f"{nid}-cb", "name": "Checkbox", "type": "INSTANCE", "componentId": "c9",
            "componentProperties": {"State": {"value": state, "type": "VARIANT"}},
            "absoluteBoundingBox": {"x": 16, "y": 200, "width": 24, "height": 24}, "children": []})
        return f
    for nid, state in (("20:1", "Unchecked"), ("20:2", "Unchecked"), ("20:3", "Checked")):
        assert _post(srv.port, "/frame", {"node": {"id": nid, "name": "Single SKU cart", "type": "FRAME",
                                                    "w": 360, "h": 800},
                                           "json": frame(nid, state), "png_b64": "iVBORw0KGgo="}) == 200
    _post(srv.port, "/done", {"file": "f", "page": {"id": "0:1", "name": "p"}})
    t.join(timeout=5)
    text = digest(tmp_path / "snap")
    assert "Identical duplicates (same name, size, rendered text and variants — ×1): 20:2" in text
    assert "### C1 (×1) — first 20:3 (render 20-3.png)" in text
    assert "shared: no text change vs the section frame" in text
    assert "  20:3 also: component variants differ: Checkbox" in text


def test_digest_classifies_content_free_frames_as_annotations(tmp_path):
    """A FRAME holding nothing but one caption string (a band label, a swatch, an icon) is an
    annotation — 3 such groups escaped figma.md's accounting on the PROJ-3394 audit (2026-09-02)."""
    srv = SnapshotServer(tmp_path / "snap", port=0, source_url=None)
    t = threading.Thread(target=srv.serve_until_done, daemon=True)
    t.start()
    band = _mini_frame("30:1", ["Existing patient selected"], name=".")
    icon = {"document": {"id": "30:2", "name": "sorting-04", "type": "FRAME",
                         "absoluteBoundingBox": {"x": 0, "y": 0, "width": 24, "height": 24},
                         "children": []}}
    real = _mini_frame("30:3", ["Cart", "Select patient"])
    for nid, payload, w, h in (("30:1", band, 1206, 130), ("30:2", icon, 24, 24), ("30:3", real, 360, 800)):
        assert _post(srv.port, "/frame", {"node": {"id": nid, "name": payload["document"]["name"],
                                                    "type": "FRAME", "w": w, "h": h},
                                           "json": payload, "png_b64": "iVBORw0KGgo="}) == 200
    _post(srv.port, "/done", {"file": "f", "page": {"id": "0:1", "name": "p"}})
    t.join(timeout=5)
    text = digest(tmp_path / "snap")
    assert '- "Existing patient selected" (node 30:1, annotation frame)' in text
    assert '- "sorting-04" (node 30:2, annotation frame)' in text
    assert "## ." not in text and "## sorting-04" not in text
    assert "## Single SKU cart (node 30:3" in text  # the real screen keeps its full section


def test_stage_snapshot_copies_into_inputs_and_writes_digest(tmp_path):
    src = _export(tmp_path / "exported")
    run = tmp_path / "run"
    run.mkdir()
    rel = stage_snapshot(src, run)
    assert rel == "_inputs/figma"
    staged = run / rel
    assert (staged / "index.json").exists() and (staged / "19028-146037.png").exists()
    assert "Select patient" in (staged / "digest.md").read_text()
    # the run now resolves as a snapshot run; a URL never does
    ctx = SimpleNamespace(run_dir=run, inputs={"figma": rel})
    assert snapshot_dir(ctx) == staged
    ctx.inputs["figma"] = "https://www.figma.com/design/ABC/x"
    assert snapshot_dir(ctx) is None


def test_stage_snapshot_refuses_a_directory_without_index(tmp_path):
    (tmp_path / "notasnap").mkdir()
    with pytest.raises(SystemExit):
        stage_snapshot(tmp_path / "notasnap", tmp_path / "run")


def test_install_copies_plugin_to_stable_path_with_fresh_id(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(figma_snapshot, "_INSTALL_DIR", tmp_path / "plugin")
    assert figma_snapshot.main(["install"]) == 0
    manifest = json.loads((tmp_path / "plugin" / "manifest.json").read_text())
    assert manifest["id"].isdigit() and len(manifest["id"]) >= 18
    assert manifest["documentAccess"] == "dynamic-page"
    # without "inspect", a Dev Mode click on the plugin silently does nothing (seen live 2026-09-02;
    # same failure class as grab/cursor-talk-to-figma#176)
    assert manifest["capabilities"] == ["inspect"]
    # a reinstall must keep the id Figma already registered, or the imported entry goes dead
    assert figma_snapshot.main(["install"]) == 0
    assert json.loads((tmp_path / "plugin" / "manifest.json").read_text())["id"] == manifest["id"]
    # Figma's validator rejects raw-IP origins ("must be a valid URL", seen live 2026-09-02) —
    # the manifest must carry the localhost form only
    assert manifest["networkAccess"]["allowedDomains"] == ["http://localhost:8790"]
    assert (tmp_path / "plugin" / "code.js").exists() and (tmp_path / "plugin" / "ui.html").exists()
    assert "Import plugin from manifest" in capsys.readouterr().out
