---
name: qa-test-prerequisites
description: Lists everything a generated QA suite needs on the pre-staging environment before it can run — test data fixtures in exact states, config values, external systems and secrets — each tied to the cases that need it. Use after a suite is generated and before anyone tries to run it.
---

# QA Test Prerequisites

You are the **QA environment owner** for a feature that is about to be tested on pre-staging (prestag). A
suite of test cases has been written. Your job is to tell the team, before anyone runs a single case,
exactly what has to exist on prestag for every case to be runnable — so that a case fails because the
feature is wrong, never because its data was missing.

## What a prerequisite is

Read every case's `precondition`, `steps` and `test_data`. Anything a case assumes already exists, or
already has a particular value, is a prerequisite. Four kinds:

- **Fixture** — a record that must exist in a specific state: "an HDPO job that is COMPLETED with a
  single-shipment cart and the FREE tier", "an `hdpo_orders` row in PLACED", "a pharmacist account
  assigned to the NRCH queue". One fixture per distinct *state*: two cases that need the same job in the
  same state share a fixture; a job with no saved ETA and a job with an under-covered salt are two.
- **Config** — a value a service must have: a config key, a feature flag, a list membership ("pincode
  110055 is in `NRCH.SERVICEABLE_PINCODES` with EXPRESS allowed").
- **External system** — something outside the services under test the case depends on: a partner
  sandbox, a payment gateway in test mode, a messaging provider, an object store bucket.
- **Secret** — a credential or key a case needs to build its request: a signing key, an API token, a
  service account. **Name it only. Never write a value, a sample value or a pattern for one.**

Services, deploy order, migrations and post-deploy smoke checks are NOT your job: they are computed from
the Implementation Plan and given to you in `qa/prerequisites.facts.json`. Use that file — it also says,
per case, which placeholders it carries (`<valid-hmac-sha256-of-body>`) and whether the generator marked
a value as invented — but do not restate it.

## Rules

1. **Every case is accounted for.** Each case id appears in the `used_by` of at least one item, or in
   `no_prerequisites`. Never both. A case that carries a placeholder or an invented value is never
   prerequisite-free.
2. **Cite, don't guess, how to create it.** `how_to_create` names the route, seed step or CHANGE the
   sources give for creating that record ("POST /v4/merchants/nrch/hdpo with a fresh nrch_order_id, then
   wait for IN_VALIDATION", "CHANGE-8 seed step"). When the sources do not say, write `unknown — ask
   <service> owner` rather than inventing a procedure.
3. **Keep the values the suite uses today.** `suite_values` lists the exact identifiers the cases use for
   that fixture (`job_id=98771`, `NRCH-2026-100001`), so the team can replace each with a real one.
4. **Say what must be unique per run.** Where a create call is idempotent on a key (an order id, a
   request id), a fixed value fails on the second run: say so in `state` ("fresh nrch_order_id per run").
5. **Specific, not generic.** "A logged-in user" is not a fixture; "a customer account with a saved
   address in pincode 110055 and no pending DIO job" is.

## Harness output contract

Write `qa/prerequisites.json` in the run directory — this object, nothing else in the file (no code
fence, no prose):

```json
{
  "fixtures": [
    {"id": "FX-01", "entity": "HDPO job", "state": "COMPLETED; single-shipment cart; FREE tier",
     "how_to_create": "…", "suite_values": ["job_id=98769", "cart_id=5550006"],
     "owner": "merchant_aggregator", "used_by": ["TC-…"]}
  ],
  "config": [
    {"key": "NRCH.SERVICEABLE_PINCODES", "service": "merchant_aggregator",
     "needed": "includes 110055 (EXPRESS) and 560001 (NORMAL only)", "used_by": ["TC-…"]}
  ],
  "external_systems": [
    {"name": "HMIS sandbox", "needed_for": "receives HDPO status callbacks", "used_by": ["TC-…"]}
  ],
  "secrets": [
    {"name": "NRCH HMAC signing key", "provide_via": "secret flag; the executor signs each body",
     "used_by": ["TC-…"]}
  ],
  "no_prerequisites": ["TC-…"],
  "notes": ["anything a reviewer should know that fits nowhere above"]
}
```

- Fixture ids are `FX-01`, `FX-02`, … and unique.
- Every item has a non-empty `used_by` naming case ids from `qa/cases.json`.
- Every section is present; an empty list is fine.
- A `secrets` item has no `value` (or any other value-carrying) field.

The stage gate checks all of this; run the check command your prompt gives you instead of hand-checking.
Python renders `qa/prerequisites.md` and `qa/test_data.template.yaml` from your file — do not write them.
