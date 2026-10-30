"""File-backed `CaseStore` — the default, and the only one that needs no server.

Layout under `<run_dir>/store/`:

    cases/<feature>.json                 {extid: {case: <canonical dict>, status, tags, suggestions[]}}
    runs/<feature>/<env>__<build>.json   {run_id, attached[], executions: {extid: {...}}}
    attachments/<scope>/<id>/<name>      files attached to an execution, case, run or plan

There is no review UI for this store: a reviewer edits `status` in the cases file (CONFIRMED / DISABLED).
A row whose status differs from the one this store writes is human-reviewed, and a re-upload keeps it
as it is and appends the would-be change to its `suggestions` — the Kiwi store's rule, in a file.

WHY the canonical dict rather than Kiwi's rendered text (survey §6.3 blocker 3): the case text is Kiwi's
transport for the steps, not the data. Keeping the dict means the store round-trips every field — including
`requirement`, `qa_comment` and `type`, which BOTH of test_agent's write paths dropped. `Case.text` is
rendered on read so `be_spec.parse_case` sees exactly what it would see from Kiwi.

This module must never open a socket. tests/testing/test_json_store.py asserts that mechanically.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from .._io import atomic_write_text, load_json_strict, locked
from ..schema import case_tags, render_text
from . import Case, Run, check_scope


def _slug(s: str) -> str:
    """A filesystem-safe stem. Feature keys are Jira-ish, but a slash would escape the store."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", s).strip("_") or "unnamed"


