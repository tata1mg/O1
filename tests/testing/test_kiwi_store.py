"""The Kiwi backend against a fake XML-RPC object.

`tcms-api` is not installed and there is no Kiwi to talk to, so `KiwiCaseStore` takes an injectable `rpc`.
That covers the part of the port that actually CHANGED — the single product-matching rule, the templates,
and the two attachment gates — rather than the parts copied verbatim from `kiwi_writer`/`kiwi_runner`.

What these tests cannot cover, and what still needs a live instance: whether `tcms-api`'s own client
accepts these calls, and the read-only `actual_duration` behaviour the start/stop-date trick works around.
"""

from __future__ import annotations

import pytest

from workflows.testing.store import STATUS, CaseStore
from workflows.testing.store.kiwi import KiwiCaseStore


class FakeTable:
    """One Kiwi entity type: filter/create/update/add_tag/add_case/add_property over a row list."""

    def __init__(self, rows=None, parent=None, name=""):
        self.rows = list(rows or [])
        self.parent = parent
        self.name = name

    def filter(self, query):
        return [r for r in self.rows if all(r.get(k) == v for k, v in (query or {}).items())]

    def create(self, fields):
        row = {"id": self.parent.next_id(), **fields}
        if self.name == "TestCase":
            # Kiwi's TestCase rows always carry the denormalised status name, which is what the read
            # side filters on. The writer only ever sets the numeric `case_status`.
            row["case_status__name"] = self.parent.status_name(fields.get("case_status"))
            self.parent.saved_by(row["id"])
        self.rows.append(row)
        self.parent.log.append((self.name, "create", dict(fields)))
        return row

    def update(self, row_id, fields):
        row = next(r for r in self.rows if r["id"] == row_id)
        row.update(fields)
        if self.name == "TestCase":
            if "case_status" in fields:
                row["case_status__name"] = self.parent.status_name(fields["case_status"])
            self.parent.saved_by(row_id)
        self.parent.log.append((self.name, "update", row_id, dict(fields)))
        return row

    # attachments: every Kiwi object that holds files exposes this pair
    def list_attachments(self, obj_id):
        return list(self.parent.attachments.get((self.name, obj_id), []))

    def add_attachment(self, obj_id, filename, b64content):
        rows = self.parent.attachments.setdefault((self.name, obj_id), [])
        rows.append({"pk": self.parent.next_id(), "url": f"https://kiwi.test/uploads/{filename}",
                     "content": b64content})

    def add_tag(self, case_id, tag):
        self.parent.tags.setdefault(case_id, []).append(tag)
        self.parent.log.append((self.name, "add_tag", case_id, tag))

    def remove_tag(self, case_id, tag):
        self.parent.tags[case_id] = [t for t in self.parent.tags.get(case_id, []) if t != tag]
        self.parent.log.append((self.name, "remove_tag", case_id, tag))

    def add_case(self, container_id, case_id):
        self.parent.log.append((self.name, "add_case", container_id, case_id))
        # Kiwi's plan<->case link is an M2M, which is why `TestCase.filter({"plan": id})` works on the
        # read side even though the writer never sets a `plan` field on the case itself.
        self.parent.plan_cases.setdefault(container_id, []).append(case_id)

    def add_property(self, ex_id, key, value):
        self.parent.props.setdefault(ex_id, {})[key] = value


