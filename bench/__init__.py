"""Small, structural, reproducible benchmarks for the harness kernel (D10: no premature optimisation —
we measure to show no regression across the strangler migration, and where the harness spends time).

Run with `uv run python -m bench <suite> [--json out.json] [--repeat N]`. See docs/harness/BENCHMARKS.md
for method, the BEFORE/control numbers, and how to run the AFTER pass once the kernel lands.
"""
