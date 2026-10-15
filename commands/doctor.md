---
description: Preflight the rfc-agent pipeline — plugin freshness, config, binaries, MCP servers, repo checkouts
---

Run these checks in order against the plugin at `${CLAUDE_PLUGIN_ROOT}` and report a PASS/FAIL line for
each, with the concrete fix for every FAIL. Read-only — change nothing without asking.

0. **Plugin freshness (FIRST — a stale cache silently runs old code).** Read the installed version from
   `${CLAUDE_PLUGIN_ROOT}/.claude-plugin/plugin.json`, then the source checkout's version: marketplace
   source path from `~/.claude/settings.json` → `extraKnownMarketplaces."rfc-agent-marketplace".source.path`,
   then `<path>/.claude-plugin/plugin.json`. Versions differ ⇒ **FAIL — every check below would validate
   the wrong code.** Fix: `claude plugin update rfc-agent@rfc-agent-marketplace` (fully-qualified id; the
   bare name errors "not found"), then **restart the session** (the running one keeps serving the old
   cache), then re-run doctor. (Measured 2026-07-28: cache at 0.4.0 while the checkout was 0.6.0 — a whole
   optimisation round invisible to plugin runs; `marketplace add` alone does NOT refresh an installed plugin.)
1. **uv + python**: `uv --version` works; `uv run --project "${CLAUDE_PLUGIN_ROOT}" python -V` reports 3.12.
2. **claude CLI**: `claude --version` works (the stage engine shells out to it) AND is **>= 2.1.232**
   — earlier builds carry a 30s protocol-probe hang on slow/malformed MCP servers. FAIL fix: update
   Claude Code.
3. **Config file exists**: `~/.config/rfc-agent/config.json` (everything the user supplies; `$RFC_AGENT_CONFIG`
   overrides the path) — FAIL if missing, fix: run `/rfc-agent:setup`. It never belongs inside
   `${CLAUDE_PLUGIN_ROOT}` (an update wipes it). WARN if it isn't mode 600 (`stat -f %Lp`): it holds secrets.
4. **Config loads**: `uv run --project "${CLAUDE_PLUGIN_ROOT}" python -c "import rfc_agent.config as c; c.get_config(); print('resolved config:', c._LOCAL)"`
   exits clean — report the path. WARN on any `unknown key(s) [...] are ignored` line (typo or a leftover from an
   older version). Then check the team values resolve: `ATLASSIAN.SITE` is set (FAIL for Confluence/Jira PRDs).
   - **Langfuse (FAIL if tracing is on and the keys are missing — the run stops with exit 2)**:
     `uv run --project "${CLAUDE_PLUGIN_ROOT}" python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"`
     — prints `langfuse=off`, or `langfuse=on keys=ok|MISSING verbose=…`. `MISSING` fix: fill
     `LANGFUSE.PUBLIC_KEY`/`LANGFUSE.SECRET_KEY` in ~/.config/rfc-agent/config.json (or export
     `LANGFUSE_PUBLIC_KEY`/`LANGFUSE_SECRET_KEY`), or turn tracing off: `LANGFUSE.ENABLED: false` in that file,
     or `--langfuse false` on a single run.
5. **REPOS_ROOT + RUNS_ROOT**: REPOS_ROOT exists and contains repo checkouts (`ls` a couple of known
   ones). Additionally WARN (not FAIL) if RUNS_ROOT resolves INSIDE REPOS_ROOT: the tracers search the
   REPOS_ROOT tree, so run dirs living in it let a search hit prior runs' findings/RFCs and cite them as if
   they were source (README: keep it outside; default `~/rfc-runs`).
6. **MCP servers**: for each entry under `MCP.mcpServers` **whose value is a dict** (skip string values —
   they are documentation notes like `_bitbucket_note`; treating them as specs crashes the loop) — stdio: the
   command binary exists (check the literal path too, not just PATH); http: report the URL. Only
   `fleetlens` (the context layer: Scope, Investigate, Review) and `bitbucket` (Scope's fallback for the repo -> service map) are
   read from config. Atlassian is NOT in config: Scope uses the user's own logged-in server — confirm
   `claude mcp list` shows either `claude.ai Atlassian` (connector) or a user-scope `atlassian` server as
   ✔ Connected instead of probing it.
7. **Figma desktop app + snapshot plugin** (required): Scope exports frames through the desktop app and the
   rfc-agent snapshot plugin, not through MCP. FAIL if `/Applications/Figma.app` is missing. FAIL if
   `uv run --project "${CLAUDE_PLUGIN_ROOT}" python -c "from rfc_agent.scope import figma; missing, m, _ = figma.ensure_plugin(); print('NOT imported' if missing else 'imported', m)"`
   prints `NOT imported`; fix: in Figma, Plugins → Development → Import plugin from manifest… → the path it
   prints (the fixed folder `~/.rfc-agent/figma_snapshot_plugin`, refreshed by every run, so it survives
   plugin updates).
8. **Auth for headless runs**: `CLAUDE_CONFIG_DIR`/oauth-token-file settings in config.json AGENT block, if
   set, point at readable paths. Empty values are a PASS (stages inherit the ambient login).
9. **Unit tests** (optional, ~5s): `uv run --project "${CLAUDE_PLUGIN_ROOT}" pytest -q --rootdir "${CLAUDE_PLUGIN_ROOT}" "${CLAUDE_PLUGIN_ROOT}/tests"`
   is green. The `--rootdir` + explicit test path are REQUIRED: a bare `pytest -q` inherits the shell's
   cwd and, run from a workspace, collects every repo it can see (measured: 255 collection errors).

Finish with a one-line verdict: READY (all pass) or BLOCKED (name the first blocking failure). If check 0
failed, say explicitly that the remaining results describe the STALE copy.
