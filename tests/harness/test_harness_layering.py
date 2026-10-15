"""Layering guard: nothing under src/harness/ may import rfc_agent or
workflows at RUNTIME. `TYPE_CHECKING`-guarded imports are tolerated (they never execute) — that is the one
documented seam until config/streams move into the kernel."""

from __future__ import annotations

import ast
from pathlib import Path

HARNESS = Path(__file__).resolve().parents[2] / "src" / "harness"
FORBIDDEN = ("rfc_agent", "workflows")

# The explicit seams: pieces of the kernel that still live in rfc_agent are reached from
# exactly these modules and nowhere else. Removing an entry here is progress; adding one is a decision and
# needs a PLAN.md item naming when it goes away.
SEAMS: dict[str, set[str]] = {
    # the Claude adapter wraps the existing primitive instead of re-implementing it
    "engines/claude_code.py": {"rfc_agent.engine", "rfc_agent.mcp_tools"},
    # the run loop and config loader have not moved yet (PLAN: harness.runner / harness.config)
    "cli.py": {"rfc_agent.runner", "rfc_agent.config", "rfc_agent.ask"},
}


def _runtime_imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
            for sub in ast.walk(node):
                guarded.add(id(sub))
    out = []
    for node in ast.walk(tree):
        if id(node) in guarded:
            continue
        if isinstance(node, ast.Import):
            out += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module:
            # `from rfc_agent import engine` names the module rfc_agent.engine — record the full dotted target
            # when the base is a forbidden package so the seam allowlist can name modules, not packages
            if node.module.split(".")[0] in FORBIDDEN and "." not in node.module:
                out += [f"{node.module}.{a.name}" for a in node.names]
            else:
                out.append(node.module)
    return out


def test_harness_has_no_runtime_dependency_on_workflow_code():
    offenders = {}
    for py in HARNESS.rglob("*.py"):
        rel = str(py.relative_to(HARNESS))
        allowed = SEAMS.get(rel, set())
        bad = [m for m in _runtime_imports(py) if m.split(".")[0] in FORBIDDEN and m not in allowed]
        if bad:
            offenders[str(py.relative_to(HARNESS))] = bad
    assert not offenders, f"harness/ imports workflow code at runtime outside the documented seams: {offenders}"


def test_documented_seams_are_still_needed():
    """A seam entry that no longer matches any import is stale — delete it so the allowlist shrinks honestly."""
    stale = {}
    for rel, mods in SEAMS.items():
        present = set(_runtime_imports(HARNESS / rel))
        unused = {m for m in mods if m not in present}
        if unused:
            stale[rel] = unused
    assert not stale, f"stale seam entries (remove them): {stale}"
