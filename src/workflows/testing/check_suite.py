"""`check_suite` — the `generate_cases` gate, runnable by the model from inside the stage.

Measured on the NR-DIGICHEM run (2026-11-02): after writing the suite the generator spent 13 turns on
hand-rolled jq/grep checks (ids, scenario<->case links, duplicates, Negative/Edge per module, REQ refs),
4 of them denied as compound or "dangerous" shell forms. Every turn re-reads the whole ~210k-token
context, so those checks were ~2.7M of the stage's 3.9M cached-input tokens, and they repeated what the
gate checks anyway. This prints the gate's own verdict in one call.

Read-only by construction: it reads `qa/cases.json` and `qa/scenarios.json` under the run dir (the cwd,
or the one path argument) and writes nothing. It is allowlisted for `generate_cases` only, as an exact
interpreter + script prefix (Stage.allow_commands).

    python check_suite.py [run_dir] [--allow-unrealised]

Exit 0 when the gate would pass, 1 when it would fail, 2 when a file is missing or not JSON.
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows.testing._io import parse_json  # noqa: E402
from workflows.testing.stages.generate_cases import suite_failures  # noqa: E402


def report(run_dir: Path, *, require_case_per_scenario: bool = True) -> tuple[int, str]:
    """-> (exit code, text). Split from `main` so tests need no subprocess."""
    loaded = {}
    for name in ("cases", "scenarios"):
        value, why = parse_json(run_dir / "qa" / f"{name}.json")
        if why or not isinstance(value, list):
            return 2, f"qa/{name}.json: {why or 'not a JSON list'}"
        loaded[name] = value
    cases, scenarios = loaded["cases"], loaded["scenarios"]
    failures = suite_failures(cases, scenarios, require_case_per_scenario=require_case_per_scenario)
    by_exec = Counter(str(c.get("execution") or "?") for c in cases)
    by_module = Counter(str(c.get("module") or "?") for c in cases)
    cited = {m for c in cases for m in re.findall(r"\bREQ-\d+\b", str(c.get("requirement") or ""))}
    reqs = sorted(cited, key=lambda r: int(r.split("-")[1]))
    lines = [
        f"{len(cases)} cases, {len(scenarios)} scenarios",
        "execution: " + ", ".join(f"{k}={v}" for k, v in sorted(by_exec.items())),
        "modules: " + ", ".join(f"{k}={v}" for k, v in sorted(by_module.items())),
        f"requirements cited ({len(reqs)}): " + (", ".join(reqs) or "none"),
        "",
    ]
    if failures:
        lines += [f"GATE WOULD FAIL — {len(failures)} problem(s):"] + [f"  - {f}" for f in failures]
        return 1, "\n".join(lines)
    return 0, "\n".join(lines + ["GATE WOULD PASS"])


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    run_dir = Path(args[0]) if args else Path.cwd()
    code, text = report(run_dir, require_case_per_scenario="--allow-unrealised" not in argv)
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
