"""the manifest key set, the decision vocabulary and the run-dir layout are public surfaces.

`manifest.json` plus the fixed artifact paths ARE the public contract (README): the eval harness reads
cost/duration/status per stage, the plugin command reads exit codes, dashboards read the decision names.
None of those consumers is in this repo, so nothing else can catch a rename. These goldens can.

Run under the fake engine, so the values are deterministic and the KEYS are what is being pinned.
"""

from __future__ import annotations

import json

import pytest

from harness.contracts import RunContext
from rfc_agent.constants import Decision, StageStatus
from rfc_agent.runner import run_pipeline

from .conftest import assert_golden, fake_stages


@pytest.fixture
def green_run(run_dir):
    ctx = RunContext(run_dir=run_dir, inputs={"repos_root": str(run_dir)}, config=None)
    run_pipeline(fake_stages(), ctx)
    return run_dir


def test_manifest_key_set_per_stage(green_run):
    manifest = json.loads((green_run / "manifest.json").read_text())
    keys = {stage: sorted(entry) for stage, entry in sorted(manifest.items())}
    assert_golden("manifest_keys.golden", json.dumps(keys, indent=2))


def test_manifest_gate_and_status_shape(green_run):
    """The two fields every consumer branches on."""
    manifest = json.loads((green_run / "manifest.json").read_text())
    for stage, entry in manifest.items():
        assert entry["status"] == StageStatus.OK, stage
        assert sorted(entry["gate"]) == ["failures", "passed"]
        assert entry["gate"]["passed"] is True and entry["gate"]["failures"] == []


def test_manifest_records_last_attempt_and_cumulative_totals(run_dir):
    """`cost_usd`/`duration_s` are LAST-ATTEMPT (the documented contract the eval harness reads);
    `attempts` + `*_total` are the cumulative truth. A retry must not make the accounting lossy."""
    ctx = RunContext(run_dir=run_dir, inputs={"repos_root": str(run_dir)}, config=None)
    from rfc_agent.runner import GateFailure

    with pytest.raises(GateFailure):
        run_pipeline(fake_stages({"beta": ["nope"]}), ctx)
    beta = json.loads((run_dir / "manifest.json").read_text())["beta"]
    assert beta["attempts"] == 2 and beta["cost_usd"] == 0.25
    assert beta["cost_usd_total"] == 0.5, "both attempts' spend survives"
    assert beta["rework_s"] >= 0 and beta["status"] == StageStatus.GATE_FAILED


def test_decision_vocabulary(green_run):
    """A recorded decision name that isn't in the enum doesn't exist (the decision-signals rule), and a
    name that silently disappears breaks whatever chart keys on it."""
    assert_golden("decision_vocabulary.golden",
                  "\n".join(sorted(str(d) for d in Decision)))


def test_recorded_decisions_are_all_in_the_vocabulary(run_dir):
    ctx = RunContext(run_dir=run_dir, inputs={"repos_root": str(run_dir)}, config=None)
    from rfc_agent.runner import GateFailure

    with pytest.raises(GateFailure):
        run_pipeline(fake_stages({"gamma": ["nope"]}), ctx)
    run_pipeline(fake_stages(), ctx)  # a second invocation: green-skips + revision decisions
    lines = (run_dir / "logs" / "decisions.jsonl").read_text().splitlines()
    recorded = {json.loads(x)["decision"] for x in lines if x.strip()}
    known = {str(d) for d in Decision} | {f"checkpoint_{a}" for a in ("continue", "rerun", "quit")}
    assert recorded, "the run recorded no decisions at all"
    assert recorded <= known, f"unknown decision name(s): {sorted(recorded - known)}"


def test_run_dir_layout(green_run):
    """The file layout is this system's API (README). Every path a green run creates, pinned."""
    paths = sorted(
        str(p.relative_to(green_run)) + ("/" if p.is_dir() else "")
        for p in green_run.rglob("*")
        # the identity/session files carry per-run ids and the activity log carries timestamps; their
        # PRESENCE is the contract, their content is not, so they are listed but not compared
        if not p.name.endswith(".tmp")
    )
    assert_golden("run_dir_layout.golden", "\n".join(paths))


