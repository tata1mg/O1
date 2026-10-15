---
description: First-time setup for a new user — checks every prerequisite of the pipeline, fills the missing config by asking, and ends READY or BLOCKED
argument-hint: (no arguments)
---

Set up rfc-agent on this machine for the pipeline (`/rfc-agent:rfc`, Scope → Summarize). The user may be brand
new. YOU run the checks, fix what you can, and ask the user only for what you can't know or do.

`PROJ="${CLAUDE_PLUGIN_ROOT}"` · `CFG=~/.config/rfc-agent/config.json` — everything the user supplies: team values
(Atlassian site, fleetlens URL, Bitbucket workspace, product surfaces, service inventory, Langfuse host),
their folders, keys and tokens. Settings resolve: env var > `CFG` > the plugin's `$PROJ/config.harness.json` (the
harness's own working values; read-only — never edit it, an update replaces the plugin).

## Rules

1. **Check before you change.** Every step starts with a check. If it passes, say so in one line and move on.
   Running this command twice must change nothing the second time.
2. **Ask with AskUserQuestion** for every missing value that isn't secret (folders, repo names, yes/no). Offer
   the detected or recommended value as the first option, marked "(Recommended)".
3. **Never take a secret through the chat.** No tokens, passwords or Authorization values in AskUserQuestion,
   never echo or `cat` one. For a secret: open `CFG` for the user
   (`open -t ~/.config/rfc-agent/config.json`), name the exact field to fill, wait for them to say done, then
   verify with a check that prints only a status.
4. **Sign-ins are the user's.** You can't complete a login or an OAuth flow. Give the exact action and what
   success looks like, then re-check.
5. **Edit `CFG` only with small Python scripts** that load the JSON, change named keys and write it back. Never
   hand-edit, never drop keys, never write any config inside `$PROJ` (an update wipes it). Team values are not
   secrets: ask for them with AskUserQuestion (a teammate can tell the user each one).
6. CRITICAL steps block the run. If one can't be fixed now, keep going with the rest, then report BLOCKED.
   OPTIONAL steps may stay unfinished: mark them `skipped` with what the pipeline loses.

## 1. Tools — CRITICAL

