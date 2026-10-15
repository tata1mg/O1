"""Suite 1: `rfc_agent.runner.run_pipeline` overhead with a fake engine over a synthetic 5-stage pipeline.

Measures the KERNEL, not the model: the engine is instant (writes its declared outputs and returns), so
whatever time is left is manifest writes, digest hashing, ledger render, decision recording, run-trace
bookkeeping and the resume/green-skip scan. Obs is disabled by construction — no LANGFUSE_* keys are set
in this process, and rfc_agent.obs prints '[obs] langfuse disabled' and no-ops (see obs.py:159,226-234) —
so obs overhead here is the no-op branch's cost, not a real exporter's.

Per-stage attribution: the fake engine records perf_counter() the instant it returns for each stage. The
gap between stage i's engine-return and stage i+1's engine-return is exactly the harness work done AFTER
stage i finished (gate check, manifest write, digest, ledger render, decision recording) PLUS the harness
work done BEFORE stage i+1 starts (green-skip check, resume scan) — the two aren't separable without
patching runner.py internals, which we deliberately don't do (D10: measure the kernel as shipped). The
first stage's harness overhead is (its engine-return time - pipeline start); the last stage's tail overhead
(finish_run, trace close) is (pipeline end - last stage's engine-return).
"""

from __future__ import annotations

import shutil
import tempfile
import time
from pathlib import Path

from bench.fixtures import make_run_dir, make_synthetic_pipeline
from bench.timing import Sample

N_STAGES = 5


def _run_once(tmp_root: Path) -> tuple[float, list[float]]:
    from rfc_agent.contracts import RunContext
    from rfc_agent.runner import run_pipeline

    run_dir = make_run_dir(tmp_root)
    stages, calls = make_synthetic_pipeline(N_STAGES)
    ctx = RunContext(run_dir=run_dir, inputs={"repos_root": str(run_dir)}, config=None)

    t0 = time.perf_counter()
    run_pipeline(stages, ctx)
    t1 = time.perf_counter()

    gaps = []
    prev = t0
    for _name, t in calls:
        gaps.append(t - prev)
        prev = t
    gaps.append(t1 - prev)  # tail: finish_run + trace close after the last stage
    return t1 - t0, gaps


def run(repeat: int = 20) -> dict:
    full = Sample(name="harness_overhead.full_pipeline", unit="s")
    per_stage: list[Sample] = [Sample(name=f"harness_overhead.stage_gap[{i}]", unit="s") for i in range(N_STAGES)]
    tail = Sample(name="harness_overhead.tail_after_last_stage", unit="s")

    with tempfile.TemporaryDirectory(prefix="bench-harness-overhead-") as tmp:
        tmp_root = Path(tmp)
        for _ in range(repeat):
            wall, gaps = _run_once(tmp_root)
            full.values.append(wall)
            for i in range(N_STAGES):
                per_stage[i].values.append(gaps[i])
            tail.values.append(gaps[N_STAGES])
        shutil.rmtree(tmp_root, ignore_errors=True)

    results = [full.to_result()] + [s.to_result() for s in per_stage] + [tail.to_result()]
    return {"results": results, "notes": [
        f"n_stages={N_STAGES}, engine=fake (instant), obs=disabled (no LANGFUSE_* keys in this process)",
    ]}
