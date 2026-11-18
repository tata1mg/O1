"""`progress` — what a running stage is doing, as a few short lines a person can follow.

    python -m workflows.testing.progress <run_dir> --watch [--from OFFSET] [--max-s 1500]
    python -m workflows.testing.progress <run_dir> --now                   one line for the current stage

WHY: the command's Monitor passed on only `STAGE started` and `gate PASSED`, so for `generate_cases` the user
saw "▶ generate_cases" and then nothing for 23 minutes (2026-11-04, NR-DIGICHEM), including a 6½-minute block
in which the model thought and wrote nothing at all. The activity log already said much more — the sources
read, "batch 4: VALSVC module (19 cases)", 16KB streamed into cases.json — it was just filtered out.

`--watch` tails `logs/activity.log` and prints:

- every lifecycle line the command acts on (`STAGE started`, `gate PASSED`, `gate FAILED`, `ENGINE FAILURE`,
  `inputs changed`), verbatim, so the command's handling of them is unchanged;
- `PROGRESS <stage> · <elapsed> of usually <range> · <phase>[: <count>] · "<latest note>"` when a model stage
  moves to a new phase (at most every 90 s), and as a heartbeat after 4 minutes with no line;
- `WATCH rearm --from <offset>` just before the Monitor's 30-minute lifetime ends: the next watcher starts at
  that byte offset, so nothing between the two is lost or repeated.

The phase is read from the log by fixed rules, and counts come from the files on disk (case ids in
`cases.json`, spec files in `qa/ui/tests/`), never from the model's own claims. "usually 19–23m" is the
range of that stage's past durations across `<RUNS_ROOT>/*-testing-*`; with no history, elapsed time only.

Read-only: writes nothing.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

RE_LINE = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\] \[([a-z_]+)\] (.*)$")
#: The lines the command acts on; passed through verbatim.
LIFECYCLE = ("STAGE started", "gate PASSED", "gate FAILED", "ENGINE FAILURE", "inputs changed")
#: What each stage is doing when its log says nothing more specific.
DEFAULT_PHASE = {
    "sources": "collecting the inputs", "generate_cases": "starting", "render_xlsx": "building the workbook",
    "evaluate": "starting", "store_cases": "uploading to Kiwi", "prerequisites": "starting",
    "derive_spec": "deriving the request spec", "execute_be": "sending the requests",
    "derive_ui": "starting", "execute_web": "running the browser tests", "publish_app": "recording the Maestro run",
    "execute_ui_agent": "agents are working through the cases in browsers",
    "execute_app": "running the Android cases on the devices (flows, then agents)",
    "convert_flows": "running the draft flows to green",
}
#: Silence after which a model stage is reported as thinking: it logs a 💭 line only once a thought ENDS.
QUIET_S = 90
#: Silence after which the line says where to look: past this a session may be stuck, not thinking.
STUCK_S = 900
PHASE_GAP_S = 90
#: The first heartbeat after 4 minutes, each next one 2 minutes later, never more than 10 apart: a stage that
#: ran 90 minutes (2026-11-04, prerequisites) would otherwise post 22 near-identical lines.
HEARTBEAT_S = 240
HEARTBEAT_STEP_S = 120
HEARTBEAT_MAX_S = 600
#: A watcher stops before the Monitor's 30-minute lifetime and hands over its offset.
MAX_S = 1500


@dataclass
class StageState:
    stage: str
    started: float
    phase: str
    attempt: int = 1
    last_line: float = 0.0
    note: str = ""
    beats: int = 0


@dataclass
class Tracker:
    """Folds activity-log lines into the current stage's state; decides which lines a person sees."""

    run_dir: Path
    history: dict[str, list[float]] = field(default_factory=dict)
    current: StageState | None = None
    last_said: float = 0.0
    said_phase: str = ""
    said_note: str = ""
    revising: set[str] = field(default_factory=set)

    # ---- input -------------------------------------------------------------------------------------------

    def feed(self, line: str, now: float) -> list[str]:
        """Lines to print for one activity-log line read at `now` (epoch seconds)."""
        m = RE_LINE.match(line.rstrip("\n"))
        if not m:
            return []
        stage, text = m.group(4), m.group(5)
        at = _log_time(m, now)
        out = [line.rstrip("\n")] if text.startswith(LIFECYCLE) else []
        if text.startswith("STAGE started"):
            attempt = 2 if stage in self.revising else 1
            self.revising.discard(stage)
            self.current = StageState(stage, at, "revising after its gate failures" if attempt > 1
                                      else DEFAULT_PHASE.get(stage, "working"), attempt, at)
            self.said_phase = self.current.phase
            return out
        if text.startswith("gate FAILED") and "automatic revision next" in text:
            self.revising.add(stage)
        if text.startswith(("gate PASSED", "gate FAILED", "ENGINE FAILURE")):
            if self.current and self.current.stage == stage:
                self.current = None
            return out
        cur = self.current
        if cur is None or cur.stage != stage:
            return out
        cur.last_line = at
        phase = phase_of(stage, text)
        if text.startswith("💬"):
            cur.note = _note(text) or cur.note
        if phase and cur.attempt == 1:
            cur.phase = phase
        if cur.phase != self.said_phase and now - self.last_said >= PHASE_GAP_S and now - cur.started >= 60:
            out.append(self.line(now))
        return out

    def tick(self, now: float) -> list[str]:
        """The heartbeat: a line after HEARTBEAT_S with nothing said about a stage still running."""
        cur = self.current
        if cur is None:
            return []
        # A phase change seen too early to say (the first minute, or within 90 s of the last line) is said once
        # the wait is over, not at the next log line: a model that reads its sources and then thinks for six
        # minutes logs nothing more, and the user saw only the 4-minute heartbeat (2026-11-05, LABS-PDP).
        if cur.phase != self.said_phase and now - self.last_said >= PHASE_GAP_S and now - cur.started >= 60:
            return [self.line(now)]
        gap = min(HEARTBEAT_S + HEARTBEAT_STEP_S * cur.beats, HEARTBEAT_MAX_S)
        if now - self.last_said >= gap and now - cur.started >= HEARTBEAT_S:
            line = self.line(now)
            cur.beats += 1
            return [line]
        return []

    # ---- output ------------------------------------------------------------------------------------------

    def line(self, now: float) -> str:
        cur = self.current
        assert cur is not None
        if cur.phase != self.said_phase:
            cur.beats = 0
        self.last_said, self.said_phase = now, cur.phase
        parts = [f"PROGRESS {cur.stage}", self._timing(cur, now)]
        phase = cur.phase
        count = self._count(cur)
        if count:
            phase += f": {count}"
        quiet = now - (cur.last_line or cur.started)
        if quiet >= STUCK_S and cur.stage in MODEL_STAGES:
            phase += (f" · no output for {_mins(quiet)}; if it stays quiet, look at "
                      f"logs/{cur.stage}.stderr")
        elif quiet >= QUIET_S and cur.stage in MODEL_STAGES:
            phase += f" · thinking (no output for {_mins(quiet)})"
        parts.append(phase)
        # a note is said once: repeated on the next line it reads as what the model is doing NOW
        if cur.note and cur.note != self.said_note:
            parts.append(f"“{cur.note}”")
            self.said_note = cur.note
        return " · ".join(parts)

    def _timing(self, cur: StageState, now: float) -> str:
        took = _mins(now - cur.started)
        if cur.attempt > 1:
            return f"{took} (attempt {cur.attempt})"
        past = self.history.get(cur.stage) or []
        if not past:
            return took
        lo, hi = min(past), max(past)
        usual = (f"~{_mins(median(past))}" if hi - lo < 120
                 else f"{int(lo) // 60}–{_mins(hi)}" if lo >= 60 else f"{_mins(lo)}–{_mins(hi)}")
        if now - cur.started > hi * 1.15:
            return f"{took}, slower than usual ({usual})"
        return f"{took} of usually {usual}"

    def _count(self, cur: StageState) -> str:
        if cur.stage == "generate_cases" and cur.phase == "writing cases":
            n = count_ids(self.run_dir / "qa/cases.json", "TC-")
            return f"{n} so far" if n else ""
        if cur.stage == "generate_cases" and cur.phase == "writing scenarios":
            n = count_ids(self.run_dir / "qa/scenarios.json", "TS-")
            return f"{n} scenarios so far" if n else ""
        if cur.stage in ("execute_ui_agent", "execute_app"):
            # only this run's: a case folder from an earlier run (a case not selected this time) keeps its
            # verdict.json, and counting it said "6 verdicts so far" before the first case ended (2026-11-18)
            folder = self.run_dir / ("qa/agent" if cur.stage == "execute_ui_agent" else "qa/android")
            done = [p for p in folder.glob("*/verdict.json")
                    if not p.parent.name.endswith(".confirm") and _mtime(p) >= cur.started - 1]
            return f"{len(done)} verdicts so far" if done else ""
        if cur.stage == "derive_ui" and cur.phase == "writing Playwright tests":
            n = len(list((self.run_dir / "qa/ui/tests").glob("TC-*.spec.ts")))
            return f"{n} so far" if n else ""
        return ""


