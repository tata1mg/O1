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
