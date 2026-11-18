---
description: Turn the draft Maestro flows that Android agent PASSes left behind (from /test-agent:execute) into flows that run green on a device — a plain run first, then a few bounded fix rounds by a model; records one comment per case in Kiwi, never an execution; optionally copies the green flows onto a maestro-automation branch for review
argument-hint: [--run-dir <testing run> | --feature F] [--cases TC-…,…] [--devices S,S] [--maestro-repo PATH] [promote --folder <flows folder>]
---

Convert draft flows for: `$ARGUMENTS`

1. **Project.** Resolve `PROJ` as `/test-agent:execute` does (`TEST_AGENT_PROJECT`, else the
   rfc-agent-marketplace install location). Every helper is `uv run --project <PROJ> python -m workflows.testing.…`.

2. **What is ready.** Run `… status <run dir> --json` (the newest testing run, or `--run-dir`, or the run
   folder of `--feature`) and read `results_android.drafts`. Empty → say no Android agent PASS left a draft
   (they appear after `/test-agent:execute ui-agent --platform android` on cases with no validated flow) and
   stop. Otherwise post the case ids and say once:
   - each draft is first run plainly on a device (no model, no cost); only a draft that fails gets up to
     three fix rounds (`testing.convert_fix_rounds`), each a model session that may change selectors and
     waits in that one flow file — never what it asserts;
   - none of these attempts is recorded as a Kiwi execution; the case's existing execution gets one comment
     saying green / stuck / rejected;
   - nothing is written into maestro-automation in this step.

3. **Run** in the background, with a Monitor on progress exactly as `/test-agent:execute` step 4 does:

   ```
   TASK="testing · convert-flows" uv run --project <PROJ> python -m workflows.testing.execute convert [--run-dir R | --feature F] [--cases …] [--devices …] [--maestro-repo …]
   ```

   `ENGINE FAILURE convert_flows: adb is not installed` / `no Android emulator or device is running` / `… has the <variant> build` → post it as it is
   and stop; never install or boot anything.

4. **Report** from `status --json` → `conversions`: one line per case — `green` (the flow file path),
   `stuck` (the `why`: the step it kept failing at, or `PRODUCT:` when the fixer judged the app itself does
   not do what the flow asserts — say that plainly, it is a finding), `rejected` (the lint reason, e.g. a
   coordinate tap). List any `warnings` (positional `index:` selectors) for the reviewer.

5. **Promote (only when asked).** Green flows stay in the run folder until someone asks to put them in
   maestro-automation. Then first show what would happen:

   ```
   uv run --project <PROJ> python -m workflows.testing.convert_flows promote <run dir> --folder <flows folder> --repo <maestro repo>
   ```

   It prints the branch (`agent-flows/<folder>-<date>`), the worktree beside the repo, and each file's
   destination (`.maestro/flows/<folder>/agent/<case>.yaml`). Ask (AskUserQuestion) **"Copy N green flows
   onto branch <branch> for review?"** with `Copy them` / `Not now`. Only on yes re-run with `--apply`. It
   creates the worktree and copies the files; it does not commit or push — say that the branch is for the
   flows' owners to review, and that until it is merged those cases keep running by agent.
