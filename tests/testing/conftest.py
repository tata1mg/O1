"""Shared fixtures for the testing-workflow suite.

`sample_*` is the deliberately-broken suite in fixtures/ (one violation per mechanical gate, so a gate
that stops firing is visible). `clean_*` is the subset that passes every gate — the baseline a pipeline
test runs on, and the control that proves a gate is not simply always red.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from harness.contracts import RunContext

FIXTURES = Path(__file__).parent / "fixtures"

#: The six cases (and their scenarios) that violate nothing. Everything else in the fixture is broken.
CLEAN_CASE_IDS = (
    "TC-RSE-SLOT-FUNC-01", "TC-RSE-SLOT-NEG-01", "TC-RSE-SLOT-EDGE-01",
    "TC-RSE-CAP-FUNC-01", "TC-RSE-CAP-NEG-01", "TC-RSE-CAP-EDGE-01",
)


def load(name: str) -> list[dict]:
    return json.loads((FIXTURES / name).read_text())


@pytest.fixture
def sample_cases() -> list[dict]:
    return load("cases.sample.json")


@pytest.fixture
def sample_scenarios() -> list[dict]:
    return load("scenarios.sample.json")


@pytest.fixture
def clean_cases() -> list[dict]:
    return [c for c in load("cases.sample.json") if c["id"] in CLEAN_CASE_IDS]


@pytest.fixture
def clean_scenarios() -> list[dict]:
    wanted = {"TS-" + cid[3:] for cid in CLEAN_CASE_IDS}
    return [s for s in load("scenarios.sample.json") if s["id"] in wanted]


class FakeFlags:
    """A `harness.flags.Flags` stand-in: resolved values only, KeyError for anything unregistered."""

    def __init__(self, values: dict) -> None:
        self._v = dict(values)

    def get(self, name, default=...):
        if name in self._v:
            return self._v[name]
        if default is not ...:
            return default
        raise KeyError(name)


def make_ctx(run_dir: Path, *, inputs: dict | None = None, flags: dict | None = None,
             engine=None) -> RunContext:
    """A RunContext with resolved testing.* defaults, so stages read flags and never os.environ."""
    from harness.flags import registry
    from workflows.testing import flags as testing_flags

    defaults = {name: registry()[name].default for name in testing_flags.NAMES}
    defaults.update({"timeout_s": 60, "model": ""})
    defaults.update(flags or {})
    run_dir.mkdir(parents=True, exist_ok=True)
    return RunContext(run_dir=run_dir, inputs=dict(inputs or {}), config=None,
                      engine=engine, flags=FakeFlags(defaults), workflow="testing")


@pytest.fixture
def ctx(tmp_path) -> RunContext:
    return make_ctx(tmp_path / "run", inputs={"feature": "RSE-1"})


@pytest.fixture
def sourced_ctx(tmp_path, clean_cases, clean_scenarios):
    """A run dir already holding sources.json + a clean suite — the state after generate_cases."""
    from workflows.testing.stages import write_json

    c = make_ctx(tmp_path / "run", inputs={"feature": "RSE-1"})
    prd = tmp_path / "src" / "prd.md"
    rfc = tmp_path / "src" / "rfc.md"
    prd.parent.mkdir(parents=True, exist_ok=True)
    prd.write_text("# PRD\nREQ-1 slots. REQ-2 unknown lab.\n")
    rfc.write_text("# RFC\n## Open questions\nQ1 cache TTL.\n")
    c.inputs.update({"prd": str(prd), "rfc": str(rfc)})
    write_json(c.path("qa/cases.json"), clean_cases)
    write_json(c.path("qa/scenarios.json"), clean_scenarios)
    return c


@pytest.fixture
def ctx_factory():
    """`make_ctx` as a fixture, for tests that need a second run dir or non-default flags."""
    return make_ctx
