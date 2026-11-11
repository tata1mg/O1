# Changelog

All notable changes to this project. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions match `.claude-plugin/plugin.json`.

## 0.2.0

### Added

- **Axon engine.** The pipeline's agents can run on Axon, the org's managed Pi, instead of `claude -p`:
  `rfc --engine axon`, or a single stage with `--stage-engine review=axon` (also config `AGENT.ENGINE` /
  `AGENT.STAGE_ENGINES`). Claude Code stays the default and its path is unchanged. Claude models map to gateway
  models by tier (`AGENT.AXON.MODELS`). `python -m rfc_agent.axon_agent doctor` checks the setup. The harness
  gains an `axon` adapter. See [docs/AXON.md](docs/AXON.md) and [decisions.md](decisions.md).
- **`/rfc` inside Axon.** `uv run python -m rfc_agent.axon_agent install` adds an `/rfc` command to Axon.
  It runs the same pipeline, shows progress above the editor and asks each pause as a question in the
  session. `/rfc attach <run>` reconnects to a run, and `/rfc doctor` checks the setup. It also adds a
  read-only `rfc_status` tool and turns on Pi's `read`/`grep`/`find`/`ls`, so asking Axon about a run doesn't
  hit terminal approval prompts. Others can install it without a checkout:
  `axon pi install git:github.com/tata1mg/O1@<tag>` (the repo is now a Pi package).
- **Confluence/Jira on Axon.** When Scope runs on Axon, it fetches the PRD through Axon's own Atlassian MCP
  sign-in (`axon pi mcp login atlassian`), so `--engine axon` no longer needs `claude`. The Axon doctor checks
  the sign-in.

## 0.1.1 (pilot)

### Changed

- **Feedback asks what was wrong in each area, not a list of reasons.** The thumb is asked first. A plain 👍 is one
  tap. 👍 with notes or 👎 opens one more screen with four areas: coverage, buildability, approach and context layer
  use. Readable summary, rollout and risk, and the run questions are asked only on request. Each area is asked once
  with its gaps in the same question, and each gap carries the stages to check first. What a person types under
  Other is kept against the question it was typed on, next to what they ticked there. Re-rating replaces the whole
  earlier rating, including typed notes it does not repeat. The old list of reasons (`--reason`) is gone. See
  [docs/FEEDBACK.md](docs/FEEDBACK.md).
- **Gaps have self-explanatory names** (`marked_covered_but_not_addressed`, `ignores_engineering_standards`, ...) and
  each rating also records which stages its gaps point at (`rfc_suspect_stage`), so "which stage to fix" is a chart.
- **Run facts are numbers on the trace too.** When a run closes, the numeric facts (PRD size, requirement coverage,
  tokens, context-layer calls, operator wait, ...) are written as `fact_*` scores so a dashboard can chart them.
- **Every PRD run gets its own folder.** `/rfc-agent:rfc --prd <url>` always starts a new run in
  `<name>-<timestamp>`, even for a PRD that was run before, so a rerun never mixes into an earlier one and each run is
  its own Langfuse session. A run that stopped, paused or failed is continued with `--run-dir <folder>` and no
  `--prd`: same folder, same options (saved in `logs/run_args.json`), finished stages skipped. It stays one run, and
  `run.json` records `attempts`, `resumes` and the time of all attempts. Giving both `--prd` and the folder of an
  existing run is refused.
- **A brief travels with every rating.** A few plain lines on the run, its review rounds and what the reviewer said
  are sent on the headline score and saved as `logs/run_brief.md`. New run numbers cover what Scope proposed against
  what was kept, the readable copy's length against the full RFC, and review findings by severity with how many were
  fixed and how many revise rounds it took.
- **Stopping a run stops its agents, and the attempt is recorded.** Ctrl-C, or a plain stop of the process, now ends
  every agent that is running (they run in their own process groups, so before they carried on and kept spending) and
  records the attempt as `stopped at <stage>`, so it can be continued with `--run-dir`.
- **How a run ended is a score.** `rfc_run_outcome` on each attempt's trace and `rfc_run_outcome_final` on the run's
  session, so a dashboard can chart stops per stage and runs that finished after a resume.
