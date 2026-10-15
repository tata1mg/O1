"""Run identity: stable across resumes, immune to directory renames, inheritable by later stages."""

import json
import uuid

from rfc_agent import identity
from rfc_agent.contracts import RunContext


def _ctx(d, **inputs):
    d.mkdir(parents=True, exist_ok=True)
    return RunContext(run_dir=d, inputs=dict(inputs), config=None)


def _fresh():
    identity._cache.clear()  # load() caches per run dir for the process


def test_first_call_mints_and_persists(tmp_path):
    _fresh()
    ident = identity.load(_ctx(tmp_path / "PROJ-1-v1", jira="PROJ-1"))
    assert ident["session_id"] == "PROJ-1-v1"          # dir name stays the human label
    assert ident["workstream_id"] == "PROJ-1"          # ticket is the business identifier
    assert ident["workstream_source"] == "jira"       # provenance, so tagging never has to guess
    assert ident["run_id"] and ident["session_uuid"]
    assert ident["run_count"] == 1
    on_disk = json.loads((tmp_path / "PROJ-1-v1" / identity.FILENAME).read_text())
    # every field is stable now — the record persists verbatim, run_id included
    assert on_disk["run_id"] == ident["run_id"]
    assert on_disk["session_uuid"] == ident["session_uuid"]


def test_resume_keeps_every_id(tmp_path):
    d = tmp_path / "PROJ-1-v1"
    _fresh()
    first = identity.load(_ctx(d, jira="PROJ-1"))
    _fresh()
    second = identity.load(_ctx(d, jira="PROJ-1"))
    assert second["session_id"] == first["session_id"]       # the ask: resume keeps the session
    assert second["session_uuid"] == first["session_uuid"]
    assert second["run_id"] == first["run_id"]               # one run_id per stage lifecycle
    assert second["run_count"] == 2                          # only the resume counter moves


def test_pre_0_22_63_file_migrates_and_sheds_chain_keys(tmp_path):
    """Old files carried a per-invocation chain (root_run_id/last_run_id, no run_id). The stable
    run_id adopts root_run_id — in-flight lineages stay continuous with their exported traces —
    and the rewrite drops the dead keys."""
    d = tmp_path / "PROJ-1-v1"
    d.mkdir(parents=True)
    (d / identity.FILENAME).write_text(json.dumps({
        "workstream_id": "PROJ-1", "workstream_source": "jira",
        "workstream_uuid": str(uuid.uuid5(identity._NS, "PROJ-1")),
        "session_id": "PROJ-1-v1", "session_uuid": identity._uuid7(),
        "root_run_id": "01a00f00-dead-7eef-8000-000000000001",
        "last_run_id": "01a00f00-dead-7eef-8000-000000000009",
        "stage": "plan", "run_count": 4, "created_at": "2026-08-14T10:00:00+00:00",
    }))
    _fresh()
    ident = identity.load(_ctx(d, jira="PROJ-1"))
    assert ident["run_id"] == "01a00f00-dead-7eef-8000-000000000001"  # adopted, not re-minted
    assert ident["run_count"] == 5
    on_disk = json.loads((d / identity.FILENAME).read_text())
    assert "root_run_id" not in on_disk and "last_run_id" not in on_disk
    assert on_disk["run_id"] == ident["run_id"]


def test_rename_does_not_split_the_session(tmp_path):
    """The pre-identity bug: session_id was run_dir.name, so renaming a live run dir split it in two."""
    d = tmp_path / "PROJ-1-v1"
    _fresh()
    first = identity.load(_ctx(d, jira="PROJ-1"))
    moved = tmp_path / "PROJ-1-v1-final"
    d.rename(moved)
    _fresh()
    after = identity.load(_ctx(moved, jira="PROJ-1"))
    assert after["session_id"] == first["session_id"] == "PROJ-1-v1"
    assert after["session_uuid"] == first["session_uuid"]
    assert after["run_id"] == first["run_id"]


def test_same_name_different_roots_do_not_merge(tmp_path):
    """The mirror bug: only Path.name was used, so an archived copy pooled with a live run."""
    _fresh()
    live = identity.load(_ctx(tmp_path / "live" / "PROJ-1", jira="PROJ-1"))
    _fresh()
    arch = identity.load(_ctx(tmp_path / "archive" / "PROJ-1", jira="PROJ-1"))
    assert live["session_id"] == arch["session_id"]           # same label, deliberately
    assert live["session_uuid"] != arch["session_uuid"]       # different runs, provably
    assert live["run_id"] != arch["run_id"]


def test_workstream_uuid_is_derivable_not_stored(tmp_path):
    """Build/test must compute the same workstream uuid from the ticket alone — no registry read."""
    _fresh()
    a = identity.load(_ctx(tmp_path / "PROJ-9-v1", jira="PROJ-9"))
    _fresh()
    b = identity.load(_ctx(tmp_path / "PROJ-9-v2", jira="PROJ-9"))
    assert a["workstream_uuid"] == b["workstream_uuid"] == str(uuid.uuid5(identity._NS, "PROJ-9"))
    assert a["session_uuid"] != b["session_uuid"]             # separate experiments stay separate


def test_no_jira_falls_back_to_the_label(tmp_path):
    _fresh()
    ident = identity.load(_ctx(tmp_path / "2026-08-12-ai-overview", jira=None))
    assert ident["workstream_id"] == "2026-08-12-ai-overview"
    assert ident["workstream_source"] == "label"      # not a ticket; must not be tagged as one

    # the collision that defeats any derived test: dir named exactly after its ticket
    _fresh()
    same = identity.load(_ctx(tmp_path / "PROJ-8", jira="PROJ-8"))
    assert same["workstream_id"] == same["session_id"] == "PROJ-8"
    assert same["workstream_source"] == "jira"


def test_corrupt_file_degrades_never_raises(tmp_path):
    d = tmp_path / "PROJ-2"
    d.mkdir(parents=True)
    (d / identity.FILENAME).write_text("{ not json")
    _fresh()
    ident = identity.load(_ctx(d, jira="PROJ-2"))
    assert ident["session_id"] == "PROJ-2"      # falls back to the pre-identity behaviour
    assert ident["run_id"]                     # and still produces a usable identity


def test_uuid7_is_time_ordered_and_wellformed():
    """Ordering holds ACROSS milliseconds — there is no monotonic counter, so same-millisecond ids are
    deliberately unordered (RFC 9562 makes the counter optional). Lifecycle ids are minted once."""
    import time as _t

    ids = []
    for _ in range(5):
        ids.append(identity._uuid7())
        _t.sleep(0.002)
    assert ids == sorted(ids)                            # v7's whole point: chronological sort
    assert all(uuid.UUID(i).version == 7 for i in ids)
    assert len({identity._uuid7() for _ in range(200)}) == 200  # and unique regardless


def test_trace_metadata_is_flat_and_drops_nulls(tmp_path):
    _fresh()
    ident = identity.load(_ctx(tmp_path / "PROJ-3", jira="PROJ-3"))
    md = identity.trace_metadata(ident)
    assert md["workstream_id"] == "PROJ-3" and md["run_id"] == ident["run_id"]
    assert md["harness_stage"] == "plan"
    assert set(md) == {"workstream_id", "workstream_source", "workstream_uuid", "session_uuid",
                       "run_id", "harness_stage", "run_count"}
    assert md["workstream_source"] == "jira"
    assert "identity_degraded" not in md  # only a degraded fallback identity carries the flag
    assert all(not isinstance(v, (dict, list)) for v in md.values())
