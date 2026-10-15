"""The route map. The rule: the map must MERGE query strings, never drop either side.
"""

from __future__ import annotations

import json

import pytest

from workflows.testing.route_map import apply_route_map, load_map


def rewrite(tmp_path, mapping, path):
    f = tmp_path / "map.json"
    f.write_text(json.dumps(mapping))
    spec, _ = apply_route_map([{"extid": "T", "path": path}], path=f)
    return spec[0]["path"]


@pytest.mark.parametrize("name,mapping,path,want", [
    # source :24-27 — the contract omitted required params; the map supplies them
    ("map supplies params the case lacked",
     {"/cart/upsell": "/labs_agg/v1/carts/upsell?types=labs&type=labs"},
     "/cart/upsell", "/labs_agg/v1/carts/upsell?types=labs&type=labs"),
    # source :30-33 — the case had its own params; both survive
    ("case params and mapped params merge",
     {"/cart/upsell": "/labs_agg/v1/carts/upsell?types=labs"},
     "/cart/upsell?user_id=U-1001", "/labs_agg/v1/carts/upsell?types=labs&user_id=U-1001"),
    ("plain rewrite", {"/a": "/b"}, "/a", "/b"),                            # source :36
    ("case params preserved", {"/a": "/b"}, "/a?x=1", "/b?x=1"),            # source :39
    ("underscore keys ignored", {"_why": "note", "/a": "/b"}, "/a", "/b"),  # source :42
    ("unmapped path untouched", {"/a": "/b"}, "/c?x=1", "/c?x=1"),          # source :45
])
def test_route_map_parity(tmp_path, name, mapping, path, want):
    assert rewrite(tmp_path, mapping, path) == want, name


# ---- beyond parity ----------------------------------------------------------------------------------
def test_a_missing_map_file_is_not_an_error():
    """A run without a route-map file returns the spec untouched rather than failing."""
    spec, rewrites = apply_route_map([{"extid": "T", "path": "/a"}], path="/nonexistent/map.json")
    assert spec[0]["path"] == "/a" and rewrites == []


def test_no_path_and_no_mapping_means_no_rewriting():
    spec, rewrites = apply_route_map([{"extid": "T", "path": "/a"}])
    assert spec[0]["path"] == "/a" and rewrites == []


def test_rewrites_name_the_case_and_both_paths():
    """Every rewrite is reported so it is never silent — the stage turns each into a decision record."""
    spec, rewrites = apply_route_map([{"extid": "TC-A", "path": "/a?x=1"}], mapping={"/a": "/b?y=2"})
    assert rewrites == [("TC-A", "/a", "/b?y=2")]
    assert spec[0]["path"] == "/b?y=2&x=1"


def test_load_map_drops_reviewer_notes(tmp_path):
    f = tmp_path / "m.json"
    f.write_text(json.dumps({"_why": "the edge renames this", "/a": "/b"}))
    assert load_map(f) == {"/a": "/b"}