class FakeRPC:
    """Just enough Kiwi to exercise the store. Tag.filter({"case": id}) is served from `tags`."""

    def __init__(self, products=(), statuses=("PROPOSED", "CONFIRMED", "DISABLED", "NEED_UPDATE")):
        self._id = 100
        self.log: list = []
        self.plan_cases: dict[int, list[int]] = {}
        self.tags: dict[int, list[str]] = {}
        self.props: dict[int, dict] = {}
        self.history: dict[int, list[dict]] = {}
        self.comments: dict[int, list[str]] = {}
        self.attachments: dict[tuple, list[dict]] = {}
        #: who Kiwi records as saving the next change: 7 is the agent's account ("svc"), anything else a human
        self.acting_user = 7
        for name in ("Classification", "Product", "Version", "PlanType", "TestCaseStatus", "TestPlan",
                     "Category", "Priority", "TestCase", "Build", "TestRun", "User",
                     "TestExecutionStatus", "TestExecution"):
            setattr(self, name, FakeTable(parent=self, name=name))
        self.Classification.rows = [{"id": 1, "name": "Diagno"}]
        self.Product.rows = [{"id": i + 10, "name": n} for i, n in enumerate(products)]
        self.PlanType.rows = [{"id": 2, "name": "Unit"}]
        self.TestCaseStatus.rows = [{"id": 30 + i, "name": s} for i, s in enumerate(statuses)]
        self.Priority.rows = [{"id": 4, "value": "P1"}, {"id": 5, "value": "P3"},
                              {"id": 6, "value": "P5"}]
        self.User.rows = [{"id": 7, "username": "svc"}, {"id": 8, "username": "qa.reviewer"}]
        self.TestExecutionStatus.rows = [{"id": 20 + i, "name": n} for i, n in enumerate(STATUS.values())]
        self.Tag = FakeTable(parent=self, name="Tag")
        self.Tag.filter = lambda q: [{"name": t} for t in self.tags.get(q.get("case"), [])]
        plain_case_filter = self.TestCase.filter

        def case_filter(query):
            query = dict(query or {})
            plan = query.pop("plan", None)
            rows = plain_case_filter(query)
            if plan is not None:
                members = self.plan_cases.get(plan, [])
                rows = [r for r in rows if r["id"] in members]
            return rows

        self.TestCase.filter = case_filter
        self.TestCase.history = lambda case_id, query=None: list(self.history.get(case_id, []))
        self.TestCase.comments = lambda case_id: [{"comment": c} for c in self.comments.get(case_id, [])]
        self.TestCase.add_comment = lambda case_id, text: self.comments.setdefault(case_id, []).append(text)
        self.Attachment = FakeTable(parent=self, name="Attachment")

        def remove_attachment(pk):
            for rows in self.attachments.values():
                rows[:] = [a for a in rows if a["pk"] != pk]

        self.Attachment.remove_attachment = remove_attachment

    def saved_by(self, case_id):
        self.history.setdefault(case_id, []).append({"history_user_id": self.acting_user})

    def next_id(self):
        self._id += 1
        return self._id

    def status_name(self, status_id):
        return next((s["name"] for s in self.TestCaseStatus.rows if s["id"] == status_id), "PROPOSED")


def store(rpc, **kw) -> KiwiCaseStore:
    kw.setdefault("user", "svc")
    return KiwiCaseStore(url="https://kiwi.test/xml-rpc/", password="s3cret",
                         web="https://kiwi.test", rpc=rpc, **kw)


def case_row(rpc, extid):
    return next(r for r in rpc.TestCase.rows if f"extid:{extid}" in rpc.tags.get(r["id"], []))


def review(rpc, extid=None, status="CONFIRMED", fields=None, user=8):
    """A human in Kiwi: set `status` (and any `fields`) on one case, or on every case."""
    status_id = next(s["id"] for s in rpc.TestCaseStatus.rows if s["name"] == status)
    rpc.acting_user = user
    rows = [case_row(rpc, extid)] if extid else list(rpc.TestCase.rows)
    for row in rows:
        rpc.TestCase.update(row["id"], {"case_status": status_id, **(fields or {})})
    rpc.acting_user = 7


def reindex(rpc):
    """Let `TestCase.filter({"tag__name": ...})` find cases by tag, so a second upsert matches them."""
    rpc.TestCase.filter = lambda q: (
        [c for c in rpc.TestCase.rows if q.get("tag__name", "") in rpc.tags.get(c["id"], [])]
        if "tag__name" in (q or {}) else
        [c for c in rpc.TestCase.rows if c["id"] in rpc.plan_cases.get(q.get("plan"), [])]
        if "plan" in (q or {}) else list(rpc.TestCase.rows)
    )


def test_it_satisfies_the_protocol():
    assert isinstance(store(FakeRPC()), CaseStore)


def test_no_password_default_exists_in_code():
    """There is no default password in code."""
    import inspect

    from workflows.testing.store import kiwi

    src = inspect.getsource(kiwi)
    assert "Kiwi-Admin" not in src
    sig = inspect.signature(KiwiCaseStore.__init__)
    assert sig.parameters["password"].default == ""


