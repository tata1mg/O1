"""`python -m harness.policy check` — the transport-neutral policy check.

See harness.policy's module docstring for the stdin/stdout contract. This entry point exists because
engines that cannot import Python in-process (Pi's TypeScript extension host) still need the ONE copy of
the policy; a ~50 ms subprocess is cheaper than a second implementation that drifts.

Exit code is ALWAYS 0, including for a malformed request: the verdict channel is stdout. A policy check
that crashes a run is worse than one that says "ask".
"""

from __future__ import annotations

import json
import sys

from . import ASK, Decision, Request, check


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__.strip())
        print("\nusage: python -m harness.policy check   (request JSON on stdin, verdict JSON on stdout)")
        return 0
    if args[0] != "check":
        print(json.dumps({"decision": ASK, "reason": f"unknown subcommand {args[0]!r}; expected 'check'"}))
        return 0
    raw = sys.stdin.read()
    try:
        obj = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as exc:
        print(json.dumps({"decision": ASK, "reason": f"malformed request JSON: {exc}"}))
        return 0
    if not isinstance(obj, dict):
        print(json.dumps({"decision": ASK, "reason": "request must be a JSON object"}))
        return 0
    try:
        decision = check(Request.from_json(obj))
    except Exception as exc:  # noqa: BLE001 — a policy crash must never kill the run
        decision = Decision(ASK, f"policy check failed: {type(exc).__name__}: {exc}")
    print(json.dumps(decision.as_dict()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
