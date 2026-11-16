# Vanilla Pi: minimal RFC pipeline support

Use Pi 1.4+ directly, without the Axon launcher. The RFC pipeline shares the tested Pi stream adapter,
Fleetlens session registration, write/citation hooks and session resume used by the Axon backend.
Claude Code remains the default. This does not change the older generic `harness run --engine pi` adapter.

Add an optional block to `~/.config/rfc-agent/config.json`:

```json
{"AGENT":{"PI":{"BIN":"pi","AGENT_DIR":"~/.pi/agent","PROVIDER":"openrouter","MODELS":{}}}}
```

Pi uses its existing credentials from that home, even when O1 is launched inside Axon.
No credentials are copied from Axon. The default Claude-tier mapping uses OpenRouter-compatible IDs:
`anthropic/claude-opus-5.5`, `anthropic/claude-sonnet-5.5`, and `anthropic/claude-haiku-5.5`.
Override `MODELS` by tier or exact stage model when using another provider. `PROVIDER` defaults to
`openrouter` to match those IDs, also when there is no `AGENT.PI` block; set it to `""` to use Pi's own selection.

```bash
RFC_AGENT_LANGFUSE=false uv run rfc --engine pi --prd /absolute/path/to/prd.md --no-figma
```

The environment switch above is the existing local-development tracing opt-out. Normal traced runs need
Langfuse credentials. Outputs, pauses, review gates and logs are unchanged; manifests identify this engine as
`pi`, not `axon`. Each agent keeps its session under the run folder.

Initial scope: local Markdown PRDs, no `--jira`. For Confluence/Jira inputs, select a supported Scope engine
explicitly, e.g. `--engine pi --stage-engine scope=claude-code`; Pi never silently falls back to Claude.
The shared model mapping is a tier mapping, not proof of identical upstream routing or context windows.