# ---- the single product-matching rule (README change 2) -----------------------------------------------
def test_the_product_is_matched_by_exact_prefixed_name():
    rpc = FakeRPC(products=["Diagno / RSE-1", "Diagno / RSE-1-OLD", "Other / RSE-1"])
    assert store(rpc)._product(rpc, "RSE-1")["name"] == "Diagno / RSE-1"


def test_a_legacy_product_created_under_another_prefix_still_resolves():
    rpc = FakeRPC(products=["Legacy / RSE-1"])
    assert store(rpc)._product(rpc, "RSE-1")["name"] == "Legacy / RSE-1"


def test_the_substring_rule_is_gone():
    """`be_requirements.kiwi_cases` matched `feature in p["name"]`, so "RSE-1" resolved to
    "Diagno / RSE-1-OLD". That was the loosest of the three rules and it is not reproduced."""
    rpc = FakeRPC(products=["Diagno / RSE-1-OLD"])
    with pytest.raises(RuntimeError, match="no product named 'Diagno / RSE-1'"):
        store(rpc)._product(rpc, "RSE-1")


def test_the_error_names_the_flag_that_fixes_it():
    rpc = FakeRPC(products=[])
    with pytest.raises(RuntimeError, match="testing.product_prefix"):
        store(rpc)._product(rpc, "RSE-1")


def test_the_product_prefix_is_a_flag():
    rpc = FakeRPC(products=["Acme :: RSE-1"])
    s = store(rpc, product_prefix="Acme :: ")
    assert s.product_name("RSE-1") == "Acme :: RSE-1"
    assert s._product(rpc, "RSE-1")["name"] == "Acme :: RSE-1"


# ---- upsert -------------------------------------------------------------------------------------------
def test_upsert_creates_the_product_plan_and_cases(clean_cases):
    rpc = FakeRPC()
    summary = store(rpc).upsert_cases("RSE-1", clean_cases)
    assert summary["created"] == len(clean_cases) and summary["updated"] == 0
    assert summary["product"] == "Diagno / RSE-1"
    assert summary["plan"] == "RSE-1 — imported test cases"
    assert summary["plan_url"].startswith("https://kiwi.test/plan/")
    assert summary["backend"] == "kiwi"


def test_upsert_tags_every_case_with_exec_and_surface(clean_cases):
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    tags = {tag for tags in rpc.tags.values() for tag in tags}
    assert "exec:http" in tags and "surface:backend" in tags
    assert "feature:RSE-1" in tags
    assert "extid:TC-RSE-SLOT-FUNC-01" in tags


def test_upsert_matches_an_existing_case_on_its_extid_within_the_feature(clean_cases):
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    reindex(rpc)
    again = store(rpc).upsert_cases("RSE-1", clean_cases)
    assert (again["created"], again["unchanged"]) == (0, len(clean_cases))
    changed = store(rpc).upsert_cases("RSE-1", [{**c, "title": c["title"] + "!"} for c in clean_cases])
    assert (changed["created"], changed["updated"]) == (0, len(clean_cases))


def test_a_retagged_case_loses_its_old_exec_tag(clean_cases):
    """The store used to only add tags, so an http case retagged manual kept `exec:http` and still ran."""
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    reindex(rpc)
    first = clean_cases[0]
    case_id = case_row(rpc, first["id"])["id"]
    rpc.tags[case_id].append("needs-device")                 # a person's own tag
    retagged = [{**first, "execution": "manual"}, *clean_cases[1:]]
    summary = store(rpc).upsert_cases("RSE-1", retagged)
    assert summary["updated"] == 1 and summary["unchanged"] == len(clean_cases) - 1
    tags = rpc.tags[case_id]
    assert "exec:manual" in tags and "exec:http" not in tags
    assert "needs-device" in tags and f"extid:{first['id']}" in tags
    assert not [e for e in rpc.log if e[:2] == ("TestCase", "update") and e[2] == case_id]  # fields untouched
    assert store(rpc).upsert_cases("RSE-1", retagged)["unchanged"] == len(clean_cases)


