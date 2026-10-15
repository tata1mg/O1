"""Suite 4: `uv run pytest -q` wall time + counts (parsed off the summary line). Repeat 1 by default —
the full suite is the expensive one, and CI already runs it every push; this suite exists to attach a
single before/after number to a PR, not to be re-run 20x locally.
"""

from __future__ import annotations

import re
import subprocess

from bench.timing import Sample, timed

_SUMMARY_RE = re.compile(
    r"(?:(?P<failed>\d+) failed, )?(?:(?P<passed>\d+) passed)(?:, (?P<skipped>\d+) skipped)?"
    r"(?:, (?P<xfailed>\d+) xfailed)?(?:, (?P<errors>\d+) error(?:s)?)? in (?P<seconds>[\d.]+)s"
)


def _parse_summary(output: str) -> dict:
    for line in reversed(output.splitlines()):
        m = _SUMMARY_RE.search(line)
        if m:
            d = m.groupdict()
            return {k: (int(v) if v and k != "seconds" else (float(v) if v else 0)) for k, v in d.items()}
    return {}


def run(repeat: int = 1) -> dict:
    wall = Sample(name="test_suite.pytest_wall", unit="s")
    counts = None
    for _ in range(repeat):
        dt, proc = timed(
            subprocess.run, ["uv", "run", "pytest", "-q"], capture_output=True, text=True, timeout=1800
        )
        wall.values.append(dt)
        counts = _parse_summary(proc.stdout + "\n" + proc.stderr)
    result = wall.to_result()
    if counts:
        result["counts"] = counts
    return {"results": [result], "notes": [f"parsed pytest summary: {counts}"]}
