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
        self.rows.append(row)
        self.parent.log.append((self.name, "create", dict(fields)))
        return row

    def update(self, row_id, fields):
        row = next(r for r in self.rows if r["id"] == row_id)
        row.update(fields)
        self.parent.log.append((self.name, "update", row_id, dict(fields)))
        return row

    def add_tag(self, case_id, tag):
        self.parent.tags.setdefault(case_id, []).append(tag)

    def add_case(self, container_id, case_id):
        self.parent.log.append((self.name, "add_case", container_id, case_id))
        # Kiwi's plan<->case link is an M2M, which is why `TestCase.filter({"plan": id})` works on the
        # read side even though the writer never sets a `plan` field on the case itself.
        self.parent.plan_cases.setdefault(container_id, []).append(case_id)

    def add_property(self, ex_id, key, value):
        self.parent.props.setdefault(ex_id, {})[key] = value


class FakeRPC:
    """Just enough Kiwi to exercise the store. Tag.filter({"case": id}) is served from `tags`."""

    def __init__(self, products=(), statuses=("CONFIRMED",)):
        self._id = 100
        self.log: list = []
        self.plan_cases: dict[int, list[int]] = {}
        self.tags: dict[int, list[str]] = {}
        self.props: dict[int, dict] = {}
        for name in ("Classification", "Product", "Version", "PlanType", "TestCaseStatus", "TestPlan",
                     "Category", "Priority", "TestCase", "Build", "TestRun", "User",
                     "TestExecutionStatus", "TestExecution"):
            setattr(self, name, FakeTable(parent=self, name=name))
        self.Classification.rows = [{"id": 1, "name": "Diagno"}]
        self.Product.rows = [{"id": i + 10, "name": n} for i, n in enumerate(products)]
        self.PlanType.rows = [{"id": 2, "name": "Unit"}]
        self.TestCaseStatus.rows = [{"id": 3, "name": s} for s in statuses]
        self.Priority.rows = [{"id": 4, "value": "P1"}, {"id": 5, "value": "P3"},
                              {"id": 6, "value": "P5"}]
        self.User.rows = [{"id": 7, "username": "svc"}]
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

    def next_id(self):
        self._id += 1
        return self._id

    def status_name(self, status_id):
        return next((s["name"] for s in self.TestCaseStatus.rows if s["id"] == status_id), "PROPOSED")


def store(rpc, **kw) -> KiwiCaseStore:
    return KiwiCaseStore(url="https://kiwi.test/xml-rpc/", user="svc", password="s3cret",
                         web="https://kiwi.test", rpc=rpc, **kw)


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
    # the fake keeps the tag index, so a second pass finds every case again
    rpc.TestCase.filter = lambda q: (
        [c for c in rpc.TestCase.rows
         if q.get("tag__name", "").startswith("extid:")
         and q["tag__name"] in rpc.tags.get(c["id"], [])]
        or [c for c in rpc.TestCase.rows
            if q.get("tag__name", "") in rpc.tags.get(c["id"], [])]
    )
    summary = store(rpc).upsert_cases("RSE-1", clean_cases)
    assert summary["updated"] == len(clean_cases) and summary["created"] == 0


def test_every_case_is_re_confirmed_after_being_added_to_the_plan(clean_cases):
    """`TestPlan.add_case` resets the status to NEED_UPDATE, so a final confirm pass is required or the
    cases cannot be attached to a run."""
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases[:1])
    updates = [e for e in rpc.log if e[0] == "TestCase" and e[1] == "update"]
    assert updates and updates[-1][3] == {"case_status": 3}
    assert any(e[0] == "TestPlan" and e[1] == "add_case" for e in rpc.log)


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
    rpc = FakeRPC()
    store(rpc).upsert_cases("RSE-1", clean_cases)
    return rpc


def test_list_cases_filters_on_status_and_tags(clean_cases):
    rpc = _seeded(clean_cases)
    cases = store(rpc).list_cases("RSE-1", status="CONFIRMED", tags=("exec:http",))
    assert {c.extid for c in cases} == {"TC-RSE-SLOT-FUNC-01", "TC-RSE-SLOT-NEG-01",
                                        "TC-RSE-CAP-FUNC-01"}
    assert store(rpc).list_cases("RSE-1", status="PROPOSED") == []


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
    assert len(rpc.props[901]["actual"]) == ACTUAL_VALUE_MAX
    assert len(rpc.props[901]["endpoint"]) == PROP_VALUE_MAX


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