def test_a_reviewed_case_gets_a_tag_suggestion_not_a_retag(clean_cases):
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    reindex(rpc)
    first = clean_cases[0]
    review(rpc, first["id"], "CONFIRMED")
    case_id = case_row(rpc, first["id"])["id"]
    summary = store(rpc).upsert_cases("RSE-1", [{**first, "execution": "manual"}, *clean_cases[1:]])
    assert summary["protected_ids"] == [first["id"]]
    assert "exec:http" in rpc.tags[case_id] and "exec:manual" not in rpc.tags[case_id]
    assert "tags: -exec:http +exec:manual" in rpc.comments[case_id][0]


# ---- review: PROPOSED on upload, and the human edit wins ----------------------------------------------
def test_cases_are_uploaded_proposed_and_never_re_confirmed(clean_cases):
    """test_agent created every case CONFIRMED and then re-CONFIRMED them all; only a reviewer approves."""
    rpc = FakeRPC()
    summary = store(rpc).upsert_cases("RSE-1", clean_cases)
    assert summary["status"] == "PROPOSED"
    assert {r["case_status__name"] for r in rpc.TestCase.rows} == {"PROPOSED"}
    assert not [e for e in rpc.log if e[0] == "TestCase" and e[1] == "update"]
    assert sum(1 for e in rpc.log if e[0] == "TestPlan" and e[1] == "add_case") == len(clean_cases)


def test_a_missing_proposed_status_is_named(clean_cases):
    with pytest.raises(RuntimeError, match="no PROPOSED case status"):
        store(FakeRPC(statuses=("CONFIRMED",))).upsert_cases("RSE-1", clean_cases)


def test_a_reviewed_case_is_never_overwritten(clean_cases):
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    reindex(rpc)
    first = clean_cases[0]["id"]
    review(rpc, first, "CONFIRMED")
    before = dict(case_row(rpc, first))
    summary = store(rpc).upsert_cases("RSE-1", [{**c, "title": "Renamed"} for c in clean_cases])
    assert summary["protected_ids"] == [first]
    assert summary["updated"] == len(clean_cases) - 1
    assert case_row(rpc, first) == before                   # status and every field as the reviewer left it
    assert "Agent suggestion for " + first in rpc.comments[case_row(rpc, first)["id"]][0]
    assert "status is CONFIRMED" in rpc.comments[case_row(rpc, first)["id"]][0]


def test_a_proposed_case_a_human_edited_is_never_overwritten(clean_cases):
    """Still PROPOSED, but `TestCase.history` shows a save by someone other than the agent."""
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    reindex(rpc)
    first = clean_cases[0]["id"]
    review(rpc, first, "PROPOSED", fields={"text": "rewritten by QA"})
    summary = store(rpc).upsert_cases("RSE-1", [{**c, "title": "Renamed"} for c in clean_cases])
    assert summary["protected_ids"] == [first]
    assert case_row(rpc, first)["text"] == "rewritten by QA"
    assert "edited by user 8" in rpc.comments[case_row(rpc, first)["id"]][0]


def test_the_same_suggestion_is_commented_once(clean_cases):
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    reindex(rpc)
    review(rpc, clean_cases[0]["id"], "DISABLED")
    renamed = [{**c, "title": "Renamed"} for c in clean_cases]
    store(rpc).upsert_cases("RSE-1", renamed)
    store(rpc).upsert_cases("RSE-1", renamed)
    assert len(rpc.comments[case_row(rpc, clean_cases[0]["id"])["id"]]) == 1


def test_an_unknown_agent_account_protects_every_existing_case(clean_cases):
    """Credentials from ~/.tcms.conf name no user here, so authorship cannot be told apart: keep, don't guess."""
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    reindex(rpc)
    summary = store(rpc, user="").upsert_cases("RSE-1", [{**c, "title": "Renamed"} for c in clean_cases])
    assert summary["protected"] == len(clean_cases) and summary["updated"] == 0


def test_the_plan_and_run_templates_are_flags(clean_cases):
    rpc = FakeRPC()
    s = store(rpc, plan_template="{feature} QA", run_template="{feature}/{env}/{build}")
    assert s.upsert_cases("RSE-1", clean_cases)["plan"] == "RSE-1 QA"
    rpc.Version.rows.append({"id": 50, "product": 101, "value": "unspecified"})
    run = s.create_run("RSE-1", "b1", "staging")
    assert rpc.TestRun.rows[0]["summary"] == "RSE-1/staging/b1"
    assert run.url == f"https://kiwi.test/runs/{run.run_id}/"


