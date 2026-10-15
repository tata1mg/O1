#!/usr/bin/env python3
"""A fake `pi` binary. Proves the adapter without an API key: it emits recorded events.

It does four things a real `pi --mode json -p` does, and nothing else:

  1. dumps its own argv (and a copy of the harness env contract) to $FAKE_PI_ARGV so tests can assert
     exactly what the adapter built;
  2. replays a recorded event stream on stdout, substituting the session id the adapter minted;
  3. writes the session JSONL where `--session-dir` + `--session-id` say it will be, so cost summation
     runs against the real path shape (`--<mangled-cwd>--/<timestamp>_<id>.jsonl`);
  4. creates the files the stage was supposed to produce ($FAKE_PI_OUTPUTS).

It deliberately reproduces pi 0.85.1's exit-code behaviour: **always exit 0**, even when the replayed
stream says the agent errored.

Knobs (all env, so nothing about the adapter has to know this file exists):
  FAKE_PI_ARGV      path to write {"argv": [...], "env": {...}, "cwd": "..."} as JSON
  FAKE_PI_STREAM    path to the recorded stream to replay (default: session.sample.jsonl beside this file)
  FAKE_PI_OUTPUTS   colon-separated run-dir-relative files to create (content: the file name)
  FAKE_PI_DENIALS   JSON list of {tool, toolCallId, reason} to append to $HARNESS_DENIALS_PATH, i.e. what
                    the guard extension would have written when it blocked a call
  FAKE_PI_SLEEP     seconds to sleep BEFORE finishing, after spawning a grandchild (timeout/kill tests)
  FAKE_PI_CHILD_PID path where the grandchild's pid is written (so a test can assert the group died)
  FAKE_PI_CHILD_TRAPS_TERM "1" -> the grandchild IGNORES SIGTERM and inherits stdout, which is what pi's
                    bash tool really spawns (node dev servers, npm). This is the shape that used to wedge
                    the reader forever: the leader dies politely, the grandchild holds the pipe open.
  FAKE_PI_STDERR    text to write to stderr (e.g. "Extension error (guard.js): boom")
  FAKE_PI_NO_STREAM "1" -> print nothing at all (a child that dies before its header)
  FAKE_PI_STDOUT_NOISE a non-JSON line to print on stdout before the stream (pi surprises happen)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Flags the adapter MUST pass for a stage to be contained and hermetic. Asserted here as well as in the
# python tests: a fake that quietly accepts a missing --no-extensions would let a real regression through.
REQUIRED = ("--mode", "-p", "--session-dir")
REQUIRED_ENV = ("HARNESS_POLICY_JSON", "HARNESS_POLICY_CMD", "HARNESS_DENIALS_PATH")


def flag_value(argv: list[str], name: str) -> str | None:
    for i, a in enumerate(argv):
        if a == name and i + 1 < len(argv):
            return argv[i + 1]
        if a.startswith(name + "="):
            return a.split("=", 1)[1]
    return None


def mangle(cwd: str) -> str:
    """Pi's session sub-directory name: drop the leading separator, replace / \\ : with -."""
    return "--" + cwd.lstrip("/").replace("/", "-").replace("\\", "-").replace(":", "-") + "--"


