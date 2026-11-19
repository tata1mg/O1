"""How each attempt of a run ended, recorded by the run itself so nobody has to be asked.

`run.py` writes one line per attempt to logs/attempts.jsonl. This module adds what a stopped or failed attempt needs
for feedback and for charts:

    stop_kind        user_stop (Ctrl-C or a plain stop, exit 130) | failure (any other non-zero exit) | killed
                     (the process died without a chance to write anything: found on the next start or by feedback)
    failure_cause    a category from CAUSES, read from the last lines the run printed (a failure only)
    failure_excerpt  those lines with secrets scrubbed, so a person reading the score sees what was said

An attempt is also marked as open (logs/attempt_open.json) while it runs. A marker still there when the next attempt
starts, or when feedback is asked for, means the process was killed hard: no end line could be written.

`state()` is what `rfc_agent.feedback --context` shows: how the latest attempt ended, so the feedback form opens on the
right path (finished, stopped, failed) and only asks "how did it end?" when the run cannot say.
"""

from __future__ import annotations

import contextlib
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

ATTEMPTS_FILE = "attempts.jsonl"
OPEN_FILE = "attempt_open.json"
USER_STOP_EXIT = 130

CAUSES = (
    "usage_or_login_limit",
    "atlassian_not_connected",
    "figma_no_response",
    "context_layer_unavailable",
    "connection_error",
    "reviewers_failed",
    "other",
)

# First match wins, so the specific causes come before the general ones.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "usage_or_login_limit",
        re.compile(
            r"hit your .{0,30}limit|usage limit|rate.?limit|limit reached|please run /login|not logged in"
            r"|invalid api key"
            r"|token (?:has )?expired|session expired|\b401\b|authentication (?:failed|error)",
            re.I,
        ),
    ),
    (
        "atlassian_not_connected",
        re.compile(
            r"atlassian.{0,80}(?:not connected|needs? auth|unauthori[sz]ed|not available|not usable|failed"
            r"|disconnected)"
            r"|no atlassian mcp server is connected|tried \d+ fresh connections|atlassian did not answer"
            r"|(?:confluence|jira).{0,80}(?:not connected|unauthori[sz]ed|could not (?:fetch|read))",
            re.I,
        ),
    ),
    (
        "figma_no_response",
        re.compile(
            r"figma.{0,80}(?:no response|not respond|timed? ?out|unavailable|failed|not connected|disconnected)", re.I
        ),
    ),
    (
        "context_layer_unavailable",
        re.compile(
            r"(?:fleetlens|context layer|context service).{0,80}(?:unavailable|not connected|failed|timed? ?out|error)"
            r"|did not answer after \d+ attempts",
            re.I,
        ),
    ),
    (
        "connection_error",
        re.compile(
            r"connection (?:error|refused|reset|aborted)|econn\w+|timed? ?out|temporary failure"
            r"|name or service not known"
            r"|network is unreachable|\bssl\b",
            re.I,
        ),
    ),
    ("reviewers_failed", re.compile(r"reviewers? (?:failed|did not|could not)|review.{0,40}failed twice", re.I)),
)
_ERRORISH = re.compile(
    r"error|fail|limit|not connected|timeout|timed out|exception|refused|unauthori|expired|stopped", re.I
)
_TAIL_LINES = 80
_EXCERPT_CAP = 300


def classify(text: str) -> str:
    """The cause category for a failure, from the lines the run printed just before it stopped."""
    for cause, pattern in _PATTERNS:
        if pattern.search(text or ""):
            return cause
    return "other"


def excerpt(text: str) -> str:
    """The few lines that most likely say what went wrong, secrets scrubbed, capped."""
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip() and "[rfc] STOPPED" not in ln]
    picked = [ln for ln in lines if _ERRORISH.search(ln)][-3:] or lines[-3:]
    joined = " | ".join(picked)[:_EXCERPT_CAP]
    from .privacy import scrub_text

    return scrub_text(joined)[0] if joined else ""


class StderrTail:
    """Stands in for sys.stderr while a run executes: everything passes straight through, and the last lines are kept
    so a failed attempt can say why. Never raises into the run."""

    def __init__(self, inner):
        self._inner = inner
        self._lines: list[str] = []
        self._partial = ""

    def write(self, s):
        with contextlib.suppress(Exception):
            self._partial += s
            *done, self._partial = self._partial.split("\n")
            self._lines = (self._lines + done)[-_TAIL_LINES:]
        return self._inner.write(s)

    def text(self) -> str:
        return "\n".join(self._lines + ([self._partial] if self._partial else []))

    def __getattr__(self, name):
        return getattr(self._inner, name)


class capture_stderr:  # noqa: N801 — used like a context manager
    def __enter__(self) -> StderrTail:
        self._saved = sys.stderr
        self.tail = StderrTail(self._saved)
        sys.stderr = self.tail
        return self.tail

    def __exit__(self, *exc) -> None:
        sys.stderr = self._saved


