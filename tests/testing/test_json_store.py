"""The JSON case store: round-trip, the two attachment gates, and the no-network guarantee.

The point of this backend is that the whole chain runs offline with no Docker and no Kiwi, so the last test
here asserts that mechanically rather than by inspection.
"""

from __future__ import annotations

import json
import socket

import pytest

from workflows.testing.store import CaseStore, get_store
from workflows.testing.store.json_store import JsonCaseStore


@pytest.fixture
def store(tmp_path) -> JsonCaseStore:
    return JsonCaseStore(tmp_path / "store")


def test_it_satisfies_the_protocol(store):
    assert isinstance(store, CaseStore)


def test_get_store_returns_the_json_backend_by_default(ctx):
    assert get_store(ctx).name == "json"


def test_get_store_rejects_an_unknown_backend(tmp_path, ctx_factory):
    ctx = ctx_factory(tmp_path / "run", flags={"testing.storage": "sqlite"})
    with pytest.raises(ValueError, match="unknown testing.storage"):
        get_store(ctx)


# ---- upsert ------------------------------------------------------------------------------------------
def test_upsert_creates_then_updates_in_place(store, clean_cases):
    first = store.upsert_cases("RSE-1", clean_cases)
    assert (first["created"], first["updated"], first["total"]) == (len(clean_cases), 0, len(clean_cases))
    second = store.upsert_cases("RSE-1", clean_cases)
    assert (second["created"], second["updated"]) == (0, len(clean_cases))
    assert len(store.list_cases("RSE-1")) == len(clean_cases)


def test_created_plus_updated_equals_total(store, clean_cases):
    """The store_cases gate depends on this arithmetic."""
    store.upsert_cases("RSE-1", clean_cases[:2])
    s = store.upsert_cases("RSE-1", clean_cases)
    assert s["created"] + s["updated"] == s["total"] == len(clean_cases)


def test_upsert_is_scoped_by_feature(store, clean_cases):
    """The same TC id under a different feature must never collide."""
    store.upsert_cases("RSE-1", clean_cases)
    other = store.upsert_cases("OTHER", clean_cases)
    assert other["created"] == len(clean_cases)
    assert len(store.list_cases("RSE-1")) == len(clean_cases)


def test_an_empty_upsert_reports_an_error_rather_than_succeeding(store):
    s = store.upsert_cases("RSE-1", [])
    assert s["total"] == 0 and s["error"] == "no cases supplied"


def test_the_canonical_dict_round_trips_including_the_dropped_columns(store, clean_cases):
    """`requirement`, `qa_comment` and `type` are the three columns a store must not drop."""
    store.upsert_cases("RSE-1", clean_cases)
    stored = {c.extid: c for c in store.list_cases("RSE-1")}
    original = clean_cases[0]
    assert stored[original["id"]].case == original
    for field in ("requirement", "qa_comment", "type"):
        assert stored[original["id"]].case[field] == original[field]


def test_list_cases_renders_the_step_text_the_deriver_reads(store, clean_cases):
    store.upsert_cases("RSE-1", clean_cases)
    case = next(c for c in store.list_cases("RSE-1") if c.extid == "TC-RSE-SLOT-FUNC-01")
    assert "1. Send GET /radiology/v1/slots?lab_id=42" in case.text
    assert "**Expected Result**" in case.text


def test_list_cases_filters_by_status_and_tags(store, sample_cases):
    store.upsert_cases("RSE-1", sample_cases)
    http = store.list_cases("RSE-1", status="CONFIRMED", tags=("exec:http",))
    assert {c.extid for c in http} == {"TC-RSE-SLOT-FUNC-01", "TC-RSE-SLOT-NEG-01",
                                       "TC-RSE-CAP-FUNC-01", "TC-RSE-CAP-FUNC-02",
                                       "TC-RSE-CAP-FUNC-03"}
    assert store.list_cases("RSE-1", status="PROPOSED") == []


def test_a_feature_key_with_a_slash_cannot_escape_the_store(store, clean_cases):
    store.upsert_cases("../../etc/RSE", clean_cases)
    written = [p for p in store.root.rglob("*.json")]
    assert all(store.root in p.parents for p in written)