def main() -> int:
    argv = sys.argv[1:]
    cwd = os.getcwd()

    if "--version" in argv or "-v" in argv:
        # answered before anything else so a doctor() probe neither trips the REQUIRED check nor
        # overwrites the argv dump a run test is asserting on
        print(os.environ.get("FAKE_PI_VERSION") or "0.85.1")
        return 0

    dump = os.environ.get("FAKE_PI_ARGV")
    if dump:
        Path(dump).write_text(
            json.dumps(
                {
                    "argv": argv,
                    "cwd": cwd,
                    "env": {k: v for k, v in os.environ.items()
                            if k.startswith(("HARNESS_", "PI_")) or k == "PYTHONPATH"},
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    missing = [f for f in REQUIRED if f not in argv]
    missing += [e for e in REQUIRED_ENV if e not in os.environ]
    if missing:
        # exit 2, NOT 0: this is the fake refusing a malformed invocation, not pi reporting an agent error
        print(f"fake_pi: adapter did not pass {missing}", file=sys.stderr)
        return 2

    session_dir = flag_value(argv, "--session-dir") or cwd
    session_id = flag_value(argv, "--session-id")
    if not session_id:
        # `--session <path|partial-id>`: real pi loads that file and takes the id from its header, so a
        # resume keeps the ORIGINAL id rather than inventing one from the argv string
        resumed = flag_value(argv, "--session")
        session_id = "unknown-session"
        if resumed:
            session_id = resumed
            p = Path(resumed)
            if p.exists():
                with open(p, encoding="utf-8") as fh:
                    header = json.loads(fh.readline() or "{}")
                session_id = header.get("id") or session_id

    if os.environ.get("FAKE_PI_STDERR"):
        print(os.environ["FAKE_PI_STDERR"], file=sys.stderr, flush=True)

    for rel in (os.environ.get("FAKE_PI_OUTPUTS") or "").split(":"):
        if rel.strip():
            p = Path(cwd) / rel.strip()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"produced by fake_pi for {rel.strip()}\n", encoding="utf-8")

    denials_path = os.environ.get("HARNESS_DENIALS_PATH")
    for rec in json.loads(os.environ.get("FAKE_PI_DENIALS") or "[]"):
        with open(denials_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "harness_denial", "ts": time.time(), **rec}) + "\n")
            fh.flush()

    stream = Path(os.environ.get("FAKE_PI_STREAM") or (HERE / "session.sample.jsonl"))
    lines: list[str] = []
    if os.environ.get("FAKE_PI_NO_STREAM") != "1":
        for line in stream.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            lines.append(line.replace("__SESSION_ID__", session_id).replace("__CWD__", cwd))

    # the session JSONL: header + every message_end's message, which is what a real pi persists and what
    # `pi_translate.session_cost` sums
    sess = Path(session_dir) / mangle(cwd) / f"{datetime.now(UTC):%Y%m%d-%H%M%S}_{session_id}.jsonl"
    sess.parent.mkdir(parents=True, exist_ok=True)
    with open(sess, "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "session", "version": 3, "id": session_id, "cwd": cwd}) + "\n")
        for i, line in enumerate(lines):
            obj = json.loads(line)
            if obj.get("type") == "message_end":
                fh.write(json.dumps({"type": "message", "id": f"e{i}", "parentId": None,
                                     "message": obj["message"]}) + "\n")

    child_pid_path = os.environ.get("FAKE_PI_CHILD_PID")
    sleep_s = float(os.environ.get("FAKE_PI_SLEEP") or 0)
    if sleep_s:
        # a grandchild in OUR process group, standing in for pi's bash tool: if the adapter kills only the
        # direct child this survives, and the test catches the orphan. It INHERITS stdout, so it also holds
        # the read pipe open — the property that turns a missed SIGKILL into a hang rather than a leak.
        if os.environ.get("FAKE_PI_CHILD_TRAPS_TERM") == "1":
            code = ("import signal, time\n"
                    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                    f"time.sleep({int(sleep_s) + 300})\n")
            child = subprocess.Popen([sys.executable, "-c", code])
        else:
            child = subprocess.Popen(["sleep", str(int(sleep_s) + 60)])
        if child_pid_path:
            Path(child_pid_path).write_text(str(child.pid), encoding="utf-8")

    if os.environ.get("FAKE_PI_STDOUT_NOISE"):
        print(os.environ["FAKE_PI_STDOUT_NOISE"], flush=True)
    for line in lines:
        print(line, flush=True)

    if sleep_s:
        time.sleep(sleep_s)
    return 0  # ALWAYS 0 — pi --mode json exits 0 even on agent error (research report §8.3)


if __name__ == "__main__":
    raise SystemExit(main())
