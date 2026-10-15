"""Contract route -> deployed route rewriting.

Ported verbatim from test_agent tools/be_execute.py:28-58, with one change: the map path is always
explicit (flag `testing.route_map`). test_agent defaulted to `config/route-map/<feature>.json` relative
to its own repo, which a harness run dir has no business reaching into.

WHY this exists at all (the original comment, kept because it is the whole justification): a contract
states the route as the service registers it — `GET /cart/upsell`, citing labs_agg/app/routes/cart.py:118.
The deployed surface is `/labs_agg/v1/carts/upsell`. Both are correct about different layers, and "copy
the route verbatim from the contract" faithfully reproduces the in-service one, which 404s at the edge and
is reported as BLOCKED for every case in the suite. The mapping is per-feature, explicit and reviewed —
not inferred: that rewrite is not a prefix, a version bump or a pluralisation rule, it is a routing
decision only the team knows. Query strings are merged; every rewrite is returned so it is never silent.
"""

from __future__ import annotations

import json
from pathlib import Path


def load_map(path: str | Path | None) -> dict[str, str]:
    """Read the map, dropping reviewer notes. Keys starting with '_' are notes, not routes."""
    if not path:
        return {}
    p = Path(path).expanduser()
    if not p.exists():
        return {}
    return {k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")}


def apply_route_map(spec: list[dict], mapping: dict[str, str] | None = None,
                    path: str | Path | None = None) -> tuple[list[dict], list[tuple[str, str, str]]]:
    """Rewrite each entry's path in place. -> (spec, [(extid, from, to)]).

    Pass either a loaded `mapping` or a `path` to load one from.
    """
    mapping = load_map(path) if mapping is None else {k: v for k, v in mapping.items() if not k.startswith("_")}
    if not mapping:
        return spec, []
    rewrites: list[tuple[str, str, str]] = []
    for e in spec:
        base, _, qs = e["path"].partition("?")
        if base in mapping:
            tbase, _, tqs = mapping[base].partition("?")
            # A mapped value may carry required parameters the contract never listed. Merge them with
            # the case's own rather than dropping either side.
            merged = "&".join(x for x in (tqs, qs) if x)
            e["path"] = tbase + ("?" + merged if merged else "")
            rewrites.append((e["extid"], base, mapping[base]))
    return spec, rewrites
