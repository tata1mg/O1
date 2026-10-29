---
description: The scope stage — fetch the PRD, pick repos / pages / Figma sections with selection questions, snapshot the repos
argument-hint: --prd <confluence-url | page-id | file> [--jira KEY] [--repos a,b] [--no-figma] [--fresh-clone]
---

Run rfc-agent's Scope for: `$ARGUMENTS`

Prep runs in the background and pauses once for the human's selection. YOU are the selection UI: when it
pauses, show its proposal as AskUserQuestion multi-selects and write the answers back. Never type the
selection yourself and never pick on the user's behalf.

## 1. Set up

```
PROJ="${CLAUDE_PLUGIN_ROOT}"
RUN="$(uv run --project "$PROJ" python -m rfc_agent.scope run-dir $ARGUMENTS)"
mkdir -p "$RUN/logs" && rm -f "$RUN/confirm/answers.json"
```

Preflight (seconds): `claude mcp list | grep -iE "claude\.ai Atlassian|^atlassian"` — either line (the claude.ai Atlassian connector, or a
user-scope server named `atlassian`) showing ✔ Connected is enough. If neither is connected, stop and tell
the user to connect one (`/mcp`). The context layer and Bitbucket
need the VPN; if scope later reports the context layer unavailable, say "VPN off?" in one line.

## 2. Start scope in the background

```
TASK="scope · <run folder name>" uv run --project "$PROJ" python -m rfc_agent.scope $ARGUMENTS --run-dir "$RUN" --confirm-file > "$RUN/logs/scope.out" 2>&1
```

The terminal's task list (`/tasks`) names a background shell by the **start of its command**, so send it as
one command that begins with the `TASK="…"` label: write `$PROJ` and `$RUN` out as literal paths, and put
no `cd`, `PROJ=` or other assignment in front of it.

If it exits 2 with `Langfuse observability is ON but the Langfuse keys are missing`, nothing ran: relay the message (add the keys to the config, or rerun with `--langfuse false`). Never pick `false` for the user.

Run it with `run_in_background`. Don't poll its output — the background shell only notifies you when scope
exits, and the selection happens mid-run. Right after the launch (same turn), start a **Monitor**
(`timeout_ms` 1800000, description `scope events`):

```
touch "$RUN/logs/scope.out" && tail -n +1 -F "$RUN/logs/scope.out" | grep --line-buffered -E 'WAITING FOR|press ⌥⌘P|ONE-TIME SETUP|OLD rfc-agent plugin|STOP:|Langfuse observability'
```

and act on each event the moment it arrives. On `WAITING FOR SELECTION`, ask the selection first — nothing
else in that turn. If the Monitor expires while scope still runs, re-arm it with `tail -n 0 -F` and read the
last ~20 lines of `scope.out` yourself in the same turn. Stop it (TaskStop) when scope exits. The events:

- **`press ⌥⌘P`** — tell the user in one line: *"Figma is open at the linked design — press ⌥⌘P in Figma
  (Plugins → Development → rfc-agent Figma snapshot) and keep the plugin window open."* The plugin only
  LISTS the screens now; it exports after the selection.
- **`ONE-TIME SETUP`** — relay the import step verbatim (Figma → Plugins → Development → Import plugin from
  manifest… → the path printed).
- **`WAITING FOR SELECTION`** — go to step 3.
- **`an OLD rfc-agent plugin window is still exporting`** — tell the user to close the plugin panel (✕) and
  press ⌥⌘P again.
- **`STOP:`** or the process exiting before step 3 — report the line and stop.

## 3. Ask the selection (AskUserQuestion)

Read `$RUN/confirm/proposal.json`: `{"header", "items": [{n, kind, key, label, note, include}], "no_design"?}`.
`kind` is `repo` | `page` | `jira` | `figma`; `include` is scope's proposal. `no_design` is present only when
no Figma design is available (the PRD links none, or no screens could be read) — its value says why.

Show the header's context line first (one sentence, e.g. "Context layer ok — 5 repos proposed").
Then ask, in this order, skipping a kind with no items:

1. **Repos** — multi-select, one option per repo: label = repo name, description = its `note` (branch,
   local/cached/not cloned, why it was proposed; a ⚠ in the note must stay visible). Question text names
   which ones are proposed: *"Which repos are in scope? Proposed: a, b, c. Use Other to add repos
   (comma-separated) or override a branch (`repo=branch`)."*
2. **Confluence pages / Jira** — multi-select; question text says they are NOT included unless picked.
3. **Figma sections** — label = section name, description = screen count + PRD overlap. If there are
   more than 8 sections, first ask one single-select: *Keep the proposed N sections (list them)* /
   *Pick sections myself* / *Skip Figma* — and only chunk the sections out on "Pick sections myself".

4. **No design** — only when the proposal has `no_design`: one single-select, header `Design`,
   *"No design is available (<no_design>). Do you want to proceed as it is?"* with options
   `Proceed without design` / `Stop`. Never answer it for the user.

AskUserQuestion limits: 2-4 options per question, 4 questions per call, header ≤ 12 chars. Chunk a kind
into questions of up to 4 options (headers like `Repos 1/2`) and use several calls when needed. A chunk
with a single item becomes a single-select `Include <label>?` with options `Yes` / `No`.

## 4. Write the answers

Map every picked label back to its item `n` and write `$RUN/confirm/answers.json` with the Write tool:

```json
{"selected": [1, 2, 4, 9], "add_repos": ["web_app_vite"], "branches": {"svc_a": "feature/x"}}
```

- `selected` = the `n` of EVERY item to keep, across all kinds. An item the user did not pick is dropped.
- "Keep the proposed sections" → add the `n` of every figma item whose `include` is true. "Skip Figma" →
  no figma items.
- From "Other" text: bare names → `add_repos`; `repo=branch` → `branches`.
- At least one repo must be selected or added; if none, ask again instead of writing the file.
- With `no_design`: "Proceed without design" → add `"proceed_without_design": true`. "Stop" → write
  `"proceed_without_design": false`; scope exits with `STOP: no design available …` — relay it (add the Figma
  link to the PRD and rerun, or rerun with `--no-figma`) and stop.

## 5. Finish

Keep watching `scope.out` — relay Figma export progress (`Figma: x/y screens exported`) at most every
~30 s. When the process exits, read `$RUN/scope.json` and report:

- per repo: `source` (snapshot / snapshot-of-last-fetch / context-index-only), branch@sha, via (your
  checkout / repo cache), and any error;
- Figma: screens exported of screens found, per link; extra pages / Jira included;
- `timings_s` total and the slowest step;
- the run dir `$RUN`, and the cleanup command
  `uv run --project "$PROJ" python -m rfc_agent.scope cleanup "$RUN"` (removes the repo snapshots).
