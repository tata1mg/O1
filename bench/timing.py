"""Repeat-and-summarize timing helpers. No deps beyond stdlib — this package runs standalone in the
BEFORE worktree (a pristine origin/main checkout) where nothing besides the repo's own deps is installed.
"""

from __future__ import annotations

import statistics
import time
from dataclasses import dataclass, field


@dataclass
class Sample:
    """One named measurement's repeated wall-time observations, in seconds."""

    name: str
    unit: str
    values: list[float] = field(default_factory=list)

    def to_result(self) -> dict:
        vs = sorted(self.values)
        n = len(vs)
        if n == 0:
            return {"name": self.name, "unit": self.unit, "n": 0, "median": None, "p95": None,
                     "min": None, "max": None}
        p95_idx = min(n - 1, max(0, round(0.95 * (n - 1))))
        return {
            "name": self.name,
            "unit": self.unit,
            "n": n,
            "median": statistics.median(vs),
            "p95": vs[p95_idx],
            "min": vs[0],
            "max": vs[-1],
        }


def timed(fn, *args, **kwargs) -> tuple[float, object]:
    """Run fn once, return (wall_seconds, return_value). Uses perf_counter — monotonic, unaffected by
    wall-clock adjustments, the standard choice for microbenchmarking in CPython."""
    t0 = time.perf_counter()
    rv = fn(*args, **kwargs)
    return time.perf_counter() - t0, rv


def repeat(name: str, fn, n: int, unit: str = "s") -> Sample:
    """Call fn() n times (no args — callers close over their fixtures), collecting each wall time."""
    s = Sample(name=name, unit=unit)
    for _ in range(n):
        dt, _ = timed(fn)
        s.values.append(dt)
    return s


def print_table(results: list[dict]) -> None:
    """Fixed-width text table: name, n, median, p95, min, max — one row per Sample.to_result()."""
    if not results:
        print("(no results)")
        return
    name_w = max(len("name"), max(len(r["name"]) for r in results))
    header = f"{'name':<{name_w}}  {'unit':>4}  {'n':>3}  {'median':>10}  {'p95':>10}  {'min':>10}  {'max':>10}"
    print(header)
    print("-" * len(header))
    for r in results:
        unit = r["unit"]

        def fmt(v, unit=unit):
            return "n/a" if v is None else f"{v:.4f}" if unit == "s" else f"{v:.2f}"

        print(f"{r['name']:<{name_w}}  {r['unit']:>4}  {r['n']:>3}  {fmt(r['median']):>10}  "
              f"{fmt(r['p95']):>10}  {fmt(r['min']):>10}  {fmt(r['max']):>10}")
