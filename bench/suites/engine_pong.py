"""Suite 5: live, tiny-cost roundtrip against the real `claude` CLI — the cheapest possible signal that
the actual model-invocation path (not the fake engine) still works end to end, and its baseline latency.

Runs `claude -p "reply with the single word pong" --output-format json --max-turns 1 --model
claude-haiku-4-5-20251001` 3 times (haiku, 1 turn, ~nothing of substance to say — this is the cheapest
model+prompt pair available and is a real spend, not fake).

AFTER-state hook: if `harness.engines.claude_code` is importable, also runs the same prompt through the
harness's Claude adapter for comparison. It does not exist yet (src/harness/engines/ has only
__init__.py as of this worktree's HEAD) so this degrades to 'not available'.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from bench.timing import Sample, timed

N_RUNS = 3
MODEL = "claude-haiku-4-5-20251001"
PROMPT = "reply with the single word pong"


def _one_pong() -> dict:
    dt, proc = timed(
        subprocess.run,
        ["claude", "-p", PROMPT, "--output-format", "json", "--max-turns", "1", "--model", MODEL],
        capture_output=True, text=True, timeout=120,
    )
    row = {"wall_s": dt, "returncode": proc.returncode}
    try:
        data = json.loads(proc.stdout)
        row["cost_usd"] = data.get("total_cost_usd")
        row["usage"] = data.get("usage")
        row["is_error"] = data.get("is_error")
    except (json.JSONDecodeError, TypeError):
        row["parse_error"] = True
        row["stderr_tail"] = proc.stderr[-500:]
    return row


def run(repeat: int = 3) -> dict:
    n = repeat if repeat else N_RUNS
    wall = Sample(name="engine_pong.claude_cli_wall", unit="s")
    rows = []
    total_cost = 0.0
    for _ in range(n):
        row = _one_pong()
        rows.append(row)
        wall.values.append(row["wall_s"])
        total_cost += row.get("cost_usd") or 0.0

    results = [wall.to_result()]
    notes = [f"model={MODEL}, n={n}, total_cost_usd={total_cost:.4f}", f"raw_rows={rows}"]

    try:
        from harness.engines import EngineRequest, claude_code

        # AFTER: attempt the same prompt through the harness's Claude adapter, for comparison against
        # the bare-CLI wall time above. ClaudeCodeEngine.run() is importable now, but its _native() path
        # hard-requires native_ctx={"stage": Stage, "ctx": RunContext} (run_claude still owns MCP
        # materialisation/effort resolution/session bookkeeping — see claude_code.py's module docstring),
        # so a bare EngineRequest without a real Stage/RunContext raises before any live call is made —
        # confirmed empirically (no cost incurred). Wiring a full pong-only Stage+RunContext is a heavier
        # integration than this suite's "cheapest possible roundtrip" scope intends; left as a TODO for
        # whoever builds a lightweight Stage/RunContext test fixture for this purpose.
        with tempfile.TemporaryDirectory(prefix="bench-pong-") as tmp:
            cwd = Path(tmp)
            eng = claude_code.ClaudeCodeEngine()
            req = EngineRequest(
                prompt=PROMPT, cwd=cwd, label="pong-probe",
                trace_path=cwd / "pong-probe.events.jsonl", timeout_s=120, model=MODEL,
            )
            try:
                eng.run(req)
                notes.append("harness_adapter: ran successfully (unexpected — update this suite's notes).")
            except ValueError as exc:
                notes.append(
                    "harness.engines.claude_code is importable but ClaudeCodeEngine.run() requires a "
                    f"real Stage/RunContext via native_ctx, not just prompt+cwd — confirmed: {exc}"
                )
    except ImportError:
        notes.append("harness.engines.claude_code not available — AFTER-only hook, skipped.")

    return {"results": results, "notes": notes}
