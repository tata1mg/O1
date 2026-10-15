"""Run identity — the stable ids one run dir and all its resumes share, and that later meta-harness
stages (build, test) inherit.

Why a file and not the directory name: session identity used to be `run_dir.name`, so renaming or
moving a run dir split its session in two, and two same-named dirs under different roots merged into
one. Identity is now written once and read back — the directory name stays the human-readable label
(and remains the default `session_id`, so existing sessions keep their names), but it is no longer
the identity.

The id model is one id per level, every field stable across resumes (settled with Saurav, 2026-08-18):

    workstream_uuid  ⊃  session_uuid  ⊃  run_id  ⊃  stage#attempt  ⊃  observations
    (the ticket)        (plan+build+test)  (this stage's whole lifecycle)

`run_id` is the STAGE-RUN handle: minted once, reused by every resume of this stage, and the thing a
later stage's "consumes" reference points at. Per-INVOCATION identity is deliberately not minted here
— each invocation is one Langfuse trace, and the SDK's `trace_id` (carried intrinsically by every
exported span) already identifies it; `run_count` orders invocations when tracing is off. The earlier
per-invocation run_id + previous/root chain (Temporal/OpenLineage-style) was removed in 0.22.63: with
a stable run_id there is nothing for a previous-pointer to point at and nothing for a root to anchor,
and a chain the session + timestamps already imply was two ids doing no work.

`workstream_uuid` is DERIVED (uuid5 over the ticket key), never stored-and-looked-up: the build and
test stages compute the same value from the same ticket with no registry read and nothing to keep in
sync.

Contract: identity NEVER fails a run. Any read/write/parse problem degrades to an in-memory fallback
whose `session_id` is the directory name — i.e. exactly the pre-0.22.47 behaviour.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .contracts import RunContext

# uuid5(NAMESPACE_DNS, <original project namespace>) — pinned as a literal so it can never drift with a
# refactor; every stage of the meta-harness must derive the SAME workstream uuid from a ticket key.
_NS = uuid.UUID("ed7598c3-9c81-5255-8050-0ee02d25cab6")

STAGE = "plan"  # rfc_agent is the PLAN stage; build/test set their own when they land
FILENAME = "identity.json"

_cache: dict[str, dict] = {}  # every field is stable, but load() still must not re-count resumes


def _uuid7() -> str:
    """UUIDv7 (RFC 9562): 48-bit millisecond timestamp + randomness, so ids sort chronologically.
    Hand-rolled because `uuid.uuid7()` only arrives in Python 3.14 and we target 3.12.

    ponytail: no monotonic counter, so ids minted inside the SAME millisecond are unordered relative
    to each other (RFC 9562 makes the counter optional). These ids are minted once per lifecycle —
    add the counter only if something starts minting in a loop."""
    ms = int(time.time() * 1000) & 0xFFFF_FFFF_FFFF
    raw = bytearray(ms.to_bytes(6, "big") + secrets.token_bytes(10))
    raw[6] = 0x70 | (raw[6] & 0x0F)  # version 7
    raw[8] = 0x80 | (raw[8] & 0x3F)  # RFC 4122 variant
    return str(uuid.UUID(bytes=bytes(raw)))


def _fallback(ctx: RunContext) -> dict:
    """Pre-identity behaviour: the directory name is the session. Used when the file is unusable."""
    return {
        "workstream_id": ctx.run_dir.name,
        "workstream_source": "label",
        "workstream_uuid": str(uuid.uuid5(_NS, ctx.run_dir.name)),
        "session_id": ctx.run_dir.name,
        "session_uuid": None,
        "run_id": None,
        "stage": STAGE,
        "run_count": 0,
        "degraded": True,
    }


def load(ctx: RunContext) -> dict:
    """This run dir's identity, minting + persisting the stable ids on first call.

    Cached per run dir for the process: `run_count` increments per invocation, so repeated calls
    within one invocation must not re-count."""
    key = str(ctx.run_dir)
    if key not in _cache:
        _cache[key] = _mint(ctx)
    return _cache[key]


def _mint(ctx: RunContext) -> dict:
    try:
        path = ctx.run_dir / FILENAME
        stored: dict = {}
        if path.exists():
            with contextlib.suppress(json.JSONDecodeError, OSError):
                loaded = json.loads(path.read_text(encoding="utf-8"))
                stored = loaded if isinstance(loaded, dict) else {}

        session_id = stored.get("session_id") or ctx.run_dir.name
        # the ticket is the business identifier when we have one (Temporal: prefer a business id over
        # a generated uuid — humans search it); otherwise the session label stands in
        jira = (ctx.inputs or {}).get("jira")
        workstream_id = stored.get("workstream_id") or jira or session_id
        # provenance, not inference: run dirs are routinely NAMED after the ticket (PROJ-3394 with
        # --jira PROJ-3394), so "workstream_id != session_id" is not a usable test for "this is a real
        # ticket" — it silently fails on exactly the runs where the ticket matters most
        source = stored.get("workstream_source") or ("jira" if jira else "label")

        ident = {
            "workstream_id": workstream_id,
            "workstream_source": source,
            "workstream_uuid": str(uuid.uuid5(_NS, str(workstream_id))),
            "session_id": session_id,
            "session_uuid": stored.get("session_uuid") or _uuid7(),
            # pre-0.22.63 files have no run_id key; their root_run_id WAS the stage-run anchor, so
            # adopting it keeps in-flight lineages continuous with their already-exported traces
            "run_id": stored.get("run_id") or stored.get("root_run_id") or _uuid7(),
            "stage": stored.get("stage") or STAGE,
            "run_count": (stored.get("run_count") or 0) + 1,
            "created_at": stored.get("created_at") or datetime.now(UTC).isoformat(timespec="seconds"),
        }

        # every field is stable now — persist the record verbatim (rewriting also sheds the
        # pre-0.22.63 chain keys: root_run_id, last_run_id)
        with contextlib.suppress(OSError):
            ctx.run_dir.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(ident, indent=2) + "\n", encoding="utf-8")
        return ident
    except Exception:  # noqa: BLE001 — identity must never fail a run
        return _fallback(ctx)


def trace_metadata(ident: dict) -> dict:
    """The identity fields worth carrying on the trace. Flat and JSON-safe: these ride
    `propagate_attributes(metadata=...)`, which stamps them on EVERY child export — so they survive a
    killed run whose root span never closes."""
    out = {
        "workstream_id": ident.get("workstream_id"),
        # provenance, not just the tag's presence: docs promised this field on the trace since
        # 0.22.63 but it was filtered out here (2026-08-19 audit — doc/code drift)
        "workstream_source": ident.get("workstream_source"),
        "workstream_uuid": ident.get("workstream_uuid"),
        "session_uuid": ident.get("session_uuid"),
        "run_id": ident.get("run_id"),
        "harness_stage": ident.get("stage"),
        "run_count": ident.get("run_count"),
        # a degraded (fallback) identity previously looked NORMAL on the trace — surface it
        "identity_degraded": ident.get("degraded"),
    }
    return {k: v for k, v in out.items() if v is not None}