def test_an_empty_upsert_reports_an_error(clean_cases):
    s = store(FakeRPC()).upsert_cases("RSE-1", [])
    assert s["total"] == 0 and s["error"] == "no cases supplied"


# ---- reads and runs -----------------------------------------------------------------------------------
def _seeded(clean_cases):
    """The suite uploaded, then approved in Kiwi by a reviewer."""
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    review(rpc)
    return rpc


def test_list_cases_filters_on_status_and_tags(clean_cases):
    rpc = _seeded(clean_cases)
    cases = store(rpc).list_cases("RSE-1", status="CONFIRMED", tags=("exec:http",))
    assert {c.extid for c in cases} == {"TC-RSE-SLOT-FUNC-01", "TC-RSE-SLOT-NEG-01",
                                        "TC-RSE-CAP-FUNC-01"}
    assert store(rpc).list_cases("RSE-1", status="PROPOSED") == []
    review(rpc, "TC-RSE-SLOT-NEG-01", "PROPOSED")
    assert [c.extid for c in store(rpc).list_cases("RSE-1", status="PROPOSED")] == ["TC-RSE-SLOT-NEG-01"]


def test_list_cases_returns_the_case_text_the_deriver_reads(clean_cases):
    rpc = _seeded(clean_cases)
    case = next(c for c in store(rpc).list_cases("RSE-1") if c.extid == "TC-RSE-SLOT-FUNC-01")
    assert "1. Send GET /radiology/v1/slots?lab_id=42" in case.text


def test_derive_spec_works_against_the_kiwi_backend(clean_cases):
    """The deriver is backend-agnostic: the same transcription off either store."""
    from workflows.testing import be_spec

    rpc = _seeded(clean_cases)
    spec, dropped, weak, total = be_spec.derive(store(rpc), "RSE-1")
    assert total == 3 and dropped == [] and weak == []
    assert {e["extid"] for e in spec} == {"TC-RSE-SLOT-FUNC-01", "TC-RSE-SLOT-NEG-01",
                                          "TC-RSE-CAP-FUNC-01"}


def test_create_run_applies_the_confirmed_and_tag_gates(clean_cases):
    rpc = _seeded(clean_cases)
    rpc.Version.rows.append({"id": 50, "product": 101, "value": "unspecified"})
    # one case is not CONFIRMED: the review gate must exclude it
    unconfirmed = next(r for r in rpc.TestCase.rows
                       if "extid:TC-RSE-SLOT-NEG-01" in rpc.tags.get(r["id"], []))
    unconfirmed["case_status__name"] = "PROPOSED"

    run = store(rpc).create_run("RSE-1", "b1", "staging", require_tag="exec:http")
    assert set(run.attached) == {"TC-RSE-SLOT-FUNC-01", "TC-RSE-CAP-FUNC-01"}
    assert run.skipped_unconfirmed == ("TC-RSE-SLOT-NEG-01",)
    assert any("exec:manual" in s for s in run.skipped_wrong_execution)


def test_the_same_summary_reuses_its_run(clean_cases):
    rpc = _seeded(clean_cases)
    rpc.Version.rows.append({"id": 50, "product": 101, "value": "unspecified"})
    a = store(rpc).create_run("RSE-1", "b1", "staging")
    b = store(rpc).create_run("RSE-1", "b1", "staging")
    assert a.run_id == b.run_id and len(rpc.TestRun.rows) == 1


def test_record_execution_sets_the_timestamps_rather_than_actual_duration(clean_cases):
    rpc = _seeded(clean_cases)
    rpc.Version.rows.append({"id": 50, "product": 101, "value": "unspecified"})
    run = store(rpc).create_run("RSE-1", "b1", "staging")
    case = next(r for r in rpc.TestCase.rows
                if "extid:TC-RSE-SLOT-FUNC-01" in rpc.tags.get(r["id"], []))
    rpc.TestExecution.rows.append({"id": 900, "run": int(run.run_id), "case": case["id"]})

    ex_id = store(rpc).record_execution(run.run_id, "TC-RSE-SLOT-FUNC-01", "FAIL",
                                        actual="body.remaining: got 2", duration_s=1.23,
                                        props={"http_status": "200"})
    row = next(r for r in rpc.TestExecution.rows if r["id"] == 900)
    assert "actual_duration" not in row               # read-only in Kiwi; derived from the two dates
    assert "." in row["start_date"] and "." in row["stop_date"]   # microsecond precision
    assert row["stop_date"].endswith("+00:00")                     # an explicit offset, never naive
    assert row["status"] == next(s["id"] for s in rpc.TestExecutionStatus.rows if s["name"] == "FAILED")
    assert rpc.props[900]["actual"] == "body.remaining: got 2"
    assert rpc.props[900]["http_status"] == "200"
    assert ex_id == "900"


