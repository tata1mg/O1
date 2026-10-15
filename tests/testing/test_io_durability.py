"""Durability and concurrency for the workflow's artifacts: atomic writes, strict reads, locking.

Covers review blockers 5.1 and 5.2. The three properties under test:

  * a write that dies mid-way leaves either the OLD file or the new one, never a torn one;
  * a reader that meets a torn file NAMES it and says what to do, rather than raising JSONDecodeError
    out of whatever happened to be reading and repeating on every retry;
  * two writers recording into one run file both survive.
"""

from __future__ import annotations

import json
import os
import threading

import pytest

from harness.contracts import GateFailure
from workflows.testing import _io
from workflows.testing.stages import read_json, read_json_gated, write_json
from workflows.testing.store.json_store import JsonCaseStore


# ---- atomic_write_text ------------------------------------------------------------------------------
def test_it_writes_the_content_and_creates_the_parent(tmp_path):
    path = tmp_path / "deep" / "qa" / "cases.json"
    assert _io.atomic_write_text(path, "hello") == path
    assert path.read_text() == "hello"


def test_a_failed_write_leaves_the_previous_version_intact(tmp_path, monkeypatch):
    """The regression this exists for: `write_text` truncates first, so a crash mid-write published a
    zero-length `qa/cases.json` and every later invocation read an empty suite."""
    path = tmp_path / "cases.json"
    _io.atomic_write_text(path, '[{"id": "TC-A-M-FUNC-01"}]')

    class Dying:
        def __init__(self, *a, **kw):
            self._f = open(*a, **kw)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self._f.close()
            return False

        def write(self, text):
            raise OSError(28, "No space left on device")

        def flush(self):
            pass

        def fileno(self):
            return self._f.fileno()

    monkeypatch.setattr(_io, "open", Dying, raising=False)
    with pytest.raises(OSError, match="No space left"):
        _io.atomic_write_text(path, "the new content")
    monkeypatch.undo()

    assert json.loads(path.read_text()) == [{"id": "TC-A-M-FUNC-01"}]
    assert list(tmp_path.glob("*.tmp")) == []   # the temp file is cleaned up, not left as an artifact


def test_the_temp_name_carries_the_pid(tmp_path, monkeypatch):
    """A FIXED temp name let two invocations sharing a run dir interleave into one temp file — the exact
    class the 0.23.3 review reopened."""
    seen = []
    real_replace = os.replace

    def spy(src, dst):
        seen.append(str(src))
        return real_replace(src, dst)

    monkeypatch.setattr(_io.os, "replace", spy)
    _io.atomic_write_text(tmp_path / "x.json", "{}")
    assert seen and f".{os.getpid()}.tmp" in seen[0]


def test_the_write_is_fsynced_before_the_rename(tmp_path, monkeypatch):
    """os.replace alone only orders the rename; some filesystems commit it ahead of the data and publish
    a zero-length file."""
    order = []
    monkeypatch.setattr(_io.os, "fsync", lambda fd: order.append("fsync"))
    real_replace = os.replace
    monkeypatch.setattr(_io.os, "replace", lambda a, b: (order.append("replace"), real_replace(a, b))[1])
    _io.atomic_write_text(tmp_path / "x.json", "{}")
    assert order == ["fsync", "replace"]


def test_a_value_that_cannot_be_serialised_does_not_truncate_the_old_file(tmp_path):
    path = tmp_path / "spec.json"
    _io.atomic_write_json(path, [{"extid": "TC-A"}])
    with pytest.raises(TypeError):
        _io.atomic_write_json(path, {"bad": object()})
    assert json.loads(path.read_text()) == [{"extid": "TC-A"}]


# ---- parse_json / the readers -----------------------------------------------------------------------
def test_an_absent_file_and_a_torn_file_are_reported_differently(tmp_path):
    absent, why_absent = _io.parse_json(tmp_path / "nope.json")
    assert absent is None and "does not exist" in why_absent

    torn = tmp_path / "cases.json"
    torn.write_text('[{"id": "TC-A-M-FU')
    value, why = _io.parse_json(torn)
    assert value is None
    assert "truncated" in why and "crashed process" in why and str(torn) in why


