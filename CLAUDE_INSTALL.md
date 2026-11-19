# Install rfc-agent and test-agent for Claude Code

Two Claude Code plugins from the same repo, for a Mac:

- **rfc-agent** turns a PRD into a tech RFC.
- **test-agent** writes QA test cases into Kiwi, and runs the confirmed ones on the web and on Android. It runs
  on rfc-agent's code and settings, so both are always installed together.

**To install, open Claude Code and say one of:**

> Set up rfc-agent by following CLAUDE_INSTALL.md

> Set up test-agent by following CLAUDE_INSTALL.md

> Set up rfc-agent and test-agent by following CLAUDE_INSTALL.md

Claude does the work. You only sign in to a few things, copy keys from the team vault, and (for RFCs) import one
plugin into Figma. It takes about 15 minutes. On Axon instead of Claude Code: [AXON_INSTALL.md](AXON_INSTALL.md).

## What you need first

Everyone:

| What | Why | Where to get it |
|---|---|---|
| Claude Code, already installed | The plugins run inside it | — |
| Access to the GitHub repo `tata1mg/O1`, **or** the plugin zip (`rfc-agent-<version>.zip`) | It's where the plugin is installed from https://github.com/tata1mg/O1 | Your team |
| The **Langfuse keys** | Every run is traced | The team vault |

For RFCs:

