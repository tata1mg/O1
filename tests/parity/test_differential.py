"""The same pipeline, two code paths, one manifest.

The direct path is `run_pipeline(stages, ctx)`. The harness path is `harness run <workflow> --engine replay` on a
recording made from the direct run. If the harness changed how a stage is accounted for, composed, gated or
recorded, the two manifests would differ here.

Timestamps and wall-clock durations are excluded from the comparison and nothing else is: `started_at`
is when the process ran and `duration_s` is how fast the machine was. Cost, usage, attempt counts,
statuses, the requires/outputs contract, the gate verdict, the inputs digest and the parallel-dispatch
ratio are all compared, because a divergence in any of them is a real regression.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness import cli, workflows
from harness.contracts import RunContext, Stage, Workflow
from harness.engines.replay import record
from rfc_agent.runner import run_pipeline

from .conftest import assert_golden, fake_stages

WORKFLOW_NAME = "parityfake"
# Fields whose value is a property of WHEN and WHERE the run happened, not of what it did.
VOLATILE = ("started_at", "duration_s", "duration_s_total", "rework_s")


def _adapter_stages() -> list[Stage]:
    """The same three stages with `engine=None`, so the run's ENGINE ADAPTER drives them.

    Identical requires/outputs to the direct set — those two lists are recorded in the manifest, so a
    mismatch here would show up as a diff and mask a real one.
    """
    return [Stage(name=s.name, requires=s.requires, outputs=s.outputs,
                  build_prompt=s.build_prompt, gate=s.gate, engine=None,
                  retry_on_gate_failure=s.retry_on_gate_failure,
                  revise_on_retry=s.revise_on_retry)
            for s in fake_stages()]


@pytest.fixture
def registered_workflow():
    """Out-of-tree registration (harness.workflows.register) — a workflow need not be a folder in
    src/workflows/ to run, which is what makes a test workflow possible at all."""
    wf = Workflow(name=WORKFLOW_NAME, description="parity fixture",
                  stages=tuple(_adapter_stages()),
                  default_pipeline=("alpha", "beta", "gamma"),
                  resolve_inputs=lambda args: {"repos_root": str(Path(args.run_dir))},
                  input_roots=("raw/",))
    workflows.register(wf)
    yield wf
    workflows._REGISTRY.pop(WORKFLOW_NAME, None)


class _Cfg:
    """No config.json in the loop: the parity suite must not depend on the operator's machine."""

    config: dict = {}

    def get(self, key, default=None):
        return default


def _direct_run(run_dir: Path) -> dict:
    (run_dir / "inputs.json").write_text("{}")
    ctx = RunContext(run_dir=run_dir, inputs={"repos_root": str(run_dir)}, config=_Cfg())
    run_pipeline(fake_stages(), ctx)
    return json.loads((run_dir / "manifest.json").read_text())


def _harness_replay_run(run_dir: Path, recording: Path, monkeypatch) -> tuple[int, dict]:
    (run_dir / "inputs.json").write_text("{}")
    monkeypatch.setattr(cli, "_default_config", lambda: _Cfg())
    code = cli.main(["run", WORKFLOW_NAME, "--run-dir", str(run_dir), "--no-interactive",
                     "--engine", "replay", "--set", f"replay.recording={recording}"])
    return code, json.loads((run_dir / "manifest.json").read_text())


def _strip(manifest: dict) -> dict:
    return {stage: {k: v for k, v in entry.items() if k not in VOLATILE}
            for stage, entry in manifest.items()
            if isinstance(entry, dict) and "status" in entry}


def test_manifests_are_equal_modulo_timestamps_and_durations(tmp_path, registered_workflow,
                                                             monkeypatch):
    direct_dir, harness_dir, rec = tmp_path / "direct", tmp_path / "harness", tmp_path / "rec"
    direct_dir.mkdir(), harness_dir.mkdir()

    direct = _direct_run(direct_dir)
    record(direct_dir, rec)
    code, replayed = _harness_replay_run(harness_dir, rec, monkeypatch)

    assert code == 0, "the replayed run must go green exactly as the recorded one did"
    assert _strip(replayed) == _strip(direct)


def test_the_volatile_fields_are_present_in_both(tmp_path, registered_workflow, monkeypatch):
    """Excluding a field from the comparison must not mean the field stopped being recorded."""
    direct_dir, harness_dir, rec = tmp_path / "direct", tmp_path / "harness", tmp_path / "rec"
    direct_dir.mkdir(), harness_dir.mkdir()
    direct = _direct_run(direct_dir)
    record(direct_dir, rec)
    _, replayed = _harness_replay_run(harness_dir, rec, monkeypatch)
    for manifest in (direct, replayed):
        for entry in _strip(manifest).keys():
            assert set(VOLATILE) <= set(manifest[entry]), f"{entry} lost a volatile field"


