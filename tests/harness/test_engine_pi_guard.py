"""The pi guard extension — syntax, and its decision table exercised in Node.

The guard is the ONLY containment a pi run has (pi ships no permission system), so it gets tested on its
own terms rather than only through the adapter: `decide()` is factored as a pure function plus an injected
policy caller, and `guard_cases.mjs` drives it directly. The last block of that driver calls the real
`python -m harness.policy check`, which is what proves the JS and Python sides agree on the contract.

Node is required by pi anyway, so a machine that can run pi can run these. Where node is absent the Node
cases skip and the syntax check still runs through `node --check` (also skipped, with a reason).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from harness.engines.pi import GUARD_JS

EXT_DIR = GUARD_JS.parent
CASES = Path(__file__).parents[1] / "fixtures" / "pi" / "guard_cases.mjs"
NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node not on PATH (pi requires Node >= 22.19 anyway)")


def test_the_guard_and_its_manifest_ship_with_the_package():
    assert GUARD_JS.exists(), "the adapter refuses to run without this file — containment has no transport"
    assert (EXT_DIR / "harness-guard.ts").exists(), "the .ts fallback for pi builds that reject a .js path"
    assert (EXT_DIR / "README.md").exists()
    manifest = json.loads((EXT_DIR / "package.json").read_text())
    assert manifest["type"] == "module"
    assert manifest["dependencies"] == {}, "the guard must stay dependency-free: no install step, no build"


def test_the_guard_never_uses_substring_path_matching():
    """pi's own shipped example guards with `path.includes(p)`. That is a convenience filter, not a
    boundary: `../` traversal and symlinks defeat it. Pinned because it is the easiest thing to
    reintroduce by accident."""
    src = GUARD_JS.read_text()
    assert "path.resolve(" in src
    assert ".includes(p)" not in src
    assert "realpath" not in src or "path.resolve(" in src


@needs_node
def test_node_check_parses_the_guard():
    for f in (GUARD_JS, CASES):
        r = subprocess.run([NODE, "--check", str(f)], capture_output=True, text=True)
        assert r.returncode == 0, f"{f.name}: {r.stderr}"


@needs_node
def test_the_guards_decision_table_and_its_agreement_with_the_python_policy():
    policy_cmd = json.dumps([sys.executable, "-m", "harness.policy", "check"])
    r = subprocess.run([NODE, str(CASES), str(GUARD_JS), policy_cmd],
                       capture_output=True, text=True, timeout=180)
    assert r.returncode == 0, f"stdout: {r.stdout}\nstderr: {r.stderr}"
    assert r.stdout.startswith("ok "), r.stdout
    ran = int(r.stdout.split()[1])
    assert ran >= 22, f"expected the full case list to run, got {ran}"
