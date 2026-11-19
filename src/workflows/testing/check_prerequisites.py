"""`check_prerequisites` — the `prerequisites` gate, runnable by the model from inside the stage.

The same reason as `check_suite`: a model that checks its own JSON with hand-rolled jq spends a full
context re-read per probe, and the sandbox denies the compound forms. This prints the gate's verdict
on `qa/prerequisites.json` in one call.

Read-only: it reads `qa/cases.json`, `qa/prerequisites.facts.json` and `qa/prerequisites.json` under the
run dir (the cwd, or the one path argument) and writes nothing. Allowlisted for `prerequisites` only.

    python check_prerequisites.py [run_dir]

Exit 0 when the gate would pass, 1 when it would fail, 2 when a file is missing or not JSON.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows.testing import prereqs  # noqa: E402
from workflows.testing._io import parse_json  # noqa: E402


def report(run_dir: Path) -> tuple[int, str]:
    """-> (exit code, text). Split from `main` so tests need no subprocess."""
    loaded = {}
    for rel, kind in (("qa/cases.json", list), ("qa/prerequisites.facts.json", dict),
                      ("qa/prerequisites.json", dict)):
        value, why = parse_json(run_dir / rel)
        if why or not isinstance(value, kind):
            return 2, f"{rel}: {why or 'not a JSON ' + ('list' if kind is list else 'object')}"
        loaded[rel] = value
    cases, facts, doc = loaded.values()
    failures = prereqs.validate(doc, cases, facts)
    counts = ", ".join(f"{s}={len(doc.get(s) or [])}" for s in (*prereqs.SECTIONS, "no_prerequisites"))
    lines = [f"{len(cases)} cases; {counts}", ""]
    if failures:
        lines += [f"GATE WOULD FAIL — {len(failures)} problem(s):"] + [f"  - {f}" for f in failures]
        return 1, "\n".join(lines)
    return 0, "\n".join(lines + ["GATE WOULD PASS"])


def main(argv: list[str]) -> int:
    run_dir = Path(argv[0]) if argv else Path.cwd()
    code, text = report(run_dir)
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
