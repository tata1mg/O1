"""`progress`: what a running stage is doing, from its activity log and the files it is writing.

The fixtures are lines in the exact shape `rfc_agent.engine.log_activity` writes, taken from the 2026-11-04
NR-DIGICHEM run, whose `generate_cases` was silent for 6½ minutes and showed the user nothing for 23.
"""

from __future__ import annotations

import io
import json
from datetime import datetime
from pathlib import Path

import pytest

from workflows.testing import progress
from workflows.testing.progress import Tracker, count_ids, history, phase_of

DAY = datetime(2026, 11, 4)


def at(hms: str) -> float:
    h, m, s = map(int, hms.split(":"))
    return DAY.replace(hour=h, minute=m, second=s).timestamp()


def log(hms: str, stage: str, text: str) -> str:
    return f"[{hms}] [{stage}] {text}"


def feed(tracker: Tracker, hms: str, stage: str, text: str) -> list[str]:
    return tracker.feed(log(hms, stage, text), at(hms))


# ---- phases --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("stage, text, phase", [
    ("generate_cases", "Read qa/sources/prd.md", "reading the sources"),
    ("generate_cases", "… Write streaming (16KB into scenarios.json)", "writing scenarios"),
    ("generate_cases", "Edit 2026-11-04-testing-NR-DIGICHEM/qa/cases.json", "writing cases"),
    ("generate_cases", "Bash /x/.venv/bin/python3 /x/src/workflows/testing/check_suite.py qa", "checking its own work"),
    ("generate_cases", "agent finished", "finishing (the gate checks its output)"),
    ("generate_cases", "💬 Now batch 4: VALSVC module (19 cases).", ""),
    ("evaluate", "Read 2026-11-04-testing-NR-DIGICHEM/qa/cases.json", "reading the sources and the suite"),
    ("evaluate", "Bash jq -r '.[].module' scenarios.json", "checking coverage"),
    ("evaluate", "Write 2026-11-04-testing-NR-DIGICHEM/qa/eval.json", "writing the scorecard"),
    ("prerequisites", "… Write streaming (16KB into prerequisites.json)", "writing the prerequisites"),
    ("derive_ui", "Bash rg -n OneVoiceButton _snap", "finding the elements in the front-end source"),
    ("derive_ui", "Write ui/tests/TC-ONEVOICE-ENTRY-FUNC-01.spec.ts", "writing Playwright tests"),
    ("derive_ui", "Edit qa/ui/plan.json", "citing locators in the plan"),
    ("store_cases", "Read qa/cases.json", ""),                      # not a model stage
])
def test_phase_of(stage, text, phase):
    assert phase_of(stage, text) == phase


def test_case_ids_are_counted_in_a_file_still_being_written(tmp_path):
    p = tmp_path / "cases.json"
    p.write_text('[{"id": "TC-A-01", "title": "x"}, {"id": "TC-A-02", "steps": "1. Send POST /v1')
    assert count_ids(p, "TC-") == 2
    assert count_ids(tmp_path / "absent.json", "TC-") == 0


# ---- what the user sees -----------------------------------------------------------------------------------


def test_lifecycle_lines_pass_through_verbatim(tmp_path):
    t = Tracker(tmp_path)
    line = log("12:25:41", "generate_cases", "STAGE started")
    assert t.feed(line, at("12:25:41")) == [line]
    assert feed(t, "12:25:55", "generate_cases", "Read qa/sources/prd.md") == []   # too soon to say anything


def test_a_long_silence_is_reported_as_thinking_then_as_worth_a_look(tmp_path):
    """The 6½ minutes in which generate_cases wrote nothing: the 💭 line only lands once the thought ends."""
    t = Tracker(tmp_path, {"generate_cases": [1147, 1341, 1366]})
    feed(t, "12:25:41", "generate_cases", "STAGE started")
    feed(t, "12:26:09", "generate_cases", "Read qa/sources/rfc.md")
    [beat] = t.tick(at("12:29:41"))
    assert beat == ("PROGRESS generate_cases · 4m of usually 19–22m · reading the sources · "
                    "thinking (no output for 3m)")
    t.current.last_line = at("12:26:09") - 1000                           # pretend it has been very quiet
    [beat] = t.tick(at("12:40:00"))
    assert "if it stays quiet, look at logs/generate_cases.stderr" in beat


