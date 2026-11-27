# CONFIG — settings and troubleshooting for rfc-agent and test-agent

A reference, not an install guide. To install, start at [INSTALL.md](INSTALL.md): it picks the guide for Claude
Code or Axon, and the agent checks every prerequisite for you. Come here to look up what a setting means, or what
a failure message points to. `/rfc-agent:doctor` (Claude Code) and `/rfc doctor` (Axon) check the whole setup at
any time.

## 1. The config files

Two files are merged (later wins), then environment variables:

| File | Holds | Who edits it |
|---|---|---|
| `config.harness.json` (plugin root) | the harness's own working values: models, timeouts, tracing behaviour, each with a note | nobody; read-only, an update replaces it |
| `~/.config/rfc-agent/config.json` | everything you supply: team values, your folders, keys and tokens | you (`chmod 600`) |

Dicts merge key by key, so you only write what you want different from `config.harness.json`. Nothing
org-specific ships in the plugin, and nothing you edit lives inside it. [config.example.json](config.example.json)
is the template; the install guides create your file from it and ask for what is missing. An unknown top-level key
(a typo like `REPO_ROOT`, or one left from an older version) is reported once and ignored.

**Never put a password in it.** The Kiwi password stays in `~/.tcms.conf` (`chmod 600`).

## 2. RFC settings

**Team values** (the same for everyone on your team; ask a teammate):

| Key | What it is |
|---|---|
| `ATLASSIAN.SITE` | Your Atlassian site. Scope fetches Confluence and Jira from it |
| `MCP.mcpServers.fleetlens.url` | The context layer's URL (see section 4) |
| `SERVICES_MAP.REPO` / `PATH` | Where your repo-to-deployed-service inventory lives |
| `GIT_REPOS_REMOTE` | The team's clone address with `{repo}` where the repo name goes, for example `git@bitbucket.org:<workspace>/{repo}.git` (copy it from any repo's Clone button). Scope clones the repos a PRD touches from it, and the Android setup clones `maestro-automation` from it. Leave empty to take it from the first git checkout in `REPOS_ROOT`. Env `RFC_AGENT_GIT_REPOS_REMOTE` wins |
| `REPOS_EXCLUDE` | Repos Scope never offers (for example an evals repo holding model answers) |
| `LANGFUSE.HOST` | Your team's Langfuse |

**Yours:**

| Key | What it is |
|---|---|
| `REPOS_ROOT` | Parent folder of your repo checkouts. A partial set is fine: Scope clones the repos you pick |
| `RUNS_ROOT` | Where run folders land (default `~/rfc-runs`). Keep it **outside** `REPOS_ROOT`: anything searchable there can be cited as if it were source |
| `LANGFUSE.PUBLIC_KEY` / `SECRET_KEY` | Your keys, from the team vault |
| `MCP.mcpServers.fleetlens.headers` | Your fleetlens token (`"Authorization": "Bearer <token>"`) |
| `OBS.USER_ID` | Trace identity. Order: `OBS.USER_ID`, then `git config user.email`, then `$USER`. Set it on CI or shared machines |

## 3. Engine settings (`AGENT`)

All optional. The pipeline runs on Claude Code by default; Axon is opt-in, per run or per stage.