def test_declared_outputs_are_byte_identical(tmp_path, registered_workflow, monkeypatch):
    """A manifest comparison is only meaningful if the artifacts match too."""
    direct_dir, harness_dir, rec = tmp_path / "direct", tmp_path / "harness", tmp_path / "rec"
    direct_dir.mkdir(), harness_dir.mkdir()
    _direct_run(direct_dir)
    record(direct_dir, rec)
    _harness_replay_run(harness_dir, rec, monkeypatch)
    for rel in ("raw/alpha.md", "findings/beta.md", "out/gamma.md"):
        assert (harness_dir / rel).read_bytes() == (direct_dir / rel).read_bytes(), rel


def test_the_harness_path_adds_flags_json_and_nothing_else(tmp_path, registered_workflow,
                                                           monkeypatch):
    """Three intended differences in the run-dir layout, and nothing else:

      flags.json                 `harness run` resolves flags, so it records what was in effect;
      logs/<stage>.events.jsonl  every ENGINE writes the canonical trace. The direct comparison run
                                 uses a bare stage callable, which is not an engine adapter and writes
                                 none — so their presence here is the seam working, not a regression;
      logs/asks_skipped.jsonl    the harness CLI records that a non-interactive run swallowed its asks.
                                 `_direct_run` bypasses the CLI, so it never gets the chance.

    Any OTHER new file is a change to the run-dir contract and this test is where it surfaces.
    """
    direct_dir, harness_dir, rec = tmp_path / "direct", tmp_path / "harness", tmp_path / "rec"
    direct_dir.mkdir(), harness_dir.mkdir()
    _direct_run(direct_dir)
    record(direct_dir, rec)
    _harness_replay_run(harness_dir, rec, monkeypatch)

    def layout(d: Path) -> set[str]:
        return {str(p.relative_to(d)) for p in d.rglob("*") if p.is_file()
                and not p.name.endswith(".tmp")}

    extra = layout(harness_dir) - layout(direct_dir)
    allowed = {"flags.json", "logs/asks_skipped.jsonl"} | {
        f"logs/{name}.events.jsonl" for name in ("alpha", "beta", "gamma")}
    assert extra <= allowed, f"the harness path created unexpected files: {sorted(extra - allowed)}"
    assert "flags.json" in extra and "logs/alpha.events.jsonl" in extra, (
        "flags.json and the canonical trace are REQUIRED of the harness path, not merely tolerated")
    assert not layout(direct_dir) - layout(harness_dir), "the harness path dropped a file"


def test_flags_json_records_the_replay_engine(tmp_path, registered_workflow, monkeypatch):
    direct_dir, harness_dir, rec = tmp_path / "direct", tmp_path / "harness", tmp_path / "rec"
    direct_dir.mkdir(), harness_dir.mkdir()
    _direct_run(direct_dir)
    record(direct_dir, rec)
    _harness_replay_run(harness_dir, rec, monkeypatch)
    dumped = json.loads((harness_dir / "flags.json").read_text())
    assert dumped["engine"]["value"] == "replay" and dumped["engine"]["source"] == "cli"
    assert dumped["replay.recording"]["value"] == str(rec)


def test_replay_is_faithful_on_a_red_gate(tmp_path, monkeypatch):
    """A recorded FAILURE must replay as the same failure — parity that only covers green runs would
    miss every regression in the retry/gate accounting."""
    from rfc_agent.runner import GateFailure

    direct_dir, harness_dir, rec = tmp_path / "direct", tmp_path / "harness", tmp_path / "rec"
    direct_dir.mkdir(), harness_dir.mkdir()
    (direct_dir / "inputs.json").write_text("{}")
    ctx = RunContext(run_dir=direct_dir, inputs={"repos_root": str(direct_dir)}, config=_Cfg())
    with pytest.raises(GateFailure):
        run_pipeline(fake_stages({"gamma": ["a blocker"]}), ctx)
    direct = json.loads((direct_dir / "manifest.json").read_text())
    assert direct["gamma"]["status"] == "gate_failed"
    record(direct_dir, rec)

    wf = Workflow(name=WORKFLOW_NAME, stages=tuple(
        Stage(name=s.name, requires=s.requires, outputs=s.outputs, build_prompt=s.build_prompt,
              gate=s.gate, engine=None, retry_on_gate_failure=s.retry_on_gate_failure,
              revise_on_retry=s.revise_on_retry)
        for s in fake_stages({"gamma": ["a blocker"]})),
        default_pipeline=("alpha", "beta", "gamma"),
        resolve_inputs=lambda args: {"repos_root": str(Path(args.run_dir))})
    workflows.register(wf)
    try:
        code, replayed = _harness_replay_run(harness_dir, rec, monkeypatch)
    finally:
        workflows._REGISTRY.pop(WORKFLOW_NAME, None)
    assert code == 2, "a red gate is exit 2 through the harness exactly as through rfc-agent"
    assert _strip(replayed) == _strip(direct)


def test_recording_layout(tmp_path):
    """The recording format is a contract between `harness record` and the replay engine."""
    direct_dir, rec = tmp_path / "direct", tmp_path / "rec"
    direct_dir.mkdir()
    _direct_run(direct_dir)
    record(direct_dir, rec)
    paths = sorted(str(p.relative_to(rec)) + ("/" if p.is_dir() else "")
                   for p in rec.rglob("*"))
    assert_golden("recording_layout.golden", "\n".join(paths))