def test_an_empty_file_reads_as_truncated_not_as_wrong_content(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text("")
    _, why = _io.parse_json(path)
    assert "empty or truncated" in why


def test_content_that_is_not_json_at_all_gets_the_writers_advice(tmp_path):
    """A markdown fence or a preamble is a writer emitting the wrong thing, not damage — and the advice
    for the two cases is different."""
    path = tmp_path / "eval.json"
    path.write_text("```json\n{}\n```")
    _, why = _io.parse_json(path)
    assert "does not contain JSON" in why and "no markdown fence" in why
    assert "truncated" not in why


def test_load_json_strict_raises_a_gate_failure_naming_the_file(tmp_path):
    path = tmp_path / "store.json"
    path.write_text("{oops")
    with pytest.raises(GateFailure, match=r"case store file .*store\.json"):
        _io.load_json_strict(path, what=f"case store file {path}")


def test_read_json_stays_tolerant_for_prompt_building(tmp_path):
    """`_build_prompt` runs before any gate and must not explode on a half-written inventory."""
    path = tmp_path / "sources.json"
    path.write_text("{trunc")
    assert read_json(path, {"sources": []}) == {"sources": []}


def test_read_json_gated_names_the_torn_file(tmp_path):
    path = tmp_path / "cases.json"
    path.write_text('[{"id"')
    value, failures = read_json_gated(path, expect=list)
    assert value is None and len(failures) == 1
    assert "truncated" in failures[0] and "cases.json" in failures[0]


def test_read_json_gated_distinguishes_the_wrong_shape(tmp_path):
    path = tmp_path / "cases.json"
    write_json(path, {"cases": []})
    value, failures = read_json_gated(path, expect=list)
    assert value is None and "is not a JSON array" in failures[0]


def test_every_stage_gate_reports_a_torn_artifact_instead_of_crashing(sourced_ctx):
    """One assertion per gate: a truncated input must come back as a failure string, never a traceback."""
    from workflows.testing.stages import derive_spec, evaluate, execute_be, generate_cases, sources, store_cases

    cases = [
        (sources.STAGE, "qa/sources.json"),
        (generate_cases.STAGE, "qa/cases.json"),
        (evaluate.STAGE, "qa/eval.json"),
        (store_cases.STAGE, "qa/store.json"),
        (derive_spec.STAGE, "qa/spec.json"),
        (execute_be.STAGE, "qa/results.json"),
    ]
    for stage, rel in cases:
        path = sourced_ctx.path(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"trunc')
        failures = stage.gate(sourced_ctx)
        assert failures, stage.name
        assert any(rel.split("/")[-1] in f for f in failures), (stage.name, failures)


# ---- the store's own writes are atomic --------------------------------------------------------------
def test_the_store_writes_through_the_atomic_writer(tmp_path, monkeypatch, clean_cases):
    calls = []
    real = _io.atomic_write_text

    def spy(path, text):
        calls.append(str(path))
        return real(path, text)

    monkeypatch.setattr("workflows.testing.store.json_store.atomic_write_text", spy)
    store = JsonCaseStore(tmp_path / "store")
    store.upsert_cases("RSE-1", clean_cases)
    run = store.create_run("RSE-1", "b1", "staging")
    store.record_execution(run.run_id, run.attached[0], "PASS")
    assert len(calls) == 3 and all(c.endswith(".json") for c in calls)


def test_a_torn_store_file_is_a_named_gate_failure_not_a_json_decode_error(tmp_path, clean_cases):
    """Before: `json.loads` raised out of `list_cases`, a traceback that repeated on every retry."""
    store = JsonCaseStore(tmp_path / "store")
    store.upsert_cases("RSE-1", clean_cases)
    store._cases_path("RSE-1").write_text('{"TC-RSE-SLOT-FUNC-01": {"cas')

    with pytest.raises(GateFailure) as exc:
        store.list_cases("RSE-1")
    assert "case store file" in str(exc.value) and "RSE-1.json" in str(exc.value)


def test_a_torn_run_file_is_also_named(tmp_path, clean_cases):
    store = JsonCaseStore(tmp_path / "store")
    store.upsert_cases("RSE-1", clean_cases)
    run = store.create_run("RSE-1", "b1", "staging")
    next((store.root / "runs").rglob("*.json")).write_text("{trunc")
    with pytest.raises(GateFailure, match="case store file"):
        store.record_execution(run.run_id, run.attached[0], "PASS")


# ---- locking ----------------------------------------------------------------------------------------
def test_locked_serialises_two_threads(tmp_path):
    path = tmp_path / "run.json"
    order = []
    started = threading.Event()

    def slow():
        with _io.locked(path):
            order.append("a-in")
            started.set()
            threading.Event().wait(0.05)
            order.append("a-out")

    def fast():
        started.wait(1)
        with _io.locked(path):
            order.append("b-in")
            order.append("b-out")

    threads = [threading.Thread(target=slow), threading.Thread(target=fast)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert order == ["a-in", "a-out", "b-in", "b-out"]


def test_the_lock_lives_on_a_sidecar_not_on_the_artifact(tmp_path):
    """`atomic_write_text` REPLACES the inode, so a lock held on the artifact itself would protect
    nothing once the rename landed."""
    path = tmp_path / "run.json"
    with _io.locked(path):
        assert (tmp_path / "run.json.lock").exists()
    assert not path.exists()   # locking does not create the artifact


def test_concurrent_record_execution_loses_nothing(tmp_path, clean_cases):
    """Blocker 5.2: unlocked, the later read-modify-write silently discarded the other's executions —
    and nothing downstream noticed, because results.json is written by the stage independently."""
    store = JsonCaseStore(tmp_path / "store")
    store.upsert_cases("RSE-1", clean_cases)
    run = store.create_run("RSE-1", "b1", "staging")
    extids = list(run.attached)
    assert len(extids) >= 6

    errors: list[BaseException] = []
    ready = threading.Barrier(len(extids))

    def record(extid: str) -> None:
        try:
            ready.wait(5)   # maximise the overlap: every thread reads at the same moment
            JsonCaseStore(tmp_path / "store").record_execution(
                run.run_id, extid, "PASS", actual=f"ok {extid}", duration_s=0.1)
        except BaseException as exc:   # noqa: BLE001 — surfaced below so the assert names it
            errors.append(exc)

    threads = [threading.Thread(target=record, args=(e,)) for e in extids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert errors == []
    data = json.loads(next((store.root / "runs").rglob("*__*.json")).read_text())
    assert sorted(data["executions"]) == sorted(extids)
    for extid in extids:
        assert data["executions"][extid]["actual"] == f"ok {extid}"


def test_concurrent_upsert_of_disjoint_slices_loses_nothing(tmp_path, sample_cases):
    """Two invocations upserting different slices of one feature must union, not clobber."""
    root = tmp_path / "store"
    JsonCaseStore(root).upsert_cases("RSE-1", [])   # nothing yet
    halves = [sample_cases[: len(sample_cases) // 2], sample_cases[len(sample_cases) // 2:]]
    ready = threading.Barrier(2)
    errors: list[BaseException] = []

    def upsert(slice_):
        try:
            ready.wait(5)
            JsonCaseStore(root).upsert_cases("RSE-1", slice_)
        except BaseException as exc:   # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=upsert, args=(h,)) for h in halves]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert errors == []
    stored = {c.extid for c in JsonCaseStore(root).list_cases("RSE-1")}
    assert stored == {c["id"] for c in sample_cases}


def test_a_rerun_prunes_executions_for_cases_no_longer_attached(tmp_path, clean_cases):
    """An execution for an unattached extid would sit in the run file reading as evidence for a case
    nobody ran (review nit 5.13, fixed while the lock went in)."""
    store = JsonCaseStore(tmp_path / "store")
    store.upsert_cases("RSE-1", clean_cases)
    first = store.create_run("RSE-1", "b1", "staging")
    for extid in first.attached:
        store.record_execution(first.run_id, extid, "PASS")

    narrowed = store.create_run("RSE-1", "b1", "staging", extids=["TC-RSE-SLOT-FUNC-01"])
    data = json.loads(next((store.root / "runs").rglob("*__*.json")).read_text())
    assert narrowed.attached == ("TC-RSE-SLOT-FUNC-01",)
    assert list(data["executions"]) == ["TC-RSE-SLOT-FUNC-01"]


def test_the_lock_sidecar_is_not_mistaken_for_a_run_file(tmp_path, clean_cases):
    store = JsonCaseStore(tmp_path / "store")
    store.upsert_cases("RSE-1", clean_cases)
    run = store.create_run("RSE-1", "b1", "staging")
    assert list((store.root / "runs").rglob("*.json.lock"))   # the sidecar exists
    assert store._find_run(run.run_id) is not None            # and _find_run steps over it
