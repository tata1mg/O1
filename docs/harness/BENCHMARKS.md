# Benchmarks

The benchmarks exist to make regressions *visible*, not to drive optimisation on their own. A delta only
means something once it clears the run-to-run noise band, and even then the question is "did this change
alter behaviour or cost", not "make it faster".

## What each suite measures, and what it deliberately does not

`bench/` is a standalone, pytest-independent package:

```bash
uv run python -m bench <suite> [--json out.json] [--repeat N]
uv run python -m bench all
make bench          # every suite, printed as tables
make bench-json     # one JSON envelope per suite under bench/results/after/
```

Six suites (`bench/suites/`):

| Suite | What it measures | What it explicitly excludes |
|---|---|---|
| `harness_overhead` | `run_pipeline` wall time over a synthetic 5-stage pipeline with a fake (instant) engine: manifest writes, digest hashing, ledger render, decision recording, resume/green-skip scan, trace open/close | Model latency (the engine never calls out); observability cost beyond the no-op branch (no `LANGFUSE_*` keys are set) |
| `gate_latency` | Wall time of the shared gate helpers (`check_citations`, `check_no_secrets`, `require_sections`) and the stage gates on minimal fixtures | Gates that need a live git-backed repos root; they would make fixture setup the dominant, non-reproducible cost |
| `cli_startup` | `rfc` and `harness` `--help` wall time, plus bare import time of the core modules | Anything beyond process and import cold-start |
| `test_suite` | `uv run pytest -q` wall time and the parsed pass/fail/skip counts. Repeat 1: one number per pass is enough to attach to a PR | A per-test breakdown (use `pytest --durations`) |
| `engine_pong` | Live `claude -p "reply with the single word pong" --output-format json --max-turns 1 --model claude-haiku-4-5-20251001` calls: wall time and real cost and usage off the JSON result, plus the same prompt through the `claude-code` adapter | Anything beyond a minimal roundtrip. This is a liveness and baseline-latency probe, not a model-quality eval |
| `stream_translate` | Time to translate a synthetic 5 MB recorded stream-json file into the canonical event schema via `harness.engines.claude_translate` | Nothing |

`harness_overhead` measures **the kernel, not the model**. Every stage's "work" is an instant file write, so
what is timed is exactly the plumbing `run_pipeline` does around a stage, independent of which engine or model
a real run would use.

## Method

- Each suite is repeated (defaults: `harness_overhead` ×20, `gate_latency` ×10, `cli_startup` ×10,
  `test_suite` ×1, `engine_pong` ×3) and reports `{n, median, p95, min, max}` per named measurement, plus a JSON
  envelope `{suite, git_sha, timestamp, machine, results, notes}`.
- Timing uses `time.perf_counter()` (monotonic, immune to wall-clock adjustments) via `bench/timing.py`.
- Fixtures are generated code (`bench/fixtures.py`), not committed binaries or captures against a live repo, so
  every run measures the same reproducible input: the synthetic pipeline and the stream-json blob are
  byte-identical every time.
- `harness_overhead`'s per-stage number is a **gap**, not an isolated cost: the fake engine records
  `perf_counter()` the instant it returns for each stage, and `stage_gap[i]` is the time between stage i's
  engine-return and stage i+1's engine-return, i.e. the harness work *after* stage i (gate, manifest, digest,
  ledger, decisions) plus the harness work *before* stage i+1 (green-skip and resume scan).
  `tail_after_last_stage` is `finish_run` plus trace close after the last stage returns. The suite measures the
  kernel as shipped, not as instrumented.
- Observability is disabled in every non-live suite by construction: no `LANGFUSE_*` env vars are set in the
  benchmark process, so tracing takes its documented no-op branch.

## Reading results

All numbers are machine-specific. Compare runs only on the same machine, in the same sitting, and judge on
**median and p95, never max** (a single cold-start call produces outliers).

Rules of thumb for what counts as noise on an idle development machine:

- **`harness_overhead.full_pipeline`:** treat anything under ±10% as noise.
- **`gate_latency`:** sub-millisecond measurements dominated by interpreter and regex-cache warmup. Treat
  anything under ±20% as noise. The suite is for catching an accidentally O(n²) gate, not micro-tuning.
- **`cli_startup`:** subprocess spawn cost dominates. Always compare the median.
- **`engine_pong`:** cost is not a stable signal at n=3, because cache-hit rates differ call to call.

To compare a change against a baseline, run `make bench-json` on the baseline commit and on your branch, and
diff the JSON envelopes.
