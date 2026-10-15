"""Suite 6 (AFTER-only hook): time translating a large recorded Claude stream-json file into the harness's
canonical event schema (src/harness/events.py). Skips cleanly today — no such translator exists yet
(src/harness/engines/ has only __init__.py). Uses bench.fixtures.generate_stream_json for a reproducible
5MB fixture instead of a committed binary, matching the same synthetic-fixture policy as harness_overhead.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from bench.timing import Sample, timed

TARGET_BYTES = 5 * 1024 * 1024


def run(repeat: int = 5) -> dict:
    try:
        from harness.engines import claude_translate
    except ImportError:
        return {"results": [], "notes": [
            "harness.engines.claude_translate not available — AFTER-only hook, skipped.",
        ]}

    from bench.fixtures import generate_stream_json

    with tempfile.TemporaryDirectory(prefix="bench-stream-") as tmp:
        fixture = generate_stream_json(Path(tmp) / "large.stream.jsonl", TARGET_BYTES)
        size = fixture.stat().st_size

        sample = Sample(name="stream_translate.translate_5mb", unit="s")
        for _ in range(repeat):
            trace_out = Path(tmp) / "large.events.jsonl"
            dt, _ = timed(claude_translate.translate_file, fixture, trace_out)
            sample.values.append(dt)

    return {"results": [sample.to_result()], "notes": [f"fixture_bytes={size}"]}
