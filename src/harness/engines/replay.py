"""The `replay` engine: re-emit a RECORDED run instead of spending money on a model.

WHY it exists (D9c + D10): "no regression" has to be mechanical. The differential parity test runs the
same pipeline through the legacy path and through `harness run` and compares manifests — which is only
meaningful if both runs see byte-identical stage outputs, and no live model gives you that. Replay also
makes the benchmarks honest: with the model removed, what is left on the clock IS harness overhead.

It is deliberately dumb. It does not simulate a model, judge anything, or invent outputs. It copies the
recorded outputs into place, re-emits the recorded trace with fresh timestamps and sequence numbers, and
returns the recorded accounting. If a label was not recorded, it FAILS LOUDLY — a replay that silently
produced nothing would read downstream as a stage that legitimately wrote no files.

RECORDING LAYOUT (`harness record <run_dir> <recording_dir>` builds it; the format is versioned):

    <recording>/
      recording.json          {"version": 1, "source_run_dir": ..., "created": ..., "labels": [...]}
      <label>/
        events.jsonl          the canonical trace to re-emit
        result.json           the recorded accounting (cost_usd, usage, duration_s, engine, ...)
        outputs/<rel>         every declared output of that stage, at its run-dir-relative path

`<label>` is a stage name for a stage-level recording, or a fan-out child's label (`ground.b2`).
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ..contracts import StageResult
from ..events import Event, EventWriter, events_path, read_events, summarize
from . import EngineRequest, EngineResult

NAME = "replay"
RECORDING_VERSION = 1
MANIFEST_NAME = "recording.json"


class RecordingError(Exception):
    """The recording cannot answer the request (missing label, unreadable directory)."""


# ---- recording -------------------------------------------------------------------------------------


def record(run_dir: Path, recording_dir: Path, *, labels: list[str] | None = None) -> Path:
    """Build a recording from a finished run dir. Returns `recording_dir`.

    Outputs come from the MANIFEST's `outputs` list, not from a directory sweep: the manifest is the
    run's own record of what each stage declared, so a recording carries exactly the contract a replay
    has to satisfy — no incidental scratch files, and a missing declared output is visible immediately
    rather than at replay time.

    A stage whose canonical trace is missing but whose raw Claude stream is present is translated on the
    way in, so recordings can be made from runs that predate the canonical trace.
    """
    run_dir, recording_dir = Path(run_dir), Path(recording_dir)
    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        raise RecordingError(f"{run_dir} has no manifest.json — a recording needs a finished run dir "
                             f"(run the pipeline first, or point at the right --run-dir).")
    manifest = json.loads(manifest_path.read_text())
    stages = {k: v for k, v in manifest.items() if isinstance(v, dict) and "status" in v}
    if labels:
        stages = {k: v for k, v in stages.items() if k in set(labels)}
        missing = sorted(set(labels) - set(stages))
        if missing:
            raise RecordingError(f"{run_dir}/manifest.json has no stage entry for {missing}")
    recording_dir.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, entry in stages.items():
        dest = recording_dir / name
        (dest / "outputs").mkdir(parents=True, exist_ok=True)
        for rel in entry.get("outputs") or []:
            src = run_dir / rel
            if not src.is_file():
                continue  # a red stage may legitimately be missing an output; replay reproduces that
            target = dest / "outputs" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, target)
        trace = events_path(run_dir, name)
        if not trace.is_file():
            raw = run_dir / "logs" / f"{name}.stream.jsonl"
            if raw.is_file():
                from .claude_translate import translate_file

                translate_file(raw, dest / "events.jsonl")
            else:
                (dest / "events.jsonl").write_text("", encoding="utf-8")
        else:
            shutil.copy2(trace, dest / "events.jsonl")
        (dest / "result.json").write_text(json.dumps({
            "status": entry.get("status"),
            "engine_desc": entry.get("engine"),
            "cost_usd": entry.get("cost_usd"),
            "usage": entry.get("usage"),
            "duration_s": entry.get("duration_s") or 0.0,
            "tool_call_count": entry.get("tool_call_count"),
            # the canonical trace has no notion of a "turn" (it is a flat event stream), so the
            # parallel-dispatch ratio cannot be re-derived from it. Carried in the recording instead.
            "multi_call_turn_pct": entry.get("multi_call_turn_pct"),
            "outputs": list(entry.get("outputs") or []),
        }, indent=2), encoding="utf-8")
        written.append(name)
    (recording_dir / MANIFEST_NAME).write_text(json.dumps({
        "version": RECORDING_VERSION,
        "source_run_dir": str(run_dir.resolve()),
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "labels": sorted(written),
    }, indent=2), encoding="utf-8")
    return recording_dir


# ---- replay ----------------------------------------------------------------------------------------


class ReplayEngine:
    """Engine adapter that replays a recording. Same two entry points as the claude-code adapter."""

    name = NAME

    def __init__(self, recording_dir: Path | str | None = None) -> None:
        if recording_dir is None:
            raise ValueError(
                "the replay engine needs a recording: `--engine replay --set replay.recording=<dir>` "
                "(build one with `harness record <run_dir> <recording_dir>`)."
            )
        self.recording_dir = Path(recording_dir).expanduser()

    # ---- protocol -------------------------------------------------------------------------------

    def supports_resume(self) -> bool:
        # nothing to resume: a replay has no session, and pretending otherwise would let a caller
        # believe warm context was reused when the outputs came off disk either way.
        return False

    def doctor(self) -> list[str]:
        if not self.recording_dir.is_dir():
            return [f"recording dir {self.recording_dir} does not exist — build one with "
                    f"`harness record <run_dir> {self.recording_dir}`."]
        meta = self.recording_dir / MANIFEST_NAME
        if not meta.is_file():
            return [f"{self.recording_dir} is not a recording (no {MANIFEST_NAME}) — build one with "
                    f"`harness record <run_dir> {self.recording_dir}`."]
        try:
            version = json.loads(meta.read_text()).get("version")
        except (OSError, json.JSONDecodeError) as exc:
            return [f"{meta} is unreadable: {exc}"]
        if version != RECORDING_VERSION:
            return [f"{meta} is recording format v{version}; this harness reads "
                    f"v{RECORDING_VERSION} — re-record it."]
        return []

    def labels(self) -> list[str]:
        try:
            return sorted(json.loads((self.recording_dir / MANIFEST_NAME).read_text()).get("labels") or [])
        except (OSError, json.JSONDecodeError):
            return []

    def run(self, req: EngineRequest) -> EngineResult:
        t0 = time.monotonic()
        rec = self._label_dir(req.label)
        self.restore_outputs(rec, req.cwd)
        trace = self.reemit(rec, req.trace_path or events_path(req.cwd, req.label))
        recorded = self._recorded_result(rec)
        return EngineResult(
            summary=summarize(trace),
            # the RECORDED duration, not the replay's: a parity comparison that swapped in a
            # microsecond duration would differ from the legacy manifest on a field the recording
            # can answer honestly. Harness overhead is measured separately (the wall clock around
            # the whole run), never by pretending the model reran.
            duration_s=float(recorded.get("duration_s") or 0.0) or (time.monotonic() - t0),
            engine_desc=str(recorded.get("engine_desc") or f"{NAME}:{self.recording_dir.name}"),
            prompt_sent=req.prompt,
            trace_path=trace,
            exit_code=0,
            native=recorded,
        )

    # ---- stage-level -----------------------------------------------------------------------------

    def run_stage(self, stage, ctx) -> StageResult:
        """Replay a whole stage: restore its declared outputs, re-emit its trace, return its accounting.

        The returned StageResult is built from the RECORDING, so a replayed run's manifest matches the
        recorded one field for field apart from timestamps — which is exactly what the differential
        parity test asserts.
        """
        rec = self._label_dir(stage.name)
        self.restore_outputs(rec, ctx.run_dir)
        trace = self.reemit(rec, events_path(ctx.run_dir, stage.name))
        summary = summarize(trace)
        recorded = self._recorded_result(rec)
        res = StageResult(
            final_text=summary.final_text,
            tool_calls=summary.tool_calls,
            cost_usd=recorded.get("cost_usd", summary.cost_usd),
            usage=recorded.get("usage", summary.usage),
            duration_s=float(recorded.get("duration_s") or 0.0),
            engine_desc=str(recorded.get("engine_desc") or f"{NAME}:{self.recording_dir.name}"),
            num_turns=summary.num_turns,
            denials=summary.denials,
            compactions=summary.compactions,
        )
        # Restore the recorded parallel-dispatch ratio exactly. `_multi_call_pct` in the runner divides
        # multi_call_turns by tool_turns and rounds to one decimal, so a denominator of 1000 reproduces
        # any recorded one-decimal percentage without inventing turn-level detail the trace never had.
        pct = recorded.get("multi_call_turn_pct")
        if isinstance(pct, (int, float)):
            res.tool_turns, res.multi_call_turns = 1000, round(float(pct) * 10)
        missing = [o for o in stage.outputs if not (ctx.run_dir / o).exists()]
        if missing and (rec / "outputs").is_dir():
            # the recording did not carry these — say so here rather than letting the output gate
            # report a model failure that never happened
            raise RecordingError(
                f"replay of stage {stage.name!r} left {missing} missing: the recording at "
                f"{rec} has no such file(s). Re-record from a run where that stage went green."
            )
        return res

    # ---- helpers ---------------------------------------------------------------------------------

    def _label_dir(self, label: str) -> Path:
        rec = self.recording_dir / label
        if not rec.is_dir():
            raise RecordingError(
                f"no recording for label {label!r} in {self.recording_dir} "
                f"(recorded: {self.labels() or 'nothing'}). A replay never invents outputs."
            )
        return rec

    @staticmethod
    def restore_outputs(rec: Path, cwd: Path) -> list[Path]:
        """Copy `<rec>/outputs/**` into `cwd`, preserving relative paths. Returns what was written."""
        src_root = rec / "outputs"
        written: list[Path] = []
        if not src_root.is_dir():
            return written
        for src in sorted(src_root.rglob("*")):
            if not src.is_file():
                continue
            dest = cwd / src.relative_to(src_root)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            written.append(dest)
        return written

    @staticmethod
    def reemit(rec: Path, trace_path: Path) -> Path:
        """Re-emit the recorded trace with FRESH timestamps and sequence numbers.

        Fresh, not copied: a replayed trace describes a run that is happening NOW. Keeping the
        recording's clock would make a replayed run's events pre-date the run itself, which breaks
        every duration derived from the trace and any reader that orders events by time.
        """
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("", encoding="utf-8")
        src = rec / "events.jsonl"
        with EventWriter(trace_path) as w:
            for raw in read_events(src):
                fields = {k: v for k, v in raw.items() if k in _EVENT_FIELDS and k not in ("seq", "ts")}
                fields.setdefault("t", raw.get("t", "log"))
                w.emit(Event(**fields))
        return trace_path

    @staticmethod
    def _recorded_result(rec: Path) -> dict:
        p = rec / "result.json"
        if not p.is_file():
            return {}
        try:
            return json.loads(p.read_text())
        except json.JSONDecodeError:
            return {}


_EVENT_FIELDS = frozenset(asdict(Event(t="x")).keys())
