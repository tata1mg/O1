"""End-to-end canary for the PostToolUse feedback channel — proven once per (machine, CLI version).

Why (v2 measurement run, 2026-09-03): CLI 2.1.139 silently changed how a PostToolUse hook's feedback
reaches the model. Both hooks kept running and passing their unit tests; a full 25-minute compose ran
with zero per-edit feedback delivered, and the loss was only discovered afterwards by transcript
archaeology. A script-level self-test would have passed — the break was on the CLI side of the
contract. So this canary goes through the CLI: a real `claude -p` under the stage's own settings
performs ONE Write that crosses contract.md's first pacing threshold (40% of 10,240 bytes), and the
hook's report must come back out of the model's mouth.

Cost discipline: the failure class is (CLI version × hook contract), so a success is cached per CLI
version under ~/.cache/rfc-agent/hooks_alive/ — the probe costs ~20-40s and a cent or two once per
CLI upgrade, not per run. Failures are never cached (a dead channel should keep screaming; a transient
API hiccup should not stick). A failed canary WARNS and records a decision — it never blocks the run,
because a dead feedback channel degrades quality, it does not break correctness.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from . import decisions
from .constants import Decision
from .contracts import RunContext

_CACHE = Path.home() / ".cache" / "rfc-agent" / "hooks_alive"
# every byte_budget report opens with this — the phrase the model can only know if the hook's
# additionalContext actually reached it (hook stdout is never echoed into the stream). Not "BYTE BUDGET
# pacing" alone: a model that overshoots the 800 lines crosses the ceiling and gets the "PAST" report.
_MARK = "BYTE BUDGET"
_MODEL = "claude-haiku-4-5-20251001"  # cheapest capable model; the channel under test is CLI-side


def cli_version(claude_bin: str = "claude") -> str:
    try:
        out = subprocess.run([claude_bin, "--version"], capture_output=True, text=True, timeout=20).stdout
        return (out.strip().split() or ["unknown"])[0]
    except Exception:  # noqa: BLE001 — any failure to identify the CLI means "do not probe"
        return "unknown"


def ensure_hooks_alive(ctx: RunContext, settings_path: Path, claude_bin: str = "claude",
                       model: str | None = None) -> bool | None:
    """True = feedback reached the model; False = it did not (warned + recorded, run continues);
    None = already proven on this CLI version (cached), nothing spawned."""
    version = cli_version(claude_bin)
    if version == "unknown":
        return None  # no identifiable CLI (or a faked one under test): nothing to prove or cache against
    marker = _CACHE / f"{version}.json"
    if marker.exists():
        return None
    scratch = ctx.run_dir / "logs" / "hook_canary"
    (scratch / "out").mkdir(parents=True, exist_ok=True)
    (scratch / "logs").mkdir(exist_ok=True)
    mcp = scratch / "mcp.json"
    mcp.write_text('{"mcpServers": {}}')
    (scratch / "logs" / "byte_budget.json").unlink(missing_ok=True)
    target = scratch / "out" / "contract.md"
    prompt = (f"Use the Write tool exactly once to create {target} containing the word hello on each of "
              f"800 lines. After the write, if any system message about that file's byte budget reaches "
              f"you, reply with its first line verbatim; otherwise reply exactly NONE.")
    cmd = [claude_bin, "-p", "--output-format", "stream-json", "--verbose",
           "--permission-mode", "acceptEdits", "--setting-sources", "project",
           "--settings", str(settings_path), "--strict-mcp-config", "--mcp-config", str(mcp),
           "--model", model or _MODEL, prompt]
    # byte_budget stays silent unless RFC_AGENT_GENERATE_BYTE_BUDGET is on (off by default since 2026-09-07),
    # and a silent hook can never put the mark in the model's mouth: without this the canary failed on
    # every CLI, was never cached, and re-ran (and re-warned) on every run.
    env = {**os.environ, "RFC_AGENT_RUN_DIR": str(scratch), "RFC_AGENT_GENERATE_BYTE_BUDGET": "1"}
    t0 = time.monotonic()
    error = ""
    out = ""
    try:
        # stdin=DEVNULL: `claude -p` otherwise waits on whatever stdin it inherits — the operator's terminal
        # when the run is interactive (2026-11-02: hit the 240s timeout before the Write, hook never ran)
        proc = subprocess.run(cmd, cwd=scratch, env=env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=240)
        out = proc.stdout or ""
        if proc.returncode:
            error = (proc.stderr or "")[-300:]
    except Exception as e:  # noqa: BLE001 — a canary must never take the run down with it
        error = str(e)[:300]
    secs = round(time.monotonic() - t0, 1)
    hook_ran = (scratch / "logs" / "byte_budget.json").exists()
    alive = _MARK in out
    detail = {"cli_version": version, "alive": alive, "hook_ran": hook_ran, "seconds": secs,
              "ts": datetime.now(UTC).isoformat(timespec="seconds"), **({"error": error} if error else {})}
    if alive:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(json.dumps(detail))
        msg = f"hook canary: PostToolUse feedback reaches the model on CLI {version} ({secs}s) — cached"
    elif hook_ran:
        msg = (f"hook canary FAILED on CLI {version}: the hook RAN (state written) but its feedback never "
               f"reached the model — the CLI-side delivery contract changed again; per-edit lints and "
               f"byte pacing are DEAD this run. Check the CLI changelog for PostToolUse changes.")
    else:
        msg = (f"hook canary FAILED on CLI {version}: the hook did not run at all"
               f"{' — ' + error if error else ''}; per-edit lints and byte pacing are DEAD this run.")
    from .engine import log_activity  # local: engine imports this module

    log_activity(ctx.run_dir, "harness", msg)
    decisions.record(ctx, "harness", Decision.HOOK_CANARY, why=msg, **detail)
    if not alive:
        print(f"  [harness] WARNING — {msg}", file=sys.stderr)
    return alive
