"""`CaseStore` — the storage seam for test cases, runs and executions.

Survey §6.3 proposed this protocol; D8 locks it with two backends. WHY: in test_agent every read and write
went straight to Kiwi over XML-RPC, so a Docker daemon plus a running server was a prerequisite for
testing ANY part of the workflow — spec derivation, execution, even `--dry-run` (be_execute.py:256-261
read Kiwi to derive a spec even when told to write nothing). With a protocol, the JSON backend makes the
whole chain runnable and testable offline, and Kiwi becomes the deployment choice it always was.

The read/write rules the protocol carries, because downstream depends on them:
  * a case is keyed by `(feature, extid)` and upserts idempotently — a regenerated suite updates in place;
  * a case is written PROPOSED and only a human approves it (CONFIRMED) or rejects it (DISABLED). Once a
    human has touched a case, a regenerated suite never overwrites it: the store reports it as
    `protected` and leaves the agent's would-be change as a suggestion beside it. The human edit wins;
  * a run only ever contains CONFIRMED cases, and (when `require_tag` is given) only cases carrying that
    tag. That double gate is the executor's whole permission model;
  * PASS/FAIL/BLOCKED/ERROR stay distinct: FAILED is a defect, ERROR is the harness, BLOCKED is the
    environment. Collapsing them is how teams learn to ignore red.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from harness.contracts import PipelineConfigError


@dataclass
class Case:
    """One stored case, backend-neutral. `text` is the rendered step blob `be_spec.parse_case` reads."""

    extid: str
    text: str = ""
    status: str = "CONFIRMED"
    tags: tuple[str, ...] = ()
    summary: str = ""
    priority: str = "P3"
    case: dict = field(default_factory=dict)  # the canonical dict, when the backend keeps it (json does)


@dataclass
class Run:
    run_id: str
    plan_id: str = ""
    build: str = ""
    environment: str = ""
    attached: tuple[str, ...] = ()
    skipped_unconfirmed: tuple[str, ...] = ()
    skipped_wrong_execution: tuple[str, ...] = ()
    url: str = ""


@runtime_checkable
class CaseStore(Protocol):
    """Every storage operation the testing workflow performs. Implementations: kiwi.py, json_store.py."""

    name: str

    def upsert_cases(self, feature: str, cases: list[dict]) -> dict:
        """Create/update cases keyed on `(feature, extid)`.

        -> {created, updated, unchanged, protected, protected_ids, total, backend, ...}. The four counts
        sum to total; `protected_ids` names the human-touched cases that were left as they were.
        """
        ...

    def list_cases(self, feature: str, *, status: str | None = None,
                   tags: tuple[str, ...] = ()) -> list[Case]:
        """Cases for a feature, optionally filtered by case status and by required tags."""
        ...

    def create_run(self, feature: str, build: str, env: str, extids: list[str] | None = None,
                   require_tag: str | None = None) -> Run:
        """Create (or reuse) the run for feature+env+build and attach its eligible cases."""
        ...

    def record_execution(self, run_id: str, extid: str, status: str, *, actual: str = "",
                         duration_s: float | None = None, props: dict | None = None,
                         finished_at: datetime | None = None) -> str:
        """Write one case's outcome. `status` is PASS | FAIL | BLOCKED | ERROR. -> the execution id.

        `finished_at` is when the case ACTUALLY ended (tz-aware; naive is read as UTC). Defaulting it to
        "now" records the moment of the write, which drifts from the test by however long the caller
        took to get here.
        """
        ...

    def attach(self, scope: str, target_id: str, name: str, content: bytes, *,
               replace: bool = False) -> bool:
        """Store one file on an object. `scope` is one of ATTACH_SCOPES; `target_id` is that object's id
        (for "execution", what `record_execution` returned). -> whether the bytes are there now.

        Idempotent by `name`: a file of that name already on the object is kept unless `replace`, so a
        re-run does not stack copies.
        """
        ...

    def attachment_url(self, scope: str, target_id: str, name: str) -> str:
        """Where the attachment stored under `name` can be fetched, or "" when it is not there."""
        ...

    def health(self) -> dict:
        """{ok: bool, ...} — reachability preflight."""
        ...


#: PASS/FAIL/BLOCKED/ERROR -> the stored status name (Kiwi's TestExecutionStatus vocabulary).
STATUS = {"PASS": "PASSED", "FAIL": "FAILED", "BLOCKED": "BLOCKED", "ERROR": "ERROR"}

#: The status a stored case may be written with (flag `testing.upload_status`). PROPOSED is the default and
#: the only one Kiwi accepts. CONFIRMED skips human review and exists for the JSON store only — offline
#: trials and tests that need a runnable suite without a reviewer.
UPLOAD_STATUSES = ("PROPOSED", "CONFIRMED")

#: Objects a file can be attached to. Each is a real choice: an execution is this case in THIS run; a case
#: is shared by every run that ever touched it (so the name must carry the run); a run or plan holds one
#: report for the whole thing.
ATTACH_SCOPES = ("execution", "case", "run", "plan")


def check_scope(scope: str) -> None:
    if scope not in ATTACH_SCOPES:
        raise ValueError(f"scope must be one of {', '.join(ATTACH_SCOPES)}, not {scope!r}")


def get_store(ctx) -> CaseStore:
    """Resolve the backend from flag `testing.storage`. Kiwi's deps are imported only if chosen."""
    backend = ctx.flag("testing.storage", "json")
    upload_status = str(ctx.flag("testing.upload_status", "PROPOSED"))
    if upload_status not in UPLOAD_STATUSES:
        raise PipelineConfigError(f"testing.upload_status={upload_status!r}; known: {list(UPLOAD_STATUSES)}")
    if backend == "json":
        from .json_store import JsonCaseStore

        return JsonCaseStore(ctx.run_dir / "store", upload_status=upload_status)
    if backend == "kiwi":
        from .kiwi import KiwiCaseStore

        if upload_status != "PROPOSED":
            raise PipelineConfigError(
                "testing.upload_status=CONFIRMED is refused for the Kiwi store: in Kiwi only a reviewer "
                "approves a case. Upload PROPOSED and approve the cases in Kiwi.")

        return KiwiCaseStore(
            url=ctx.flag("testing.kiwi_url", ""),
            user=ctx.flag("testing.kiwi_user", ""),
            password=ctx.flag("testing.kiwi_password", ""),
            web=ctx.flag("testing.kiwi_web", "https://localhost:8443"),
            insecure=bool(ctx.flag("testing.insecure_tls", True)),
            product_prefix=ctx.flag("testing.product_prefix", "Diagno / "),
            plan_template=ctx.flag("testing.plan_template", "{feature} — imported test cases"),
            run_template=ctx.flag("testing.run_template", "{feature} · {env} · {build}"),
        )
    raise ValueError(f"unknown testing.storage {backend!r}; known: json, kiwi")
