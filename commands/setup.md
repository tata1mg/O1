---
description: First-time setup for a new user — for RFCs (/rfc-agent:rfc), testing (/test-agent:*) or both; checks every prerequisite, fills the missing config by asking, and ends READY or BLOCKED
argument-hint: (no arguments)
---

Set up this machine for the RFC pipeline (`/rfc-agent:rfc`, Scope → Summarize), for testing (`/test-agent:test`,
`/test-agent:execute`, `/test-agent:convert-flows`), or both. test-agent has no code of its own: it runs this
plugin's harness, config and tracing, so testing needs the shared steps below too. The user may be brand new. YOU
run the checks, fix what you can, and ask the user only for what you can't know or do.

`PROJ="${CLAUDE_PLUGIN_ROOT}"` · `CFG=~/.config/rfc-agent/config.json` — everything the user supplies: team values
(Atlassian site, fleetlens URL, Bitbucket workspace, service inventory, Langfuse host),
their folders, keys and tokens. Settings resolve: env var > `CFG` > the plugin's `$PROJ/config.harness.json` (the
harness's own working values; read-only — never edit it, an update replaces the plugin).

`/test-agent:*` do not run from `$PROJ`: they run the harness from the marketplace's install location, a
different folder with its own Python environment. Resolve it once as `TP` (for testing or both):
`python3 -c 'import json,os; print(os.environ.get("TEST_AGENT_PROJECT") or json.load(open(os.path.expanduser("~/.claude/plugins/known_marketplaces.json")))["rfc-agent-marketplace"]["installLocation"])'`.

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

## 0. What is this machine for — ask first

If the user already said (the install guide passes it on: "for testing", "for RFCs", "for both"), use that. Otherwise ask
(AskUserQuestion) **"What will you use it for?"**:
- `Both (Recommended)` — every step below.
- `RFCs from PRDs` — steps 1–9; skip 10–11.
- `Testing only (Kiwi cases, web and Android runs)` — skip steps 5, 6, 7 and 9, and step 4's REPOS_ROOT is
  optional. Say in one line that `/rfc-agent:rfc` won't be ready, and that setup can be run again to add it.

| Step | RFCs | Testing |
|---|---|---|
| 1 Tools · 2 CLI sign-in · 3 Config · 8 Langfuse | CRITICAL | CRITICAL |
| 4 Folders | CRITICAL | RUNS_ROOT only |
| 5 Repo access · 6 Atlassian · 7 fleetlens · 9 Figma | as below | skipped |
| 10 Testing (Kiwi, Playwright) | skipped | CRITICAL |
| 11 Android | skipped | OPTIONAL, asked |

Mark a skipped step `skipped — not needed for <choice>` in the report.

## 1. Tools — CRITICAL

