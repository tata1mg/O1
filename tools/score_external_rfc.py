"""Score an EXTERNAL (human-written, pipeline-free) RFC against the pipeline's own deterministic gates —
the constant-quality baseline arm. Usage:

    .venv/bin/python tools/score_external_rfc.py --rfc PATH [--contract PATH] [--plan PATH] \
        --run-dir runs/<same-prd-run> --repos-root ~/Desktop/code

Applicable gates: citation resolution/qualification, REQ coverage (against the run's typed
requirements.json), absence-claim recheck, cross-doc consistency (when a plan is given), secrets.
Deliberately NOT applied, and why:
  - witness: trace-backed anti-fabrication for agents; a human has no tool trace, and a human's citations
    are accountable to their author, not to this gate;
  - provenance: exists because the sealed-room GENERATE agent may only cite what upstream stages read —
    a human is not in a sealed room;
  - RFC_SHAPE sections / plan CHANGE shape: pipeline formatting contracts, reported as INFO not FAIL —
    a human RFC may be organized differently without being worse.
Report both arms' numbers side by side; time the human arm yourself (wall-clock of the writing session).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from rfc_agent.gates import (  # noqa: E402
    CITATION_RE,
    check_absence_claims,
    check_citations,
    check_citations_qualified,
    check_no_secrets,
    claimed_local_repos,
    mentioned_req_ids,
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rfc", required=True)
    ap.add_argument("--contract")
    ap.add_argument("--plan")
    ap.add_argument("--run-dir", required=True, help="pipeline run for the SAME PRD (supplies requirements.json)")
    ap.add_argument("--repos-root", required=True)
    args = ap.parse_args()

    repos_root = Path(args.repos_root).expanduser()
    run_dir = Path(args.run_dir)
    docs = {"rfc": Path(args.rfc)}
    if args.contract:
        docs["contract"] = Path(args.contract)
    if args.plan:
        docs["plan"] = Path(args.plan)

    all_text = ""
    total_resolving = 0
    total_cites = 0
    print("=== gate results (external RFC) ===")
    for name, p in docs.items():
        text = p.read_text(encoding="utf-8", errors="replace")
        all_text += text + "\n"
        cites = [m.group(0) for m in CITATION_RE.finditer(text)]
        total_cites += len(cites)
        fails, warns, resolved = check_citations(text, repos_root, f"{name}", run_dir=run_dir)
        total_resolving += resolved
        q = check_citations_qualified(text, name)
        print(f"[{name}] citations={len(cites)} resolving={resolved} unresolved={len(fails)} unqualified={len(q)}")
        for f in fails[:5]:
            print(f"    FAIL {f[:150]}")
        for f in check_no_secrets(p):
            print(f"    FAIL {f[:150]}")

    repos = claimed_local_repos(all_text, repos_root) or [d.name for d in repos_root.iterdir() if d.is_dir()]
    ab = check_absence_claims(all_text, repos_root, repos, "external-rfc")
    print(f"[absence-claims] {'PASS' if not ab else f'{len(ab)} FALSE claim(s)'}")
    for f in ab[:5]:
        print(f"    {f[:170]}")

    req_p = run_dir / "extract" / "requirements.json"
    if req_p.exists():
        data = json.loads(req_p.read_text())
        req_ids = [r["id"] for r in data.get("requirements", []) if r.get("id")]
        present = mentioned_req_ids(all_text)
        missing = [r for r in req_ids if r not in present]
        print(f"[REQ coverage] {len(req_ids) - len(missing)}/{len(req_ids)} mentioned; missing: {missing or 'none'}")
        print("    NOTE: a human RFC may cover a requirement without naming its REQ-id — count semantic")
        print("    coverage by hand before comparing; this line is the mechanical lower bound.")
    else:
        print("[REQ coverage] SKIPPED — no extract/requirements.json in the run dir")

    cited_repos = sorted({c.split("/")[0] for c in CITATION_RE.findall(all_text) if isinstance(c, str)})
    print(f"\n=== metrics ===\ntotal citations: {total_cites} | resolving: {total_resolving} | repos cited: {len([r for r in repos if f'{r}/' in all_text])}")
    print("NOT applied (by design): witness (needs an agent tool-trace), provenance (sealed-room rule),")
    print("RFC_SHAPE/plan formatting (pipeline contracts, not quality).")


if __name__ == "__main__":
    main()
