"""The executor's assertion semantics.

Every test is one `check(...)` call with fixed expectations. `subprocess.run` is replaced so no request leaves
the machine.

The rule these protect: **a FAIL must mean a defect.** Every case here is one where the executor could
otherwise report a defect that does not exist, or miss one that does — both are the same lie, pointing in
different directions.
"""

from __future__ import annotations

import json
import unittest.mock as mock

import pytest

from workflows.testing import be_execute
from workflows.testing.be_execute import run_case

ENV = {"origin": "https://svc.test", "headers": {}, "cookie": None, "method": "GET", "body": None}

SLOTS = {"slots": [{"starttime": "11:00", "available": True,
                    "departments": [{"department": "radio_mri", "available": True,
                                     "remaining_capacity": 2}]}]}


def respond(payload, status="200"):
    """Ported verbatim from the original (:29-37): a fake subprocess.run returning curl's own framing."""
    body = (json.dumps(payload) if payload is not None else "<html>not json</html>")

    def fake_run(cmd, **kw):
        class R:
            stdout = f"{body}\n__STATUS__{status}"
        return R()
    return mock.patch.object(be_execute.subprocess, "run", fake_run)


def check(payload, case, want_status, *, status="200", detail_has=None):
    with respond(payload, status):
        r = run_case({"extid": "T", "path": "/s", **case}, ENV)
    assert r["status"] == want_status, f"got {r['status']} ({r['detail'][:90]}), want {want_status}"
    if detail_has:
        assert detail_has in r["detail"], f"detail {r['detail'][:90]!r} lacks {detail_has!r}"


# ---- `equals: null` means "must not be there" (source :44-63) ---------------------------------------
def test_absent_element_satisfies_equals_null():
    """Generated suites express "exactly N slots" as an assertion that element N is null. Treating
    absent as a failure would report a defect for behaving as specified."""
    check(SLOTS, {"expect_status": 200, "assert": [
        {"path": "slots.0.departments.1", "equals": None},
        {"path": "slots.1", "equals": None}]}, "PASS")


def test_present_but_null_also_satisfies_equals_null():
    check({"slots": [{"departments": None}]},
          {"expect_status": 200, "assert": [{"path": "slots.0.departments", "equals": None}]},
          "PASS")


def test_an_element_that_should_be_absent_but_is_present_still_fails():
    check({"slots": [{"departments": [{"department": "a"}, {"department": "b"}]}]},
          {"expect_status": 200, "assert": [{"path": "slots.0.departments.1", "equals": None}]},
          "FAIL")


# ---- a genuinely missing field is still a defect (source :66-82) ------------------------------------
def test_real_contract_drift_still_fails():
    check(SLOTS, {"expect_status": 200, "assert": [{"path": "slots.0.nonexistent", "equals": "x"}]},
          "FAIL", detail_has="key not in response")


def test_wrong_value_still_fails():
    check(SLOTS, {"expect_status": 200,
                  "assert": [{"path": "slots.0.departments.0.department", "equals": "radio_ct"}]},
          "FAIL")


def test_happy_path_passes():
    check(SLOTS, {"expect_status": 200, "assert": [
        {"path": "slots.0.starttime", "present": True},
        {"path": "slots.0.departments.0.remaining_capacity", "equals": 2}]}, "PASS")


# ---- transport is never a defect (source :85-92) -----------------------------------------------------
# Measured 2026-08-21: a Cloudflare 503 was first written up as two bugs that did not exist.
@pytest.mark.parametrize("code,want", [("503", "ERROR"), ("401", "BLOCKED"),
                                       ("404", "BLOCKED"), ("429", "BLOCKED")])
def test_transport_codes_are_never_fail(code, want):
    check(SLOTS, {"expect_status": 200}, want, status=code)


def test_non_json_body_is_error_not_fail():
    check(None, {"expect_status": 200}, "ERROR")


# ---- an unmet precondition is BLOCKED, never a false PASS (source :95-99) ----------------------------
def test_unmet_precondition_is_blocked():
    check({"slots": [], "cohort": ""},
          {"expect_status": 200, "require": [{"path": "cohort", "non_empty": True}],
           "assert": [{"path": "slots", "present": True}]},
          "BLOCKED", detail_has="precondition not met")


# ---- an unasked-for 400 is a bad request, not a defect (source :102-113) -----------------------------
def test_unexpected_400_is_blocked_not_fail():
    """The CP contract documented "unchanged path/params" without listing them, so derived cases omitted
    types/type. Four came back FAIL against a service healthy enough to return three PASSes seconds
    later."""
    check({"error": "bad request"}, {"expect_status": 200}, "BLOCKED", status="400",
          detail_has="check the parameters")