| Key | What it is |
|---|---|
| `AGENT.ENGINE` | `claude-code` (default) or `axon` |
| `AGENT.STAGE_ENGINES` | Pin single stages, for example `{"review": "axon"}`. `--engine` and `--stage-engine` on a run beat both |
| `AGENT.DEFAULT_MODEL` | Keep the **`[1m]` suffix**, or the first request fails with `Prompt is too long` |
| `AGENT.CLAUDE_BIN` | The `claude` binary (default `claude`) |
| `AGENT.CLAUDE_CONFIG_DIR`, `AGENT.CLAUDE_CODE_OAUTH_TOKEN_FILE` | A separate Claude login for the agents. Empty means your normal login (a subscription or an API key both work) |
| `AGENT.EXTRA_DISALLOWED_TOOLS` | Comma-separated tool or server patterns denied in every stage (for example a claude.ai connector you keep away from the agents) |
| `AGENT.AXON.BIN` | The `axon` binary (default `axon`) |
| `AGENT.AXON.ATLASSIAN_SERVER` | The Atlassian server's name in Axon's `mcp.json`, used by Scope (default `atlassian`) |
| `AGENT.AXON.MODELS` | Maps the stages' Claude models to gateway models, by tier or exact id. `python -m rfc_agent.axon_agent models` prints the mapping |
| `AGENT.AXON.THRESHOLD_COMPACTION` | Allow Axon's early compaction (default `false`: it summarises evidence away) |
| `AGENT.AXON.RECONNECTS` | Resumes after a lost gateway connection (default 2) |
| `SCOPE.DISCOVERY_MODEL` | The model of the agent that finds the repos a PRD touches (default `claude-opus-5-5`) |

