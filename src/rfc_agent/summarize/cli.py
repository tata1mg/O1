"""Summarize — a concise, human-readable copy of the final RFC. Design: docs/SUMMARIZE.md.

    python -m rfc_agent.summarize --run-dir <run dir after Revise> [--fresh]

One fresh agent reads only out/rfc.md and writes out/rfc.readable.md. Not reviewed, not gated: the only check is
that the file was written (one retry). Runs automatically at the end of Revise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from pathlib import Path

from .. import observe
from ..config import get_config
from ..investigate import agent as ag
from ..investigate.cli import _mmss, _read
from ..investigate.progress import Progress
from ..legacy_names import config_section, migrate_run_dir

PROMPT = Path(__file__).with_name("prompts") / "readable.md"
DEFAULTS = {"MODEL": "claude-sonnet-5-5", "EFFORT": "medium", "TIMEOUT_S": 600, "SIZE_TARGET_KB": "6–12"}
OUT = "out/rfc.readable.md"


def settings(cfg) -> dict:
    user = {k: v for k, v in config_section(cfg, "SUMMARIZE").items() if not str(k).startswith("_")}
    return {**DEFAULTS, **user}


@observe.entry("summarize")
def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m rfc_agent.summarize",
                                description="Summarize: readable copy of the RFC.")
    p.add_argument("--run-dir", required=True, help="the run folder, after Revise")
    p.add_argument("--fresh", action="store_true", help="write it again even if it is up to date")
    from ..axon_agent import add_engine_args, apply_engine_args

    add_engine_args(p)
    args = p.parse_args(argv)
    apply_engine_args(args.engine, args.stage_engine)
    run_dir = Path(args.run_dir).expanduser().resolve()
    migrate_run_dir(run_dir)
    rfc = run_dir / "out" / "rfc.md"
    if not rfc.exists():
        print(f"summarize: {rfc} is missing — run Draft, Review and Revise first", file=sys.stderr)
        return 1
    out = run_dir / OUT
    state_path = run_dir / "work" / "summarize.state.json"
    state = _read(state_path) or {}
    src_hash = hashlib.sha256(rfc.read_bytes()).hexdigest()[:16]
    prog = Progress(run_dir, stage="summarize")
    if not args.fresh and out.exists() and state.get("source_hash") == src_hash:
        prog.milestone("skip", f"Readable copy is up to date → {out}")
        return 0
    cfg = get_config()
    s = settings(cfg)
    t0 = time.monotonic()
    prog.step("5")
    prog.milestone("start", "Summarize: writing the readable copy")
    out.unlink(missing_ok=True)
    spec = ag.AgentSpec(
        "readable",
        s["MODEL"],
        s["EFFORT"],
        ("Read", "Write", "Edit"),
        (OUT,),
        use_context=False,
        timeout_s=int(s["TIMEOUT_S"]),
    )
    prompt = PROMPT.read_text(encoding="utf-8").replace("{{SIZE_TARGET_KB}}", str(s["SIZE_TARGET_KB"]))
    runs = []
    for attempt in (1, 2):
        r = ag.run(
            spec, prompt, run_dir, cfg, on_event=prog.tick, stage="summarize", step="run" if attempt == 1 else "retry"
        )
        runs.append({"ok": r.ok, "seconds": r.seconds, "cost_usd": round(r.cost_usd, 4), "error": r.error})
        if out.exists():
            break
    secs = round(time.monotonic() - t0, 1)
    cost = sum(x["cost_usd"] for x in runs)
    work = run_dir / "work"
    work.mkdir(exist_ok=True)
    if not out.exists():
        (run_dir / "logs").mkdir(exist_ok=True)
        (run_dir / "logs" / "summarize.json").write_text(json.dumps({"ok": False, "runs": runs}, indent=2))
        prog.milestone("failed", f"Summarize: the readable copy was not written ({runs[-1]['error'] or 'no file'})")
        print("summarize: the readable copy was not written", file=sys.stderr)
        return 1
    state_path.write_text(json.dumps({"source_hash": src_hash}, indent=2), encoding="utf-8")
    kb = round(len(out.read_bytes()) / 1024, 1)
    full_kb = round(len(rfc.read_bytes()) / 1024, 1)
    (run_dir / "logs" / "summarize.json").write_text(
        json.dumps(
            {
                "ok": True,
                "settings": s,
                "seconds": secs,
                "cost_usd": round(cost, 3),
                "size_kb": kb,
                "full_rfc_kb": full_kb,
                "runs": runs,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    prog.milestone(
        "done", f"Readable copy written: {kb} KB (full RFC {full_kb} KB) · {_mmss(secs)}, ${cost:.2f} → {out}"
    )
    prog.step("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