# ---- runs and executions ------------------------------------------------------------------------------
def test_create_run_applies_both_gates(store, sample_cases):
    """A run only holds CONFIRMED cases, and with require_tag only cases carrying that tag."""
    store.upsert_cases("RSE-1", sample_cases)
    run = store.create_run("RSE-1", "b1", "staging", require_tag="exec:http")
    assert set(run.attached) == {"TC-RSE-SLOT-FUNC-01", "TC-RSE-SLOT-NEG-01", "TC-RSE-CAP-FUNC-01",
                                "TC-RSE-CAP-FUNC-02", "TC-RSE-CAP-FUNC-03"}
    wrong = " ".join(run.skipped_wrong_execution)
    assert "TC-RSE-CAP-NEG-01 (exec:manual)" in wrong
    assert "TC-RSE-SLOT-EDGE-01 (exec:ui)" in wrong


def test_an_unclassified_case_is_reported_as_having_no_exec_tag(store, clean_cases):
    store.upsert_cases("RSE-1", [{**clean_cases[0], "execution": ""}])
    run = store.create_run("RSE-1", "b1", "staging", require_tag="exec:http")
    assert run.attached == ()
    assert run.skipped_wrong_execution == ("TC-RSE-SLOT-FUNC-01 (no exec: tag)",)


def test_extids_restricts_the_run(store, clean_cases):
    store.upsert_cases("RSE-1", clean_cases)
    run = store.create_run("RSE-1", "b1", "staging", extids=["TC-RSE-SLOT-FUNC-01"])
    assert run.attached == ("TC-RSE-SLOT-FUNC-01",)


def test_the_same_build_reuses_its_run(store, clean_cases):
    """"Re-running the same build updates that run's executions instead of creating a second run"."""
    store.upsert_cases("RSE-1", clean_cases)
    a = store.create_run("RSE-1", "b1", "staging")
    store.record_execution(a.run_id, "TC-RSE-SLOT-FUNC-01", "PASS", actual="ok", duration_s=1.5)
    b = store.create_run("RSE-1", "b1", "staging")
    assert b.run_id == a.run_id
    data = json.loads(next(store.root.joinpath("runs").rglob("*.json")).read_text())
    assert data["executions"]["TC-RSE-SLOT-FUNC-01"]["status"] == "PASS"


def test_record_execution_stores_the_props_and_the_detail(store, clean_cases):
    store.upsert_cases("RSE-1", clean_cases)
    run = store.create_run("RSE-1", "b1", "staging")
    store.record_execution(run.run_id, "TC-RSE-SLOT-FUNC-01", "FAIL", actual="body.remaining: got 2",
                           duration_s=0.4, props={"http_status": "200", "endpoint": "/x"})
    data = json.loads(next(store.root.joinpath("runs").rglob("*.json")).read_text())
    row = data["executions"]["TC-RSE-SLOT-FUNC-01"]
    assert row["status"] == "FAIL" and row["actual"] == "body.remaining: got 2"
    assert row["props"]["http_status"] == "200"


def test_recording_against_an_unattached_case_raises(store, clean_cases):
    store.upsert_cases("RSE-1", clean_cases)
    run = store.create_run("RSE-1", "b1", "staging", extids=["TC-RSE-SLOT-FUNC-01"])
    with pytest.raises(RuntimeError, match="was the case attached"):
        store.record_execution(run.run_id, "TC-RSE-CAP-FUNC-01", "PASS")


def test_recording_against_an_unknown_run_raises(store):
    with pytest.raises(RuntimeError, match="was create_run called"):
        store.record_execution("nope", "TC-X", "PASS")


def test_health_reports_ok_and_no_network(store):
    assert store.health() == {"ok": True, "backend": "json", "root": str(store.root), "network": False}


# ---- the no-network guarantee -------------------------------------------------------------------------
def test_the_json_store_never_opens_a_socket(monkeypatch, store, clean_cases):
    """Every socket constructor is poisoned for the duration: a backend that reached the network would
    raise here rather than quietly needing a server the way the Kiwi one does."""
    def boom(*a, **kw):
        raise AssertionError("the json store must not touch the network")

    monkeypatch.setattr(socket, "socket", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)

    store.upsert_cases("RSE-1", clean_cases)
    store.list_cases("RSE-1", status="CONFIRMED", tags=("exec:http",))
    run = store.create_run("RSE-1", "b1", "staging", require_tag="exec:http")
    store.record_execution(run.run_id, run.attached[0], "PASS", actual="ok", duration_s=0.1)
    assert store.health()["network"] is False


def test_importing_the_workflow_does_not_import_tcms_api():
    """`tcms_api` is imported lazily inside KiwiCaseStore._rpc, so the CLI can scan this workflow with
    the dependency absent."""
    import sys

    import workflows.testing  # noqa: F401
    from workflows.testing.store import kiwi  # noqa: F401

    assert "tcms_api" not in sys.modules