def test_a_long_property_value_is_truncated(clean_cases):
    from workflows.testing.store.kiwi import ACTUAL_VALUE_MAX, PROP_VALUE_MAX

    rpc = _seeded(clean_cases)
    rpc.Version.rows.append({"id": 50, "product": 101, "value": "unspecified"})
    run = store(rpc).create_run("RSE-1", "b1", "staging")
    case = next(r for r in rpc.TestCase.rows
                if "extid:TC-RSE-SLOT-FUNC-01" in rpc.tags.get(r["id"], []))
    rpc.TestExecution.rows.append({"id": 901, "run": int(run.run_id), "case": case["id"]})
    store(rpc).record_execution(run.run_id, "TC-RSE-SLOT-FUNC-01", "PASS",
                                actual="a" * 5000, props={"endpoint": "b" * 5000})
    assert len(rpc.props[901]["actual"]) == ACTUAL_VALUE_MAX == 255       # Kiwi 16: varchar(255), rejected above
    assert len(rpc.props[901]["endpoint"]) == PROP_VALUE_MAX == 255
    store(rpc).record_execution(run.run_id, "TC-RSE-SLOT-FUNC-01", "FAIL", actual="line one\nline two")
    assert rpc.props[901]["actual"] == "line one line two"


def test_recording_for_an_unattached_case_raises(clean_cases):
    rpc = _seeded(clean_cases)
    rpc.Version.rows.append({"id": 50, "product": 101, "value": "unspecified"})
    run = store(rpc).create_run("RSE-1", "b1", "staging")
    with pytest.raises(RuntimeError, match="was the case attached"):
        store(rpc).record_execution(run.run_id, "TC-RSE-SLOT-FUNC-01", "PASS")


def test_an_unknown_status_is_rejected_before_any_write(clean_cases):
    with pytest.raises(ValueError, match=r"status must be one of \['PASS', 'FAIL', 'BLOCKED', 'ERROR'\]"):
        store(_seeded(clean_cases)).record_execution("1", "TC-X", "SKIPPED")


def test_health_reports_rather_than_raising():
    class Broken:
        def __getattr__(self, name):
            raise ConnectionRefusedError("kiwi is down")

    result = KiwiCaseStore(rpc=Broken()).health()
    assert result["ok"] is False and "ConnectionRefusedError" in result["error"]
    assert result["network"] is True


def test_record_execution_uses_the_actual_finish_time(clean_cases):
    """Naive UTC on a KIWI_USE_TZ=True / Asia/Kolkata instance was stored 5h30m off; the stop time is also
    when the test ended, not when its row was written."""
    from datetime import UTC, datetime, timedelta, timezone

    rpc = _seeded(clean_cases)
    rpc.Version.rows.append({"id": 50, "product": 101, "value": "unspecified"})
    run = store(rpc).create_run("RSE-1", "b1", "staging")
    rpc.TestExecution.rows.append({"id": 902, "run": int(run.run_id),
                                   "case": case_row(rpc, "TC-RSE-SLOT-FUNC-01")["id"]})
    ist = timezone(timedelta(hours=5, minutes=30))
    store(rpc).record_execution(run.run_id, "TC-RSE-SLOT-FUNC-01", "PASS", duration_s=2.0,
                                finished_at=datetime(2026, 10, 30, 15, 0, 0, tzinfo=ist))
    row = next(r for r in rpc.TestExecution.rows if r["id"] == 902)
    assert row["stop_date"] == "2026-10-30T15:00:00.000000+05:30"
    assert row["start_date"] == "2026-10-30T14:59:58.000000+05:30"
    store(rpc).record_execution(run.run_id, "TC-RSE-SLOT-FUNC-01", "PASS", duration_s=1.0,
                                finished_at=datetime(2026, 10, 30, 9, 0, 0))   # naive -> read as UTC
    assert row["stop_date"] == datetime(2026, 10, 30, 9, tzinfo=UTC).isoformat(timespec="microseconds")