**Check:** `uv --version && git --version && [ "$(type -t rg)" = file ] && rg --version | head -1`. (Claude Code's
own `rg` is a shell function, which doesn't count: the pipeline needs a real binary.)

**If any is missing:** tell the user in one line that you'll install the missing tools, then run
`bash "$PROJ/scripts/install-deps.sh"`. It needs no Homebrew and no password: uv and rg go into `~/.local/bin`
(rg is the official GitHub release, sha256-checked). Then prefix later commands with
`export PATH="$HOME/.local/bin:$PATH";` so this session sees them. If git is missing, the script opens the
macOS "Install Command Line Tools" dialog and exits: ask the user to click Install, wait until it finishes,
then run the script again.

**Python env:** `uv run --project "$PROJ" python -V` prints 3.12.x. If not: `uv sync --project "$PROJ"`.

## 2. Claude CLI sign-in — CRITICAL

The pipeline starts its own headless `claude` processes. They use the **command-line tool's** sign-in, which
is separate from the desktop app's: the app can be signed in while the CLI is not.

**Check:** `claude auth status` shows `"loggedIn": true`.

**If not:** ask the user to open Terminal, run `claude`, type `/login`, finish in the browser, then quit. Re-check.
Symptom when this is missing: Scope stops with *"headless claude refused the request: Not logged in"*.

## 3. Config file — CRITICAL

**Check:** `test -f ~/.config/rfc-agent/config.json`. If missing, ask (AskUserQuestion) "Do you have a teammate's
rfc-agent config.json to start from?" (only its team values are reusable; their keys and folders are not):
- No (Recommended for a first user) →
  `mkdir -p ~/.config/rfc-agent && cp "$PROJ/config.example.json" ~/.config/rfc-agent/config.json`; the later
  steps ask for every value it still lacks.
- Yes → ask for its path, copy it to `~/.config/rfc-agent/config.json`, then with a Python edit (rule 5) blank
  every personal value it carries: `REPOS_ROOT`, `RUNS_ROOT`, `LANGFUSE.PUBLIC_KEY` / `SECRET_KEY`,
  `MCP.mcpServers.fleetlens.headers`, the `bitbucket` server's `args` and `BITBUCKET_USERNAME` /
  `BITBUCKET_APP_PASSWORD`, `OBS.USER_ID`. The later steps fill them in.

Then `chmod 600 ~/.config/rfc-agent/config.json` (it holds secrets).

An existing `CFG` from an older version needs no migration: keys it lacks come from `config.harness.json`. If
loading prints `unknown key(s) [...] are ignored`, offer to remove them (a Python edit, rule 5) — they're typos or
leftovers.

**Verify:** `uv run --project "$PROJ" python -c "import rfc_agent.config as c; c.get_config(); print('resolved:', c._LOCAL)"`
prints the `~/.config/rfc-agent/config.json` path with no error. `stat -f %Lp ~/.config/rfc-agent/config.json`
is `600` (fix: `chmod 600`).

## 4. Folders — CRITICAL

Read the current `REPOS_ROOT` and `RUNS_ROOT` from `CFG` (expand `~`).

**REPOS_ROOT** is where the user's repo checkouts live. If it's unset, or the configured folder doesn't exist,
or is still the example's placeholder, find candidates: folders under `~` (depth ≤ 3, skip `Library`, `.cache`,
`node_modules`) holding at least one git checkout whose `origin` is in the team's Bitbucket workspace
(`CFG`'s `MCP.mcpServers.bitbucket.env.BITBUCKET_DEFAULT_WORKSPACE`, if set). Ask (AskUserQuestion): the best candidate (Recommended), other candidates,
`~/code (create it)`; Other = a path. Create it if it doesn't exist.

**RUNS_ROOT** is where RFC runs and deliverables land. Ask with `~/rfc-runs` (Recommended) first. It must NOT be
inside REPOS_ROOT (the pipeline searches REPOS_ROOT, and old runs there get cited as if they were code). Create it.

Write both into `CFG` with a Python edit (rule 5).

## 5. Repo access — CRITICAL

The pipeline clones repos it needs, and it works out the clone URL from **the first git checkout in
REPOS_ROOT, alphabetically** (all org repos share one Bitbucket workspace).

**Check — clone URL:**

```bash
uv run --project "$PROJ" python -c "from pathlib import Path; from rfc_agent.config import get_config; from rfc_agent.scope.repos import remote_template; r=Path(get_config().get('REPOS_ROOT')).expanduser(); print(remote_template(r))"
```

Must print a clone URL for the team's workspace, like `git@bitbucket.org:<workspace>/{repo}.git`.
- `None` (no checkout yet): ask which repo they work on most (Other = name), then
  `git clone git@bitbucket.org:<workspace>/<repo>.git "<REPOS_ROOT>/<repo>"`. Re-check.
- A non-Bitbucket URL (e.g. `github.com`): the first checkout alphabetically isn't a Bitbucket repo. Tell the
  user which folder it is and offer: move it out of REPOS_ROOT, or pick a different REPOS_ROOT. Don't move
  anything without a yes.

Also ask (multi-select, optional) which other repos they usually touch, and clone the missing ones. A partial
set is fine: the pipeline clones what it needs itself.

## 6. Atlassian (Confluence + Jira) — CRITICAL for Confluence/Jira PRDs

**Site first.** Check: `ATLASSIAN.SITE` resolves to a real site, not the example's placeholder:
`uv run --project "$PROJ" python -c "from rfc_agent.config import get_config as g; s=g().get('ATLASSIAN.SITE') or ''; print(s if s and 'your-company' not in s else 'MISSING')"`.
If `MISSING`, ask (AskUserQuestion) for the team's Atlassian site (`https://<name>.atlassian.net`; not a secret)
and write it to `CFG` as `ATLASSIAN.SITE` (rule 5). Below, `<SITE>` is that value.

The pipeline reads PRDs through Atlassian's MCP server. **Either** one works:
- the **claude.ai Atlassian connector** (tools `mcp__claude_ai_Atlassian__*`), or
- a user-scope server named `atlassian` added with `claude mcp add ... atlassian https://mcp.atlassian.com/v1/mcp`
  (tools `mcp__atlassian__*`).

**Check:** `claude mcp list | grep -iE "claude\.ai Atlassian|^atlassian"` — at least one line shows `✔ Connected`. If none does, ask the user to
connect one (a listed `atlassian` that needs auth: `/mcp` → atlassian → authenticate). For the connector: claude.ai → Settings → Connectors → Atlassian → Connect, sign in, allow
access to `<SITE>`. Then re-check.

If `claude mcp list` still doesn't show it but the user says it's connected, run the real test (≈10 s, a few
cents; needs step 2 done):

```bash
uv run --project "$PROJ" python -c "
import tempfile; from pathlib import Path; from rfc_agent.scope import atlassian as at
d=Path(tempfile.mkdtemp())
with at.ConnectorSession(d/'probe.jsonl', cwd=d) as s:
    try: s.connect('<SITE>'); print('atlassian_ok')
    except at.AtlassianError as e: print('atlassian_fail:', e)
"
```

A PRD given as a local markdown file needs none of this, so if the user can't connect now, mark it BLOCKED
for Confluence/Jira only.

## 7. Context layer (fleetlens) — OPTIONAL, strongly recommended

Scope uses it to find which repos a PRD touches; Investigate and Review use it to trace endpoints and callers
across repos. Without it, the user must name the repos themselves (`--repos`). It is set up **only** through
`CFG` — never with `claude mcp add`.

The URL is a **team value**; the Authorization value is a **secret**. Both go in `CFG` under
`MCP.mcpServers.fleetlens` (the name must be exactly `fleetlens`), never in the plugin or its repo.

**Check:** the resolved `MCP.mcpServers.fleetlens.url` is set and doesn't contain `your-fleetlens-host`,
and the resolved `headers.Authorization` is non-empty (print only `True/False`, never the value).
If `CFG` still has a `context-service` entry, tell the user it is no longer read and can be deleted.

**If the URL is missing:** ask (AskUserQuestion) for it (not a secret) and write it to `CFG` (rule 5).
**If the token is missing:** it's in the team vault. Tell the user to copy the rfc-agent fleetlens token from the
vault, then open `CFG` and tell them to add `"headers": {"Authorization": "Bearer <the token>"}` under
`MCP.mcpServers.fleetlens`. Ask (AskUserQuestion) "Done?" with options "Done (Recommended)" and "No vault access
yet". Done → re-check. No access → mark `skipped`: "get vault access, then copy the fleetlens token into config".

**Reachability:** `curl -s -m 5 -o /dev/null -w "%{http_code}" "<url>"` is not `000`. `000` → VPN off.

**Repo-discovery team values — OPTIONAL.** Check (prints counts only):
`uv run --project "$PROJ" python -c "from rfc_agent.config import get_config as g; c=g(); print('surfaces:', len(c.get('SCOPE.SURFACES') or {}), '| inventory repo:', (c.get('SERVICES_MAP') or {}).get('REPO') or 'none')"`.
`SCOPE.SURFACES` (product surfaces → the repos that build them) and `SERVICES_MAP` (where the repo → service
inventory lives) are team values that help Scope propose repos. If either is missing or still the example's
placeholder, ask whether a teammate can share them; if so, open `CFG` and have the user paste them in, else mark
`skipped`: "Scope proposes fewer repos; add them with --repos".

## 8. Langfuse tracing — CRITICAL unless turned off

Every run is traced to Langfuse by default (`LANGFUSE.ENABLED: true`), and a traced run with no keys stops
before it starts.

**Check:** `uv run --project "$PROJ" python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"` prints `langfuse=off` or `langfuse=on keys=ok` (exit 0). It never prints a key.

**If `keys=MISSING`:** the keys are in the team vault. Tell the user to copy the Langfuse public and secret keys
from the vault, open `CFG`, and tell them to paste them into `LANGFUSE.PUBLIC_KEY` and `LANGFUSE.SECRET_KEY`. Then
ask (AskUserQuestion) "Langfuse keys pasted?":
- Done (Recommended) → re-check. The host is a team value: if the team's isn't `https://cloud.langfuse.com`, set `LANGFUSE.HOST` in
  `CFG` too.
- No vault access, run without tracing → set `LANGFUSE.ENABLED` to `false` with a Python edit (rule 5), re-check
  (`langfuse=off`), and mark the step `skipped`: "runs aren't traced; set LANGFUSE.ENABLED true once you have keys".

`LANGFUSE.VERBOSE` (default `false`) adds one span per tool call. Leave it unless the user asks.

## 9. Figma — CRITICAL

Designs are read only through the Figma desktop app and the rfc-agent snapshot plugin. No Figma MCP server or
connector is involved. Both parts are required.

**A — the app.** Check: `test -d /Applications/Figma.app`. If missing, ask the user to install it from
https://www.figma.com/downloads/, open it and sign in (the website can't run development plugins). Ask
"Done?", then re-check.

**B — the snapshot plugin.** Figma must load it from a fixed folder,
`~/.rfc-agent/figma_snapshot_plugin`. The plugin's own folder changes with every update, so an import from there
would stop working. This check refreshes the fixed folder with the current plugin, then reports (it's the same
check a run does):

```bash
uv run --project "$PROJ" python -c "from rfc_agent.scope import figma; missing, m, old = figma.ensure_plugin(); print('NOT imported' if missing else 'imported'); print('manifest:', m); print('old copies:', old)"
```

- `imported` → ✅. Every run refreshes the fixed folder, so Figma always runs the latest plugin.
- `NOT imported` → show the user the `manifest:` path and ask them to, in the Figma app, open any design file,
  then **Plugins → Development → Import plugin from manifest…** and pick that file. If `old copies` lists
  folders, also ask them to remove those entries under **Plugins → Development → Manage plugins in
  development**. Success: **Plugins → Development** lists **rfc-agent Figma snapshot** once. Ask "Done?", then
  re-check. If it still says `NOT imported` but the user sees the entry, accept it and note "confirmed by the
  user" (Figma may write its settings file later).

## 10. Report

One table, one row per step: ✅ / ❌ / skipped, plus a short note (versions, resolved paths, the clone URL
template, what's skipped and why). End with exactly one of:

- **READY** — then show the first command: `/rfc-agent:rfc --prd <confluence-url>` (add `--repos a,b` if the
  context layer is skipped).
- **BLOCKED: <first failed CRITICAL step>** — with the one action that unblocks it.

Not needed, so don't set them up or check them: Google Sheets, the Figma MCP server or connector (designs come
only from the snapshot plugin, step 9), Bitbucket SSH keys. The Bitbucket MCP server is an optional fallback (SETUP.md §4).
