# Changelog

All notable changes to this project. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions match `.claude-plugin/plugin.json`.

## 0.1.1 (pilot)

### Changed

- **Feedback asks what was wrong in each area, not a list of reasons.** A plain 👍 is one tap. 👍 with notes or 👎
  opens two more short screens: coverage, buildability, approach and context layer use, then readable summary,
  rollout and risk, and how the run went. Each area is asked once with its gaps in the same question, and each gap
  carries the stages to check first. What a person types under Other is kept against the question it was typed
  on, next to what they ticked there. Re-rating replaces the whole earlier rating. The old list of reasons
  (`--reason`) is gone. See [docs/FEEDBACK.md](docs/FEEDBACK.md).
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
- **How a run ended is a score.** `rfc_run_outcome` on each attempt's trace and `rfc_run_outcome_final` on the run's
  session, so a dashboard can chart stops per stage and runs that finished after a resume.
- **Run facts grew.** Every feedback score and the `rfc-run` observation now carry input size (PRD words, Figma
  sections and frames), total tokens and cache hit ratio, requirement and open-question counts, compactions and
  peak context, context layer calls and errors, and how long a person kept the run waiting.
- **Review records how long the triage prompt waited** (`triage_wait_s` in `logs/review.json`), apart from the
  stage's own time.

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