- **Run facts grew.** Every feedback score and the `rfc-run` observation now carry input size (PRD words, Figma
  sections and frames), total tokens and cache hit ratio, requirement and open-question counts, compactions and
  peak context, context layer calls and errors, and how long a person kept the run waiting.
- **Review records how long the triage prompt waited** (`triage_wait_s` in `logs/review.json`), apart from the
  stage's own time.
- **Every run ends with a short summary.** Outcome, time and cost, input size, requirements, review findings and any
  failed uploads to Langfuse. It is printed in the terminal, saved as `logs/run_summary.md` and shown in the trace's
  Output pane. It says "Reviewer: not rated yet" until someone rates.
- **Failed uploads to Langfuse are counted.** Each failed or dropped upload is written to `logs/activity.log` as
  `[langfuse] export failed: …` and counted per run as `fact_langfuse_export_failures`. See
  [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md) for `LANGFUSE_TIMEOUT`.
- **Langfuse tracing is required.** The `--langfuse` flag and the `LANGFUSE.ENABLED` setting are gone, and
  `--langfuse false` is refused. A run with no Langfuse keys stops before it starts (exit 2), and `/rfc-agent:setup`
  ends BLOCKED without them. Only the environment variable `RFC_AGENT_LANGFUSE=false` still turns tracing off, as an
  internal switch for tests and development. `--langfuse-verbose` stays and applies to the call that gives it.
- **A resumed run gets `--yes` and `--confirm-file` back** with the other options it started with.
- **The run brief says "no Figma exported"** when the PRD had no Figma, so a "missing Figma flow" gap can be read
  against it.

### Fixed

- **Draft's cost and time stay in the run totals.** Revise's re-checks used to overwrite them with zero, so a run
  reported less than it cost.
- **A new PRD can no longer go into an existing run folder.** `--prd` with the folder of an existing run is refused,
  with or without `--redo-scope`; `--run-dir <folder> --redo-scope` runs Scope again on the run's saved PRD.
- **Re-rating without `--stage` clears the stage an earlier rating of the same trace named.**
- **Only span-exporter errors count as failed uploads**, and a trace that fails to open no longer leaves the
  watcher installed; the spans are flushed before the run's numbers are counted.
- **A Ctrl-C while an agent is starting no longer leaks it:** the agent stays registered until it is ended.
- **One malformed `scope.json` no longer drops the run's numbers.**
- **`make smoke` uses a new folder each time.**
- **Verbose tracing no longer crashes** on a `permission_denied` event whose message is plain text.
- **The fleetlens tools `get_source`, `get_engineering_guidance` and `discover_libraries` are allowed.** Agents were
  refused them before.

### Security

- `certifi` 2026.7.22 → 2026.10.23 (PYSEC-2026-4320) and the OpenTelemetry packages 1.44.0 → 1.45.2
  (CVE-2026-114603).

## 0.1.0 (pilot)

First public release.

### Added

- **The RFC pipeline.** `/rfc-agent:rfc --prd <confluence-url>` turns a PRD into a grounded tech RFC in six
  stages (Scope, Investigate, Draft, Review, Revise, Summarize). Each stage can also be run on its own. A
  deterministic Python gate checks the output before the next stage starts. See [docs/HLD.md](docs/HLD.md).
- **Install and setup by asking Claude.** Say "Set up rfc-agent by following INSTALL.md". `/rfc-agent:setup`
  checks every prerequisite and `/rfc-agent:doctor` diagnoses a broken install. No Homebrew is needed.
- **Context layer.** [fleetlens](https://github.com/tata1mg/fleetlens) is used as the MCP context layer to find
  the repos a PRD touches and to trace endpoints and callers across them.
- **Figma designs** are read through the desktop app and the rfc-agent snapshot plugin.
- **Tracing and feedback in Langfuse.** Every run is traced, with secrets scrubbed client-side before export.
  `/rfc-agent:rfc` ends by asking for a 👍/👎 and `/rfc-agent:feedback` rates any run later. See
  [docs/OBSERVABILITY.md](docs/OBSERVABILITY.md).
- **The harness**, an agent-agnostic runner with `claude-code`, `pi` and `replay` engines, and one workflow,
  `testing` (QA test cases from an RFC). See [docs/harness/README.md](docs/harness/README.md).
- **A security gate** (`scripts/security/`): secret, dependency and static scans, run by a pre-push hook and
  GitHub Actions.
