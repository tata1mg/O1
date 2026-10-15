"""`CaseStore` — the storage seam for test cases, runs and executions.

Survey §6.3 proposed this protocol; D8 locks it with two backends. WHY: in test_agent every read and write
went straight to Kiwi over XML-RPC, so a Docker daemon plus a running server was a prerequisite for
testing ANY part of the workflow — spec derivation, execution, even `--dry-run` (be_execute.py:256-261
read Kiwi to derive a spec even when told to write nothing). With a protocol, the JSON backend makes the
whole chain runnable and testable offline, and Kiwi becomes the deployment choice it always was.

Three read/write rules the protocol carries over from Kiwi's semantics, because downstream depends on them:
  * a case is keyed by `(feature, extid)` and upserts idempotently — a regenerated suite updates in place;
  * a run only ever contains CONFIRMED cases, and (when `require_tag` is given) only cases carrying that
    tag. That double gate is the executor's whole permission model;
  * PASS/FAIL/BLOCKED/ERROR stay distinct: FAILED is a defect, ERROR is the harness, BLOCKED is the
    environment. Collapsing them is how teams learn to ignore red.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


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
        """Create/update cases keyed on `(feature, extid)`. -> {created, updated, total, backend, ...}."""
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
                         duration_s: float | None = None, props: dict | None = None) -> str:
        """Write one case's outcome. `status` is PASS | FAIL | BLOCKED | ERROR."""
        ...

    def health(self) -> dict:
        """{ok: bool, ...} — reachability preflight."""
        ...


#: PASS/FAIL/BLOCKED/ERROR -> the stored status name (Kiwi's TestExecutionStatus vocabulary).
STATUS = {"PASS": "PASSED", "FAIL": "FAILED", "BLOCKED": "BLOCKED", "ERROR": "ERROR"}


def get_store(ctx) -> CaseStore:
    """Resolve the backend from flag `testing.storage`. Kiwi's deps are imported only if chosen."""
    backend = ctx.flag("testing.storage", "json")
    if backend == "json":
        from .json_store import JsonCaseStore

        return JsonCaseStore(ctx.run_dir / "store")
    if backend == "kiwi":
        from .kiwi import KiwiCaseStore

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
