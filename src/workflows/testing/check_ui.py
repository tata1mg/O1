"""`check_ui` — the `derive_ui` gate, runnable by the model from inside the stage.

The same reason as `check_suite` and `check_prerequisites`: one call prints the verdict, instead of the
model hand-rolling probes the sandbox denies. It also runs `playwright test --list`, the one check a model
cannot do by reading its own TypeScript: whether every spec actually loads.

Reads `qa/ui/cases.json`, `qa/ui/roots.json`, `qa/ui/settings.json` and `qa/ui/plan.json` under the run
dir (the cwd, or the one path argument) and the spec files; writes only the runner's config and
node_modules link in `qa/ui`. Allowlisted for `derive_ui` only.

    python check_ui.py [run_dir]

Exit 0 when the gate would pass, 1 when it would fail, 2 when a file is missing or not JSON.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ in (None, ""):  # run as a script: make `workflows.testing` importable from src/
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from workflows.testing import ui_web  # noqa: E402
from workflows.testing._io import parse_json  # noqa: E402


def report(run_dir: Path) -> tuple[int, str]:
    """-> (exit code, text). Split from `main` so tests need no subprocess."""
    ui = ui_web.ui_dir(run_dir)
    loaded = {}
    for name, kind in ((ui_web.CASES, list), ("roots.json", list), (ui_web.PLAN, dict)):
        value, why = parse_json(ui / name)
        if why or not isinstance(value, kind):
            return 2, f"{ui_web.UI_DIR}/{name}: {why or 'not a JSON ' + ('list' if kind is list else 'object')}"
        loaded[name] = value
    cases, roots, plan = loaded.values()
    settings, _ = parse_json(ui / ui_web.SETTINGS)
    login_required = bool((settings or {}).get("login_required")) if isinstance(settings, dict) else False
    titles, list_error = ui_web.list_titles(ui, login=isinstance(plan.get("login"), dict))
    failures = ui_web.validate(plan, cases, ui, titles, list_error, [Path(r) for r in roots], login_required)
    head = (f"{len(cases)} cases; {len(plan.get('tests') or [])} tests, "
            f"{len(plan.get('skipped') or [])} skipped; Playwright loaded {len(titles)}")
    if failures:
        return 1, "\n".join([head, "", f"GATE WOULD FAIL — {len(failures)} problem(s):"]
                            + [f"  - {f}" for f in failures])
    return 0, f"{head}\n\nGATE WOULD PASS"


def main(argv: list[str]) -> int:
    run_dir = Path(argv[0]) if argv else Path.cwd()
    code, text = report(run_dir)
    print(text)
    return code


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
