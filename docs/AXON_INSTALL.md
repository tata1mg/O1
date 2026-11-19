# Install rfc-agent for Axon

This puts an `/rfc` command into Axon, the org's managed Pi. `/rfc --prd <confluence-url>` turns a PRD into a
grounded tech RFC: it runs the whole pipeline with its agents on Axon, shows progress above the editor, and asks
each pause as a question in the session. The same install adds `/test`, which writes QA test cases into Kiwi
and runs the confirmed web and backend ones ([Testing on Axon](#testing-on-axon)). You don't need Claude Code.
It runs on a Mac and takes about 20 minutes to set up.

Only testing? Do steps 1–4, then [Testing on Axon](#testing-on-axon), which has its own check. Skip 5–7
(Atlassian, Figma and `/rfc doctor` are for RFCs), and in step 4 only `RUNS_ROOT` and the Langfuse keys are
needed.

How the pipeline works, and what differs from Claude Code: [AXON.md](AXON.md).

## What you need first

| What | Why | Where to get it |
|---|---|---|
| Access to the GitHub repo `tata1mg/O1` | `/rfc` is installed from it | Your team |
| An account on your team's **Confluence / Jira** site | PRDs and tickets are read from there | Your team |
| The **team values**: Atlassian site, fleetlens URL, product surfaces, service inventory | Shared settings, not secret. Easiest: a teammate's `config.json` | A teammate |
| The **fleetlens token** and its URL | Scope, Investigate and Review trace code across repos through it. Needs the VPN | The team vault |
| The **Langfuse keys** | Every run is traced, and a run without them stops before it starts | The team vault |
| The **Figma desktop app**, signed in, with edit access to the PRD's Figma files | Only if PRDs link Figma designs | figma.com/downloads |

## 1. Install Axon and sign in

Skip this if `axon` already opens a session for you.

```bash
curl --proto '=https' --tlsv1.2 -fsSL https://deputydev.ai/axon/install.sh | sh
```

Open a new terminal, then:

```bash
axon auth login --browser
```

See the [Axon user guide](https://1mgtech.atlassian.net/wiki/spaces/DD/pages/5666996423) if this fails.

## 2. Install `/rfc`

```bash
axon pi install git:github.com/tata1mg/O1@feat/axon-engine
```

Pi clones the repo into `~/.axon/pi/git/github.com/tata1mg/O1`. That folder is also where the pipeline runs
from, so this guide calls it `RFC`:

```bash
RFC=~/.axon/pi/git/github.com/tata1mg/O1
```

Use the ref your team announces. Until there's a release tag, it's the `feat/axon-engine` branch.

## 3. Tools: uv, git, ripgrep

```bash
bash "$RFC/scripts/install-deps.sh"
```

It installs whatever is missing, without Homebrew or a password: uv and ripgrep go into `~/.local/bin`. If git is
missing, it opens macOS's "Install Command Line Tools" dialog. Click Install, then run the script again.

Then build the Python environment once:

```bash
uv sync --project "$RFC"
```

**On the VPN (Zscaler):** if this fails with `invalid peer certificate: UnknownIssuer`, the VPN is re-signing
HTTPS traffic. Make uv trust the macOS keychain, now and in every future terminal, then rerun the sync:

```bash
echo 'export UV_NATIVE_TLS=1' >> ~/.zshrc && export UV_NATIVE_TLS=1
```

## 4. The config file

Everything you supply goes in `~/.config/rfc-agent/config.json`. Start from a teammate's file if you have one,
otherwise from the example:

```bash
mkdir -p ~/.config/rfc-agent && cp "$RFC/config.example.json" ~/.config/rfc-agent/config.json && chmod 600 ~/.config/rfc-agent/config.json
```

Open it (`open -t ~/.config/rfc-agent/config.json`) and fill in:

| Field | Value |
|---|---|
| `REPOS_ROOT` | The folder holding your repo checkouts, e.g. `~/code`. Keep at least one team repo checked out there: the pipeline works out clone URLs from the first one, alphabetically |
| `RUNS_ROOT` | Where runs and RFCs land, e.g. `~/rfc-runs`. Not inside `REPOS_ROOT` |
| `ATLASSIAN.SITE` | The team's Atlassian site, e.g. `https://<team>.atlassian.net` |
| `MCP.mcpServers.fleetlens.url` | The fleetlens URL |
| `MCP.mcpServers.fleetlens.headers` | `{"Authorization": "Bearer <token from the vault>"}` |
| `LANGFUSE.PUBLIC_KEY`, `LANGFUSE.SECRET_KEY` | From the vault. Required: every run is traced |
| `SERVICES_MAP` | Team value. Optional: without them, name repos with `--repos` |

A teammate's file carries their personal values too. Replace `REPOS_ROOT`, `RUNS_ROOT`, the keys, and
`OBS.USER_ID` with your own. Every field is described in [SETUP.md](../SETUP.md).

Check that it loads, and that the Langfuse keys are right:

```bash
uv run --project "$RFC" python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"
```

It prints `langfuse=on keys=ok …`. `keys=MISSING` means the keys aren't in the file yet.

## 5. Connect Axon to Atlassian

Needed for Confluence and Jira PRDs. A local `--prd file.md` doesn't need it.

```bash
axon pi mcp add atlassian --url https://mcp.atlassian.com/v1/mcp
```

```bash
axon pi mcp login atlassian
```

The second command opens Atlassian's consent page in the browser.

## 6. Figma (only if PRDs link designs)

Designs are read through the Figma desktop app and a small plugin, which Figma loads from a fixed folder:

```bash
uv run --project "$RFC" python -c "from rfc_agent.scope import figma; missing, m, old = figma.ensure_plugin(); print('NOT imported' if missing else 'imported'); print('manifest:', m)"
```

If it prints `NOT imported`: in the Figma app, open any design file, then **Plugins → Development → Import
plugin from manifest…** and pick the `manifest:` path it printed. **Plugins → Development** should then list
**rfc-agent Figma snapshot**.

## 7. Check

Start Axon and run:

```
/rfc doctor
```

It prints `axon: READY`, or one `FAIL` line per problem with its fix. It checks the Axon sign-in, the managed Pi
version, the gateway models, fleetlens (VPN on) and the Atlassian sign-in.

## Your first RFC

In Axon:

```
/rfc --prd <confluence-page-url>
```

Add `--jira <KEY>` if there's a ticket. Every flag the `rfc` command takes works here (`--repos`, `--no-figma`,
`--ask`, `--ask-review`, `--yes`, …).

- **It runs for 30–60 minutes in the background.** Progress shows above the editor: stage, milestones, agents
  started.
- **It pauses to ask you questions.** First which repos, pages and Figma sections are in scope, later a few
  questions about the requirements. They open as questions in the session. To answer a list, pick rows to tick
  or untick them, then `✔ Done`. Nothing moves until you answer.
- **For each Figma link,** it opens the design in Figma and asks you to press **⌥⌘P** (run last plugin). The
  first time, run it from **Plugins → Development → rfc-agent Figma snapshot** instead.
- **Ask about the run any time** ("status?", "what is it doing?"). Axon answers with the `rfc_status` tool and
  Pi's read-only `read`/`grep`/`find`/`ls`, so these questions don't trigger terminal approval prompts.
- **When it's done,** the time, cost and RFC paths are posted into the session. Read `out/rfc.readable.md`
  first. `out/rfc.md` is the full RFC with every code reference.
- **It keeps running if you quit Axon.** `/rfc attach <run folder>` follows it again and asks any pause still
  waiting.

If it stops, fix what it reports, then continue it with `/rfc --run-dir <run folder>` (the folder is in the
closing message). Finished stages are kept. `/rfc --prd` again starts a new run in a new folder.

## Testing on Axon

`/test` runs the same `testing` workflow as Claude Code's test-agent, with its agents on Axon.

| On Axon | Command |
|---|---|
| Write cases from a finished RFC run into Kiwi, then run them once you confirm them | `/test --rfc-run-dir <run folder>` |
| Run CONFIRMED web (Playwright tests) and backend cases again | `/test execute [web\|backend\|all]` |
| Score a suite | `/test evaluate --run-dir <run folder>` |
| Follow or check a run | `/test status`, `/test attach <run folder>`, `/test doctor` |

**Not on Axon yet:** the agent kinds of Claude Code's `/test-agent:execute`: `ui-agent` (an agent drives a
browser through each web case, also straight from a Kiwi plan), `android` (an emulator), and
`/test-agent:convert-flows`. Their safety guards (which pages the agent may open and which actions it may
click) are Claude Code hooks, and the stages refuse to start on Axon. Run those from Claude Code
([INSTALL.md](../INSTALL.md)): it uses this same clone, config and Kiwi login.

### Set up testing

Once, after steps 1–4:

1. The testing extra (Kiwi's client and the report libraries). Use it on every later sync too: a plain
   `uv sync` removes it again.
   ```bash
   uv sync --project "$RFC" --extra testing
   ```
2. The team's Kiwi settings (not secret: the Kiwi web URL, the product prefix such as `Sandbox / `, and the
   Kiwi user the agent records as):
   ```bash
   uv run --project "$RFC" python -m workflows.testing.preflight setup-kiwi --kiwi-web <kiwi url> --product-prefix "<prefix>" --kiwi-user <user>
   ```
3. Your Kiwi login. It holds your password, so create the file in an editor, not with a command:
   ```bash
   touch ~/.tcms.conf && chmod 600 ~/.tcms.conf && open -t ~/.tcms.conf
   ```
   Type these lines with your values (the url is the Kiwi web URL plus `/xml-rpc/`) and save:
   ```
   [tcms]
   url = https://<kiwi-host>/xml-rpc/
   username = <user>
   password = <password>
   ```
4. Playwright, for web cases. Needs Node 18+ (`node -v`); it downloads a browser:
   ```bash
   cd "$RFC/tools/ui-runner" && npm ci && npx playwright install chromium
   ```
5. Check. It prints one line per item and ends `TESTING READY`:
   ```bash
   uv run --project "$RFC" python -m workflows.testing.preflight testing --engine axon
   ```
   `ui-agent` and `Android` lines are notes for Claude Code; on Axon they don't block anything.

Web runs against a logged-in page ask for a login file the first time; `/test` explains it then.

## Updating

```bash
axon pi update --extensions
```

On a branch ref this pulls the latest commit. A tag stays where it is. Then `/reload` in any open Axon session,
and run `uv sync --project "$RFC"` in case dependencies changed (`--extra testing` too if you run `/test`; and
`npm ci` in `$RFC/tools/ui-runner` if its `package-lock.json` changed). To move to a new tag, remove the package and
install it again at the new ref:

```bash
axon pi remove git:github.com/tata1mg/O1
```

## If something goes wrong

| You see | Fix |
|---|---|
| `/rfc` isn't a command | Run `/reload`, or restart Axon. Check `axon pi list` shows `git:github.com/tata1mg/O1` |
| An error that `/rfc` already exists, or two `/rfc` commands | You also have the development install (`python -m rfc_agent.axon_agent install`). Remove one: `uv run python -m rfc_agent.axon_agent uninstall` in that checkout |
| `Your Axon session is invalid` / `Axon login is required` | `axon auth login --browser` |
| `invalid peer certificate: UnknownIssuer` | The VPN re-signs HTTPS: `export UV_NATIVE_TLS=1` (step 3), then restart Axon from that terminal |
| `rfc: … uv` not found | Axon didn't get your shell's PATH. Set `RFC_AGENT_UV` to the output of `which uv`, then restart Axon |
| `could not list gateway models` | Check `axon auth status` and the VPN, then `axon doctor` |
| `Axon's Atlassian MCP server … is not signed in` | `axon pi mcp login atlassian` |
| `context layer unavailable` / fleetlens FAIL | VPN on, and check the fleetlens URL and token in the config file |
| `Langfuse observability is ON but the Langfuse keys are missing` | Paste the keys from the vault into the config file |
| A repo shows `no access` or `clone failed` | Check you can `git clone` that repo yourself, and fix your git credentials for its host |
| `/test`: `there is no Kiwi login` / `testing.storage is 'json'` | [Set up testing](#set-up-testing), steps 2–3 |
| `/test`: `tcms-api is not installed` | `uv sync --project "$RFC" --extra testing` |
| `execute_ui_agent runs on the claude-code engine only` | ui-agent and Android runs need Claude Code ([Testing on Axon](#testing-on-axon)) |
| Terminal approval prompts when you ask Axon other things | That's Axon's org terminal policy, for bash commands. `/terminal-permissions` shows what it allows |

## Uninstall

```bash
axon pi remove git:github.com/tata1mg/O1
```

This deletes the clone in `~/.axon/pi/git`. Your config file and runs stay where they are.