Precedence for the engine: `--stage-engine` > `--engine` > `AGENT.STAGE_ENGINES` > `AGENT.ENGINE` > `claude-code`.
`logs/run.json` records which engine each stage used. The model mapping and what differs on Axon are in
[AXON_INSTALL.md](AXON_INSTALL.md#beyond-rfc-and-test).

## 4. Testing settings (`TESTING`)

test-agent's settings, one per `testing.*` flag. The install guides fill them by asking; you rarely edit them by
hand. Every flag: [src/workflows/testing/README.md](src/workflows/testing/README.md).

| Key | Written by | What it is |
|---|---|---|
| `storage` | `preflight setup-kiwi` | `kiwi` for the team's Kiwi, `json` for offline trials (default `json`, which never opens a socket) |
| `kiwi_web` | `preflight setup-kiwi` | The Kiwi web URL (team value) |
| `product_prefix` | `preflight setup-kiwi` | The Kiwi product prefix, for example `Sandbox / ` (team value) |
| `kiwi_user` | `preflight setup-kiwi` | The Kiwi account the agent records as. It must be the agent's own: a case saved by any other account counts as touched by a human and is never overwritten |
| `kiwi_url`, `insecure_tls` | `preflight setup-kiwi` | The XML-RPC address if it differs from the web URL, and whether to skip TLS verification |
| `environments` | `preflight setup-web` | Comma-separated environment names a run may target |
| `base_url_template` | `preflight setup-web` | The web address with `{env}`, for example `https://{env}.example.com` |
| `production_base_url` | `preflight setup-web` | The live site's address, so "on prod" needs no typing. It only names the address: running there still needs `--production` |
| `sessions_dir` | `preflight setup-web` | Folder of recorded login sessions (default `~/.secrets/playwright`). Files are passed to the browser, never read |
| `ui_login`, `curl` | `preflight setup-web` | The **path** of a test-account login file, and of a captured curl (the path is stored, never the contents) |
| `maestro_repo` | `preflight setup-android` | Your `maestro-automation` checkout. Android runs stop with a how-to-set-up message when it is empty |

Secrets for testing stay out of this file: the Kiwi password in `~/.tcms.conf`, logins and curls under
`~/.secrets/`.

## 5. Connections

**Atlassian** (needed for Confluence and Jira PRDs; a local `--prd file.md` needs none).
- *Claude Code:* Scope fetches through your own signed-in Atlassian server, because Python cannot ride the OAuth
  itself. It needs a user-scope server on **Atlassian MCP v2** (v1 is deprecated and its tools are gone):

  ```bash
  claude mcp add --scope user --transport http atlassian https://mcp.atlassian.com/v2/mcp
  claude mcp list
  ```

  Authenticate with `/mcp` inside a `claude` session; `claude mcp list` must show `✔ Connected`. The server must be
  named `atlassian` and added with `--scope user` (the run's headless `claude` starts from the run folder, so a
  project-scoped server is not seen). **Already on v1?** Run `claude mcp remove atlassian` first, then the add above.
  The `claude.ai Atlassian` connector is still v1: it does not work here.
- *Axon:* `axon pi mcp login atlassian` (the install guide does it). `AGENT.AXON.ATLASSIAN_SERVER` names the server.
  **Already on v1?** `axon pi mcp remove atlassian`, `axon pi mcp add atlassian --url https://mcp.atlassian.com/v2/mcp`,
  `axon pi mcp login atlassian`.

**fleetlens** (the context layer; recommended). A plain HTTP MCP server under `MCP.mcpServers.fleetlens` (keep
that name), set in `config.json` only: no `claude mcp add`. It needs network reachability (VPN if internal).
Scope uses it to find the repos a PRD touches (about $0.50 and 90 seconds per run). Without it, pass the repos
with `--repos a,b`.

**bitbucket** (optional, Scope's fallback). When your service-inventory repo is not checked out and the 7-day
cache is stale, Scope reads the file through this local stdio server from a `bitbucket-mcp` checkout (workspace is
the team's; `BITBUCKET_USERNAME` and `BITBUCKET_APP_PASSWORD` are yours, read scopes only). Without it the run
still works and the mapping falls back to `STEERING.REPO_ALIASES`.

**Langfuse.** Tracing is required, and a run without keys refuses to start. Set `LANGFUSE.PUBLIC_KEY`,
`SECRET_KEY` and `HOST` (or the standard environment variables, which win). There is no setting or flag to turn it
off. On a shared Claude subscription the Claude account email is meaningless, so each person's
`git config user.email` should be their own.

**Figma** (RFCs, only when a PRD links designs). Scope exports the linked frames through the Figma **desktop app**
and the rfc-agent snapshot plugin; no Figma MCP server is involved. Import the plugin once from its fixed folder:
Figma, Plugins, Development, Import plugin from manifest, then
`~/.rfc-agent/figma_snapshot_plugin/manifest.json`. Every run refreshes that folder from the installed plugin, so
Figma always runs the latest version. Scope then asks you to press one shortcut per link.

## 6. Failure signature, cause, fix

| You see | Root cause | Fix |
|---|---|---|
| `Prompt is too long`, $0-1, first request | A bare model id (200K window) plus a fat startup manifest | Keep `[1m]` in `DEFAULT_MODEL`; prune connectors with `EXTRA_DISALLOWED_TOOLS` |
| `unknown key(s) [...] are ignored` | A typo, or a key left from an older version | Fix or delete it |
| Scope stops: `ATLASSIAN.SITE is not set` | Team values missing | Add them to your `config.json` (ask a teammate), or re-run the install guide |
| Scope says Atlassian is not connected | No signed-in Atlassian server | Section 5, then re-run |
| `context layer unavailable` | fleetlens unreachable or the token is wrong | VPN on; check `url` and `headers` in `config.json` |
| Run refuses to start: Langfuse keys missing | Tracing is required | Add the keys from the vault |
| `~/.tcms.conf is missing` / `testing.storage is 'json'` | Kiwi is not set up on this machine | Re-run the install guide for testing. You write `~/.tcms.conf` yourself |
| `Kiwi … is unreachable` | VPN off, a wrong Kiwi URL or login, or Kiwi itself is down (a 502 or 503) | Check your Kiwi setup: the VPN is on, `kiwi_web` and `kiwi_user` in `TESTING` are right, and `~/.tcms.conf` has your url, username and password. Run `python -m workflows.testing.preflight testing` to see which check fails, then retry |
| `Playwright is not installed` / `needs Chromium build …` | The web runner's browser is missing | Run the command the message names, in the plugin's `tools/ui-runner` |
| `no Android device is ready` / `no maestro-automation checkout` | No booted emulator with the app, or `maestro_repo` is empty | Boot the emulator; run `preflight setup-android --clone`, or re-run the install guide and say yes to Android |