**Check:** `uv --version && git --version && [ "$(type -t rg)" = file ] && rg --version | head -1`. (Claude Code's
own `rg` is a shell function, which doesn't count: the pipeline needs a real binary.)

**If any is missing:** tell the user in one line that you'll install the missing tools, then run
`bash "$PROJ/scripts/install-deps.sh"`. It needs no Homebrew and no password: uv and rg go into `~/.local/bin`
(rg is the official GitHub release, sha256-checked). Then prefix later commands with
`export PATH="$HOME/.local/bin:$PATH";` so this session sees them. If git is missing, the script opens the
macOS "Install Command Line Tools" dialog and exits: ask the user to click Install, wait until it finishes,
then run the script again.

**Python env:** `uv run --project "$PROJ" python -V` prints 3.12.x. If not: `uv sync --project "$PROJ"`. For
testing, sync with the testing extra instead (Kiwi's client, the workbook and screenshot libraries):
`uv sync --project "$PROJ" --extra testing`. Use it every time on a testing machine: a plain `uv sync` removes the
extra again.

**For testing or both, also sync the test-agent folder** (`TP`, above), or `/test-agent:test` stops at its first
check with "tcms-api is not installed": `uv sync --project "$TP" --extra testing`. Check it with
`uv run --project "$TP" python -c "import tcms_api; print('testing extra: ok')"`.

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

For **Testing only**, REPOS_ROOT is optional: skip it unless they also want Android (step 11 clones
maestro-automation next to it).

**RUNS_ROOT** is where RFC and testing runs and deliverables land. Ask with `~/rfc-runs` (Recommended) first. It must NOT be
inside REPOS_ROOT (the pipeline searches REPOS_ROOT, and old runs there get cited as if they were code). Create it.

Write both into `CFG` with a Python edit (rule 5).

## 5. Repo access — CRITICAL

The pipeline clones the repos a PRD touches when a run needs them, so nothing has to be cloned now. It only needs
to know the team's **clone address**: `GIT_REPOS_REMOTE` in `CFG`, a team value with `{repo}` where the repo name
goes (all org repos share one Bitbucket workspace). If it is not set, the pipeline falls back to the origin of
**the first git checkout in REPOS_ROOT, alphabetically**.

**Check — clone address** (prints the address the pipeline will use, or `None`):

```bash
uv run --project "$PROJ" python -c "from pathlib import Path; from rfc_agent.config import get_config; from rfc_agent.scope.repos import configured_remote, remote_template; r=Path(get_config().get('REPOS_ROOT')).expanduser(); print('configured:', configured_remote()); print('used:', remote_template(r, configured_remote()))"
```

- `configured:` is set → ✅, say the address and move on.
- `configured: None`, `used:` is an address (taken from a checkout) → offer (AskUserQuestion) "Save this clone
  address in your config so it no longer depends on that checkout?" with `Save it (Recommended)`, `Not now`. On
  save, write it to `CFG` as `GIT_REPOS_REMOTE` (rule 5).
- Both `None` (no checkout, nothing set): ask (AskUserQuestion; a team value, not a secret) for the team's clone
  address: "Open any repo of the team in Bitbucket, click Clone, copy the SSH address, and replace the repo name
  with `{repo}` — for example `git@bitbucket.org:<workspace>/{repo}.git`. A teammate can tell you." Check it
  contains `{repo}`, then write it to `CFG` as `GIT_REPOS_REMOTE` (rule 5). Re-check. Do not clone anything.
- `used:` is a non-Bitbucket address (e.g. `github.com`) and nothing is configured: the first checkout
  alphabetically isn't a team repo. Set `GIT_REPOS_REMOTE` as above; it wins over the checkout. Don't move
  anything.

Optional: if the user wants a repo checked out locally, `git clone <the address with {repo} filled in>
"<REPOS_ROOT>/<repo>"`. A partial set is fine: the pipeline clones what it needs itself.

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

**Repo-discovery team value — OPTIONAL.** Check:
`uv run --project "$PROJ" python -c "from rfc_agent.config import get_config as g; c=g(); print('inventory repo:', (c.get('SERVICES_MAP') or {}).get('REPO') or 'none')"`.
`SERVICES_MAP` (where the repo → service inventory lives) is a team value that helps Scope map fleetlens's
services to repos. If it is missing or still the example's placeholder, ask whether a teammate can share it; if
so, open `CFG` and have the user paste it in, else mark
`skipped`: "Scope proposes fewer repos; add them with --repos".

## 8. Langfuse tracing — CRITICAL

Every run is traced to Langfuse, and a run with no keys stops before it starts. Tracing is required: there is no
setting or flag to turn it off.

**Check:** `uv run --project "$PROJ" python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"` prints `langfuse=on keys=ok` (exit 0), or `langfuse=on keys=MISSING` (exit 2). It never prints a key.

**If `keys=MISSING`:** the keys are in the team vault. Tell the user to copy the Langfuse public and secret keys
from the vault, open `CFG`, and tell them to paste them into `LANGFUSE.PUBLIC_KEY` and `LANGFUSE.SECRET_KEY`. Then
ask (AskUserQuestion) "Langfuse keys pasted?":
- Done (Recommended) → re-check. The host is a team value: if the team's isn't `https://cloud.langfuse.com`, set `LANGFUSE.HOST` in
  `CFG` too.
- No vault access → this step stays BLOCKED and setup ends BLOCKED: tracing is required, so ask the team for access
  to the Langfuse keys, then run `/rfc-agent:setup` again.

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

## 10. Testing: Kiwi and Playwright (test-agent) — CRITICAL for testing

Test cases live in the team's Kiwi TCMS; web cases run through Playwright (tools/ui-runner) and the ui-agent
drives a browser through Playwright MCP.

**Check** (no feature or RFC run needed; prints no secret):
`uv run --project "$TP" python -m workflows.testing.preflight testing --json` (through `TP`, the folder
`/test-agent:*` really run from, so a missing package shows up now and not on the first test). Each row has `id`,
`level`, `msg`, `fix`. Go through the rows that aren't `ok`:

- **`kiwi` with "testing.storage is …"** — Kiwi isn't set up here. Ask (AskUserQuestion; team values, not
  secrets) for the Kiwi web URL, the product prefix (e.g. `Sandbox / `) and the Kiwi username the agent records
  as, then run `… preflight setup-kiwi --kiwi-web <url> --product-prefix "<prefix>" --kiwi-user <user>`. It
  writes `TESTING` in `CFG`. Re-check.
- **`kiwi_conf` missing** — the Kiwi login is a password, so the user creates the file (rule 3: never through
  the chat, and not on a command line either, where it stays in shell history). Create the empty file and open
  it for them: `touch ~/.tcms.conf && chmod 600 ~/.tcms.conf && open -t ~/.tcms.conf`. Ask them to type these
  three lines with their own values (the url is the Kiwi web URL from `CFG`'s `TESTING.kiwi_web` plus
  `/xml-rpc/`) and save:
  ```
  [tcms]
  url = https://<kiwi-host>/xml-rpc/
  username = <user>
  password = <password>
  ```
  Ask "Done?", then re-check. A `warn` that it's readable by others → `chmod 600 ~/.tcms.conf`.
- **`kiwi_client` "tcms-api is not installed"** — `uv sync --project "$TP" --extra testing` (and the same for
  `"$PROJ"` if section 1 was skipped).
- **`kiwi` unreachable** — VPN off, or the Kiwi server is down. Say which; it stays BLOCKED until it answers.
- **`playwright` or `agent_browser`** — needs Node 18+ (`node -v`; if missing, ask the user to install Node 22
  from nodejs.org: no Homebrew assumed). Then run `cd "$PROJ/tools/ui-runner" && npm ci && npx playwright install
  chromium`, then whatever the `agent_browser` row's `fix` names (Playwright MCP's own Chromium build). Both
  download browsers (~150 MB each): say so in one line first. Re-check. They stay a note, not a block, only if
  the user runs backend cases alone.
