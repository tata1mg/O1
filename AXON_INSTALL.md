# Install rfc-agent and test-agent for Axon

This puts two commands into Axon, the org's managed Pi, on a Mac:

- **`/rfc`** turns a PRD into a grounded tech RFC: `/rfc --prd <confluence-url>` runs the whole pipeline with its
  agents on Axon, shows progress above the editor, and asks each pause as a question in the session.
- **`/test`** writes QA test cases into Kiwi and runs the confirmed ones: backend, web (Playwright tests, or an
  agent in a browser) and Android. It runs on rfc-agent's code and settings, so both are installed together.

You don't need Claude Code. It takes about 20 minutes.

**This guide assumes Axon is already installed and signed in, and that you are running it inside Axon.** Not
there yet? Your team's pilot guide covers installing Axon and signing in. [INSTALL.md](INSTALL.md) sends an agent
here once it detects it is running in Axon.

**To install, open Axon and say one of:**

> Set up test-agent by following INSTALL.md

> Set up rfc-agent by following INSTALL.md

> Set up rfc-agent and test-agent by following INSTALL.md

(Give the path or the GitHub link to `INSTALL.md`.) Axon does the work. You only sign in to a few things, copy
keys from the team vault, type your Kiwi password into a file, and (for RFCs) import one plugin into Figma. Prefer
to do it by hand? Every step below is a command you can run yourself, in order.