MODEL_STAGES = ("generate_cases", "evaluate", "prerequisites", "derive_ui")
_SOURCES = re.compile(r"^Read (?:\S*/)?(?:qa/sources/|qa/cases\.json|qa/scenarios\.json|qa/prerequisites\.facts)")
_SELF_CHECK = re.compile(r"check_(suite|prerequisites|ui)\.py|playwright test --list")


def phase_of(stage: str, text: str) -> str:
    """The phase one log line puts a model stage in, "" when it does not move it."""
    if stage not in MODEL_STAGES:
        return ""
    if text == "agent finished":
        return "finishing (the gate checks its output)"
    if text.startswith("Bash") and _SELF_CHECK.search(text):
        return "checking its own work"
    writing = re.search(r"(?:Write|Edit)\b.*?([\w./-]+\.(?:json|ts))\)?$", text)
    if writing:
        target = writing.group(1)
        if stage == "generate_cases":
            if target.endswith("scenarios.json"):
                return "writing scenarios"
            if target.endswith("cases.json"):
                return "writing cases"
        if stage == "evaluate" and target.endswith("eval.json"):
            return "writing the scorecard"
        if stage == "prerequisites" and target.endswith("prerequisites.json"):
            return "writing the prerequisites"
        if stage == "derive_ui" and target.endswith(".spec.ts"):
            return "writing Playwright tests"
        if stage == "derive_ui" and target.endswith("plan.json"):
            return "citing locators in the plan"
        return ""
    if _SOURCES.match(text):
        return {"generate_cases": "reading the sources", "evaluate": "reading the sources and the suite",
                "prerequisites": "reading the suite and the plan"}.get(stage, "")
    if stage == "evaluate" and text.startswith("Bash"):
        return "checking coverage"
    if stage == "derive_ui" and text.startswith(("Read", "Bash grep", "Bash rg", "Bash find", "Grep", "Glob")):
        return "finding the elements in the front-end source"
    return ""


