"""`check_verdict` — the `execute_ui_agent` verdict check, runnable by the agent on its own verdict.

The same reason as `check_ui` and `check_suite`: a verdict the stage rejects is a case's whole session
lost, and the agent that wrote it is the only one who can put it right. In the third pilot an agent
judged five clauses, spelled one state `not_reached`, and the run recorded an ERROR with nothing in it —
it was never told. One call here prints exactly what the stage would reject, while there is still a
session to fix it in.

    python check_verdict.py qa/agent/<case id>        (from the run dir)

Reads `verdict.json` in that folder and checks it against the evidence files beside it. Writes nothing.
Exit 0 when the stage would accept it, 1 when it would not, 2 when the file is missing or not JSON.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows.testing import ui_agent  # noqa: E402
from workflows.testing._io import parse_json  # noqa: E402


def report(folder: Path) -> tuple[int, str]:
    """-> (exit code, text). Split from `main` so tests need no subprocess."""
    verdict, why = parse_json(folder / ui_agent.VERDICT)
    if why:
        return 2, f"{folder.name}/{ui_agent.VERDICT}: {why}"
    extid = folder.name.removesuffix(".confirm")
    problems = ui_agent.validate_verdict(verdict, extid, folder)
    if problems:
        return 1, "\n".join([f"REJECTED — {len(problems)} problem(s). Fix {ui_agent.VERDICT} and run this again:",
                             *[f"- {p}" for p in problems]])
    waiting = [c["text"] for p in verdict["parts"] for c in p["expected"] if c.get("blocker") == ui_agent.PENDING]
    line = f"OK — {ui_agent.summary(verdict)}"
    if waiting:
        line += (f"\nNOT FINISHED: {len(waiting)} clause(s) still say '{ui_agent.PENDING}'. That is right while "
                 f"you are working; before you stop, judge each or give its real blocker.")
    return 0, line


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: check_verdict.py qa/agent/<case id>")
        return 2
    code, text = report(Path(args[0]))
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