- **`claude`** — step 2.

**Web logins — OPTIONAL, don't create them here.** Runs against a logged-in page take a recorded session
(`--ui-auth <file>`) or a test-account file, both under `~/.secrets/playwright/` and made per environment the
first time `/test-agent:test` or `/test-agent:execute` needs one; those commands walk through it. Only say they
exist.

## 11. Android test runs (test-agent) — OPTIONAL

`/test-agent:execute` can run CONFIRMED cases on the Android app: each case's validated Maestro flow, else an
agent on an emulator. The flows live in another team's repo, **maestro-automation**, which needs a real git
clone on this machine (its flows change daily; `convert-flows` writes branches back to it). Web and backend
runs never need it.

**Check:** `uv run --project "$PROJ" python -m workflows.testing.preflight setup-android --check --json` →
`configured` and `checkout_ok` true = ✅, say the path and move on.

**Otherwise ask first** (AskUserQuestion): **"Do you want to run test cases on Android too? It needs the
maestro-automation repo on this machine."**
- `Yes, clone it (Recommended)` → `… preflight setup-android --clone --json`. It clones into
  `suggested_path` (next to the user's other repos), using the team's clone address (`GIT_REPOS_REMOTE`, step 5),
  and saves the path in `CFG` (`TESTING.maestro_repo`). A failure names the cause: no SSH access to the repo →
  ask a teammate for access to `maestro-automation`, then re-run; no clone address → set `GIT_REPOS_REMOTE`
  (step 5) or pass `--url`.
- `I already have it` → ask for the folder (Other = path), then `… preflight setup-android --path <folder> --json`.
- `No, web only` → mark `skipped`: "Android runs are off; run /rfc-agent:setup again to add them".

Then report `adb` and `maestro` from the check: either missing is a note, not a block ("Android runs need the
Android SDK platform-tools and the Maestro CLI, and a booted emulator with the app"). Never install the SDK,
an emulator or an APK from here.

## 12. Report

One table, one row per step: ✅ / ❌ / skipped, plus a short note (versions, resolved paths, the clone URL
template, what's skipped and why). End with exactly one of:

- **READY** — then show the first command for each path set up:
  - RFCs: `/rfc-agent:rfc --prd <confluence-url>` (add `--repos a,b` if the context layer is skipped).
  - Testing: `/test-agent:execute ui-agent --feature <name> --kiwi-plan <plan id>` runs an existing Kiwi plan's
    CONFIRMED cases; `/test-agent:test --rfc-run-dir <run>` writes cases from a finished RFC run. Plain words work
    too: "cart-sanity on stag, cases 173,172".
  - If test-agent isn't installed (`claude plugin list` has no `test-agent@rfc-agent-marketplace`), say to run
    `claude plugin install test-agent@rfc-agent-marketplace` and restart Claude Code.
- **BLOCKED: <first failed CRITICAL step>** — with the one action that unblocks it.

Not needed, so don't set them up or check them: Google Sheets, the Figma MCP server or connector (designs come
only from the snapshot plugin, step 9), Bitbucket SSH keys. The Bitbucket MCP server is an optional fallback (CONFIG.md §5).
