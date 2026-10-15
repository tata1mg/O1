# Scope

Scope collects every input for an RFC run and settles **every human decision up front**, before any
model does real work. After its one confirm screen, the rest of the pipeline never asks a person anything.

It fetches the PRD, finds the repos the PRD touches, lets you confirm them in one step, and snapshots the code
so every later stage reads a stable, read-only copy.

---

## Contents

1. [Running it](#running-it)
2. [What it does, step by step](#what-it-does-step-by-step)
3. [Decisions and what each one considers](#decisions-and-what-each-one-considers)
4. [Edge cases](#edge-cases)
5. [Output](#output)
6. [Configuration](#configuration)
7. [Models and cost](#models-and-cost)
8. [Safety](#safety)
9. [Troubleshooting](#troubleshooting)
10. [Known limitations](#known-limitations)
11. [Code map](#code-map)

---

## Running it

### Before the first run

| Need | Why | Check |
|---|---|---|
| Network access to your context layer and Bitbucket | Repo discovery and snapshots need them | Scope says "context layer unavailable" otherwise |
| Atlassian connected in Claude Code: the claude.ai Atlassian connector **or** a user-scope `atlassian` server | The PRD, child pages and Jira are fetched through it | `claude mcp list` shows it `✔ Connected`; otherwise `/mcp` → authenticate |
| Figma desktop app, logged in, with access to the file | Only for PRDs that link a Figma design | — |
| The Figma plugin imported **from the repo** (once) | Figma runs whatever folder the plugin was imported from | Figma → Plugins → Development → Import plugin from manifest… → `<repo>/tools/figma_snapshot_plugin/manifest.json` |

### From Claude Code (selection questions)

```
/rfc-agent:scope --prd <confluence url | page id | file> [--jira KEY] [--repos a,b] [--no-figma] [--fresh-clone]
```

Claude runs Scope in the background, relays the Figma keypress prompt, shows the proposal as clickable
selection questions, writes your answers back, and reports the summary. Defined in
[commands/scope.md](../commands/scope.md).

### From a terminal (typed selection)

```bash
uv run python -m rfc_agent.scope --prd "<confluence url>"
```

| Option | Effect |
|---|---|
| `--prd` | Required. Confluence URL, bare page id, or a local markdown file |
| `--jira KEY` | Also fetch this Jira issue (key or URL) |
| `--repos a,b` | Repos you already know are in scope. Always proposed, listed first |
| `--repos-root` | Where your checkouts live (default: config `REPOS_ROOT`) |
| `--run-dir` / `--out` | Exact run folder / parent for the auto-named one (default: config `RUNS_ROOT`) |
| `--yes` | No confirm screen: accept the proposal as shown |
| `--confirm-file` | Confirm through files instead of the terminal (what `/rfc-agent:scope` uses). Automatic when there is no terminal |
| `--no-figma` | Ignore Figma links |
| `--reuse-figma ask\|always\|never` | What to do when a previous export of the same link exists (default `ask`) |
| `--figma-wait N` | Seconds to wait for the plugin's screen list, per link (default 600) |
| `--fresh-clone` | Never touch your checkouts: every repo comes from the repo cache |
| `--model` | Model that makes the Atlassian connector calls (default `claude-haiku-4-5`) |

Other subcommands:

```bash
uv run python -m rfc_agent.scope run-dir --prd <...> [--jira KEY]   # print where a run would land
uv run python -m rfc_agent.scope cleanup <run_dir>                   # remove the run's repo snapshots
```

Exit code 0 on success, 1 on a stop (the reason is printed as `STOP: …`).

---

## What it does, step by step

```
 ┌─ 1. context layer health check ──────────┐  (in parallel)
 └─ 2. Atlassian: PRD + child-page list + Jira ┘
 3. read the PRD: Figma links, linked pages, Jira keys, embedded images
 ┌─ 4a. Figma: open each link, plugin sends its SCREEN LIST ┐  (in parallel)
 └─ 4b. repo candidates: context layer + PRD mentions + --repos, then probe each remote ┘
 5. ONE confirm screen: repos (+branch), pages, Jira, Figma sections
 ┌─ 6a. repo snapshots          ┐
 ├─ 6b. ticked pages / Jira     ┤  (in parallel)
 └─ 6c. Figma exports ONLY the picked sections ┘
 7. write scope.json, findings/scope_approved.json, raw/figma_index.md, raw/links.md
```

### 1. Context layer health check

Sends an MCP `initialize` to the configured `fleetlens` URL, up to 3 times, 2 s apart. It runs while the
Atlassian connector starts, so it costs no extra time.

- **Considers:** only whether the service answers. Any answer counts as alive.
- **If it is down:** Scope does **not** stop. The confirm screen shows `context layer: UNAVAILABLE — <reason>`
  and you add repos yourself (`+name`). If no repos end up selected, Scope stops.

### 2. Atlassian fetch (PRD, child-page list, Jira)

A headless `claude -p` session (Haiku 4.5) bound to your Atlassian connector. It runs with **no built-in
tools** (it cannot read, write or run anything) and only these connector tools allowed:
`getAccessibleAtlassianResources`, `getConfluencePage`, `getConfluencePageDescendants`, `getJiraIssue`.

1. **Warm-up turn.** claude.ai connectors are still `pending` when a headless session's first turn starts,
   so a one-shot call sees no Atlassian tools at all. The session's first message is a throwaway; by the
   second, the connector is `connected`.
2. **Auth check.** If the connector reports `needs-auth` or `failed`, Scope stops with the fix (`/mcp`).
3. **cloudId.** `getAccessibleAtlassianResources`, matched against config `ATLASSIAN.SITE` (Scope stops if it
   is unset); the first site is the fallback.
4. **Fetch.** One turn asks for the PRD page (markdown), its descendants, and the Jira issue if given.
5. **Writing.** **Python writes every file straight from the tool result in the stream.** The model never
   retypes the PRD, so it cannot trim, summarise or "tidy" it. The PRD body is Atlassian's own markdown.

The session stays open until the end of the run, so the pages and Jira you tick at the confirm screen are
fetched in the same session, with no second warm-up.

**A local file PRD** (`--prd ./prd.md`) is copied as-is with a provenance header. The connector session only
starts if `--jira` is also given.

### 3. Reading the PRD

From `raw/prd.md`, with plain regexes, no model:

| What | How | Used for |
|---|---|---|
| Figma links | `figma.com/(design\|file\|proto)/<key>/<name>?node-id=<a>-<b>`. Key and node id are validated; duplicates are dropped | Step 4a |
| Linked Confluence pages | `/wiki/…/pages/<id>` in the body, excluding the PRD itself and its children | Confirm screen, unticked |
| Jira keys | `ABC-123` patterns, excluding the `--jira` one | Confirm screen, unticked |
| Embedded images | `![…](blob:…)` markup | Counted and warned about (the connector cannot return them) |

### 4a. Figma: screen list first

For each Figma link (skipped with `--no-figma`):

1. **Plugin check.** Scope reads Figma's `settings.json` to find which folder the plugin is imported from.
   If it is a copy outside the repo, Scope copies the repo's `code.js`/`ui.html` into it (so stale code
   can't run) and tells you to re-import from the repo.
2. **Reuse?** If a previous export of the same link is cached (`~/.cache/rfc-agent/figma/<key>-<node>/`),
   it is reused per `--reuse-figma`. Interactive terminal: asks, showing its age and screen count.
   `--yes`, `--confirm-file` or no terminal: reuses automatically.
3. **Receiver.** A local server on `127.0.0.1:8790` (the port the plugin's manifest allows) is started in
   plan mode, with one session per link, keyed by the link's node id.
4. **Open.** `open -a Figma "https://www.figma.com/design/<key>/<name>?node-id=<a>-<b>"`: the link rebuilt
   only from its validated parts, with Figma brought to the front.
5. **Your keypress.** The terminal says: press **⌥⌘P** in Figma (or Plugins → Development → rfc-agent Figma
   snapshot) and keep the plugin window open. Figma does not allow a plugin to be started from outside the
   app, so this one keypress cannot be automated.
6. **Screen list.** The plugin finds the linked node (by id, on any page of the file), walks it, and sends
   only names, sections and sizes. No images yet. The panel then says *"Found N screens. Pick the sections
   in the terminal — keep this open."*

**What counts as a screen** (in the plugin): FRAME, COMPONENT, INSTANCE or GROUP nodes that are visible and
**at least 200 px on both sides**. SECTIONs and COMPONENT_SETs are containers and are walked, nested ones too.
Flow arrows, connector lines, text labels, icons, dots and header strips are skipped.

### 4b. Repo candidates (meanwhile, in the background)

Two calls run side by side:

- **The discovery agent** (Sonnet, `SCOPE.DISCOVERY_MODEL`). A headless `claude -p` gets the whole PRD and
  only the context layer's read-only tools: no built-in tools, no other MCP server. It looks around the index
  (service graph, endpoints, callers), usually 20–30 lookups, and ends with up to 8 services, each with a
  one-line reason. It takes about 90 s and $0.50. Its stream is in `logs/scope.discovery.stream.jsonl`,
  written as it runs; it also gets a task card in `/rfc-agent:rfc` (`logs/agents/scope.discovery.log`). If it
  fails (limit hit, timeout, no JSON at the end), Scope says so and you add repos on the confirm screen.
- **One Haiku call** reads the whole PRD and returns a summary of at most 120 words and which **product
  surfaces** from the surface table it changes. About 10–17 s and $0.01–0.02.

Then four sources, in this order:

1. **`--repos`.** Always proposed, first.
2. **Context layer.** The agent's services, in its order, each with its reason ("context layer: owns the
   For You surface …"). Service names are mapped to repo names through the repo → service inventory
   (`SERVICES_MAP`); a name the inventory doesn't know is lower-cased (`API-Gateway` → `api-gateway`,
   Bitbucket's slug). See [Decisions](#decisions-and-what-each-one-considers).
3. **Surface table.** Each surface the PRD changes adds its repos (config `SCOPE.SURFACES`). This is how
   frontend repos get in. A surface with no repo in the table is reported: *"the PRD changes mobile_app but
   the surface table names no repo for it"*.
4. **Repos the PRD names.** Matched against the inventory's repo list, never a directory listing.

#### Why an agent, not one search

A single `discover_services` query ranks candidates by similarity, and for a whole PRD the scores come out
flat: the right services sit anywhere in the top dozen, or not at all. An agent that explores the service
graph, endpoints and callers finds the owning services reliably, and each answer carries a reason you can
judge. Frontend services still rank low for any feature, which is what the surface table covers.

Then every candidate's remote is **probed** in parallel (`git ls-remote --symref`). The probe gives the
default branch and whether the repo exists on Bitbucket.

### 5. The confirm screen

One numbered list, in four groups:

| Group | Default | Note shown |
|---|---|---|
| Repos | Ticked, except names Bitbucket doesn't have | Branch · local checkout / in repo cache / not cloned · why it was proposed · ⚠ warnings |
| Child and linked Confluence pages | Unticked | Fetched only if ticked |
| Jira keys found in the PRD | Unticked | Fetched only if ticked |
| Figma sections | Pre-ticked by PRD overlap | Screen count, overlap score |

- **Terminal:** numbers toggle (`3 7 9`), `+repo_a,repo_b` adds repos, `b 2=feature/x` overrides repo 2's
  branch, Enter accepts, `q` aborts.
- **`/rfc-agent:scope`:** the same list as selection questions. Scope writes `confirm/proposal.json` and
  waits (up to 60 min) for `confirm/answers.json`:
  `{"selected": [n…], "add_repos": [...], "branches": {"repo": "branch"}}`.
- **`--yes`:** the list is printed and accepted as is.

At least one repo must be selected.

### 6a. Repo snapshots

Each selected repo becomes a **read-only, detached git worktree** at `<run>/_snap/<repo>`, pinned to the
default branch's latest commit. Scope never reads your working checkout directly. In parallel, each git step
under a 120 s timeout:

| Situation | What happens |
|---|---|
| You have it in `REPOS_ROOT` | `git fetch --depth=1 origin <branch>` into your checkout (moves remote-tracking refs only; your branch, files and uncommitted work are untouched), then `git worktree add --detach` from it. **Nothing is cloned** |
| You don't have it | Shallow-cloned **once** into the repo cache `~/.cache/rfc-agent/repos/<repo>`, then a worktree from there. Later runs only fetch |
| `--fresh-clone` | Every repo comes from the repo cache; your checkouts are not touched at all |
| The fetch fails (network, Bitbucket down) | Snapshot of `origin/<branch>` **as last fetched**, recorded as `snapshot-of-last-fetch` with its commit date |
| No checkout, no cache, clone fails | `context-index-only`, with the reason. The run continues; later stages read that repo through the context layer |

### 6b. Ticked pages and Jira

Fetched in the still-open connector session, written byte-exact to `raw/linked/`.

### 6c. Figma export of the picked sections

Scope hands each waiting plugin the ids of the frames in the ticked sections. The plugin exports **only
those**: the exact `JSON_REST_V1` node tree plus a PNG render (2× for phone frames, capped at 4096 px on the
long side). Progress is printed; Scope waits up to 30 min, then keeps whatever arrived as a partial export.
The export is cached per link, copied into `<run>/_inputs/figma/<key>-<node>/` with a digest, and indexed in
`raw/figma_index.md`.

### 7. Artifacts

`scope.json`, `findings/scope_approved.json`, `raw/links.md`, `raw/figma_index.md`, `logs/scope.json`, then a
printed summary. See [Output](#output).

---

## Decisions and what each one considers

| Decision | Considers | Rule |
|---|---|---|
| **Is the context layer usable?** | MCP `initialize` answers | 3 tries, 2 s apart. Down → continue and let you add repos |
| **Which services own this feature?** | The discovery agent: the whole PRD plus the context layer's read tools | Up to **8** services, in the agent's order, each with its reason |
| **Which UI repos?** | Surfaces Haiku says the PRD changes, looked up in `SCOPE.SURFACES` | Each detected surface adds its repos; a surface with none is reported for you to add |
| **Service → repo name** | The inventory, then `STEERING.REPO_ALIASES` (your config wins) | `svc_a_background` → `svc_a`. A miss is retried with `_service` appended; the lower-cased name as last resort. Several deployed variants of one repo collapse into one entry |
| **Does the PRD name a repo?** | Repo names in the inventory, matched in PRD text | Names under 5 characters are ignored. `lab_buddy` matches "lab buddy" / "lab-buddy". Single-word names count only in a repo context: `` `search` ``, "search service", "repo search" |
| **Which repos are never proposed?** | `REPOS_EXCLUDE` | Keeps repos such as an evals repo (which may hold model answers) and this tool's own repo out of the list. Naming one in `--repos` overrides |
| **Default branch** | 1. remote `HEAD` via `ls-remote` (authoritative) → 2. local `origin/HEAD` → 3. `origin/master` / `origin/main` exists locally → 4. `master` | The "how" is recorded per repo (`remote HEAD`, `local origin/HEAD (remote unreachable)`, …). A branch override on the confirm screen wins |
| **Does the repo exist on Bitbucket?** | `ls-remote` output | "may not have access … no longer exists" → **missing**: shown with ⚠ and **unticked** (unless you have it locally). "must whitelist your IP" / timeouts → **unreachable**: ⚠ "couldn't reach Bitbucket", stays ticked |
| **Your checkout or the cache?** | Is `REPOS_ROOT/<repo>/.git` there; `--fresh-clone` | Checkout present and no `--fresh-clone` → your checkout. Otherwise the repo cache |
| **Which Figma sections are pre-ticked?** | Word overlap between the PRD and each section (fresh export: section and frame names; reused export: also the frames' visible text). Words of 4+ letters, minus common words | 3 sections or fewer → all ticked. Otherwise fresh export: overlap ≥ 2; reused export: overlap ≥ 8 |
| **What is a Figma screen?** | Node type and size | FRAME / COMPONENT / INSTANCE / GROUP, visible, ≥ 200 px on both sides. SECTION / COMPONENT_SET walked |
| **Reuse a Figma export?** | Cache entry for the same file key + node id; `--reuse-figma`; terminal or not | `always`, `--yes`, `--confirm-file`, no terminal → reuse. `ask` with a terminal → asks. `never` → re-export |
| **Is the PRD usable?** | `raw/prd.md` exists and is ≥ 200 characters | Otherwise `STOP` |
| **Which pages and Jira are fetched?** | Your ticks | Nothing beyond the PRD (and `--jira`) is fetched unless ticked: child pages are often a team's own technical docs, and tickets carry decisions a person should opt into |

---

## Edge cases

### Inputs

| Case | Behaviour |
|---|---|
| `--prd` is a bare page id | Accepted (`5541855972`) |
| `--prd` is a local file | Copied with a provenance header; no connector session unless `--jira` |
| `--prd` is neither a page nor an existing file | `STOP: --prd … is neither a Confluence page nor a file` |
| Short `/wiki/x/…` Confluence link | Not resolved: no page id in it. Pass the full page URL or the id |
| PRD fetch returns nothing / under 200 characters | `STOP: the PRD came back empty`; see `logs/scope.atlassian.stream.jsonl` |
| PRD has pasted screenshots | Counted, `WARN … cannot be fetched through the connector`, recorded as `prd_images_not_fetched` |
| A connector call errors | Printed (`connector error on <tool> …`); the run continues with what it has |
| The connector does not finish a turn in 180 s | Stops with that message |

### Atlassian connector

| Case | Behaviour |
|---|---|
| Connector `needs-auth` / `failed` | `STOP` with the fix: `/mcp` → authenticate the Atlassian connector |
| No site matches `ATLASSIAN.SITE` | Uses the first accessible site |
| Connector still `pending` | Handled by the warm-up turn |

### Context layer and repos

| Case | Behaviour |
|---|---|
| Context layer unreachable | Continues; the confirm screen says so; add repos with `+name`. No repos at all → `STOP` |
| Context layer proposes no frontend repo | Expected: it ranks frontend services low for any feature. The surface table adds them; if the surface has no repo configured, add it with `+name` |
| Haiku summary fails (usage limit, timeout, bad JSON) | Scope says why and queries with the PRD's opening; no surfaces are detected, so add UI repos by hand |
| Service maps to a name Bitbucket lacks | ⚠ NOT FOUND, unticked, so you can fix it with `+right_name` before anything runs |
| Network down | Probes say "couldn't reach Bitbucket". Local repos are snapshotted from their last fetch and marked `snapshot-of-last-fetch`; uncloned ones become `context-index-only` |
| Your checkout is on another branch or has uncommitted work | Untouched. The snapshot comes from `origin/<default>` |
| Your checkout is shallow | Works; the fetch is `--depth=1` |
| Remote's default branch is renamed | The `ls-remote` probe follows the remote, not your stale local `origin/HEAD` |
| Branch override names a branch that doesn't exist | The fetch and worktree fail → that repo is `context-index-only` with the error |
| A run crashed and left a worktree registered | `git worktree prune` runs before every snapshot |
| Two runs clone the same uncached repo at once | Each clones into a temp folder and renames; the loser uses the winner's clone |
| A clone is interrupted | The temp folder is removed; the cache never holds a half clone |
| A snapshot folder already exists for the run | Removed (worktree deregistered) and recreated |

### Figma

| Case | Behaviour |
|---|---|
| Plugin imported from a copy outside the repo | Scope copies the repo's code into it every run and tells you to re-import from the repo |
| A plugin window from an earlier run is still exporting | Its uploads are rejected; Scope logs "an OLD rfc-agent plugin window is still exporting … close it (✕) and press ⌥⌘P again". ⌥⌘P does not start a new run while a panel is open; it only focuses it |
| Linked node isn't in the file the plugin runs in (wrong tab, branch, duplicate) | Nothing selected → the panel names the file and says to switch to the PRD link's tab. Frames selected → uses the selection, and Scope logs that it did. It never silently exports the whole page |
| Link has no `node-id` | Session keyed `''`; the plugin uses your selection, else the current page |
| Several Figma links in one PRD | One receiver serves all of them, one session per node id. Each link gets its own keypress; each plugin window waits for the picks |
| Port 8790 busy | That link fails with "port 8790 busy … is another export running?" |
| No screen list within `--figma-wait` (default 10 min) | That link is recorded as failed; the run continues without it |
| Ctrl-C while waiting for the screen list | Skips that link; any waiting plugin is told to stop |
| Linked node holds no screens | The plugin reports "No screens found"; nothing to pick |
| Export not finished within 30 min | Whatever arrived is kept as a partial export; the summary says `export did not finish` if nothing did |
| A frame fails to render as PNG | Its JSON still ships; the digest marks the render as missing |
| Figma desktop isn't macOS | The automatic `open` is macOS-only; open the link yourself, then press the shortcut |

### Confirm

| Case | Behaviour |
|---|---|
| No terminal and no `--yes` | File-based confirm (`confirm/proposal.json` → `confirm/answers.json`) |
| No answers within 60 min | Stops |
| Selection keeps no repos | Terminal: asks again. File mode: stops |
| `+name` for a name Bitbucket lacks | Added but shown unticked with ⚠ |

---

## Output

The run folder is `RUNS_ROOT/<jira-key>` or `RUNS_ROOT/prd-<page id>` (e.g.
`~/rfc-runs/prd-5541855972/`), unless `--run-dir` is given.

| Path | Contents |
|---|---|
| `scope.json` | **Start here.** Per repo: `source`, `branch`, `branch_from`, `sha`, `commit_date`, `via`, `fetch`, `error`, `why`. Context-layer status. `repo_discovery`: the summary, surfaces, unmapped surfaces, the query sent and whether it was the summary or the PRD's opening. Pages and Jira included. Figma per link: mode (`new` / `reused` / `failed`), screens found, sections picked. `timings_s` per step. `connector_cost_usd` (includes the summary call) |
| `raw/prd.md` | The PRD as Confluence returned it, with a provenance header (`source_url`, `fetched_at`, `title`, `page_id`, `last_modified`) |
| `raw/jira.md` | The `--jira` issue as returned |
| `raw/linked/` | Ticked child or linked pages (`<id>-<title>.md`) and Jira (`jira-<KEY>.md`) |
| `raw/links.md` | Child pages, linked pages, Jira keys and Figma links found in the PRD, whether or not they were fetched |
| `raw/figma_index.md` | Per picked section: each screen's name, node id, render path, visible text |
| `_inputs/figma/<key>-<node>/` | `index.json`, per-screen `<id>.json` + `<id>.png`, `digest.md` |
| `_snap/<repo>/` | Read-only snapshot of each repo (git worktree). Remove with `scope cleanup <run_dir>`, not `rm` |
| `findings/scope_approved.json` | `{"repos": [...]}` |
| `confirm/proposal.json`, `confirm/answers.json` | File-mode confirm: what was proposed and what was picked |
| `logs/scope.json` | Timings |
| `logs/scope.atlassian.stream.jsonl` | The raw connector session, for debugging a fetch |
| `logs/scope.out` | Scope's own output when run through `/rfc-agent:scope` |

Caches outside the run folder:

| Path | Contents |
|---|---|
| `~/.cache/rfc-agent/repos/<repo>` | Shallow clones of repos you don't have locally (override: `RFC_AGENT_REPO_CACHE`) |
| `~/.cache/rfc-agent/figma/<key>-<node>/` | Last export per Figma link |

---

## Configuration

From `~/.config/rfc-agent/config.json` (see [config.example.json](../config.example.json)):

| Key | Used for | Default |
|---|---|---|
| `REPOS_ROOT` | Where your checkouts are | `~/Documents` |
| `RUNS_ROOT` | Where run folders go | `~/rfc-runs` |
| `REPOS_EXCLUDE` | Repos never proposed (comma list) | none |
| `MCP.mcpServers.fleetlens` | Context layer URL and headers (keep the name `fleetlens`) | — (without it, no context-layer proposals) |
| `STEERING.REPO_ALIASES` | Manual service → repo names; wins over the inventory | `{}` |
| `SCOPE.SURFACES` | Product surfaces → repos: `{"name": {"what": "one line Haiku reads", "repos": [...]}}` | none |
| `SERVICES_MAP.REPO` / `PATH` | Where the repo → service inventory lives (an Ansible `group_vars` file) | none |
| `SCOPE.DISCOVERY_MODEL` | The model of the discovery agent | `claude-sonnet-5` |
| `SCOPE.SUMMARY_MODEL` | The model that writes the summary | `claude-haiku-4-5-20251001` |
| `ATLASSIAN.SITE` | Which Atlassian site to fetch from | none — required for Confluence/Jira |
| `AGENT.CLAUDE_BIN` | The `claude` binary | `claude` |

Environment: `RFC_AGENT_REPO_CACHE` (repo cache location), `RFC_AGENT_CONFIG` (config file path),
`ATLASSIAN_SITE` (overrides `ATLASSIAN.SITE`).

Fixed limits (in code): connector turn 180 s · git step 120 s · `ls-remote` 20 s · Figma screen list
600 s per link (`--figma-wait`) · Figma export 30 min · file confirm 60 min · discovery agent 300 s ·
summary 120 s · ≤ 8 context repos · screen ≥ 200 px.

---

## Models and cost

| Part | Model |
|---|---|
| Atlassian connector calls | **Claude Haiku 4.5** (`--model` to change). Calls tools only; writes nothing. About $0.10–0.23 per run |
| Repo discovery agent | **Claude Sonnet** (`SCOPE.DISCOVERY_MODEL`). About $0.50 and 90 s |
| PRD summary + surfaces | **Claude Haiku 4.5**, one call, no tools. About $0.01–0.02 |
| Everything else in Scope | No model: Python, git, the Figma plugin |
| `/rfc-agent:scope` | Plus the Claude Code session you run it in, which only shows the questions and relays progress |

---

## Safety

- **The PRD is untrusted text.** The connector session has no built-in tools and only four read-only
  Atlassian tools. Figma file keys and node ids are validated before they reach `open`, and the URL is
  rebuilt from validated parts only.
- **Your checkouts are read-only to Scope.** It only runs `git fetch` (remote-tracking refs) and
  `git worktree add` (a registration under `.git/worktrees`, removed by `cleanup`). `--fresh-clone`
  avoids even that.
- **The Figma receiver** binds to `127.0.0.1` only, writes only `<validated node id>.{json,png}` and
  `index.json`, and accepts screens only for the links it was started with.
- **Nothing is written outside** the run folder, the two caches, and (for the plugin sync) the folder
  Figma has registered for the plugin.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| Plugin panel shows "Exporting x/N" with `HTTP 404` | A stale plugin window, or stale plugin code | Close the panel (✕), make sure the plugin is imported from the repo, press ⌥⌘P |
| Panel stuck on "Connecting to receiver…" | Plugin code is out of date | Re-import from the repo; the current code always reports its phase or an error |
| Panel: "the linked node … is not in this file" | The plugin ran in another tab, a branch or a duplicate | Switch to the tab Scope opened from the PRD link, or select frames and rerun |
| `context layer unavailable` | Network down, or the service is down | Reconnect and rerun, or add repos with `+name` |
| A repo shows `⚠ NOT FOUND on Bitbucket` | The service name mapped to the wrong repo name | `+correct_repo_name`; consider adding it to `STEERING.REPO_ALIASES` |
| `STOP: the claude.ai Atlassian connector is 'needs-auth'` | Connector not authenticated | `/mcp` → authenticate the Atlassian connector |
| Repo shows `snapshot-of-last-fetch` | The fetch failed (network) | Fine for a draft; reconnect and rerun for the latest code |

---

## Known limitations

- **Embedded PRD images are not fetched,** only counted. The connector cannot return them.
- **Default-branch detection has no Bitbucket API fallback.** When the remote is unreachable, it relies on
  your local refs.
- **Short Confluence links** (`/wiki/x/…`) are not resolved.
- **The Figma keypress cannot be automated** (Figma does not allow starting plugins from outside).
- **Opening Figma automatically is macOS-only.**
- **Frontend repos come from the surface table,** because the discovery agent ranks frontend services low.
  The table needs the repos for each surface filled in, under their Bitbucket names.
- The discovery agent costs about $0.50 and 90 s per run (hidden behind the Figma step when the PRD has links).

---

## Code map

| File | Role |
|---|---|
| [src/rfc_agent/scope/cli.py](../src/rfc_agent/scope/cli.py) | The flow, timings, artifacts, summary; `run-dir` and `cleanup` subcommands |
| [src/rfc_agent/scope/atlassian.py](../src/rfc_agent/scope/atlassian.py) | Connector session (warm-up, auth check, byte-exact extraction), PRD parsing helpers |
| [src/rfc_agent/scope/context.py](../src/rfc_agent/scope/context.py) | Context-layer health, the discovery agent, PRD summary + surfaces (Haiku), surface table, service → repo, PRD mentions |
| [src/rfc_agent/scope/repos.py](../src/rfc_agent/scope/repos.py) | Remote probe, default branch, snapshots, repo cache, cleanup |
| [src/rfc_agent/scope/figma.py](../src/rfc_agent/scope/figma.py) | Link parsing, plugin sync, two-phase export, sections, `figma_index.md` |
| [src/rfc_agent/scope/confirm.py](../src/rfc_agent/scope/confirm.py) | Terminal confirm and file-based confirm |
| [src/rfc_agent/figma_snapshot.py](../src/rfc_agent/figma_snapshot.py) | Receivers: `PlannedServer` (Scope) and `SnapshotServer` (manual `serve`) |
| [tools/figma_snapshot_plugin/](../tools/figma_snapshot_plugin) | The Figma plugin (import its `manifest.json`) |
| [commands/scope.md](../commands/scope.md) | `/rfc-agent:scope`: background run + selection questions |
| [tests/rfc/test_scope.py](../tests/rfc/test_scope.py) | Parsing, confirm, snapshots against throwaway remotes, cache, receivers, plugin sync |
