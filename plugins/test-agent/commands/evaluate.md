---
description: Score an existing testing run's suite on demand — the 8-dimension rubric (0–5) and the 14 pass/fail compliance checks — without regenerating anything (~15 min, ~$3–4)
argument-hint: [--run-dir <testing run>]
---

Evaluate the suite for: `$ARGUMENTS`

The evaluator is not part of `/test-agent:test` any more: generation goes straight to the Kiwi upload, and the
score is something you ask for. It scores `qa/cases.json` in the run dir — the suite as generated (or as edited
there), not edits a reviewer made in Kiwi.

1. **Project.** Resolve `PROJ` once and write it literally below:

   ```
   python3 -c 'import json,os; print(os.environ.get("TEST_AGENT_PROJECT") or json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json")))["rfc-agent-marketplace"]["installLocation"])'
   ```

2. **Preview.** Run `uv run --project <PROJ> python -m workflows.testing.evaluation $ARGUMENTS --print`. It
   picks the newest testing run (or `--run-dir`). On `missing …`, say what is missing and stop (a suite that
   was never generated needs `/test-agent:test`). Otherwise post one line: the run dir, the feature, how many
   cases (`… python -m workflows.testing.status <run dir> --json` → `cases.total`), and that it takes ~15 min
   and ~$3–4.

3. **Run** in the background (`run_in_background`), one command starting with the label:

   ```
   TASK="testing · evaluate · <run folder name>" uv run --project <PROJ> python -m workflows.testing.evaluation <same arguments>
   ```

   and in the same turn a **Monitor** (`timeout_ms` 1800000) on the run's activity log:

   ```
   cd "<run dir>/logs" && touch activity.log && tail -n 0 -F activity.log | grep --line-buffered -E '\] (STAGE started|gate PASSED|gate FAILED|ENGINE FAILURE)'
   ```

   `gate FAILED … automatic revision next` → `↻ evaluate gate failed — revising once automatically`, not an
   error yet. Stop the Monitor when the command exits.

4. **Report** from `… status <run dir> --json` → `eval`:
   - the score `overall/5` (coverage, quality) and how it is computed (coverage = average of dimensions 1–5,
     quality = average of 6–8, overall = average of the two);
   - compliance `PASS`/`FAIL`, and each failed check in plain words with how many violations
     (`qa/eval.json` → `compliance_checks.<name>.violations`);
   - `missing_scenarios`: how many gaps the evaluator found;
   - where the full scorecard is: `<run dir>/qa/eval.json`.

   If the run stopped, post `status --json` → `next.why.message` when present, else the evaluate stage's
   `failures`, and offer `Retry`.
