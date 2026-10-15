# SETUP — everything rfc-agent needs before its first run

Operator-facing prerequisites, in install order. The quickest route is `/rfc-agent:setup` in Claude Code: it
checks every item below and fills the missing config by asking. Run `/rfc-agent:doctor` after any update.

## 0. TL;DR checklist

- [ ] uv, ripgrep and git installed (`/rfc-agent:setup` runs `scripts/install-deps.sh`); `uv run --project <plugin> python -V` → 3.12
- [ ] Claude Code CLI ≥ 2.1.232, logged in (`claude auth status`)
- [ ] `~/.config/rfc-agent/config.json` in place, with your team's values and your own
- [ ] `REPOS_ROOT` holds your repo checkouts; `RUNS_ROOT` is OUTSIDE it
- [ ] Atlassian ✔ Connected in `claude mcp list` (claude.ai connector or a user-scope `atlassian` server)
- [ ] (recommended) `MCP.mcpServers.fleetlens` reachable (VPN if internal)
- [ ] Langfuse keys, or tracing turned off (`LANGFUSE.ENABLED: false`)
- [ ] Figma desktop app installed and signed in, with the rfc-agent snapshot plugin imported (required)

## 1. Host toolchain

| Tool | Why | Check |
|---|---|---|
| **uv** | runs the pipeline (`uv run rfc`), manages Python 3.12 | `uv --version` |
| **Claude Code CLI** | every agent is a headless `claude -p` child | `claude --version` ≥ 2.1.232 |
| **ripgrep** | Investigate's repo facts and checks | `rg --version` |
| **git** | repo snapshots and clones, Langfuse user identity | `git --version` |
| **Node 22 + npx** | only if you configure the optional `bitbucket` stdio server | `node -v` |

uv, ripgrep and git: `bash scripts/install-deps.sh` installs whichever is missing (no Homebrew, no password;
uv and rg go into `~/.local/bin`). `/rfc-agent:setup` runs it for you.

**Claude auth:** agents inherit the ambient login — a Max/Pro subscription login or an API key both work.
Nothing to configure unless you need a separate identity (then `AGENT.CLAUDE_CONFIG_DIR` /
`AGENT.CLAUDE_CODE_OAUTH_TOKEN_FILE`).

## 2. Install the plugin

From a **local checkout** (recommended — survives updates; a git-installed cache is wiped):

```
/plugin marketplace add /path/to/rfc_agent
/plugin install rfc-agent@rfc-agent-marketplace
```

After every version bump: `claude plugin update rfc-agent@rfc-agent-marketplace` (fully-qualified id)
**and restart the session** — the running one keeps serving the old cache. Verify with doctor item 0.

## 3. Config files

Two files, merged (later wins), then environment variables:

| File | Holds | Who edits it |
|---|---|---|
| `config.harness.json` (plugin root) | the harness's own working values — models, timeouts, tracing behaviour — each with a note | nobody; read-only, a plugin update replaces it |
| `~/.config/rfc-agent/config.json` | everything you supply: team values, your folders, keys and tokens | you (`chmod 600`) |

Dicts merge key by key, so you only write what you want different from `config.harness.json`. Nothing
org-specific ships in the plugin, and nothing you edit lives inside it.

```bash
mkdir -p ~/.config/rfc-agent
cp config.example.json ~/.config/rfc-agent/config.json && chmod 600 ~/.config/rfc-agent/config.json
```

`/rfc-agent:setup` does this for you and asks for anything missing. A plugin update never needs you to edit your
file. An unknown top-level key (a typo like `REPO_ROOT`, or one left from an older version) is reported once and
ignored.

**Team values** (the same for everyone on your team — ask a teammate):

- **`ATLASSIAN.SITE`** — your Atlassian site; Scope fetches Confluence/Jira from it.
- **`MCP.mcpServers.fleetlens.url`** and the `bitbucket` server's workspace — section 4.
- **`SCOPE.SURFACES`** — your product surfaces → the repos that build them. Scope matches the PRD against them.
- **`SERVICES_MAP.REPO` / `PATH`** — where your repo → deployed-service inventory lives.
- **`REPOS_EXCLUDE`** — repos Scope never offers (e.g. an evals repo holding model answers).
- **`LANGFUSE.HOST`** — your team's Langfuse.

**Yours:**

- **`REPOS_ROOT`** — parent directory of your repo checkouts.
- **`RUNS_ROOT`** — where run dirs land (default `~/rfc-runs`). Keep it **outside** REPOS_ROOT: anything
  searchable there can be cited as if it were source.