def test_a_case_that_expects_400_still_evaluates_normally():
    check({"ok": True}, {"expect_status": 400, "assert": [{"path": "ok", "equals": True}]},
          "PASS", status="400")


# ---- an uncompilable pattern is ERROR, and does not take the run with it (source :116-129) ----------
def test_invalid_pattern_is_error_not_a_crash():
    """A mis-transcribed pattern used to raise re.error out of run_case, aborting the suite: 2026-08-26,
    7 cases had passed and the remaining 18 were left IDLE."""
    check({"url": "https://cdn.test/a.png"},
          {"expect_status": 200, "assert": [{"path": "url", "matches": "^https:\\"}]},
          "ERROR", detail_has="not a valid regular expression")


def test_an_invalid_pattern_in_a_precondition_is_error_too():
    check({"url": "https://cdn.test/a.png"},
          {"expect_status": 200, "require": [{"path": "url", "matches": "("}]},
          "ERROR", detail_has="not a valid regular expression")


def test_a_valid_pattern_still_judges_normally():
    check({"url": "https://cdn.test/a.png"},
          {"expect_status": 200, "assert": [{"path": "url", "matches": "^https://"}]},
          "PASS")


# ---- parse_curl and dig ---------------------------------------------------------------------------
def test_parse_curl_extracts_the_envelope():
    env = be_execute.parse_curl(
        """curl 'https://api.test/v1/cart?types=labs' \\
  -X POST \\
  -H 'x-client: web' \\
  -H 'Cookie: sid=abc; csrf=def' \\
  --data-raw '{"types":"labs"}' --compressed"""
    )
    assert env["origin"] == "https://api.test"
    assert env["method"] == "POST"
    assert env["headers"]["x-client"] == "web"
    assert env["cookie"] == "sid=abc; csrf=def"   # a Cookie header becomes the -b cookie
    assert env["body"] == '{"types":"labs"}'


def test_parse_curl_defaults_to_get_and_survives_an_unquotable_line():
    env = be_execute.parse_curl("curl https://api.test/x -H 'a: b' --location 'unbalanced")
    assert env["method"] == "GET" and env["origin"] == "https://api.test"


@pytest.mark.parametrize("path,want", [
    ("a.b", 1),
    ("l.0.x", "y"),
    ("l.5", ("__MISSING__", "l.5")),
    ("a.nope", ("__MISSING__", "a.nope")),
    ("a.b.c", ("__MISSING__", "a.b.c")),   # walking into a scalar
    ("l.notanindex", ("__MISSING__", "l.notanindex")),
])
def test_dig_paths_and_misses(path, want):
    assert be_execute.dig({"a": {"b": 1}, "l": [{"x": "y"}]}, path) == want


def test_content_length_header_is_not_forwarded():
    """A stale content-length from the captured curl would corrupt every request with a new body."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = cmd

        class R:
            stdout = '{"ok":true}\n__STATUS__200'
        return R()

    env = {**ENV, "headers": {"content-length": "99", "x-client": "web"}}
    with mock.patch.object(be_execute.subprocess, "run", fake_run):
        run_case({"extid": "T", "path": "/s", "expect_status": 200}, env)
    assert "content-length: 99" not in seen["cmd"]
    assert "x-client: web" in seen["cmd"]


def test_curl_path_and_timeout_are_configurable():
    """The curl binary path and the request timeout are flags, not literals."""
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"], seen["timeout"] = cmd, kw.get("timeout")

        class R:
            stdout = '{"ok":true}\n__STATUS__200'
        return R()

    with mock.patch.object(be_execute.subprocess, "run", fake_run):
        run_case({"extid": "T", "path": "/s", "expect_status": 200}, ENV,
                 timeout=7, curl_path="/opt/bin/curl")
    assert seen["cmd"][0] == "/opt/bin/curl"
    assert "-m" in seen["cmd"] and seen["cmd"][seen["cmd"].index("-m") + 1] == "7"
    assert seen["timeout"] == 7 + be_execute.SUBPROCESS_SLACK_S


def test_a_timeout_is_error_with_no_http_status():
    def fake_run(cmd, **kw):
        raise be_execute.subprocess.TimeoutExpired(cmd, 1)

    with mock.patch.object(be_execute.subprocess, "run", fake_run):
        r = run_case({"extid": "T", "path": "/s"}, ENV)
    assert r["status"] == "ERROR" and r["detail"] == "request timed out" and "http" not in r