| What | Why | Where to get it |
|---|---|---|
| An account on your team's **Confluence / Jira** site | PRDs and tickets are read from there | Your team |
| The **team values**: Atlassian site, fleetlens URL, product surfaces, service inventory | Settings shared by the whole team. Not secret | A teammate |
| A running **fleetlens** server (the context layer), its URL and token | Scope, Investigate and Review use it to find and trace code across repos. fleetlens is open source: [github.com/tata1mg/fleetlens](https://github.com/tata1mg/fleetlens) | Your team runs it. The token is in the team vault |
| The **Figma desktop app**, signed in, with **edit access** to the PRD's Figma files | The pipeline reads designs through it and a small plugin. Plugins work on the free tier, but you need edit access to each file, so for now use a dev account. Only needed if the PRD links Figma designs | figma.com/downloads |

For testing:

| What | Why | Where to get it |
|---|---|---|
| A **Kiwi TCMS** login, and the team's Kiwi URL and product prefix | Test cases and results live there | Your team |
| **Node 18+** (22 recommended) | Web cases run through Playwright | nodejs.org |
| The VPN, if Kiwi or the test environments are internal | Runs reach them from your Mac | — |
| For Android only: the Android SDK platform-tools (`adb`), the Maestro CLI, Java, an emulator with the app, and access to the `maestro-automation` repo | Android cases run their Maestro flow, or an agent, on the emulator | Your team |

---

## Instructions for Claude

You are setting up rfc-agent, test-agent, or both, for the user. Follow the steps in order.

**What for.** Take it from the user's request: "rfc-agent" alone → RFCs; "test-agent" alone → testing; both
named → both. If it isn't clear, ask (AskUserQuestion) "What will you use it for?": "Both (Recommended)", "RFCs
from PRDs", "Testing only". Carry the answer into Step 3.

**Rules**

1. **Run every command yourself.** Every step starts with a check. If it passes, say so in one line and move
   on. Running this guide twice must change nothing the second time.
2. **Only the user can sign in, copy from the vault, or click in an app.** For those (marked **User:**), tell
   them exactly what to do and what success looks like. Ask "Done?" with AskUserQuestion, then re-check.
3. **Never take a secret through the chat.** No token, key or password in a message or an AskUserQuestion
   answer, and never print one. Secrets go into the config file; checks print only a status.
4. **Ask choices with AskUserQuestion**, with the recommended option first, marked "(Recommended)".
5. **Don't set up** Homebrew, Claude Code, the Figma MCP server or Figma connector. None
   is needed.

### Step 1 — Sign in the `claude` command

The pipeline starts its own `claude` processes in the background. They use the command-line sign-in, which is
separate from the desktop app's.

**Check:** `claude auth status` shows `"loggedIn": true`.

**User:** open the Terminal app, run `claude`, type `/login`, and finish the sign-in in the browser. Then type
`/exit`.

### Step 2 — Install the plugin

**Check:** `claude plugin list --json` has `rfc-agent@rfc-agent-marketplace` with `"enabled": true` (and, for
testing, `test-agent@rfc-agent-marketplace` too). If so, skip to "Find the plugin folder".

Ask (AskUserQuestion) "How are you installing rfc-agent?": "From GitHub (Recommended)" if the user has access to
`tata1mg/O1`, or "From a zip file".

**From GitHub:**

```bash
claude plugin marketplace add tata1mg/O1
claude plugin install rfc-agent@rfc-agent-marketplace
claude plugin install test-agent@rfc-agent-marketplace
```

Install test-agent only for testing or both; it needs rfc-agent either way. This tracks the repo's default branch, which is what the team installs from.

**From a zip file:** ask for the zip's path (usually `~/Downloads/rfc-agent-<version>.zip`). Unzip it into
`~/rfc-agent`, a folder that stays put: the plugin runs straight from it, so don't delete or move it. If
`~/rfc-agent` already exists, look at it first, and replace it only if it holds an older rfc-agent (it has
`.claude-plugin/plugin.json` with `"name": "rfc-agent"`).

```bash
unzip -q <zip-path> -d ~/rfc-agent-unzip && rm -rf ~/rfc-agent && mv ~/rfc-agent-unzip/rfc-agent ~/rfc-agent && rmdir ~/rfc-agent-unzip
claude plugin marketplace add ~/rfc-agent
claude plugin install rfc-agent@rfc-agent-marketplace
claude plugin install test-agent@rfc-agent-marketplace
```

(The last line only for testing or both.)

**Check:** `~/rfc-agent/.claude-plugin/marketplace.json` exists, and `claude plugin list` shows
`rfc-agent@rfc-agent-marketplace` read from `~/rfc-agent`.

**Find the plugin folder.** In `claude plugin list --json`, the `rfc-agent@rfc-agent-marketplace` entry gives:

- `installPath` — call it `PLUGIN`. The plugin's commands run from here.

### Step 3 — Run the plugin's setup

This session doesn't see the plugin's `/rfc-agent:*` commands until Claude Code restarts. So don't ask the
user to restart yet. Instead, read `PLUGIN/commands/setup.md` and follow it end to end, here and now, with the
"what for" answer as its step 0. Wherever it says `$PROJ` or `${CLAUDE_PLUGIN_ROOT}`, use `PLUGIN`.

What it covers depends on the answer:
- **Everyone:** the tools (it installs uv, ripgrep and git if missing), the `claude` sign-in, the config file,
  the runs folder and Langfuse.
- **RFCs:** the repos folder and a first checkout, Atlassian, fleetlens, and Figma (the desktop app plus the
  rfc-agent snapshot plugin).
- **Testing:** Kiwi (the team's settings, and a `~/.tcms.conf` login the user types in themselves), Playwright
  (`npm ci` and its browsers), and, if the user wants it, Android (it clones `maestro-automation`).

For secrets (the fleetlens token, Langfuse keys, the Kiwi password) it has the user paste them into a file
itself.

It ends with **READY**, or **BLOCKED** plus the one thing to fix. If BLOCKED, help the user fix that, then
re-run only the failed step.

### Step 4 — Hand over

Tell the user, in this order:

1. **Restart Claude Code** (quit it fully, then open it again). Then the `/rfc-agent:*` commands appear.
2. **Their first run:** for RFCs, `/rfc-agent:rfc --prd <confluence-page-url>` ("Your first RFC" below). For
   testing, "Your first test run" below.

---

## Your first RFC

In Claude Code:

```
/rfc-agent:rfc --prd <confluence-page-url>
```

Add `--jira <KEY>` if there's a ticket. Here's what happens:

- **It runs for 30–60 minutes in the background.** Claude posts short progress lines.
- **It pauses to ask you questions.** First which repos, pages and Figma sections are in scope, later a few
  questions about the requirements. Answer them in the chat. Nothing moves until you do.
- **For each Figma link,** it opens the design in the Figma app and asks you to press **⌥⌘P** (run last
  plugin). The first time, run it from **Plugins → Development → rfc-agent Figma snapshot** instead. Keep the
  plugin's small window open until Claude says the screens are exported.
- **You can watch the agents work.** In the terminal, the **rfc agents** panel lists every agent and shows
  the output of the one you pick: press ctrl+x tab, then that agent's number. `/tasks` also lists one
  entry per agent.
- **Read `out/rfc.readable.md` first** when it's done (Claude gives you the path). `out/rfc.md` is the full
  RFC with every code reference.

If it stops, fix what Claude reports and run the same command again. It carries on from where it stopped.

## Your first test run

In Claude Code, run the CONFIRMED cases of an existing Kiwi test plan:

```
/test-agent:execute ui-agent --feature <name> --kiwi-plan <plan id>
```

Or say it in plain words: `/test-agent:execute cart-sanity on stag, cases 173,172, show the browser`. To
write new cases from a finished RFC run instead: `/test-agent:test --rfc-run-dir <run folder>`. Cases you
confirm in Kiwi are then run with `/test-agent:execute`.

- **It asks before anything risky:** production, a logged-in session, a test account, or actions such as
  coupons. Nothing on production runs without `--production`.
- **Results go to Kiwi:** a test run with PASS / FAIL / BLOCKED / ERROR per case, the agent's notes and a
  failure screenshot. The summary in the chat links the screenshots of failed cases.
- **Web cases** take ~5 min and ~$1.50 each as an agent; several run in parallel.
- **Android cases** need a booted emulator with the app. A case with a validated Maestro flow runs that flow
  (free); otherwise an agent drives the app.

More detail: [src/workflows/testing/README.md](src/workflows/testing/README.md).

## Updating

Ask Claude: "Update rfc-agent". It runs:

```bash
claude plugin marketplace update rfc-agent-marketplace
claude plugin update rfc-agent@rfc-agent-marketplace
```

Installed from a zip? Get the new zip first and replace `~/rfc-agent` with it (the same unzip command as in
Step 2), then run the two commands above. Your settings live in `~/.config/rfc-agent/`, so replacing the folder
loses nothing.

Then restart Claude Code and run `/rfc-agent:setup` once. A new version can add settings, and setup fills them
in. On a testing machine it also re-syncs the testing extra and re-runs `npm ci` if the Playwright runner
changed.

## If something goes wrong

| You see | Fix |
|---|---|
| `Not logged in · Please run /login` | Sign in the `claude` command: in Terminal, run `claude`, then `/login` |
| `/rfc-agent:setup` or `/rfc-agent:rfc` isn't found | Restart Claude Code. Check `claude plugin list` shows `rfc-agent@rfc-agent-marketplace` enabled |
| Stuck fetching the PRD, or "Atlassian is not connected" | Reconnect Atlassian (claude.ai → Settings → Connectors), then check `claude mcp list` |
| `ATLASSIAN.SITE is not set` | Run `/rfc-agent:setup` and give the team's Atlassian site |
| `context layer unavailable` | Run `/rfc-agent:setup` to check the fleetlens URL and token, and that the server is reachable |
| A repo shows `no access` or `clone failed` | Check you can `git clone` that repo yourself, and fix your git credentials for its host |
| `Langfuse tracing is required but the Langfuse keys are missing` | Copy the keys from the vault into `~/.config/rfc-agent/config.json`, or run `/rfc-agent:setup` |
| Figma: nothing happens when you press ⌥⌘P | Run it once from **Plugins → Development → rfc-agent Figma snapshot**. If it isn't listed, run `/rfc-agent:setup`: it re-imports it |
| Figma: "import it once from its fixed folder" or "an OLD rfc-agent plugin window" | Do what the message says: import the manifest path it shows, or close the old plugin window (✕) and press ⌥⌘P again |

| `~/.tcms.conf is missing` / `testing.storage is 'json'` | Run `/rfc-agent:setup` and pick testing: it sets up Kiwi |
| `tcms-api is not installed` | `uv sync --project <plugin folder> --extra testing` |
| `Kiwi … is unreachable` | Check your Kiwi setup: the VPN is on, the Kiwi URL and username are right (`/rfc-agent:setup` re-asks them), and `~/.tcms.conf` has your login. Then retry. A 502/503 means Kiwi itself is down |
| `Playwright is not installed` / `Playwright MCP needs Chromium build …` | Run the command the message names (in the plugin's `tools/ui-runner`) |
| `/test-agent:…` isn't found | `claude plugin install test-agent@rfc-agent-marketplace`, then restart Claude Code |
| `no Android device is ready` / `no maestro-automation checkout` | Boot the emulator with the app installed; run `/rfc-agent:setup` and say yes to Android |

Still stuck? Run `/rfc-agent:doctor`. It checks the install, config, tools, connections and repos, and names
what's wrong.

Every setting in the config file: [CONFIG.md](CONFIG.md).