What differs from Claude Code, the model mapping and the engine flags: [Beyond `/rfc` and `/test`](#beyond-rfc-and-test).

## What you need first

Everyone:

| What | Why | Where to get it |
|---|---|---|
| Access to the GitHub repo `tata1mg/O1` | Both commands are installed from it | Your team |
| **Node 22.19 or newer** | Axon's own Pi runtime needs it (Playwright later needs Node 18+, so one Node covers both) | Already there if Axon runs; otherwise nodejs.org |
| The **Langfuse keys** | Every run is traced, and a run without them stops before it starts | The team vault |

For RFCs:

| What | Why | Where to get it |
|---|---|---|
| An account on your team's **Confluence / Jira** site | PRDs and tickets are read from there | Your team |
| The **team values**: Atlassian site, fleetlens URL, product surfaces, service inventory | Shared settings, not secret. Easiest: a teammate's `config.json` | A teammate |
| The **fleetlens token** and its URL | Scope, Investigate and Review trace code across repos through it. Needs the VPN | The team vault |
| The **Figma desktop app**, signed in, with edit access to the PRD's Figma files | Only if PRDs link Figma designs | figma.com/downloads |

For testing:

| What | Why | Where to get it |
|---|---|---|
| A **Kiwi TCMS** login, and the team's Kiwi URL and product prefix | Test cases and results live there | Your team |
| **Node 18+** (22 recommended) | Web cases run through Playwright | nodejs.org |
| The VPN, if Kiwi or the test environments are internal | Runs reach them from your Mac | — |
| For Android only: `adb`, the Maestro CLI, Java, an emulator with the app, and access to the `maestro-automation` repo | Android cases run their Maestro flow, or an agent, on the emulator | Your team |

---

## Instructions for Axon

You are setting up rfc-agent, test-agent, or both, for the user. Follow the steps in order.

`RFC` is the folder `axon pi list` prints under `git:github.com/tata1mg/O1` (normally
`~/.axon/pi/git/github.com/tata1mg/O1`; if the user pointed you at this guide inside another checkout, it is that
checkout). `CFG` is `~/.config/rfc-agent/config.json`.

**How to write commands.** Axon's terminal policy refuses, or asks the user to approve, any command with shell
syntax. Measured in a headless session: `ls ~/.axon/pi/extensions` and `test -f … && echo ok` were refused, and the
same commands with an absolute path and no `&&` ran. So in every command below:

- replace `~` with the user's home folder written out (`/Users/<name>/…`), and write `RFC` and `CFG` out in full;
- one command per call: no `&&`, `;`, pipes, redirects, `$VAR`, `$(…)` or globs;
- to look at a folder or a file's existence, prefer your own `ls` and `find` tools over the shell.

A `python -c "…"` one-liner is fine as given. If a prompt still appears, tell the user it is Axon's terminal policy
and that approving it is safe for the commands in this guide.

**What for.** Take it from the user's request: "rfc-agent" alone → RFCs; "test-agent" alone → testing; both named
→ both. If it isn't clear, ask "What will you use it for?" with "Both (Recommended)", "RFCs from PRDs" and
"Testing only".

| Step | RFCs | Testing only |
|---|---|---|
| 1 Earlier installs | yes | yes |
| 2 Tools and the Python environment | yes | yes |
| 3 The config file | yes | yes |
| 4 Folders | `REPOS_ROOT` and `RUNS_ROOT` | `RUNS_ROOT` only (`REPOS_ROOT` too if they want Android) |
| 5 Langfuse | yes | yes |
| 6 Team values and fleetlens | yes | skip |
| 7 Atlassian | yes | skip |
| 8 Figma | if PRDs link designs | skip |
| 9 Testing: Kiwi and Playwright | skip | yes |
| 10 Android | skip | if they want it |
| 11 Final check and hand-over | yes | yes |

**Rules**

1. **Run every command yourself.** Every step starts with a check. If it passes, say so in one line and move on.
   Running this guide twice must change nothing the second time.
2. **Only the user can sign in, copy from the vault, type a password, or click in an app.** For those (marked
   **User:**), tell them exactly what to do and what success looks like, ask "Done?", then run the check again.
3. **Never take a secret through the chat.** No token, key or password in a message or an answer, and never
   print or read one back. For a secret, open the file for the user (`open -t <file>`), name the exact field,
   wait, then verify with a check that prints only a status. Do not read `CFG` or `~/.tcms.conf` with the `read`
   tool or `cat` once they hold secrets: use the checks below, which print a status and nothing else.
4. **Ask for every value that isn't secret** (folders, team values, yes/no), with the detected or recommended
   value first, marked "(Recommended)".
5. **Change `CFG` only with the commands given here**, or with a small Python script that loads the JSON, sets
   named keys and writes it back. Never rewrite the file, never drop keys, and never write config inside `RFC`
   (an update replaces that folder).
6. A step marked CRITICAL blocks. If one can't be fixed now, carry on with the rest and end **BLOCKED**, naming
   it. An OPTIONAL step may stay unfinished: say what is lost without it.
7. **Don't set up** Homebrew, Claude Code, or a Figma MCP server. None is needed.

### Step 1 — Earlier installs

There are two ways `/rfc` and `/test` get into Axon, and having both gives two copies of each command.

**Check:** `axon pi list` shows `git:github.com/tata1mg/O1` (with any `@<ref>` after it: say which ref it is, and
move on), and the folder `~/.axon/pi/extensions` holds neither `rfc-agent.js` nor `test-agent.js`. An empty or
missing folder passes.

- The package is missing → you are reading this guide from a link or a checkout, not from the installed package.
  Install it yourself, then tell the user to type `/reload` (or restart Axon) and say the install sentence again:
  ```bash
  axon pi install git:github.com/tata1mg/O1@main
  ```
  Pi clones the repo into `~/.axon/pi/git/github.com/tata1mg/O1`, which is also where the pipeline runs from
  (`RFC` above). It tracks `main`; use a release tag instead if the team announces one.
- `rfc-agent.js` or `test-agent.js` is there → that is a development install pointing at a checkout. Ask
  "Remove the development install and use the package? (Recommended)". On yes:
  ```bash
  uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -m rfc_agent.axon_agent uninstall
  ```
  It removes only those two files. If uv isn't installed yet, do Step 2 first and come back.
- The user asked for a clean reinstall → **User:** in a terminal, `axon pi remove git:github.com/tata1mg/O1`,
  then the install command above, then start again. The config file, the Kiwi login and the runs are kept.

### Step 2 — Tools and the Python environment — CRITICAL

**Check:** `uv --version`, `git --version` and `rg --version` each print a version.

If one is missing:

```bash
bash ~/.axon/pi/git/github.com/tata1mg/O1/scripts/install-deps.sh
```

It installs what is missing without Homebrew or a password: uv and ripgrep go into `~/.local/bin`. If git is
missing it opens macOS's "Install Command Line Tools" dialog. **User:** click Install, wait for it to finish; then
run the script again.

**Check:** `uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -V` prints 3.12.x, and, for testing or
both, this prints `testing extra: ok`:

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -c "import tcms_api; print('testing extra: ok')"
```

If either fails, build the environment with one of these, not both. Use the second for testing or both, and on
every later sync too, because a plain `uv sync` removes the testing extra again:

RFCs only:

```bash
uv sync --project ~/.axon/pi/git/github.com/tata1mg/O1
```

Testing, or both:

```bash
uv sync --project ~/.axon/pi/git/github.com/tata1mg/O1 --extra testing
```

If it fails with `invalid peer certificate: UnknownIssuer`, the VPN (Zscaler) is re-signing HTTPS traffic. Run the
sync again as `UV_NATIVE_TLS=1 uv sync …`, and tell the user to add `export UV_NATIVE_TLS=1` to `~/.zshrc`
themselves and restart Axon from a new terminal, so later runs have it too.

### Step 3 — The config file — CRITICAL

Everything the user supplies goes in `CFG`.

**Check:** the file `~/.config/rfc-agent/config.json` exists (look with your `ls` tool; the shell form
`test -f <full path>` prints nothing and answers with its exit code, 0 = there). If it is missing, ask "Do you have a teammate's rfc-agent
config.json to start from?" (only its team values are reusable; their keys and folders are not):

- No →
  ```bash
  mkdir -p ~/.config/rfc-agent
  ```
  ```bash
  cp ~/.axon/pi/git/github.com/tata1mg/O1/config.example.json ~/.config/rfc-agent/config.json
  ```
- Yes → ask for its path and copy it to `CFG` the same way. Then blank, with a Python edit (rule 5),
  `REPOS_ROOT`, `RUNS_ROOT`, `OBS.USER_ID`, the Langfuse keys and the fleetlens `Authorization` header: those
  were the teammate's.

Then, always:

```bash
chmod 600 ~/.config/rfc-agent/config.json
```

**Check:** this prints the path with no error:

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -c "import rfc_agent.config as c; c.get_config(); print('resolved:', c._LOCAL)"
```

### Step 4 — Folders — CRITICAL

**Check:** this prints two folders that exist, and `RUNS_ROOT` is not inside `REPOS_ROOT`:

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -c "from pathlib import Path; from rfc_agent.config import get_config as g; c=g(); [print(k, Path(str(c.get(k) or '-')).expanduser(), Path(str(c.get(k) or '-')).expanduser().is_dir()) for k in ('REPOS_ROOT','RUNS_ROOT')]"
```

- **`RUNS_ROOT`** is where runs and their results land. Ask, with `~/rfc-runs` (Recommended) first. Create it.
- **`REPOS_ROOT`** (RFCs, or testing with Android) is the folder holding the user's repo checkouts, e.g. `~/code`.
  Ask for it. Nothing needs to be cloned now: the pipeline clones the repos a PRD touches when a run needs them.
- **`GIT_REPOS_REMOTE`** (RFCs, or testing with Android) is the team's clone address, a team value with `{repo}`
  where the repo name goes. **Check** what the pipeline will use:
  ```bash
  uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -c "from pathlib import Path; from rfc_agent.config import get_config; from rfc_agent.scope.repos import configured_remote, remote_template; r=Path(str(get_config().get('REPOS_ROOT') or '.')).expanduser(); print('configured:', configured_remote()); print('used:', remote_template(r, configured_remote()))"
  ```
  `configured:` already set → move on. `None`: ask "What is your team's clone address?" (not a secret): in Bitbucket,
  open any team repo, click Clone, copy the SSH address and replace the repo name with `{repo}`, for example
  `git@bitbucket.org:<workspace>/{repo}.git`; a teammate can tell them. It must contain `{repo}`. If `used:`
  already shows the right address (taken from an existing checkout), offer to save it so it no longer depends on
  that checkout.

Write them with a Python edit (rule 5), for example:

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -c "import json, pathlib; p=pathlib.Path.home()/'.config/rfc-agent/config.json'; d=json.loads(p.read_text()); d['RUNS_ROOT']='~/rfc-runs'; d['GIT_REPOS_REMOTE']='<the clone address>'; p.write_text(json.dumps(d, indent=2))"
```

### Step 5 — Langfuse — CRITICAL

**Check:** it prints `langfuse=on keys=ok …` (exit 0). It never prints a key.

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -c "from rfc_agent import observe; raise SystemExit(observe.preflight())"
```

`keys=MISSING` → **User:** run `open -t ~/.config/rfc-agent/config.json` for them and ask them to paste
`LANGFUSE.PUBLIC_KEY` and `LANGFUSE.SECRET_KEY` from the team vault, and save. Then check again.

### Step 6 — Team values and fleetlens (RFCs) — CRITICAL for RFCs

These are shared settings. A config copied from a teammate already has them.

**Check:** the Atlassian site is set:

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -c "from rfc_agent.config import get_config as g; s=g().get('ATLASSIAN.SITE') or ''; print(s if s and 'your-company' not in s else 'MISSING')"
```

`MISSING` → ask for the team's Atlassian site (e.g. `https://<team>.atlassian.net`) and for the fleetlens URL, and
write `ATLASSIAN.SITE` and `MCP.mcpServers.fleetlens.url` with a Python edit (rule 5). `SERVICES_MAP` is optional:
without it the user names repos with `--repos`.

The fleetlens token is a secret. **User:** open `CFG` for them and ask them to set
`MCP.mcpServers.fleetlens.headers` to `{"Authorization": "Bearer <token from the vault>"}`. It is verified in
Step 11 (`/rfc doctor`, VPN on). Every field is described in [CONFIG.md](CONFIG.md).

If the user has also added fleetlens to Axon themselves (`axon pi mcp list` shows `fleetlens`), leave it: their
entry is used on Axon, with `direct` or `codemode` exposure, and `/rfc doctor` checks that one and says which.

### Step 7 — Atlassian (RFCs) — CRITICAL for Confluence and Jira PRDs

A local `--prd file.md` doesn't need it.

**Check:** `axon pi mcp list` shows `atlassian`. If not:

```bash
axon pi mcp add atlassian --url https://mcp.atlassian.com/v1/mcp
```

**User:** in a terminal, `axon pi mcp login atlassian`, and finish the consent page it opens in the browser. It is
verified in Step 11.

### Step 8 — Figma (RFCs) — OPTIONAL

Only if PRDs link designs. Designs are read through the Figma desktop app and a small plugin, which Figma loads
from a fixed folder.

**Check:** this prints `imported`:

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -c "from rfc_agent.scope import figma; missing, m, old = figma.ensure_plugin(); print('NOT imported' if missing else 'imported'); print('manifest:', m)"
```

`NOT imported` → **User:** in the Figma app, open any design file, then **Plugins → Development → Import plugin
from manifest…** and pick the `manifest:` path that was printed. **Plugins → Development** should then list
**rfc-agent Figma snapshot**. Check again.

### Step 9 — Testing: Kiwi and Playwright — CRITICAL for testing

**Check** (needs no feature or RFC run, prints no secret). Each row has `id`, `level`, `msg` and `fix`; a failing
row also has `kind`: `setup` (not done yet) or `error` (broken). The JSON's `summary` has `ready`, `to_set_up`,
`errors`, `notes` and `next`:

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -m workflows.testing.preflight testing --engine axon --json
```

Go through the rows that aren't `ok`:

- **`kiwi` with "testing.storage is …"** — Kiwi isn't set up here. Ask for the Kiwi web URL, the product prefix
  (e.g. `Sandbox / `) and the Kiwi user the agent records as (team values, not secrets), then:
  ```bash
  uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -m workflows.testing.preflight setup-kiwi --kiwi-web <kiwi url> --product-prefix "<prefix>" --kiwi-user <user>
  ```
- **`kiwi_conf` missing** — the Kiwi login is a password, so the user creates the file. Run these, one at a time:
  ```bash
  touch ~/.tcms.conf
  ```
  ```bash
  chmod 600 ~/.tcms.conf
  ```
  ```bash
  open -t ~/.tcms.conf
  ```
  **User:** type these lines with your own values (the url is the Kiwi web URL plus `/xml-rpc/`) and save:
  ```
  [tcms]
  url = https://<kiwi-host>/xml-rpc/
  username = <user>
  password = <password>
  ```
- **`kiwi_client` "tcms-api is not installed"** — the second `uv sync` of Step 2 (`--extra testing`).
- **`kiwi` unreachable** — the VPN is off, or Kiwi is down. Say which; it stays BLOCKED until it answers.
- **`playwright` or `agent_browser`** — needs Node 18+ (`node -v`; if missing, **User:** install Node 22 from
  nodejs.org). Say first that this downloads Chromium (about 290 MB), then run, one at a time:
  ```bash
  npm --prefix ~/.axon/pi/git/github.com/tata1mg/O1/tools/ui-runner ci
  ```
  ```bash
  npm --prefix ~/.axon/pi/git/github.com/tata1mg/O1/tools/ui-runner exec playwright install chromium
  ```
  That one install serves both the `playwright` and `agent_browser` rows.

Run the check again until every row is `ok`, or only notes are left. After each re-check, tell the user in one line what the `summary` says: what is done (`ready`), what is still
to set up (`to_set_up`, shown ⏳: the normal state mid-setup, not a failure), any `errors` (shown ❌: set up but
broken now), and the `next` step. Never repeat "not ready" on its own. `notes` (Android not set up, a browser
warning) are not problems.

**Web logins — don't create them here.** Runs against a logged-in page take a recorded session (`--ui-auth <file>`)
or a test-account file, made per environment the first time `/test` needs one; `/test` walks through it. Only say
that they exist.

### Step 10 — Android test runs — OPTIONAL

`/test execute android` runs CONFIRMED cases on the Android app: each case's validated Maestro flow, else an agent
on an emulator. The flows live in another team's repo, **maestro-automation**, which needs a git clone on this
machine. Web and backend runs never need it.

**Check:** `configured` and `checkout_ok` are true, and so are `adb` and `maestro`. (`suggested_path` is only
where `--clone` would put a new checkout; ignore it when one is configured.) The check does not look for Java or
a booted emulator: `/test execute android` says so itself when one is missing.

```bash
uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -m workflows.testing.preflight setup-android --check --json
```

Otherwise ask first: "Do you want to run test cases on Android too? It needs the maestro-automation repo on this
machine."

- "Yes, clone it (Recommended)" → `… preflight setup-android --clone --json`. A failure names the cause: no SSH
  access to the repo → the user asks a teammate for access, then re-run; no clone address → set
  `GIT_REPOS_REMOTE` (Step 4) or pass `--url`.
- "I already have it" → ask for the folder, then `… preflight setup-android --path <folder> --json`.
- "No" → skip. Say that web and backend runs are unaffected.

Never install `adb`, Maestro or Java, and never boot an emulator: say what the check found missing and leave it to
the user.

### Step 11 — Final check and hand-over

**Check:**

- RFCs: `uv run --project ~/.axon/pi/git/github.com/tata1mg/O1 python -m rfc_agent.axon_agent doctor` ends
  `axon: READY`. It checks the Axon sign-in, the managed Pi version, the gateway models, fleetlens (VPN on) and the
  Atlassian sign-in; each problem is one `FAIL` line with its fix. (`/rfc doctor` in the session runs the same.)
- Testing: the Step 9 check, without `--json`, ends with a line starting `TESTING READY`. Anything left in
  `to_set_up` or `errors` at the end is what BLOCKED names.

End with **READY**, or **BLOCKED** plus the one thing to fix. Then tell the user, in this order:

1. Type `/reload` (or restart Axon) so `/rfc` and `/test` are loaded from the package.
2. Their first run: for RFCs, `/rfc --prd <confluence-page-url>` ("Your first RFC" below). For testing, "Your
   first test run" below.

---

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
- **Rate it.** When the run ends, Axon offers once to rate it (`Rate it now` / `Later`). Any time after, run
  `/rfc feedback` (the run you are following, else the newest under `RUNS_ROOT`) or
  `/rfc feedback --run-dir <run folder>`. The questions depend on how the run ended: a finished RFC gets a thumb
  (plain 👍 is one tap, 👍 with notes and 👎 add four areas and two time questions), a stopped run why you stopped it,
  a failed run whether the recorded cause is right; the last two end with "Would you run it again?". Answers land on
  that run's Langfuse trace, the same as `/rfc-agent:feedback` in Claude Code. Rating a run twice shows your earlier
  answers and a preview before it replaces them. `up` or `down` after the command (`/rfc feedback up`) answers the
  thumb. Area questions are lists: pick rows to tick or untick, then `✔ Done`.

If it stops, fix what it reports, then continue it with `/rfc --run-dir <run folder>` (the folder is in the
closing message). Finished stages are kept. `/rfc --prd` again starts a new run in a new folder.

## Your first test run

`/test` runs the same `testing` workflow as Claude Code's test-agent, with its agents on Axon.

| On Axon | Command |
|---|---|
| Run the CONFIRMED cases of a Kiwi plan with an agent: in a browser, and on an emulator | `/test execute ui-agent --feature <name> --kiwi-plan <plan id>` |
| Android only | `/test execute android --feature <name> --kiwi-plan <plan id>` |
| Write cases from a finished RFC run into Kiwi, then run them once you confirm them | `/test --rfc-run-dir <run folder>` |
| Run CONFIRMED web (Playwright tests) and backend cases again | `/test execute [web\|backend\|all]` |
| Score a suite | `/test evaluate --run-dir <run folder>` |
| Follow or check a run | `/test status`, `/test attach <run folder>`, `/test doctor` |

- **It asks before anything risky:** production, a logged-in session, a test account, or actions such as coupons.
  Nothing on production runs without `--production`.
- **Results go to Kiwi:** a test run with PASS / FAIL / BLOCKED / ERROR per case, the agent's notes and a failure
  screenshot. The summary in the session gives the path of each failed case's screenshot.
- **An agent case is a model session on every run**, and Axon keeps a backup of every agent session, page contents
  included. An Android case with a validated Maestro flow runs that flow and costs nothing.
- **Flags only.** Plain words ("cart-sanity on stag, cases 173,172, show the browser") are understood by Claude
  Code's `/test-agent:execute`, not by `/test`. `/test help` lists the flags.
- **Not on Axon:** `/test-agent:convert-flows`, and code lookup for the browser agent (`testing.agent_code_context`).

More detail: [src/workflows/testing/README.md](src/workflows/testing/README.md).

## Beyond `/rfc` and `/test`

### Choosing the engine from a terminal or Claude Code

Axon is opt-in, per run or per stage. Claude Code stays the default.

```bash
uv run rfc --prd <confluence url | page id | file.md> --engine axon
```

```bash
uv run rfc --run-dir <run> --from review --engine axon
```

```bash
uv run rfc --prd <…> --stage-engine review=axon
```

The first runs everything on Axon. The second redoes one stage onward on Axon. The third keeps everything on
Claude except Review. From Claude Code, the plugin commands take the same flags, for example
`/rfc-agent:rfc --prd … --engine axon`. Every stage CLI accepts them too
(`python -m rfc_agent.review --run-dir … --engine axon`). To make Axon the default, set it in
`~/.config/rfc-agent/config.json`:

```json
{"AGENT": {"ENGINE": "axon", "STAGE_ENGINES": {}, "AXON": {"MODELS": {}}}}
```

Precedence: `--stage-engine` > `--engine` > `AGENT.STAGE_ENGINES` > `AGENT.ENGINE` > `claude-code`.
`logs/run.json` records which engine each stage used.

For development, install the commands from your own checkout instead of the package
(`uv run python -m rfc_agent.axon_agent install`, then `/reload` after edits). Use one or the other: both
register `/rfc` and `/test`. `… axon_agent uninstall` drops the checkout stubs.

### Models

Each stage names a Claude model; on Axon it is mapped to a gateway model by tier. The gateway serves Opus 5.5
and Sonnet 5.5, so those stages run on Claude. It has no Haiku:

| Stage model | Used by | Gateway model |
|---|---|---|
| `claude-opus-5-5[1m]` | Investigate lead, Draft, Review, Revise | `anthropic/claude-opus-5.5` |
| `claude-opus-5-5` | Investigate UI reader, Scope discovery | `anthropic/claude-opus-5.5` |
| `claude-sonnet-5-5` | tracers, Summarize | `anthropic/claude-sonnet-5.5` |
| `claude-haiku-4-5…` | Scope's Atlassian fetch | `gemini-3.8-flash` |

Print the mapping with `uv run python -m rfc_agent.axon_agent models`. Override by tier or exact id in config:

```json
{"AGENT": {"AXON": {"MODELS": {"opus[1m]": "openai/gpt-6.1-terra", "haiku": "deepseek/deepseek-v4.1-flash"}}}}
```

Effort maps to Pi's `--thinking` level of the same name.

### What is the same, and what isn't

The same on both engines: one prompt per agent on stdin; built-in tools limited to what the stage allows (no
shell, no web); fleetlens read-only tools; writes confined to each agent's own files; the write-time citation
check and the "wrap up" nudge at the call budget; resume across steps; and logs in the Claude stream format
(`logs/<stage>.<label>.<step>.stream.jsonl`). Axon adds the raw Pi stream (`.pi.jsonl`) and Pi sessions under
`logs/pi-sessions/`.

Different on Axon:

- **Scope fetches Confluence and Jira through Axon's own Atlassian sign-in** (Step 7). A Scope that runs on
  Claude (for example `--stage-engine review=axon`) still uses Claude's connector.
- **A run started on Claude can't resume its Claude sessions on Axon.** The agent starts cold, as when a Claude
  session has expired.
- **Early (threshold) compaction is off for pipeline agents**, because it summarises evidence away. Set
  `AGENT.AXON.THRESHOLD_COMPACTION: true` to allow it.
- **Axon backs up each agent session to DeputyDev**, as it does for interactive use.
- **A lost AI Gateway connection doesn't restart the agent.** Pi retries the turn itself up to 3 times. If those
  run out, the agent is resumed in its own Pi session up to `AGENT.AXON.RECONNECTS` times (default 2; the testing
  workflow's flag is `axon.reconnects`). Each resume writes its own log, and cost and time include every attempt.
- **Quality and cost differ.** The prompts were tuned on Claude. Compare a few runs before relying on it.

Design notes and the reasoning behind each choice: [decisions.md](decisions.md).

## Updating

```bash
axon pi update --extensions
```

On a branch ref this pulls the latest commit. A tag stays where it is. Then `/reload` in any open Axon session,
and run `uv sync --project ~/.axon/pi/git/github.com/tata1mg/O1` in case dependencies changed (`--extra testing`
too if you run `/test`; and `npm ci` in that folder's `tools/ui-runner` if its `package-lock.json` changed). To move to a new tag, remove the package and
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
| `invalid peer certificate: UnknownIssuer` | The VPN re-signs HTTPS: `export UV_NATIVE_TLS=1` (Step 2), then restart Axon from that terminal |
| `rfc: … uv` not found | Axon didn't get your shell's PATH. Set `RFC_AGENT_UV` to the output of `which uv`, then restart Axon |
| `could not list gateway models` | Check `axon auth status` and the VPN, then `axon doctor` |
| `Axon's Atlassian MCP server … is not signed in` | `axon pi mcp login atlassian` |
| `context layer unavailable` / fleetlens FAIL | VPN on, and check the fleetlens URL and token in the config file |
| `Langfuse observability is ON but the Langfuse keys are missing` | Paste the keys from the vault into the config file |
| A repo shows `no access` or `clone failed` | Check you can `git clone` that repo yourself, and fix your git credentials for its host |
| `/test`: `there is no Kiwi login` / `testing.storage is 'json'` | Step 9 above, or ask Axon to "set up test-agent" again |
| `/test`: `tcms-api is not installed` | `uv sync --project ~/.axon/pi/git/github.com/tata1mg/O1 --extra testing` |
| `execute_ui_agent needs an engine with MCP` | The run was started with `--engine pi`. Drop it: `/test` uses Axon by default |
| `no Android device is ready` / `no maestro-automation checkout` | Boot the emulator with the app installed; Step 10 sets up the checkout |
| Terminal approval prompts when you ask Axon other things | That's Axon's org terminal policy, for bash commands. `/terminal-permissions` shows what it allows |

## Uninstall

```bash
axon pi remove git:github.com/tata1mg/O1
```

This deletes the clone in `~/.axon/pi/git`. Your config file and runs stay where they are.