def count_ids(path: Path, prefix: str) -> int:
    """Distinct `"id": "<prefix>…"` values in a file being written — it need not be valid JSON yet."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return 0
    return len(set(re.findall(rf'"id"\s*:\s*"({re.escape(prefix)}[^"]+)"', text)))


def history(root: Path, exclude: Path | None = None) -> dict[str, list[float]]:
    """{stage: [seconds per attempt]} from every finished stage of earlier testing runs under `root`."""
    out: dict[str, list[float]] = {}
    for manifest in root.glob("*-testing-*/manifest.json") if root.is_dir() else ():
        if exclude and manifest.parent.resolve() == exclude.resolve():
            continue
        try:
            rows = json.loads(manifest.read_text())
        except (OSError, ValueError):
            continue
        for stage, row in rows.items():
            if isinstance(row, dict) and row.get("status") == "ok" and row.get("duration_s_total"):
                # an attempt's average: a revision is shorter than a first attempt, so this errs early
                out.setdefault(stage, []).append(row["duration_s_total"] / max(1, row.get("attempts") or 1))
    return out


def runs_root() -> Path:
    from .preflight import load_config

    cfg = load_config()
    return Path((cfg.get("RUNS_ROOT") if cfg else None) or "~/rfc-runs").expanduser()


def _log_time(m: re.Match, now: float) -> float:
    """The log's hh:mm:ss as epoch seconds: today, or yesterday when that would be in the future."""
    base = datetime.fromtimestamp(now)
    at = base.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3)), microsecond=0)
    if at.timestamp() > now + 60:
        at -= timedelta(days=1)
    return at.timestamp()


