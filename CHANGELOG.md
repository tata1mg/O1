# Changelog

All notable changes to this project. Format follows [Keep a Changelog](https://keepachangelog.com/);
versions match `.claude-plugin/plugin.json`.

## 0.3.0

Review feedback from running rfc-agent on Axon (#14, #15, #18, #19, #29, #30). test-agent is **0.9.1** (the parameter-error
fix below; its version is in `plugins/test-agent/.claude-plugin/plugin.json`).

### Breaking

- **Atlassian MCP v2 only (#15).** Scope reads the PRD with `getConfluenceContent`, sub-pages through `executeRead`
  (`getConfluenceContentDescendants`) and Jira with `getJiraIssue`; the site hostname is the cloudId, so the
  `getAccessibleAtlassianResources` lookup is gone. v1 (`/v1/mcp`, deprecated, removed 2027-03-01) is not supported:
  a server still on v1 stops Scope with "looks like it is still on v1", and the Axon doctor names the three commands.
  **Everyone re-adds the server once:** Claude Code `claude mcp remove atlassian` then `claude mcp add --scope user
  --transport http atlassian https://mcp.atlassian.com/v2/mcp`, then `/mcp` to sign in; Axon `axon pi mcp remove
  atlassian`, `axon pi mcp add atlassian --url https://mcp.atlassian.com/v2/mcp`, `axon pi mcp login atlassian`. The
  `claude.ai Atlassian` connector is still v1 and does not work.

### Added

- **`--figma` and `--instructions` (#19, #14).** `--figma <link>` (repeatable) gives the run a Figma design; it replaces
  any link in the PRD, and so does a Figma link typed in the instructions. `--instructions <text | file>` adds
  instructions that win over the PRD where they conflict: the untouched PRD is kept as `raw/prd.source.md`, the
  instructions as `raw/instructions.md`, and `raw/prd.md` carries them on top for every stage. Both are remembered on
  resume.
- **`/rfc` in plain words (#14).** `/rfc Build an rfc from <PRD link> and use the latest flows` becomes `--prd`,
  `--figma` and `--instructions` (a PRD link is required; without one nothing starts). The trace is tagged
  `input:prompt` and carries the prompt, clipped to 300 words. Works in Claude Code and Axon.
- **Add a design link at the no-design question (#19).** With no Figma link anywhere, Scope asks *Proceed without
  design / Stop / Add your design* before Figma runs, so a pasted link goes through the normal screen picking.
- **Langfuse records what the operator gave the run:** input mode, where the Figma link came from (flag,
  instructions, PRD, added at the prompt, none) and which PRD links were ignored, and the instructions (300 words).
- **Mermaid diagrams are parsed before the RFC is handed over (#18).** Draft's gate runs Mermaid's own parser on every
  diagram, repairs `;` in message text (`#59;`), sends what still fails to the lead once with the parser's message, and
  shows a diagram that still fails as plain code under a note instead of a block that errors on render. The counts
  and reasons go to `out/check.json` and to Langfuse (`mermaid_*` scores, and `mermaid_failed`: 1 when any diagram is
  shown as code, else 0). The parser installs itself on first use (Node + npm, pinned by `package-lock.json`); without
  them the step is skipped and says so.

### Changed

- **Repo prompt (#30).** After the repo list, Scope offers *Continue with these repos* or *Add more repos / set a
  branch*; the text box appears only on the second, says what it is for and that Enter skips, and Scope reports what
  happened to each name typed (added with its branch, or not on Bitbucket and dropped). Repos typed on an earlier
  pass are no longer forgotten when the list is shown again.
- **`--yes` help text** now says it runs unattended and takes the default for every decision that would pause the run,
  instead of a list of what it skips.

- **`/rfc doctor` on Axon is a table** (`doctor --table`): every check on its own row with PASS / FAIL / WARN and, for a
  failure, how to fix it, like the Claude Code doctor. It adds checks the Axon doctor lacked: Node 22.19+ for Pi,
  the config file and `ATLASSIAN.SITE`, Langfuse keys, the repos and runs folders, the Figma app and plugin, and the
  Mermaid check's Node/npm. The check on Atlassian also fails when the server is still on v1.

### Fixed

- **Any wrong parameter now says so (#29), for `/rfc` and `/test`, on Claude Code and Axon.** One shared parser
  (`harness/cli_errors.py`, used by `rfc`, every stage CLI, the harness and the test-agent helpers) names the wrong
  option, suggests the nearest one and lists what the command supports: `[rfc] STOP: Invalid parameter found:
  --figmaa (did you mean --figma?), please check supported parameters while running the /rfc command. Supported: …`.
  Nothing is checked by option name. The Axon `/test` command also posts the STOP / error lines (or the exit code)
  of any launch that ends badly, where it used to end without a reason. Exit codes are unchanged.
- **Figma links without a file name were missed (#19).** `figma.com/design/<key>?node-id=0-1`, a bare
  `figma.com/design/<key>` and `node-id=71%3A125` were read as "the PRD links no Figma design" and stopped the run.
  They are found now.

## 0.2.1

test-agent is listed in the same marketplace as **0.9.0** (new on `main`: the plugin, with the testing workflow's
execution stages). Its version is in `plugins/test-agent/.claude-plugin/plugin.json`.

### Added

- **`/rfc feedback` on Axon.** `/rfc feedback [--run-dir R] [up|down]` asks the same questions as
  `/rfc-agent:feedback` for a finished, stopped, failed or unknown-end run (and a re-rating, with a preview) and
  saves them on the run's Langfuse trace through `feedback.py`. Axon offers it once when a run ends. Each area has
  a `Something else (type it)` row for a gap that is not listed, and a note is asked only where a gap was ticked.
- **`GIT_REPOS_REMOTE`** (env `RFC_AGENT_GIT_REPOS_REMOTE`): the team's clone address, with `{repo}` where the repo
  name goes. Scope and the Android setup clone from it first and fall back to the first checkout in `REPOS_ROOT`.
  Setup asks for it once, so install no longer needs a repo to be cloned.

### Changed

- **One install flow.** `INSTALL.md` is a router: the agent detects Claude Code or Axon, follows only that guide,
  and stops with a "raise a GitHub issue" message on any other harness. The Claude guide is `CLAUDE_INSTALL.md`,
  the Axon guide `AXON_INSTALL.md` (moved out of `docs/`, installs from `main`, assumes Axon is installed and
  signed in). `SETUP.md` is replaced by `CONFIG.md` (settings and a failure table). `docs/AXON.md` is folded into
  `AXON_INSTALL.md` and removed. **Links to the old paths break.**
- test-agent is 0.9.0 (it was never on `main`; 0.9.0 stays above the 0.8.0 some testers already hold).

### Fixed

- `/test-agent:*` run from the marketplace's folder, not the plugin cache that setup synced, so the Kiwi client
  was missing on the first test. `/rfc-agent:setup` now installs and checks that folder too.
- Axon rating: a typed note after "Nothing wrong" was scored as a gap.

## 0.2.0

### Added

- **Axon engine.** The pipeline's agents can run on Axon, the org's managed Pi, instead of `claude -p`:
  `rfc --engine axon`, or a single stage with `--stage-engine review=axon` (also config `AGENT.ENGINE` /
  `AGENT.STAGE_ENGINES`). Claude Code stays the default and its path is unchanged. Claude models map to gateway
  models by tier (`AGENT.AXON.MODELS`). `python -m rfc_agent.axon_agent doctor` checks the setup. The harness
  gains an `axon` adapter. See [AXON_INSTALL.md](AXON_INSTALL.md) and [decisions.md](decisions.md).
- **`/rfc` inside Axon.** `uv run python -m rfc_agent.axon_agent install` adds an `/rfc` command to Axon.
  It runs the same pipeline, shows progress above the editor and asks each pause as a question in the
  session. `/rfc attach <run>` reconnects to a run, and `/rfc doctor` checks the setup. It also adds a
  read-only `rfc_status` tool and turns on Pi's `read`/`grep`/`find`/`ls`, so asking Axon about a run doesn't
  hit terminal approval prompts. Others can install it without a checkout:
  `axon pi install git:github.com/tata1mg/O1@<tag>` (the repo is now a Pi package).
- **Confluence/Jira on Axon.** When Scope runs on Axon, it fetches the PRD through Axon's own Atlassian MCP
  sign-in (`axon pi mcp login atlassian`), so `--engine axon` no longer needs `claude`. The Axon doctor checks
  the sign-in.

### Changed

- **Scope finds repos with one Opus agent, in two tiers.** The Haiku PRD summary and the surface table
  (`SCOPE.SURFACES`, `SCOPE.SUMMARY_MODEL`) are gone. One agent (`claude-opus-5-5`, `SCOPE.DISCOVERY_MODEL`)
  explores fleetlens and returns "changes" repos (proposed ticked) and "maybe" repos (proposed unticked, with a
  reason), backend and frontend, up to 15. Same on Claude Code and Axon.
- **Langfuse Python SDK v5, ready for the Langfuse v4 platform.** `langfuse>=5,<6` (5.2.0 locked). A trace's
  input and output are now its root observation's own (`set_trace_io` / `set_current_trace_io` are gone in v5).
  The secret scrubber decodes v5's JSON-encoded metadata and input/output before scrubbing, so redacting a key
  no longer corrupts the JSON. `scripts/mine_reasons.py` reads the Observations API v2
  (`/api/public/v2/observations`, cursor pages, `--days`) instead of the v1 endpoint Langfuse Cloud retires on
  2026-11-16. `tests/infra/test_langfuse_v5.py` fails on any call v5 removed or any retired endpoint.

### Fixed

- **Investigate: a Figma interaction with no action no longer crashes step 1b.** An on-click with no action
  set exports as `"actions": [null]`, and reading prototype links raised `AttributeError`. Empty actions and
  interactions are now skipped.
- **Axon: a recovered retry no longer fails the agent.** A failed model attempt that Pi retried successfully
  kept its error; now only exhausted retries and aborts fail.
- **Scope: the fleetlens token is no longer left on disk.** Without a run folder, the discovery agent's MCP
  config went to `$TMPDIR` and stayed there; it now goes in a scratch folder that is deleted.
- **Scope: a failing progress callback kills the discovery agent** instead of leaving it running.
- **Scope: an error Claude reports with exit 0 (e.g. a usage limit) keeps its reason** on the agent card
  instead of "exit 0".
- **Rate limits are counted from protocol fields** and failed-result text (`API Error: 429`, Pi's
  `rate_limit_error`), not from any message that mentions rate limits.

## 0.1.2 (pilot)

### Changed

- **Approach gaps: one "poor approach" gap.** `ignores_existing_infra` is replaced by `poor_approach` ("poor approach
  for our systems, e.g. the wrong service or place to build it"). `ignores_engineering_standards` stays separate.
- **Plainer gap labels.** Coverage: "RFC says it's covered, but the software design in the RFC doesn't address it."
  Buildability: "Too vague to start coding", "Ambiguous interface or API contract", "Doesn't say what to build first or
  what depends on what", "A source is cited but doesn't say that".
- **Time questions after the thumb** (after 👍 with notes or 👎, skippable; a plain 👍 stays one tap): minutes this RFC saved you versus writing it yourself,
  and minutes you spent reviewing and correcting the RFC after it was generated. Stored as numeric scores
  (`rfc_minutes_saved`, `rfc_minutes_spent`), written only when typed.
- **`rfc_minutes_status`** (`given`, `partial`, `skipped`, `not_rated`) is written on every rating, so a re-rate always
  replaces it. `--minutes-skipped` records a skip. Read the status before the minutes: numbers left by an earlier
  rating are stale when the status is not `given` or `partial`.
  `--minutes-skipped` together with typed minutes is refused.

### Added

- A stopped run (a stage failure, Ctrl-C or a stop) prints the feedback command for that run folder:
  `/rfc-agent:feedback --run-dir <folder>`. The folder is quoted, so a path with spaces can be copied.
- **Feedback for a stopped or failed run.** The run records how each attempt ended (`logs/attempts.jsonl`: user stop,
  failure with a cause category and a scrubbed excerpt, or killed) and scores it on the trace (`rfc_run_stop_kind`,
  `rfc_run_failure_cause`). `/rfc-agent:feedback` reads that and opens on the matching path, so nobody is asked how the
  run ended: a stopped run asks the main reason (one choice, typed Other) and whether to run it again; a failed run asks
  whether the recorded cause is right and whether to run it again. Scores: `rfc_run_stop_reason`,
  `rfc_failure_cause_confirmed`, `rfc_failure_cause_reported`, `rfc_rerun`, and `rfc_run_end` only when the run has no
  end record. Only answered questions are written.
- **Feedback is kept per attempt.** A run that was stopped, rated, resumed and finished keeps the stopped attempt's
  feedback; the finished attempt gets its own rating. Each score's metadata has `attempt`, `attempts` and `run_state`.
  See [docs/FEEDBACK.md](docs/FEEDBACK.md#a-run-that-stopped-or-failed).
- A process that is killed without a chance to write anything is recorded as `killed` on the next start
  (`logs/attempt_open.json`).
- **Re-rating shows the earlier answers.** `feedback --context` returns `previous`, the earlier rating of the same
  attempt (from the run folder, or from Langfuse when the run folder has none), and the form shows it. A question left
  unanswered keeps its earlier answer (`--rerate`), and a re-rating is confirmed on a summary table
  (`--rerate --preview`, which sends nothing) before it is submitted. A different attempt starts blank.
- **A table of what was sent after every submission,** for finished, stopped and failed attempts, with a "Compared
  with last time" column on a re-rating. See [docs/FEEDBACK.md](docs/FEEDBACK.md#re-rating-an-attempt).

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