def test_a_new_phase_is_said_with_the_count_from_disk_and_the_latest_note(tmp_path):
    (tmp_path / "qa").mkdir()
    (tmp_path / "qa/cases.json").write_text(json.dumps([{"id": f"TC-A-{i:02}"} for i in range(61)]))
    t = Tracker(tmp_path)
    feed(t, "12:25:41", "generate_cases", "STAGE started")
    feed(t, "12:43:23", "generate_cases", "💬 Now batch 4: VALSVC module (19 cases) — the largest single batch.")
    [line] = feed(t, "12:44:18", "generate_cases", "Edit run/qa/cases.json")
    assert line == ("PROGRESS generate_cases · 18m · writing cases: 61 so far · "
                    "“Now batch 4: VALSVC module (19 cases) — the largest single batch.”")
    # the same phase again says nothing; a new one within 90 s waits for the next chance
    assert feed(t, "12:44:40", "generate_cases", "Edit run/qa/cases.json") == []
    assert feed(t, "12:45:00", "generate_cases", "agent finished") == []


def test_a_phase_change_too_early_to_say_is_said_without_waiting_for_another_line(tmp_path):
    """The model read its sources in the first 30 s and then thought in silence: say so at ~1m, not at 4m."""
    t = Tracker(tmp_path, last_said=at("17:34:30"))
    feed(t, "17:34:33", "generate_cases", "STAGE started")
    assert feed(t, "17:34:57", "generate_cases", "Read qa/sources/rfc.md") == []
    assert t.tick(at("17:35:30")) == []
    [line] = t.tick(at("17:36:00"))
    assert line.startswith("PROGRESS generate_cases · 1m · reading the sources")
    assert t.tick(at("17:36:30")) == []                                    # said once


@pytest.mark.parametrize("text, note", [
    # 2026-11-05, LABS-PDP: cut at 90 characters this read "… I'll delegate the mechanical JSON…"
    ("💬 Given the scale of this suite (11 modules, ~90+ cases), I'll delegate the mechanical JSON writing to "
     "a script and check every case against the grammar myself. Then the gate.",
     "Given the scale of this suite (11 modules, ~90+ cases), I'll delegate the mechanical JSON writing to a "
     "script and check every case against the grammar myself."),
    ("💬 The gate passes. Here's the summary. ## Step 0 — Resource Inventory | Source | Status |",
     "The gate passes. Here's the summary."),                             # a short first sentence takes the next
    ("💬 | Figma | ❌ Blocked — not supplied | No design file", ""),       # the closing summary's table rows
    ("💬 **Fault-injection hooks** for `validationservice` are missing.",
     "Fault-injection hooks for validationservice are missing."),
])
def test_notes_are_whole_sentences(text, note):
    assert progress._note(text) == note


def test_a_runaway_sentence_is_cut_at_a_word():
    note = progress._note("💬 " + "word " * 100)
    assert note.endswith("word…") and len(note) <= progress.NOTE_MAX + 1


def test_a_note_is_quoted_once_and_a_table_row_does_not_replace_it(tmp_path):
    t = Tracker(tmp_path)
    feed(t, "12:25:41", "generate_cases", "STAGE started")
    feed(t, "12:33:02", "generate_cases", "💬 Now I'll write the two output files.")
    [first] = feed(t, "12:33:36", "generate_cases", "… Write streaming (16KB into scenarios.json)")
    assert first.endswith("“Now I'll write the two output files.”")
    feed(t, "12:34:00", "generate_cases", "💬 | Figma | ❌ Blocked — not supplied |")
    [beat] = t.tick(at("12:38:00"))
    assert "“" not in beat                                                 # nothing new to quote
    assert t.current.note == "Now I'll write the two output files."


def test_heartbeats_space_out_on_a_stage_that_runs_long(tmp_path):
    """prerequisites ran 94 minutes on 2026-11-04: a fixed 4-minute beat would have posted 22 lines."""
    t = Tracker(tmp_path)
    feed(t, "13:11:51", "prerequisites", "STAGE started")
    beats, now = [], at("13:11:51")
    while now < at("14:45:00"):
        now += 30
        beats += t.tick(now)
    assert 10 <= len(beats) <= 13


def test_a_revision_is_shown_as_its_attempt_without_an_eta(tmp_path):
    t = Tracker(tmp_path, {"evaluate": [359, 450, 520]})
    feed(t, "12:48:28", "evaluate", "STAGE started")
    feed(t, "12:54:10", "evaluate", "gate FAILED (2 failure(s)) — automatic revision next")
    feed(t, "12:54:10", "evaluate", "STAGE started")
    [beat] = t.tick(at("12:58:21"))
    assert beat.startswith("PROGRESS evaluate · 4m (attempt 2) · revising after its gate failures")


