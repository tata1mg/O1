"""Reproducible fixtures shared across suites: a synthetic 5-stage pipeline (harness_overhead) and a
generated stream-json blob (stream_translate). Kept in one module so both BEFORE and AFTER runs regenerate
byte-identical inputs from the same seed instead of shipping a binary fixture that could drift silently.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# NB: imported lazily inside functions (not at module scope) so `import bench.fixtures` never requires
# rfc_agent to be importable — bench/__main__.py needs to print --help/usage even if a suite's target
# package isn't installed (e.g. the AFTER-only harness.engines.claude_translate hook).


def counting_engine(calls: list[tuple[str, float]]):
    """A Stage engine that writes every declared output instantly and records (stage_name, perf_counter
    at return) so callers can attribute inter-stage gaps to the harness (manifest write, digest, ledger
    render, decisions) rather than to engine work — the fake engine itself does no real work, matching
    tests/test_runner.py's fake_engine but with a timing hook."""
    import time as _time

    from rfc_agent.contracts import RunContext, Stage, StageResult

    def _engine(stage: Stage, ctx: RunContext) -> StageResult:
        for rel in stage.outputs:
            p = ctx.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"content of {rel}\n" * 20)  # non-trivial so digests hash real bytes
        calls.append((stage.name, _time.perf_counter()))
        return StageResult(final_text="done", cost_usd=0.01, usage={"input_tokens": 10},
                            duration_s=0.0, engine_desc="fake")

    return _engine


def make_synthetic_pipeline(n_stages: int = 5):
    """N stages, each requiring the previous stage's single output — a straight-line pipeline (no fan-out,
    no gate failures) so harness_overhead measures kernel plumbing, not stage-specific logic. Returns
    (stages, calls) where `calls` is filled in-place by the engine as the pipeline runs."""
    from rfc_agent.contracts import Stage

    calls: list[tuple[str, float]] = []
    engine = counting_engine(calls)
    stages = []
    for i in range(n_stages):
        name = f"s{i}"
        requires = ("inputs.json",) if i == 0 else (f"out/s{i - 1}.md",)
        outputs = (f"out/{name}.md",)
        stages.append(Stage(
            name=name, requires=requires, outputs=outputs,
            build_prompt=lambda ctx: "p", gate=lambda ctx: [], engine=engine,
        ))
    return stages, calls


def make_run_dir(tmp_root: Path) -> Path:
    run_dir = tmp_root / f"run-{time.time_ns()}"
    run_dir.mkdir(parents=True)
    (run_dir / "inputs.json").write_text("{}")
    return run_dir


# ---- stream-json fixture (AFTER-only stream_translate hook) --------------------------------------

def _assistant_tool_event(i: int) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": f"msg-{i}",
            "content": [
                {"type": "text", "text": f"Looking at file {i}, this is a routine read of a source file."},
                {"type": "tool_use", "name": "Read", "id": f"call-{i}",
                 "input": {"file_path": f"/repo/src/module_{i % 50}.py"}},
            ],
            "usage": {"input_tokens": 1200 + i, "cache_read_input_tokens": 800, "cache_creation_input_tokens": 0},
        },
        "timestamp": "2026-09-08T00:00:00Z",
    }


def _tool_result_event(i: int) -> dict:
    body = ("def handler():\n    return 'ok'\n" * 8) + f"\n# file {i}\n"
    return {
        "type": "user",
        "message": {"content": [{"type": "tool_result", "tool_use_id": f"call-{i}", "content": body}]},
    }


def generate_stream_json(path: Path, target_bytes: int = 5 * 1024 * 1024) -> Path:
    """Deterministic synthetic stream-json (event shapes lifted from src/rfc_agent/streams.py's parser:
    assistant/tool_use messages + user/tool_result echoes, closed by one terminal `result` event) grown to
    at least target_bytes so stream_translate has a reproducible, regeneratable large-file fixture instead
    of a committed binary. Same seed (deterministic content, no randomness) => byte-identical across runs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        i = 0
        written = 0
        init = {"type": "system", "subtype": "init", "model": "claude-sonnet-5", "session_id": "bench-session"}
        line = json.dumps(init) + "\n"
        fh.write(line)
        written += len(line)
        while written < target_bytes:
            for ev in (_assistant_tool_event(i), _tool_result_event(i)):
                line = json.dumps(ev) + "\n"
                fh.write(line)
                written += len(line)
            i += 1
        result = {"type": "result", "is_error": False, "result": "done", "total_cost_usd": 1.23,
                   "usage": {"input_tokens": 100000, "output_tokens": 5000}, "num_turns": i}
        fh.write(json.dumps(result) + "\n")
    return path