class JsonCaseStore:
    name = "json"

    def __init__(self, root: Path, *, upload_status: str = "PROPOSED") -> None:
        self.root = Path(root)
        self.upload_status = upload_status

    # ---- paths ----------------------------------------------------------------------------------
    def _cases_path(self, feature: str) -> Path:
        return self.root / "cases" / f"{_slug(feature)}.json"

    def _run_path(self, feature: str, env: str, build: str) -> Path:
        return self.root / "runs" / _slug(feature) / f"{_slug(env)}__{_slug(build)}.json"

    def _read(self, path: Path) -> dict:
        """Absent = empty. Present-but-torn = `GateFailure` naming the file, never a `JSONDecodeError`
        escaping out of `list_cases` and repeating on every retry."""
        if not path.exists():
            return {}
        return load_json_strict(path, what=f"case store file {path}") or {}

    def _write(self, path: Path, data: dict) -> None:
        atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n")

    # ---- CaseStore ------------------------------------------------------------------------------
    def upsert_cases(self, feature: str, cases: list[dict]) -> dict:
        if not cases:
            return {"created": 0, "updated": 0, "total": 0, "backend": self.name,
                    "feature": feature, "error": "no cases supplied"}
        path = self._cases_path(feature)
        # Read-modify-write: hold the lock across BOTH so a concurrent upsert of a different slice of the
        # same feature cannot land on a stale copy and drop the other writer's cases.
        with locked(path):
            stored = self._read(path)
            created = updated = unchanged = 0
            protected: list[str] = []
            for c in cases:
                extid = str(c["id"])
                row = stored.get(extid)
                if row is None:
                    created += 1
                elif row.get("case") == c:
                    unchanged += 1
                    continue
                elif row.get("status") != self.upload_status:
                    # reviewed: the human decision stands, the agent's change is only offered
                    suggestion = {"case": c, "why": f"status is {row.get('status')}"}
                    if suggestion not in row.setdefault("suggestions", []):
                        row["suggestions"].append(suggestion)
                    protected.append(extid)
                    continue
                else:
                    updated += 1
                stored[extid] = {
                    "case": c,
                    # The approval signal is the case status, so the executor needs no permission model
                    # of its own. PROPOSED unless an offline run asked for CONFIRMED.
                    "status": self.upload_status,
                    "tags": case_tags(c, feature),
                    "summary": f"[{extid}] {c.get('title', '')}".strip(),
                }
            self._write(path, stored)
        return {"created": created, "updated": updated, "unchanged": unchanged, "protected": len(protected),
                "protected_ids": protected, "total": len(cases), "backend": self.name,
                "feature": feature, "path": str(path), "status": self.upload_status}

    def list_cases(self, feature: str, *, status: str | None = None,
                   tags: tuple[str, ...] = ()) -> list[Case]:
        stored = self._read(self._cases_path(feature))
        out: list[Case] = []
        for extid, row in sorted(stored.items()):
            if status is not None and row.get("status") != status:
                continue
            row_tags = tuple(row.get("tags") or ())
            if any(t not in row_tags for t in tags):
                continue
            out.append(Case(extid=extid, text=render_text(row.get("case") or {}),
                            status=row.get("status", "CONFIRMED"), tags=row_tags,
                            summary=row.get("summary", ""),
                            priority=str((row.get("case") or {}).get("priority") or ""),
                            case=row.get("case") or {}))
        return out

    def create_run(self, feature: str, build: str, env: str, extids: list[str] | None = None,
                   require_tag: str | None = None) -> Run:
        path = self._run_path(feature, env, build)
        attached, unconfirmed, wrong_exec = [], [], []
        for case in self.list_cases(feature):
            if extids is not None and case.extid not in extids:
                continue
            if case.status != "CONFIRMED":
                unconfirmed.append(case.extid)          # the review gate, enforced here
                continue
            if require_tag and require_tag not in case.tags:
                exec_tag = next((t for t in case.tags if t.startswith("exec:")), None)
                wrong_exec.append(f"{case.extid} ({exec_tag or 'no exec: tag'})")
                continue                                # the execution-classification gate
            attached.append(case.extid)
        with locked(path):
            existing = self._read(path)
            run_id = existing.get("run_id") or f"{_slug(feature)}__{_slug(env)}__{_slug(build)}"
            # Re-running the same build reuses the run and KEEPS its executions, but only for cases still
            # attached: an execution for an extid this run no longer contains would sit in the file with
            # no corresponding results row, reading as evidence for a case nobody ran.
            kept = {k: v for k, v in (existing.get("executions") or {}).items() if k in set(attached)}
            data = {"run_id": run_id, "feature": feature, "environment": env, "build": build,
                    "attached": attached, "skipped_unconfirmed": unconfirmed,
                    "skipped_wrong_execution": wrong_exec,
                    "executions": kept}
            self._write(path, data)
        return Run(run_id=run_id, plan_id=_slug(feature), build=build, environment=env,
                   attached=tuple(attached), skipped_unconfirmed=tuple(unconfirmed),
                   skipped_wrong_execution=tuple(wrong_exec), url=path.as_uri())

    def record_execution(self, run_id: str, extid: str, status: str, *, actual: str = "",
                         duration_s: float | None = None, props: dict | None = None,
                         finished_at: datetime | None = None) -> str:
        path = self._find_run(run_id)
        if path is None:
            raise RuntimeError(f"no run {run_id!r} in {self.root} — was create_run called?")
        # THE race this lock exists for: two invocations (or two threads) recording into one run each
        # read the whole file, add their own row and write it back. Unlocked, the later write silently
        # discards every execution the other one recorded, and nothing downstream notices because
        # qa/results.json is written by the stage independently of the store.
        with locked(path):
            data = self._read(path)
            if extid not in (data.get("attached") or []):
                raise RuntimeError(
                    f"no execution for extid {extid} in run {run_id} — was the case attached?")
            stop = finished_at or datetime.now(UTC)
            if stop.tzinfo is None:
                stop = stop.replace(tzinfo=UTC)
            data.setdefault("executions", {})[extid] = {
                "status": status, "actual": actual, "duration_s": duration_s, "props": props or {},
                "finished_at": stop.isoformat(timespec="microseconds"),
            }
            self._write(path, data)
        return f"{run_id}:{extid}"

    def attach(self, scope: str, target_id: str, name: str, content: bytes, *,
               replace: bool = False) -> bool:
        check_scope(scope)
        path = self._attachment_path(scope, target_id, name)
        if path.exists() and not replace:
            return True
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        tmp.write_bytes(content)
        tmp.replace(path)
        return True

    def attachment_url(self, scope: str, target_id: str, name: str) -> str:
        check_scope(scope)
        path = self._attachment_path(scope, target_id, name)
        return path.as_uri() if path.exists() else ""

    def health(self) -> dict:
        return {"ok": True, "backend": self.name, "root": str(self.root), "network": False}

    # ---- helpers ---------------------------------------------------------------------------------
    def _attachment_path(self, scope: str, target_id: str, name: str) -> Path:
        return self.root / "attachments" / scope / _slug(str(target_id)) / _slug(name)

    def _find_run(self, run_id: str) -> Path | None:
        for p in sorted((self.root / "runs").rglob("*.json")):
            if p.name.endswith(".lock") or p.name.endswith(".tmp"):
                continue  # sidecars of `locked` / `atomic_write_text`, not artifacts
            if self._read(p).get("run_id") == run_id:
                return p
        return None
