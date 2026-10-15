"""`uv run python -m bench <suite> [--json out.json] [--repeat N]` — see bench/__init__.py."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from bench.machine import envelope
from bench.suites import SUITE_NAMES, load
from bench.timing import print_table


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m bench", description=__doc__)
    ap.add_argument("suite", choices=[*SUITE_NAMES, "all"], help="which suite to run")
    ap.add_argument("--json", type=Path, default=None, help="write the result envelope as JSON to this path")
    ap.add_argument("--repeat", type=int, default=None, help="override the suite's default repeat count")
    args = ap.parse_args(argv)

    suite_names = list(SUITE_NAMES) if args.suite == "all" else [args.suite]
    envelopes = []
    for name in suite_names:
        mod = load(name)
        outcome = mod.run() if args.repeat is None else mod.run(args.repeat)
        print(f"\n=== {name} ===")
        print_table(outcome["results"])
        for note in outcome.get("notes", []):
            print(f"  note: {note}")
        env = envelope(name, outcome["results"])
        env["notes"] = outcome.get("notes", [])
        envelopes.append(env)

    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        payload = envelopes[0] if len(envelopes) == 1 else {"suites": envelopes}
        args.json.write_text(json.dumps(payload, indent=2, default=str))
        print(f"\nwrote {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