def stop_kind(code: int) -> str | None:
    return None if code == 0 else "user_stop" if code == USER_STOP_EXIT else "failure"


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _append(run_dir: Path, entry: dict) -> None:
    with (run_dir / "logs" / ATTEMPTS_FILE).open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def open_attempt(run_dir: Path, *, kind: str, start_stage: str) -> None:
    """Mark an attempt as running. If the previous one never closed, it was killed: write its line now, so it counts."""
    marker = run_dir / "logs" / OPEN_FILE
    with contextlib.suppress(Exception):
        if marker.exists():
            old = json.loads(marker.read_text(encoding="utf-8"))
            stage = old.get("stage") or old.get("from")
            _append(run_dir, {"ts": old.get("ts") or _now(), "kind": old.get("kind", "new"), "from": old.get("from"),
                              "exit": None, "status": f"killed at {stage}" if stage else "killed",
                              "stop_kind": "killed", "stage": stage})  # fmt: skip
        marker.write_text(
            json.dumps({"ts": _now(), "kind": kind, "from": start_stage, "stage": start_stage}), encoding="utf-8"
        )


def mark_stage(run_dir: Path, stage: str) -> None:
    """Remember which stage the open attempt is in, so a hard kill can say where."""
    marker = run_dir / "logs" / OPEN_FILE
    with contextlib.suppress(Exception):
        data = json.loads(marker.read_text(encoding="utf-8"))
        data["stage"] = stage
        marker.write_text(json.dumps(data), encoding="utf-8")


def close_attempt(run_dir: Path, *, kind: str, start_stage: str, code: int, status: str, stage: str | None,
                  tail_text: str = "", extra: dict | None = None) -> dict:  # fmt: skip
    """Write the attempt's end line and clear the open marker. Returns the line.

    `extra` adds fields to the line as they are (run.py passes the O1 commit the attempt ran on)."""
    entry: dict = {"ts": _now(), "kind": kind, "from": start_stage, "exit": code, "status": status, **(extra or {})}
    sk = stop_kind(code)
    if sk:
        entry["stop_kind"], entry["stage"] = sk, stage
    if sk == "failure":
        entry["failure_cause"] = classify(tail_text)
        entry["failure_excerpt"] = excerpt(tail_text)
    _append(run_dir, entry)
    with contextlib.suppress(OSError):
        (run_dir / "logs" / OPEN_FILE).unlink()
    return entry


def read_attempts(run_dir: Path) -> list[dict]:
    out: list[dict] = []
    with contextlib.suppress(OSError):
        for line in (run_dir / "logs" / ATTEMPTS_FILE).read_text(encoding="utf-8").splitlines():
            with contextlib.suppress(json.JSONDecodeError):
                d = json.loads(line)
                if isinstance(d, dict):
                    out.append(d)
    return out


def _end_of(a: dict) -> str:
    if a.get("exit") == 0:
        return "finished"
    if a.get("stop_kind") == "user_stop" or a.get("exit") == USER_STOP_EXIT:
        return "stopped"
    if a.get("stop_kind") == "failure" or isinstance(a.get("exit"), int):
        return "failed"
    return "unknown"


def _stage_of(a: dict) -> str | None:
    if a.get("stage"):
        return a["stage"]
    m = re.search(r"(?:stopped|killed) at (\w+)", a.get("status") or "")
    return m.group(1) if m else None


def _unfinished(attempts: list[dict]) -> list[dict]:
    """The attempts that did not finish, numbered by their place in the run."""
    return [
        {"attempt": i + 1, "end": _end_of(a), "stage": _stage_of(a)}
        for i, a in enumerate(attempts)
        if _end_of(a) != "finished"
    ]


def state(run_dir: Path) -> dict:
    """How the latest attempt ended: `end` is finished | stopped | failed | unknown (no end record: killed, still
    running, or a run that predates attempt records). `earlier` lists the attempts before it that did not finish."""
    attempts = read_attempts(run_dir)
    total = len(attempts)
    base = {"cause": None, "excerpt": None}
    if (run_dir / "logs" / OPEN_FILE).exists():  # the latest attempt never closed
        stage = None
        with contextlib.suppress(Exception):
            m = json.loads((run_dir / "logs" / OPEN_FILE).read_text(encoding="utf-8"))
            stage = m.get("stage") or m.get("from")
        return base | {"end": "unknown", "stage": stage, "attempt": total + 1, "attempts": total + 1,
                       "earlier": _unfinished(attempts)}  # fmt: skip
    if not attempts:  # a run from before attempt records: run.json knows done, nothing more
        status = None
        with contextlib.suppress(Exception):
            status = json.loads((run_dir / "logs" / "run.json").read_text(encoding="utf-8")).get("status")
        m = re.search(r"stopped at (\w+)", status or "")
        return base | {"end": "finished" if status == "done" else "unknown", "stage": m.group(1) if m else None,
                       "attempt": 1, "attempts": 1, "earlier": []}  # fmt: skip
    last = attempts[-1]
    return {
        "end": _end_of(last),
        "stage": _stage_of(last),
        "cause": last.get("failure_cause"),
        "excerpt": last.get("failure_excerpt"),
        "attempt": total,
        "attempts": total,
        "earlier": _unfinished(attempts[:-1]),
    }