- **`LANGFUSE.PUBLIC_KEY` / `SECRET_KEY`** — section 6.
- **`MCP.mcpServers.fleetlens.headers`** and the `bitbucket` credentials and path — section 4.
- Any harness value you want different, e.g. `AGENT.DEFAULT_MODEL` (keep the **`[1m]` suffix**) or
  `LANGFUSE.VERBOSE`. `config.harness.json` lists them all.

## 4. MCP servers

### Atlassian (CRITICAL for Confluence/Jira PRDs) — not in config.json
Scope fetches the PRD through **your own** logged-in Atlassian server; Python cannot ride the OAuth itself.
Either the claude.ai Atlassian connector, or a user-scope server:

```bash
claude mcp add --transport http atlassian https://mcp.atlassian.com/v1/mcp
# then inside a claude session: /mcp -> authenticate
claude mcp list   # must show: atlassian ... ✔ Connected   (or: claude.ai Atlassian ... ✔ Connected)
```

A local-path PRD (`--prd ./prd.md`) needs none of this.

### fleetlens — the context layer (recommended — Scope, Investigate, Review)
Plain HTTP MCP in `MCP.mcpServers.fleetlens` (keep that name): the team's `url` and your `headers`
(`"Authorization": "Bearer <token>"`, the token from the team vault). Set it up in config.json only — no `claude mcp add`. Only prerequisite is
network reachability (VPN if internal). Scope runs a small agent over it to find the repos a PRD touches (about
$0.50 and 90 s per run); without it, pass the repos yourself with `--repos a,b`. It replaced `context-service`
in 0.40.0: an old `context-service` entry is ignored and can be deleted.

### bitbucket (optional — Scope's fallback)
Scope maps repos to deployed services from your team's inventory (`SERVICES_MAP`). If that repo is not checked
out and the 7-day cache is stale, it reads the file through this server instead. Local stdio server from the
`bitbucket-mcp` checkout: `command: node`, `args: [--dns-result-order=ipv4first, /path/to/bitbucket-mcp/src/server.js]`,
`env: BITBUCKET_DEFAULT_WORKSPACE` (the team's) and `BITBUCKET_USERNAME` / `BITBUCKET_APP_PASSWORD` (yours, read
scopes only). Without it the run still works; the
mapping falls back to `STEERING.REPO_ALIASES`.

## 5. Repo workspace

- Check out the repos your PRDs usually touch under `REPOS_ROOT`. A partial set is fine: Scope clones the
  repos you pick, working out the clone URL from the first git checkout in REPOS_ROOT.

## 6. Observability

- Tracing is **on by default**, and a run with no keys refuses to start. The keys are in the team vault. Set `LANGFUSE.PUBLIC_KEY` /
  `SECRET_KEY` / `HOST` (or the standard env vars, which win), or turn it off with `LANGFUSE.ENABLED: false`
  (one run: `--langfuse false`).
- Trace identity: `OBS.USER_ID` (set explicitly on CI/shared boxes) > `git config user.email` > `$USER`. On a
  shared Claude subscription the Claude account email is meaningless — make sure each operator's
  `git config user.email` is their own.

## 7. Figma (required)

Scope exports the linked frames through the **desktop app** and the rfc-agent snapshot plugin — no Figma MCP
server is involved. Both are required: install the Figma desktop app and sign in, then import the plugin once
from its fixed folder: Figma → Plugins → Development → Import plugin from manifest… →
`~/.rfc-agent/figma_snapshot_plugin/manifest.json`. Every run refreshes that folder from the installed plugin, so
Figma always runs the latest version, even after an update. `/rfc-agent:setup` checks both. Scope then asks you to
press one shortcut per link.

## 8. Failure signature → cause → fix

| You see | Root cause | Fix |
|---|---|---|
| `Prompt is too long`, $0-1, first request | bare model id (200K window) + fat startup manifest | keep `[1m]` in DEFAULT_MODEL; prune connectors (EXTRA_DISALLOWED_TOOLS) |
| `unknown key(s) [...] are ignored` | a typo, or a key left from an older version | fix or delete it |
| Scope stops: `ATLASSIAN.SITE is not set` | team values missing | add them to your config.json (ask a teammate), or run `/rfc-agent:setup` |
| Scope says Atlassian is not connected | no logged-in Atlassian server | section 4, then re-run |
| run refuses to start: Langfuse keys missing | tracing is on by default | add the keys, or `LANGFUSE.ENABLED: false` |
