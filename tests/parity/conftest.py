"""Shared fixtures for the parity suite: the fake pipeline both paths run, and golden-file plumbing.

The fake engine is the point. Parity means "the same run, two code paths" — with a live model the
outputs differ run to run and nothing can be compared. A fake engine that writes deterministic files
makes the manifest, the decision vocabulary and the run-dir layout exactly reproducible.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from harness.contracts import Stage, StageResult

GOLDEN_DIR = Path(__file__).parent
# Regenerate every golden in this directory instead of asserting against it. Use it ONLY when a change
# to a public surface is INTENDED, and read the resulting diff as the review artefact — a golden that
# is regenerated without being read is a golden that proves nothing.
REGENERATE = os.environ.get("PARITY_REGENERATE") == "1"


def assert_golden(name: str, actual: str) -> None:
    path = GOLDEN_DIR / name
    if REGENERATE:
        path.write_text(actual if actual.endswith("\n") else actual + "\n", encoding="utf-8")
        pytest.skip(f"regenerated {name} (PARITY_REGENERATE=1)")
    expected = path.read_text(encoding="utf-8")
    assert actual.rstrip("\n") == expected.rstrip("\n"), (
        f"{name} drifted. If the change is INTENDED, re-run with PARITY_REGENERATE=1 and review the "
        f"diff — this file is a public surface (see tests/parity/README.md)."
    )


def fake_engine(tag: str = "content"):
    """Deterministic stage engine: writes every declared output, reports fixed accounting."""
    def _engine(stage: Stage, ctx) -> StageResult:
        for rel in stage.outputs:
            p = ctx.path(rel)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(f"{tag}:{stage.name}:{rel}\n")
        return StageResult(final_text=f"{stage.name} done", cost_usd=0.25,
                           usage={"input_tokens": 100, "output_tokens": 20},
                           engine_desc="fake:parity", num_turns=3, tool_turns=4, multi_call_turns=1)

    return _engine


PIPELINE_NAMES = ("alpha", "beta", "gamma")


def fake_stages(gate_failures: dict[str, list[str]] | None = None, engine=None) -> list[Stage]:
    """A 3-stage pipeline with a real requires/outputs chain, so composition errors are reachable."""
    gf = gate_failures or {}
    eng = engine if engine is not None else fake_engine()
    return [
        Stage(name="alpha", requires=("inputs.json",), outputs=("raw/alpha.md",),
              build_prompt=lambda c: "alpha", gate=lambda c, n="alpha": gf.get(n, []), engine=eng),
        Stage(name="beta", requires=("raw/alpha.md",), outputs=("findings/beta.md",),
              build_prompt=lambda c: "beta", gate=lambda c, n="beta": gf.get(n, []), engine=eng,
              retry_on_gate_failure=1, revise_on_retry=True),
        Stage(name="gamma", requires=("findings/beta.md",), outputs=("out/gamma.md",),
              build_prompt=lambda c: "gamma", gate=lambda c, n="gamma": gf.get(n, []), engine=eng),
    ]


@pytest.fixture
def run_dir(tmp_path) -> Path:
    d = tmp_path / "run"
    d.mkdir()
    (d / "inputs.json").write_text("{}")
    return d