def test_stage_outputs_land_at_their_declared_paths(green_run):
    for rel in ("raw/alpha.md", "findings/beta.md", "out/gamma.md"):
        assert (green_run / rel).is_file(), f"{rel} is a declared output and must exist"


def test_flags_json_is_absent_without_resolved_flags(green_run):
    """The legacy call path resolves no flags and must not invent the file ."""
    assert not (green_run / "flags.json").exists()


def test_no_reported_cost_records_null_never_zero(run_dir):
    """An engine that reports NO cost must land `null` in the manifest, not 0.0.

    A hard timeout writes no terminal result event, so there is genuinely no cost to report; Pi has no
    terminal cost event at all. `0.0` would read downstream as "this stage ran for free", which is a
    different and false claim — and it silently corrupts any cost roll-up that averages over stages.
    """
    from harness.contracts import Stage, StageResult

    def costless(stage: Stage, ctx) -> StageResult:
        for rel in stage.outputs:
            ctx.path(rel).parent.mkdir(parents=True, exist_ok=True)
            ctx.path(rel).write_text("x")
        return StageResult(engine_desc="costless", cost_usd=None, usage=None)

    ctx = RunContext(run_dir=run_dir, inputs={"repos_root": str(run_dir)}, config=None)
    run_pipeline(fake_stages(engine=costless), ctx)
    raw = (run_dir / "manifest.json").read_text()
    manifest = json.loads(raw)
    for stage, entry in manifest.items():
        assert entry["cost_usd"] is None, f"{stage} fabricated a cost of {entry['cost_usd']!r}"
        assert entry["usage"] is None, stage
    assert '"cost_usd": null' in raw, "the on-disk JSON must carry null, which is what consumers read"


def test_a_costless_stage_survives_the_record_replay_round_trip(run_dir, tmp_path):
    """The convention has to hold through the replay engine too, or parity would launder a null into
    a zero the moment a recording is involved."""
    from harness.contracts import Stage, StageResult
    from harness.engines.replay import ReplayEngine, record

    def costless(stage: Stage, ctx) -> StageResult:
        for rel in stage.outputs:
            ctx.path(rel).parent.mkdir(parents=True, exist_ok=True)
            ctx.path(rel).write_text("x")
        return StageResult(engine_desc="costless", cost_usd=None)

    ctx = RunContext(run_dir=run_dir, inputs={"repos_root": str(run_dir)}, config=None)
    run_pipeline(fake_stages(engine=costless), ctx)
    rec = record(run_dir, tmp_path / "rec")
    assert json.loads((rec / "alpha" / "result.json").read_text())["cost_usd"] is None

    fresh = tmp_path / "fresh"
    fresh.mkdir()
    replayed = ReplayEngine(rec).run_stage(
        fake_stages()[0], type("C", (), {"run_dir": fresh, "path": lambda s, r: fresh / r})())
    assert replayed.cost_usd is None, "replay must not turn a null cost into 0.0"


def test_run_totals_reports_zero_only_for_stages_that_actually_ran(run_dir):
    """`run_totals` SUMS, so 0.0 is the honest answer once a costless stage has run — the null
    convention is per-stage. This pins the boundary so neither side drifts into the other."""
    from harness.contracts import Stage, StageResult
    from rfc_agent.runner import run_totals

    def costless(stage: Stage, ctx) -> StageResult:
        for rel in stage.outputs:
            ctx.path(rel).parent.mkdir(parents=True, exist_ok=True)
            ctx.path(rel).write_text("x")
        return StageResult(engine_desc="costless", cost_usd=None)

    ctx = RunContext(run_dir=run_dir, inputs={"repos_root": str(run_dir)}, config=None)
    run_pipeline(fake_stages(engine=costless), ctx)
    cost, _secs, tries = run_totals(json.loads((run_dir / "manifest.json").read_text()))
    assert cost == 0 and tries == 3
