"""The spec deriver's step grammar. Every test below is one `check(...)`-style call with fixed expectations.

Each case here is a shape observed in a real suite or a rule the skill promises; if one breaks, cases
silently stop being executable.
"""

from __future__ import annotations

import json

import pytest

from workflows.testing.be_spec import parse_case


def check(text, *, expect_drop=None, path=None, method=None, status=None, asserts=None, requires=None):
    """The original script's `check` helper, raising instead of appending to a FAILED list."""
    entry, why = parse_case("TC-X", text)
    if expect_drop is not None:
        assert entry is None, f"expected drop ({expect_drop}), got {json.dumps(entry)}"
        assert expect_drop in why, f"dropped for the wrong reason: {why!r}"
        return
    assert entry is not None, f"unexpectedly dropped: {why}"
    if path is not None:
        assert entry.get("path") == path
    if method is not None:
        assert entry.get("method") == method
    if status is not None:
        assert entry.get("expect_status") == status
    if asserts is not None:
        assert entry.get("assert") == asserts
    if requires is not None:
        assert entry.get("require") == requires


# ---- the canonical form the skill mandates (source :46-61) ------------------------------------------
def test_canonical():
    check(
        """1. Send GET /radiology/v1/slots?lab_id=42
2. Assert status == 200
3. Assert body.slots.0.departments is present
4. Assert body.slots.0.departments.0.available == true
5. Assert body.slots.0.remaining_capacity == 1
6. Assert body.slots.0.starttime matches /^[0-9]{2}:[0-9]{2}$/
7. Require body.data.experiment_enabled is non-empty
""",
        path="/radiology/v1/slots?lab_id=42", method="GET", status=200,
        asserts=[{"path": "slots.0.departments", "present": True},
                 {"path": "slots.0.departments.0.available", "equals": True},
                 {"path": "slots.0.remaining_capacity", "equals": 1},
                 {"path": "slots.0.starttime", "matches": "^[0-9]{2}:[0-9]{2}$"}],
        requires=[{"path": "data.experiment_enabled", "non_empty": True}],
    )


# ---- shapes real suites already use (source :64-75) -------------------------------------------------
def test_python_ish_form_labs_29354_suite():
    check(
        """1. Send GET /cart/upsell?user_id=U-1001 (labs_agg/app/routes/cart.py:118-125).
2. Assert response.status_code == 200.
3. Assert response.json()["renew"] == true.
4. Assert response.json().get("is_renewal", false) == false.
""",
        path="/cart/upsell?user_id=U-1001", method="GET", status=200,
        asserts=[{"path": "renew", "equals": True},
                 {"path": "is_renewal", "equals": False}],
    )


# source :77-86
def test_bare_verb_assert_http_200_bare_dotted_path():
    check(
        """1. POST /pwa-api/api/v4/carts/subscriptions/upsell_data with body {"types":"labs"}.
2. Assert HTTP 200.
3. Assert data.mix_panel_data.labs_careplan_variant is present and non-empty.
""",
        path="/pwa-api/api/v4/carts/subscriptions/upsell_data", method="POST", status=200,
        asserts=[{"path": "data.mix_panel_data.labs_careplan_variant", "non_empty": True}],
    )


# ---- things that must NOT be derived (source :89-119) -----------------------------------------------
def test_status_code_is_transport_never_a_body_path():
    check(
        """1. Send GET /x
2. Assert response.status_code == 204.
""",
        path="/x", status=204, asserts=[],
    )


def test_prose_field_name_must_not_become_a_field_called_the():
    check(
        """1. Send GET /cart/upsell?user_id=U-1001.
2. Assert status == 200.
3. Assert the headline field == "Renew now and get benefits worth upto 450".
4. Assert no sub-badge field present in the response.
""",
        path="/cart/upsell?user_id=U-1001", status=200, asserts=[],
    )


def test_unsupported_operator_yields_no_assertion_not_a_guess():
    check(
        """1. Send GET /x
2. Assert status == 200
3. Assert cp_renewal_savings_bucket is one of the documented buckets.
""",
        path="/x", status=200, asserts=[],
    )


# source :122-141
def test_no_route_stated_is_dropped():
    check(
        """1. Request variant A payload (Y=450, plan=Mini).
2. Assert headline string == "Saved 1000".
""",
        expect_drop="route is not stated",
    )


def test_route_but_nothing_checkable_is_dropped():
    check(
        """1. Send GET /x
2. Confirm the banner looks right.
""",
        expect_drop="no transcribable assertion",
    )


def test_no_numbered_steps_is_dropped():
    check("**Expected Result**\nIt works.\n", expect_drop="no numbered steps")


# ---- request parameters stated after the route, not inside it (source :144-169) ----------------------
def test_simple_scalar_params_fold_into_the_query_string():
    check(
        """1. Send GET /radiology/v1/slots with lab_id=1042, start_date=2026-08-26
2. Assert status == 200
""",
        path="/radiology/v1/slots?lab_id=1042&start_date=2026-08-26", method="GET", status=200,
    )


def test_list_valued_params_are_dropped_never_guessed():
    check(
        """1. Send `GET /radiology/v1/slots` with `lab_id=1042`, `skus=["SKU-MRI-001"]`, `start_date=2026-08-26`.
2. Assert the HTTP status code is `200`.
3. Assert `response.slots[0].departments[0].available == true`.
""",
        expect_drop="list/object",
    )


def test_explicit_query_string_in_the_path_wins():
    check(
        """1. Send GET /x?a=1 with note=whatever
2. Assert status == 204
""",
        path="/x?a=1", status=204,
    )


# ---- a pattern may escape the / delimiter (source :172-188) -----------------------------------------
def test_an_escaped_slash_does_not_end_the_pattern():
    """The generated TASK-29354 suite writes URLs this way. Stopping at the first `/` captured
    `^https:\\`, which is not a valid regex, and the executor raised on it mid-run."""
    check(
        r"""1. Send GET /cart/upsell
2. Assert status == 200
3. Assert body.data.widgets.0.data.bg_image_box.url matches /^https:\/\//
""",
        path="/cart/upsell", status=200,
        asserts=[{"path": "data.widgets.0.data.bg_image_box.url", "matches": "^https://"}],
    )


def test_other_escapes_belong_to_the_pattern_and_survive():
    check(
        r"""1. Send GET /x
2. Assert status == 200
3. Assert body.data.request_id matches /^\d{3}\.\w+$/
""",
        path="/x", status=200,
        asserts=[{"path": "data.request_id", "matches": r"^\d{3}\.\w+$"}],
    )


# ---- beyond parity: shapes the original never covered ------------------------------------------------
@pytest.mark.parametrize("verb", ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
def test_every_documented_verb_parses(verb):
    check(f"1. Send {verb} /x\n2. Assert status == 200\n", path="/x", method=verb, status=200)


def test_status_only_entry_is_flagged_weak():
    """`_status_only` is how derive() reports "passes on a 200 whatever the body says"."""
    entry, why = parse_case("TC-X", "1. Send GET /x\n2. Assert status == 200\n")
    assert entry["_status_only"] is True and why == ""


def test_require_and_assert_land_in_different_buckets():
    entry, _ = parse_case("TC-X", """1. Send GET /x
2. Assert status == 200
3. Require body.cohort is non-empty
4. Assert body.value == 3
""")
    assert entry["require"] == [{"path": "cohort", "non_empty": True}]
    assert entry["assert"] == [{"path": "value", "equals": 3}]
