"""Suite registry — `python -m bench <name>` looks names up here. Each module exposes `run(repeat: int)
-> dict` returning `{"results": [Sample.to_result(), ...], "notes": [...]}` (notes are free-text lines the
CLI prints and BENCHMARKS.md quotes, e.g. "not available" for an AFTER-only hook)."""

from __future__ import annotations

SUITE_NAMES = (
    "harness_overhead",
    "cli_startup",
    "test_suite",
    "engine_pong",
    "stream_translate",
)


def load(name: str):
    if name not in SUITE_NAMES:
        raise SystemExit(f"unknown suite {name!r} — known: {list(SUITE_NAMES)}")
    import importlib

    return importlib.import_module(f"bench.suites.{name}")
