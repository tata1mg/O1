"""Workflow discovery + composition.

A workflow lives in `src/workflows/<name>/__init__.py` and exposes `WORKFLOW: harness.contracts.Workflow`.
Discovery is by package scan (like rfc_agent's stage registry) plus an optional explicit registration
hook for out-of-tree workflows (`register(workflow)`), so adding a workflow is a folder, not a code edit.

Composition rules (harness-level, workflow-agnostic):
  - the pipeline is the workflow's default_pipeline unless --pipeline overrides it;
  - `disabled_stages` removes names from the pipeline; each removal is recorded as a decision;
  - slot compatibility is enforced at run time by the stages' requires/outputs contracts (unchanged).
"""

from __future__ import annotations

import importlib
import pkgutil

from .contracts import PipelineConfigError, Stage, Workflow

_REGISTRY: dict[str, Workflow] = {}
_SCANNED = False


def register(wf: Workflow) -> Workflow:
    existing = _REGISTRY.get(wf.name)
    if existing is not None and existing is not wf:
        raise RuntimeError(f"duplicate workflow name {wf.name!r}")
    _REGISTRY[wf.name] = wf
    return wf


def _scan() -> None:
    global _SCANNED
    if _SCANNED:
        return
    _SCANNED = True
    try:
        pkg = importlib.import_module("workflows")
    except ModuleNotFoundError:
        return
    for m in pkgutil.iter_modules(pkg.__path__):
        mod = importlib.import_module(f"workflows.{m.name}")
        wf = getattr(mod, "WORKFLOW", None)
        if isinstance(wf, Workflow):
            register(wf)


def names() -> list[str]:
    _scan()
    return sorted(_REGISTRY)


def get(name: str) -> Workflow:
    _scan()
    try:
        return _REGISTRY[name]
    except KeyError:
        raise PipelineConfigError(f"unknown workflow {name!r}; available: {names()}") from None


def compose(wf: Workflow, pipeline: list[str] | None, disabled: list[str]) -> list[Stage]:
    wanted = list(pipeline) if pipeline else list(wf.default_pipeline)
    unknown = [n for n in wanted if n not in {s.name for s in wf.stages}]
    if unknown:
        raise PipelineConfigError(f"unknown stage(s) {unknown} for workflow {wf.name!r}; "
                                  f"registered: {sorted(s.name for s in wf.stages)}")
    kept = [n for n in wanted if n not in set(disabled)]
    return [wf.stage(n) for n in kept]
