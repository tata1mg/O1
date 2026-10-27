#!/usr/bin/env python3
"""A fake `axon` launcher. Proves the Axon backend without a sign-in: `axon pi ...` replays a Pi stream.

What a real `axon pi --mode json -p` does that matters to the adapter, and nothing else:

  1. requires `pi` as the first argument (everything after it is Pi's argv);
  2. reads the prompt from stdin TO EOF (real Pi blocks until stdin closes — a caller that forgets to close
     it hangs, which is exactly the bug this fake would expose by blocking too);
  3. dumps argv, the prompt and the harness env contract to $FAKE_AXON_ARGV;
  4. replays a Pi `--mode json` stream (fixture or $FAKE_AXON_STREAM), with the session id from
     `--session-id` (or from the `--session <file>` name on a resume) substituted;
  5. writes the session JSONL under `--session-dir` so cost summation runs against a real file;
  6. creates $FAKE_AXON_OUTPUTS (colon-separated run-dir-relative files).
Also `--version` (prints a launcher version) and `auth status` / `doctor pi` for the adapter's doctor().
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def flag(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
    return None


def main() -> int:
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print("0.2.6")
        return 0
    if argv[:2] == ["auth", "status"]:
        print("Axon session is valid.")
        return int(os.environ.get("FAKE_AXON_AUTH_EXIT", "0"))
    if argv[:2] == ["doctor", "pi"]:
        print("[info] pi: version 1.4.0 (pinned)")
        return 0
    if argv[:1] != ["pi"]:
        print(f"fake axon: unexpected argv {argv}", file=sys.stderr)
        return 2
    pi = argv[1:]
    prompt = sys.stdin.read()
    resume = flag(pi, "--session")
    session_id = flag(pi, "--session-id") or (Path(resume).stem.split("_")[-1] if resume else "no-id")
    dump = os.environ.get("FAKE_AXON_ARGV")
    if dump:
        keep = ("RFC_TOOLS_CONFIG", "HARNESS_POLICY_JSON", "HARNESS_POLICY_CMD", "HARNESS_DENIALS_PATH",
                "INVESTIGATE_RUN_DIR", "INVESTIGATE_WRITE_ALLOW", "INVESTIGATE_CALL_BUDGET", "PI_OFFLINE",
                "PI_CODING_AGENT_DIR", "PYTHONPATH")
        Path(dump).write_text(json.dumps({"argv": sys.argv, "prompt": prompt, "cwd": os.getcwd(),
                                          "env": {k: os.environ.get(k) for k in keep}}))
    stream = Path(os.environ.get("FAKE_AXON_STREAM") or HERE / "pi.sample.jsonl")
    lines = [ln.replace("__SID__", session_id) for ln in stream.read_text().splitlines() if ln.strip()]
    session_dir = flag(pi, "--session-dir")
    if session_dir:
        target = Path(resume) if resume else Path(session_dir) / f"2026-10-27T00-00-00-000Z_{session_id}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8") as f:
            for ln in lines:
                ev = json.loads(ln)
                if ev.get("type") == "message_end":
                    f.write(json.dumps({"type": "message", "message": ev["message"]}) + "\n")
    for rel in filter(None, os.environ.get("FAKE_AXON_OUTPUTS", "").split(":")):
        p = Path(os.getcwd()) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(rel)
    for ln in lines:
        print(ln, flush=True)
    return 0  # like real pi --mode json: exit 0 even when the stream says the agent failed


if __name__ == "__main__":
    sys.exit(main())