#: A note longer than this is cut at a word, so one runaway sentence cannot fill the chat.
NOTE_MAX = 280


def _note(text: str) -> str:
    """A 💬 line as a quote of whole sentences: the first, plus the next when the first is short.

    Cut to 90 characters it read "I'll delegate the mechanical JSON…" (2026-11-05, LABS-PDP), which says
    nothing about what is happening. The model's closing summary — markdown tables, `## Step 0` — is not
    narration and gives "" (the previous note stands).
    """
    body = text[1:].strip()
    if body.startswith(("|", "#", "-")):
        return ""
    body = re.split(r"\s(?:#{1,6}\s|\|)", body, maxsplit=1)[0]          # stop where a table or heading begins
    sentences = re.split(r"(?<=[.!?])\s+", re.sub(r"[*`]+", "", body).strip())
    note = sentences[0]
    if len(note) < 60 and len(sentences) > 1:
        note += " " + sentences[1]
    if len(note) > NOTE_MAX:
        note = note[:NOTE_MAX].rsplit(" ", 1)[0].rstrip(",;:—-") + "…"
    return note.strip()


def _mins(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60}m" if seconds >= 60 else f"{seconds}s"


# ---- the watch loop -----------------------------------------------------------------------------------------


def watch(run_dir: Path, *, start: int | None, max_s: float = MAX_S, poll_s: float = 2.0,
          out=sys.stdout, clock=time.time, sleep=time.sleep) -> int:
    """Tail the activity log until `max_s`; return the byte offset reached.

    Without `start`, what is already in the log is read silently to learn where the run is, and only its
    lifecycle lines from the last 30 seconds are printed: the run is launched in the same turn as the
    watcher and can log its first `STAGE started` before the watcher opens the file.
    """
    log = run_dir / "logs" / "activity.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.touch()
    tracker = Tracker(run_dir, history(runs_root(), exclude=run_dir))
    began = clock()

    def say(lines: list[str]) -> None:
        for ln in lines:
            print(ln, file=out, flush=True)

    with open(log, "rb") as f:
        if start is None:
            for raw in f:
                lines = tracker.feed(raw.decode("utf-8", "replace"), began)
                m = RE_LINE.match(raw.decode("utf-8", "replace"))
                if m and _log_time(m, began) >= began - 30:
                    say([ln for ln in lines if not ln.startswith("PROGRESS")])
            tracker.last_said = began
        else:
            f.seek(start)
        pending = b""
        while clock() - began < max_s:
            chunk = f.read()
            if chunk:
                pending += chunk
                *complete, pending = pending.split(b"\n")
                for raw in complete:
                    say(tracker.feed(raw.decode("utf-8", "replace"), clock()))
            say(tracker.tick(clock()))
            sleep(poll_s)
        offset = f.tell() - len(pending)
    say([f"WATCH rearm --from {offset}"])
    return offset


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime
    except OSError:  # removed between the glob and the stat
        return 0.0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="progress")
    ap.add_argument("run_dir")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--now", action="store_true")
    ap.add_argument("--from", dest="start", type=int)
    ap.add_argument("--max-s", type=float, default=MAX_S)
    args = ap.parse_args(argv)
    run_dir = Path(args.run_dir).expanduser()
    if args.watch:
        watch(run_dir, start=args.start, max_s=args.max_s)
        return 0
    tracker = Tracker(run_dir, history(runs_root(), exclude=run_dir))
    now = time.time()
    try:
        for line in (run_dir / "logs" / "activity.log").read_text(errors="replace").splitlines():
            tracker.feed(line, now)
    except OSError:
        pass
    print(tracker.line(now) if tracker.current else "no stage running")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