# ---- attachments (ported from test_agent kiwi_runner.add_attachment, 0188771) -------------------------
def test_attach_is_idempotent_by_name_unless_replaced():
    import base64

    rpc = FakeRPC()
    s = store(rpc)
    assert s.attach("execution", "900", "response-TC-1.json", b"one")
    assert s.attach("execution", "900", "response-TC-1.json", b"two")
    rows = rpc.attachments[("TestExecution", 900)]
    assert len(rows) == 1 and base64.b64decode(rows[0]["content"]) == b"one"
    assert s.attach("execution", "900", "response-TC-1.json", b"two", replace=True)
    rows = rpc.attachments[("TestExecution", 900)]
    assert len(rows) == 1 and base64.b64decode(rows[0]["content"]) == b"two"


def test_each_scope_reaches_its_own_kiwi_object():
    rpc = FakeRPC()
    s = store(rpc)
    for scope, api in (("case", "TestCase"), ("run", "TestRun"), ("plan", "TestPlan")):
        s.attach(scope, "5", "report.json", b"{}")
        assert (api, 5) in rpc.attachments
    with pytest.raises(ValueError, match="scope must be one of"):
        s.attach("product", "5", "x", b"")


def test_attachment_url_reads_back_by_name_with_a_suffixed_fallback():
    rpc = FakeRPC()
    rpc.attachments[("TestExecution", 900)] = [
        {"pk": 1, "url": "https://kiwi.test/uploads/response_qWGCM4Y.json"},
        {"pk": 2, "url": "https://kiwi.test/uploads/maestro.log"},
    ]
    s = store(rpc)
    assert s.attachment_url("execution", "900", "maestro.log").endswith("/maestro.log")
    assert s.attachment_url("execution", "900", "response.json").endswith("response_qWGCM4Y.json")
    assert s.attachment_url("execution", "900", "absent.png") == ""


def test_a_failing_attachment_is_reported_not_raised():
    rpc = FakeRPC()

    def boom(*a):
        raise ConnectionResetError("gone")

    rpc.TestExecution.add_attachment = boom
    assert store(rpc).attach("execution", "900", "r.json", b"{}") is False


def test_an_execution_comment_is_posted_once():
    rpc = FakeRPC()
    posted: list = []
    rpc.TestExecution.get_comments = lambda ex_id: [{"comment": t} for i, t in posted if i == ex_id]
    rpc.TestExecution.add_comment = lambda ex_id, text: posted.append((ex_id, text))
    s = store(rpc)
    assert s.comment_execution("5", "## TC-A-01 — PASS") and s.comment_execution("5", "## TC-A-01 — PASS")
    assert posted == [(5, "## TC-A-01 — PASS")]


def test_a_failing_comment_is_reported_not_raised():
    rpc = FakeRPC()

    def boom(*a):
        raise RuntimeError("kiwi down")

    rpc.TestExecution.get_comments = boom
    assert store(rpc).comment_execution("5", "x") is False


def test_kiwi_escaping_is_not_a_change_and_is_undone_on_read(clean_cases):
    """Kiwi stores `"` and `'` HTML-escaped. Unescaped compare: an unchanged case stays unchanged; unescaped
    read: derive_spec sees the step text as written."""
    import html as html_mod

    rpc = FakeRPC()
    quoted = [{**clean_cases[0], "title": "the 'You' tab", "steps": 'Assert body.status == "ok"'}]
    store(rpc).upsert_cases("RSE-1", quoted)
    reindex(rpc)
    for row in rpc.TestCase.rows:
        row["summary"], row["text"] = html_mod.escape(row["summary"]), html_mod.escape(row["text"])
    assert store(rpc).upsert_cases("RSE-1", quoted)["unchanged"] == 1
    case = store(rpc).list_cases("RSE-1")[0]
    assert "'You'" in case.summary and '"ok"' in case.text
