"""Langfuse v4 platform / Python SDK v5 migration guard. Every Langfuse call in obs/observe sits inside
suppress(Exception), so a removed SDK method fails SILENTLY in production (span.update_trace vanished
in 4.14 and session/input were null on every trace). This scans the code for calls v5 removed and
for public-API endpoints Langfuse Cloud retires at the v4 cutover (2026-11-16)."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = [*sorted((ROOT / "src").rglob("*.py")), *sorted((ROOT / "scripts").rglob("*.py")),
           *sorted((ROOT / "tools").rglob("*.py")), *sorted((ROOT / "bench").rglob("*.py"))]

# (what, pattern) — a call, not a mention in a comment or docstring
REMOVED = [
    ("set_trace_io / set_current_trace_io (v5: update the root observation)",
     r"\.set(?:_current)?_trace_io\("),
    ("update_trace (removed in 4.14)", r"\.update_trace\("),
    ("api.trace.get / api.trace.list (v5: api.observations.get_many)", r"\.api\.trace\.(?:get|list)\("),
    ("api.sessions (v5: api.observations.get_many)", r"\.api\.sessions\."),
    ("api.legacy.* (removed in v5)", r"\.api\.legacy\."),
    ("api.ingestion (removed in v5)", r"\.api\.ingestion\."),
    ("api.scores_v3 (v5: api.scores.get_many)", r"\.api\.scores_v3\."),
    ("api.dataset_run_items (v5: api.experiments)", r"\.api\.dataset_run_items\."),
    ("dataset run helpers (v5: api.experiments)", r"\.(?:get_dataset_runs?|delete_dataset_run)\("),
    ("blocked_instrumentation_scopes (v5: should_export_span)", r"blocked_instrumentation_scopes\s*="),
    ("legacy public API v1 observations/traces/sessions (v4: /api/public/v2/observations)",
     r"/api/public/(?:observations|traces|sessions)\b"),
    ("legacy ingestion endpoint", r"/api/public/ingestion\b"),
]


def _code_lines(path: Path):
    in_doc = False
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if stripped.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or stripped.startswith("#"):
            continue
        yield n, line.split("  #")[0]


@pytest.mark.parametrize("what,pattern", REMOVED, ids=[w.split(" (")[0] for w, _ in REMOVED])
def test_no_removed_or_retired_langfuse_calls(what, pattern):
    rx = re.compile(pattern)
    hits = [f"{p.relative_to(ROOT)}:{n}" for p in SCANNED for n, code in _code_lines(p) if rx.search(code)]
    assert not hits, f"{what}: {hits}"


def test_sdk_is_v5():
    from importlib.metadata import version

    assert version("langfuse").split(".")[0] == "5"
