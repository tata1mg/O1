# Install rfc-agent

rfc-agent is a Claude Code plugin that turns a PRD into a tech RFC. It runs on a Mac.

**To install it, open Claude Code and say:**

> Set up rfc-agent by following INSTALL.md

Claude does the work. You only sign in to a few things, copy two keys from the team vault, and import one
plugin into Figma. It takes about
15 minutes.

## What you need first

| What | Why | Where to get it |
|---|---|---|
| Claude Code, already installed | The plugin runs inside it | — |
| Access to the GitHub repo `tata1mg/O1` | It's where the plugin is installed from | Your team |
| An account on your team's **Confluence / Jira** site | PRDs and tickets are read from there | Your team |
| The **team values**: Atlassian site, fleetlens URL, product surfaces, service inventory | Settings shared by the whole team. Not secret | A teammate |
| A running **fleetlens** server (the context layer), its URL and token | Scope, Investigate and Review use it to find and trace code across repos. fleetlens is open source: [github.com/tata1mg/fleetlens](https://github.com/tata1mg/fleetlens) | Your team runs it. The token is in the team vault |
| The **Langfuse keys** | Secrets for run tracing | The team vault |
| The **Figma desktop app**, signed in, with **edit access** to the PRD's Figma files | The pipeline reads designs through it and a small plugin. Plugins work on the free tier, but you need edit access to each file, so for now use a dev account. Only needed if the PRD links Figma designs | figma.com/downloads |

---

## Instructions for Claude

You are setting up rfc-agent for the user. Follow the steps in order.

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

**Check:** `claude plugin list --json` has `rfc-agent@rfc-agent-marketplace` with `"enabled": true`. If so, skip to
"Find the plugin folder".

Install from GitHub:

```bash
claude plugin marketplace add tata1mg/O1
claude plugin install rfc-agent@rfc-agent-marketplace
```

This tracks the repo's default branch, which is what the team installs from.

**Find the plugin folder.** In `claude plugin list --json`, the `rfc-agent@rfc-agent-marketplace` entry gives:

- `installPath` — call it `PLUGIN`. The plugin's commands run from here.
- `readFromFolder`, if present — call it `SOURCE`. It stays the same across updates.

### Step 3 — Run the plugin's setup

This session doesn't see the plugin's `/rfc-agent:*` commands until Claude Code restarts. So don't ask the
user to restart yet. Instead, read `PLUGIN/commands/setup.md` and follow it end to end, here and now. Wherever
it says `$PROJ` or `${CLAUDE_PLUGIN_ROOT}`, use `PLUGIN`.

It covers, in order: the tools (it installs uv, ripgrep and git if missing), the config file, the repos and
runs folders, a first repo checkout, Atlassian, fleetlens, Langfuse, and Figma (the desktop app plus the
rfc-agent snapshot plugin, both required). For the fleetlens token and the Langfuse keys, it asks the user to
copy them from the team vault and paste them into the config file.

It ends with **READY**, or **BLOCKED** plus the one thing to fix. If BLOCKED, help the user fix that, then
re-run only the failed step.

### Step 4 — Hand over

Tell the user, in this order:

1. **Restart Claude Code** (quit it fully, then open it again). Then the `/rfc-agent:*` commands appear.
2. **Their first run:** `/rfc-agent:rfc --prd <confluence-page-url>`. Point them to "Your first RFC" below.

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

## Updating

Ask Claude: "Update rfc-agent". It runs:

```bash
claude plugin marketplace update rfc-agent-marketplace
claude plugin update rfc-agent@rfc-agent-marketplace
```

Then restart Claude Code and run `/rfc-agent:setup` once. A new version can add settings, and setup fills them
in.

## If something goes wrong

| You see | Fix |
|---|---|
| `Not logged in · Please run /login` | Sign in the `claude` command: in Terminal, run `claude`, then `/login` |
| `/rfc-agent:setup` or `/rfc-agent:rfc` isn't found | Restart Claude Code. Check `claude plugin list` shows `rfc-agent@rfc-agent-marketplace` enabled |
| Stuck fetching the PRD, or "Atlassian is not connected" | Reconnect Atlassian (claude.ai → Settings → Connectors), then check `claude mcp list` |
| `ATLASSIAN.SITE is not set` | Run `/rfc-agent:setup` and give the team's Atlassian site |
| `context layer unavailable` | Run `/rfc-agent:setup` to check the fleetlens URL and token, and that the server is reachable |
| A repo shows `no access` or `clone failed` | Check you can `git clone` that repo yourself, and fix your git credentials for its host |
| `Langfuse observability is ON but the Langfuse keys are missing` | Copy the keys from the vault into `~/.config/rfc-agent/config.json`, or add `--langfuse false` to the run command |
| Figma: nothing happens when you press ⌥⌘P | Run it once from **Plugins → Development → rfc-agent Figma snapshot**. If it isn't listed, run `/rfc-agent:setup`: it re-imports it |
| Figma: "import it once from its fixed folder" or "an OLD rfc-agent plugin window" | Do what the message says: import the manifest path it shows, or close the old plugin window (✕) and press ⌥⌘P again |

Still stuck? Run `/rfc-agent:doctor`. It checks the install, config, tools, connections and repos, and names
what's wrong.

Every setting in the config file: [SETUP.md](SETUP.md).