def test_slower_than_usual_is_said_plainly(tmp_path):
    t = Tracker(tmp_path, {"prerequisites": [411, 545, 600]})
    feed(t, "13:11:51", "prerequisites", "STAGE started")
    [beat] = t.tick(at("13:26:35"))
    assert "14m, slower than usual (6–10m)" in beat


def test_a_finished_stage_stops_the_heartbeat(tmp_path):
    t = Tracker(tmp_path)
    feed(t, "12:25:41", "generate_cases", "STAGE started")
    feed(t, "12:48:28", "generate_cases", "gate PASSED — stage done (1366s)")
    assert t.tick(at("12:58:00")) == []


# ---- history --------------------------------------------------------------------------------------------------


def test_history_is_per_attempt_from_finished_stages_of_other_runs(tmp_path):
    def run(name, rows):
        d = tmp_path / name
        d.mkdir()
        (d / "manifest.json").write_text(json.dumps(rows))
        return d

    run("2026-11-02-testing-A", {"evaluate": {"status": "ok", "attempts": 2, "duration_s_total": 900}})
    run("2026-11-03-testing-B", {"evaluate": {"status": "gate_failed", "attempts": 2, "duration_s_total": 999}})
    me = run("2026-11-04-testing-C", {"evaluate": {"status": "ok", "attempts": 1, "duration_s_total": 10}})
    assert history(tmp_path, exclude=me) == {"evaluate": [450.0]}


# ---- the watch loop -------------------------------------------------------------------------------------------


class Clock:
    """Wall clock for `watch`: each sleep moves time on and may append lines to the log first."""

    def __init__(self, start: float, log: Path, script: dict[int, list[str]]) -> None:
        self.t, self.log, self.script, self.sleeps = start, log, script, 0

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps += 1
        for line in self.script.get(self.sleeps, []):
            with open(self.log, "a") as f:
                f.write(line + "\n")
        self.t += s


def test_watch_hands_over_its_offset_and_misses_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(progress, "runs_root", lambda: tmp_path / "none")
    (tmp_path / "logs").mkdir()
    activity = tmp_path / "logs/activity.log"
    activity.write_text("\n".join([
        log("11:00:00", "sources", "STAGE started"),                  # an earlier launch: not repeated
        log("12:25:41", "sources", "gate PASSED — stage done (0s)"),  # this launch, before the watcher opened
    ]) + "\n")
    clock = Clock(at("12:25:50"), activity, {
        1: [log("12:25:52", "generate_cases", "STAGE started")],
        3: [log("12:25:55", "generate_cases", "Read qa/sources/prd.md")],
    })
    out = io.StringIO()
    offset = progress.watch(tmp_path, start=None, max_s=10, poll_s=2, out=out, clock=clock, sleep=clock.sleep)
    lines = out.getvalue().splitlines()
    assert lines == [log("12:25:41", "sources", "gate PASSED — stage done (0s)"),
                     log("12:25:52", "generate_cases", "STAGE started"), f"WATCH rearm --from {offset}"]
    assert offset == activity.stat().st_size

    # the next watcher starts at that offset: a line written in between is seen exactly once
    with open(activity, "a") as f:
        f.write(log("12:48:28", "generate_cases", "gate PASSED — stage done (1366s)") + "\n")
    clock = Clock(at("12:48:30"), activity, {})
    out = io.StringIO()
    progress.watch(tmp_path, start=offset, max_s=4, poll_s=2, out=out, clock=clock, sleep=clock.sleep)
    assert out.getvalue().splitlines()[0] == log("12:48:28", "generate_cases", "gate PASSED — stage done (1366s)")


def test_watch_joins_a_run_already_under_way_without_replaying_it(tmp_path, monkeypatch):
    """Started mid-stage (a resumed command), the watcher knows the stage and phase but repeats nothing."""
    monkeypatch.setattr(progress, "runs_root", lambda: tmp_path / "none")
    (tmp_path / "logs").mkdir()
    activity = tmp_path / "logs/activity.log"
    activity.write_text("\n".join([
        log("12:25:41", "generate_cases", "STAGE started"),
        log("12:26:09", "generate_cases", "Read qa/sources/rfc.md"),
    ]) + "\n")
    clock = Clock(at("12:31:00"), activity, {})
    out = io.StringIO()
    progress.watch(tmp_path, start=None, max_s=250, poll_s=10, out=out, clock=clock, sleep=clock.sleep)
    lines = out.getvalue().splitlines()
    assert lines[0].startswith("PROGRESS generate_cases · 9m · reading the sources · thinking (no output for")
    assert lines[-1].startswith("WATCH rearm --from ")
