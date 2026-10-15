# Test scenarios

Generated from the test code: one line per test function (parametrised tests count once). The sentence is the test's docstring summary when it has one, otherwise its name.

**1000 test functions** in 72 files (rfc: 173, infra: 313, harness: 236, testing: 260, parity: 18).


## tests/rfc/ — 173 tests

### test_activity_feed.py (6)

_The live feed: agent ticks → logs/activity.log, one card log per agent, logs/sessions.json, and the `rfc_agent.watch` follower behind each card._

- target names the file relative to the run
- consume sends start and tool ticks
- progress writes the feed and the sessions
- each agent gets its own card log
- watch follows one agent until it ends
- watch stops when the run ends

### test_ask.py (1)

- CLI-native asks must write the operator-actions file themselves (previously prompt-enforced

### test_draft.py (4)

_Draft: coverage check and rendering, references, and an end-to-end run with a fake `claude`._

- coverage check rules
- render and insert are idempotent
- draft end to end with fake claude
- draft needs investigate

### test_draft_gate.py (10)

_Draft's gate: each check, and --check-only runs with a fake `claude` (fixed → clean; unfixable → known issues)._

- good rfc has no must fix
- structure
- change blocks
- secrets skip placeholders
- mermaid lint
- flags
- gate fix round makes it clean
- gate leftovers become known issues once
- check only needs an rfc
- test guard blocks the real claude

### test_feed_server.py (19)

_Feed page: the line-classifier and per-agent colouring are the only logic worth pinning._

- every line kind gets its own class
- 💬 and gate FAILED are the lines a human must not miss — both bold, fail is red.
- Same label -> same colour every time; a fresh label takes the next hue (up to 9 lanes/run).
- The page's cls() and this file's mirror must stay identical — pin the source order.
- 0.24.0: the feed page grows an operator input (lane + message) posting to /steer, and the
- 0.24.1: POST /steer was CSRF-able — a no-preflight simple POST from any web page (or a
- feed has no cors wildcard and page sends the header
- 0.24.6 steering-bar UX (field report): the four behaviors that made it feel dead, pinned.
- 0.24.13 (field screenshot): four structural defects, each pinned.
- 0.24.14 (field screenshot, ultrawide window): the bar floated mid-screen over dead space and
- 0.24.15: the field is ADDRESSING, not routing — 'lane' was harness-internal jargon (the
- 0.24.15 follow-up (field report): a fresh page mislabeled finished stages as queued because
- 0.24.16 (field report: 'why do previous logs get deleted on reload?'): the old handler
- Field report 2026-08-20: a lane-'ground' steer during a --stages review run was skipped by
- Research round 2 (2026-08-21): with fate bounded (invocation TTL) and messages retractable
- Fix (c) of the 2026-08-24 trace audit: test steers polluted radiology-v3's steering corpus
- prerun steers still flow
- Dismiss must not lie: retract writes the ledger tombstone (future pumps' consumed_before
- chip states and commit only lane contract

### test_figma_snapshot.py (11)

_figma_snapshot: the quota-free Figma channel. A private dev plugin (tools/figma_snapshot_plugin) exports JSON_REST_V1 + PNG per frame and POSTs them to the localhost receiver; the receiver writes a snapshot directory; `digest` renders it into the compact inventory INGEST reads instead of calling the_

- receiver writes frames and index
- receiver answers cors preflight and health
- receiver rejects unsafe node ids
- digest reading order variants and hidden pruning
- digest reports missing index loudly
- The 145-frame lesson (2026-09-02 benchmark: 12.1m/$4.70, worse than MCP): layout duplicates
- Text alone must not decide 'identical': a checked-vs-unchecked checkbox changes no string.
- A FRAME holding nothing but one caption string (a band label, a swatch, an icon) is an
- stage snapshot copies into inputs and writes digest
- stage snapshot refuses a directory without index
- install copies plugin to stable path with fresh id

### test_freshness.py (4)

_Green-skip freshness: a green stage may only skip when the inputs it was built from are byte-identical now. Without this, `--stages extract --force` then a resume let a green GROUND carry findings tagged against the OLD REQ numbering — the manifest-path twin of the fragment-cache poisoning closed in_

- digest changes when upstream output changes
- extract reads EVERY raw file — an operator paste (raw/instrumentation.md) or a figma refetch
- digest stable when nothing changed
- ingest has no upstream — its digest never blocks a skip (its input is the remote PRD).

### test_identity.py (10)

_Run identity: stable across resumes, immune to directory renames, inheritable by later stages._

- first call mints and persists
- resume keeps every id
- Old files carried a per-invocation chain (root_run_id/last_run_id, no run_id). The stable
- The pre-identity bug: session_id was run_dir.name, so renaming a live run dir split it in two.
- The mirror bug: only Path.name was used, so an archived copy pooled with a live run.
- Build/test must compute the same workstream uuid from the ticket alone — no registry read.
- no jira falls back to the label
- corrupt file degrades never raises
- Ordering holds ACROSS milliseconds — there is no monotonic counter, so same-millisecond ids are
- trace metadata is flat and drops nulls

### test_investigate.py (13)

_Investigate: plan checks, rendering, post-agent checks, hook, UI assignment, pauses, and an end-to-end run with a fake `claude` that plays the lead, tracers and 1c by writing the files they would write (no tokens spent)._

- presets and precedence
- plan check catches each rule
- plan check tracers read only scope repos
- repos needing attention and render
- load json tolerates fence
- tracer checks split must fix and flags
- ui checks
- hook confines writes and nudges once
- ui readers keep a flows section together
- pause file mode returns picks feedback and answers
- investigate end to end with fake claude
- investigate needs stage0 output
- hook checks citations at write time

### test_mine_reasons.py (5)

_scripts/mine_reasons.py pure helpers — the analysis passes over the why-layer (0.24.53). Network fetch is not tested; the helpers take plain observation dicts._

- lane resolution falls back to stage metadata
- reason coverage and scopes
- knowing doing flags only cross file same message ops
- uncertainty table counts reasons and digest blocks
- anchor sweep finds reasons digests and gate outputs

### test_observe.py (23)

_Langfuse tracing for the new pipeline (rfc_agent.observe): config, the no-keys stop, trace shape, verbose._

- take flags strips both forms
- take flags rejects a non boolean
- defaults are on and not verbose
- precedence flag over env over config
- off by flag never loads the config
- new keys are optional in an older config
- on without keys stops before the stage runs
- off runs untraced with the flag stripped
- scope utilities are never traced
- standalone stage gets its own trace
- whole run one trace with a span per stage
- sdk failure at open does not stop the run
- agent records model tokens and cost
- verbose adds one span per tool call
- verbose from config
- parallel agents nest under the stage
- failed agent is an error
- agent untraced when off
- gate scores and verdict
- pause answers are recorded
- scope summary call is recorded
- preflight
- The real Investigate (fake claude) under tracing: lead steps named, tracers parallel under the stage, gates.

### test_repo_info.py (2)

_Investigate's per-repo index facts from the context layer (fleetlens): code index yes/no, index age._

- code index and age against each snapshot
- no layer or no build date means unknown

### test_review.py (5)

_Review: finding parsing, checks, merge, and end-to-end runs with a fake `claude`._

- parse check and merge
- flow parts
- review end to end with retry
- review triage pause file mode
- review needs an rfc

### test_revise.py (5)

_Revise: fix-log / confirmation parsing, statuses, the ⚠ section, and a two-round run with a fake `claude`._

- parse review fixes and confirmations
- settle matrix
- unresolved section is idempotent
- revise two rounds end to end
- revise needs review

### test_run.py (6)

_The single command (rfc_agent.run): sequencing, option passing, skipping Scope, --from, stopping._

- full run passes each stage its options
- existing inputs skip scope and from starts later
- stops at a failing stage
- systemexit from a stage is a stop
- needs prd or inputs
- from rejects an unknown stage

### test_scope.py (21)

_Scope: parsing, confirm input, git snapshots against a throwaway remote, receiver targets._

- page and jira ids
- parse page keeps body byte exact
- parse descendants and links
- figma links validated and deduped
- confirm toggle add and branch
- snapshot reads fresh origin tip and leaves checkout alone
- missing repo cloned once into cache then reused
- probe flags wrong names before confirm
- snapshot falls back to last fetch when remote is down
- receiver serves targets and records sections
- planned export lists first then exports only picked
- confirm via file applies selection
- plugin code synced into the folder figma loads
- An update replaces the plugin's per-version folder; the fixed folder Figma loads must follow it.
- A usage-limit reply used to surface as 'connector not usable'; it must name the real cause.
- The user-scope `atlassian` server counts; one connected server wins over another needing auth.
- added repo uses the spelling bitbucket has and drops are reported
- slow atlassian gets a fresh connection
- slow atlassian gives up after three connections
- missing sign in is not retried
- The common case: the server connects 10-30 s in, after the warm-up turn. No fresh connection needed.

### test_scope_discovery.py (9)

_Scope repo discovery: the fleetlens discovery agent, the Haiku PRD summary, the surface table, fallbacks._

- summary and known surfaces only
- summary failures are reported not raised
- surface table from config and unmapped
- discovery agent is isolated and parsed
- discovery failures are reported not raised
- agent services become repos
- gather order and meta
- gather without the layer still uses surfaces
- discovery timeout kills the agent and closes its card

### test_scope_main.py (10)

_Scope's whole flow (`python -m rfc_agent.scope`) with a local PRD file and throwaway git remotes._

- local prd runs end to end and writes every artifact
- local prd gets a provenance header
- snapshot is cleaned up without touching the run
- prd that is neither a page nor a file stops
- no repos named and no context layer stops
- confluence page without a site stops before any connector call
- a named repo that is not on the remote is dropped loudly
- run dir subcommand names the folder without creating it
- run dir prefers the jira key when given
- cleanup needs exactly one argument

### test_services_map.py (5)

_Repo<->service inventory mapping (research verdict 2026-08-15): the catalog keys by SERVICE name, the pipeline by checkout dir — verified live: "web_app_b" -> null while the catalog indexes web_desktop/web_mobile. The devops Ansible inventory is the authoritative map; aliases override it._

- multi variant repos collect all service names
- orphan entries are skipped not guessed
- repo for service reverse lookup and alias override
- Nothing org-specific in the code: without SERVICES_MAP.REPO there is no map, even with a checkout.
- Operator addition 2026-08-15: no local example-devops checkout -> fetch via the bitbucket MCP

### test_steering.py (1)

_Context-layer steering (v7 postmortem, 2026-08-10): conventions the composer would otherwise invent. Accelerator contract: any fetch/parse failure yields NO steering, never a broken compose._

- distill carries idioms and provenance

### test_summarize.py (3)

_Summarize: the readable copy — written, skipped when up to date, rewritten when the RFC changes, one retry._

- readable copy written then kept up to date
- readable copy one retry then error
- readable needs an rfc

## tests/infra/ — 313 tests

### test_approve_readonly.py (18)

_approve_readonly hook — the environment tooth from the v5 postmortem (2026-08-10)._

- v5 grep awk pipeline allowed
- v5 awk range scan allowed
- v5 for loop sed context allowed
- v5 for loop grep docs allowed
- v5 cd and for sed allowed
- v5 find root denied with hint
- v5 inline python defers
- writes and escapes never allowed
- git read subcommands allowed write not
- scoped find allowed
- non bash and garbage defer
- hea3394 awk redirect denied with broker hint
- unsafe awk segment denied with hint
- adhoc python denied with broker hint
- find deny names actual repos root
- pinned broker invocation is not denied
- quoted class words are data not commands
- never classes stay silent outside sandbox

### test_backjump.py (9)

_Cross-stage feedback: REVIEW-style blockers jump back to the producing stage, bounded._

- blocker jumps back and recovers
- Phase 2 of the plugin flow runs `--stages generate,review` as a subset; the review->generate
- backjump is bounded
- Revision mode: on a guided rerun the previous attempt stays on disk; without it, hygiene deletes.
- Trace evidence must span exactly the attempts that produced the current outputs: a revision keeps
- A new invocation must NOT enter revision mode against a previous run's guidance left on disk.
- same stage retry still records decision
- v10 (2026-08-11): a budget-exhausted review stop discarded verified findings — resume re-ran
- Cross-invocation resume of a red revise_on_retry stage: the seeder writes guidance AND records

### test_bonus_patch.py (4)

_Bonus patch before stopping: a retry budget exhausted on a mechanical-only residue grants ONE constrained patch pass instead of halting the run (runtime audit 2026-09-03: a run stopped with 2 mechanical failures, cost an 8-minute human resume, and the resumed patch pass fixed them in 18s)._

- mechanical residue gets one bonus pass and recovers
- semantic residue still stops the run
- mixed residue disqualifies the bonus
- bonus fires at most once

### test_byte_budget.py (7)

_Continuous byte-budget accounting: the ceiling is visible DURING authoring, not only at the terminal gate (v1 binary-searched it for 4.3 min; v2's revision died at it). Two regimes (2026-08-18): below the ceiling warn once per threshold; PAST the ceiling report exact bytes on every edit — silence th_

- below the first pacing threshold is silent
- Write-to-budget (trim-spiral forensics 2026-09-03): 40%/70% hand the model the arithmetic it
- crossing ninety percent warns once with section sizes
- past the ceiling reports exact bytes on every edit
- dropping back under the ceiling goes silent again
- files without a budget are ignored
- default is silent but writes the canary marker

### test_config.py (8)

_The config loader: config.harness.json (shipped) < config.json (yours) < env vars._

- missing config json fails loudly and says how to fix
- a small user file gets every default
- user values win and dicts merge key by key
- env override wins and coerces
- unknown sections are reported not fatal
- bad json names the file
- harness file holds no org values and only known sections
- materialize mcp subset

### test_diff_only_revision.py (5)

_Diff-only revision protocol (0.22.59): a revision session may EDIT the deliverables but never re-Write one whole. Revisions were measured emitting more output tokens than the composes they revise, 60-80% of it unchanged text re-typed around a small delta._

- revision write of an existing deliverable is denied instructively
- edits and new files still pass while revising
- Observed live 2026-08-18: a denied Write was rerouted as an Edit whose old_string was the
- The flag is set on revision invocations only — a first compose must be able to Write.
- containment still wins over everything

### test_gates.py (22)

- citation valid resolves
- citation line out of range fails
- citation missing file fails
- citation nonlocal repo warns not fails
- Ambiguous basenames resolved by line count: `booking.py:1210` can only live in a candidate that
- Evidence-table resolution: when the run's verified pack cites exactly one path with the bare
- v5 (2026-08-10): contract.md's only qualified order.py was routes/ (232 lines); bare
- v5: rfc.md cited BOTH svc_a order.py files qualified, so the old exactly-one bail-out
- v5: a 9,688-line test mock (state_feeder_service tests/mock_service_clients/order.py)
- `builder.py:56,68-76,3022-3031` used to validate ONLY line 56 — the tail's fabricated ranges
- failure classifier maps known wordings
- note inert drain dedupes and clears
- secrets and crossdoc classify above other
- Coherence contract between the two families over the same vocabulary (review finding #10):
- 0.24.56: the 0.24.34 output-shape lints fell into `other`, hiding the lint-churn class from
- Paragraph-scoped proximity repair (2026-09-02, srp-bottomsheet-chat-history: 23 bare-citation
- DESIRED: cross-paragraph proximity is NOT trusted — a same-basename citation in another
- The real residual specimen (findings.md:63, 2026-09-02): bare `index.js:144-151` in one
- Partial paths (`BookingAssistant/index.js:261`) rode through as 'non-local repo' WARNINGS —
- Both new repairs inherit the hard line-count constraint: a candidate shorter than the cited
- 2026-09-03 (srp-bottomsheet-chat-history-v2): 23 bare cites sat in constraint paragraphs whose
- v8 ground (2026-09-08): 13 bare `index.js:144`-style citations rode a 14.7-min full-model

### test_gates_prd_soft_markers.py (1)

_Ingest prd.md marker scan: soft-dependency fetch failures (instrumentation sheet as an Office file) must not hard-fail the gate; a PRD-level auth wall still must._

- absence regex ignores quoted error literals

### test_guard_writes.py (6)

_Write-guard containment — quoted-span redirect parsing (v7 postmortem, 2026-08-10)._

- `awk -F: '$1>4300'` was denied — the redirect regex parsed the QUOTED `1>4300` as an fd
- sort is prefix-allowlisted as read-only, but -o/--output writes with no `>` for the redirect
- use_figma is arbitrary-JS-write-capable; the grant (mcp_tools) is safe only because this
- v7 incident (2026-09-07): the compose session rationalized a direct Write of out/contract.md
- guard deliverables mirror emit allowed
- Containment must never fail open: an interpreter that cannot import harness.policy denies (exit 2).

### test_hooks_canary.py (2)

_End-to-end feedback-channel canary (v2 run 2026-09-03: hooks passed every unit test while the CLI side of the PostToolUse contract had silently changed — a 25-minute compose ran blind)._

- alive is cached per cli version and never reprobed
- dead channel warns records and is not cached

### test_ledger.py (28)

_M2: scan ledger + trace-backed gates — the fabricated-citation catch._

- build ledger collects reads and searches
- witnessed via tool output too
- An Agent/Task tool_result is the subagent's final report — model PROSE that can name any path:line
- Results witness only when correlated to a clean originating call — an orphan tool_result (no
- Revision mode REQUIRES reading the previous findings.md, whose content is full of repo citations —
- A Bash command targeting the run's own artifacts is excluded end-to-end: the command string the
- The exclusion is per-interaction, not per-stream: repo reads in the same session keep witnessing.
- Witness must span rotated attempt streams: a citation read in attempt 1 and carried over by a
- One `find`/`ls` over the repos used to witness every listed path — 'witnessed' must mean read or
- The agent's Write of findings.md must not witness the citations it contains (self-witnessing).
- witnessed gate catches fabricated citation
- witnessed gate passes traced citation
- witnessed gate skips non local repos
- write_ledger takes ALL the stage's streams (parallel shards + rotations), not just the main one.
- write ledger none when no stream
- run local citations resolve against run dir
- qualified citation gate
- qualify citations fixes unique owner
- A bare citation whose basename the SAME doc already qualifies exactly once resolves to that path,
- Two different qualified paths for one basename = the doc has not established which; fall through
- qualify citations ambiguous left for gate
- witnessed accepts unprefixed form
- qualify citations line context tiebreak
- Parallel-subagent scanning (GROUND/REVIEW fan-out) must not weaken the anti-fabrication gate:
- Field incident 2026-08-20 (records-import run, 4 identical GROUND failures, ~$15): bare
- The tightening must not weaken the anti-self-witness protection: every real own artifact —
- A Grep PATTERN containing a word like 'reviewed_by' tripped the old bare 'review' token and
- The deliberate residue: a repo file EXACTLY named like an own artifact (docs/plan.md) is

### test_memory.py (3)

_Durable memory: the scorer-blind learning loop (m13). The Factorio lesson is the design constraint: a learning pass that can see its scorer learns to game it — so promotion reads process signals only, and eval-mode runs never write memory at all (enforced in cli, pinned here at the module level)._

- memory text folds into prompt below guidance
- v5 postmortem: outcome must move. Absorbed classes retire their prose; a python-denial note
- audit never downgrades violated

### test_obs.py (58)

_obs must be a clean no-op when unconfigured — observability never fails a run._

- noop when unconfigured
- GateFailure must pass through run_trace/stage_span/agent_generation untouched
- clip truncates deep strings
- The exact SDK calls obs makes must exist with the kwargs we pass — every call sits inside
- Rollout signals (2026-08-11): `release` = installed plugin version (segments gate pass-rate,
- The shared-Max-plan account flows as trace METADATA (subscription/cost attribution), sourced
- The human half of the signal loop: command-layer choices land as operator:* events on the next
- The WHY layer on the LLM node: bounded digest — opening moves and conclusions both survive a
- decision_event with NO ambient span and NO traceparent env must emit NOTHING — an observation
- A subprocess (selfcheck) with RFC_AGENT_TRACEPARENT set attaches its decision to the LIVE
- Main process mid-run: ambient span wins — no trace_context, no forced flush.
- Worker threads have no ambient OTEL context: an explicit parent handle must own the child
- Langfuse model analytics key on EXACT names: '(3-persona parallel)' and '[1m]' decorations
- traceparent() emits W3C form; _env_trace_context parses it back to the TraceContext keys.
- Langfuse builds its agent graph from every observation type EXCEPT span/event/generation, and its
- Renaming a run dir must not split its session — session_id is read from identity.json.
- A generation's input is the training half of every dataset item the annotate->dataset loop
- Annotating a single observation hides the rest of the trace (documented Langfuse limitation),
- Tags are Langfuse's filter chips (metadata is not filterable the same way). The ticket tag must
- engine.run_claude gives CLI --model absolute precedence over DEFAULT_MODEL/STAGE_MODELS, so a
- The manifest is cumulative across resumes: without the `ran` filter every later invocation
- Fan-out workers repeat per stage attempt; without the ambient suffix the Aggregated view
- The CLI stamps every assistant/user event with a ms timestamp; the tool span must end at
- event ns parses cli timestamps
- Decisions are attempt-suffixed like stages/gates: unsuffixed repeating decisions were the
- gate_started opens the evaluator BEFORE evaluation; record_gate(handle=) finishes it with the
- Stage nodes showed Input/Output: undefined to annotators (census 2026-08-17: 13/13 empty).
- The gate merge stamps a one-line verdict as the STAGE span's output — self-describing for
- Shards were the one agent class with input:null — detail (scope) now rides as input.
- 0.22.64: an observation seen in isolation (annotation queue, API, dataset provenance) must be
- 0.22.66: propagated identity (run_id/session/user) lives in the MAIN thread's OTel context and
- 0.22.67/68: the Aggregated graph is DECLARED via langgraph_node/step. Node-rich lanes: every
- 0.22.73: the thinking tap APPENDS across attempts/invocations, so digests must start at the
- All-cache ground runs have engine_desc 'cache (…)' — 'cache' must not enter model analytics.
- finish generation carries denials and compactions
- A PASS with disabled checks must be distinguishable from a policed PASS (the audit's worst
- rfc_rating becomes a chartable score (human_rfc_rating), and a non-interactive run's owed-but-
- The trace output block is cumulative manifest state — entries must say whether THIS invocation
- Per-worker truth (audit 2026-08-19): each persona/shard node carries its OWN cost/usage/model
- Review finding #5: 'crashed (no stream recovered)' parsed to model='crashed' — the same
- Review finding #2: the rating is collected post-run and flushed on the NEXT invocation —
- Feedback-timing redesign (2026-08-20): ratings are engagement-gated, so deferral past
- ungrounded rating is labeled as such
- Review findings #2 + #4: finish_run must store this trace's id (the rating's future target)
- 0.23.3 revert guard: env credentials reach obs ONLY via config._ENV_MAP (env-wins at config
- 0.24.5: a steer posted DURING a stage flushes at that stage's boundary and gets its own
- 0.24.11: measured 2026-08-19 — the OTEL batch delay was ~4s of the ~5s steer→Langfuse
- 0.24.12: the observation is positioned at the moment the human ACTED (the record's ts), not
- config snapshot never rides propagated attributes
- P0 (0.24.50): each tool observation carries the thinking that governed it — `reason` (clipped,
- The live drain hands each tool_use its governing thinking block: same_message when they share
- P1 (0.24.50): the generation digest = whole non-tool thinking turns from the stream (source
- Fan-out workers get the same preference: their own stream's deliberation beats the tap file.
- deliberation digest keeps recent whole blocks and names elisions
- 0.24.54: review's failure_classes is the single degenerate class review_blocker, so the
- 0.24.57: the GROUND->GENERATE handoff reads findings.md from disk, so the trace held only a
- Clipping mid-blob made the Langfuse media parser traceback on every screenshot Read
- The Read tool's image result is an Anthropic image block, not a data URI; Langfuse's exporter

### test_operator_wait.py (4)

_Run self-accounting for the two audit buckets nobody could read off the manifest (runtime audit 2026-09-03): operator-wait (~28 of 109 min, excavated from four log files) and rework (attempts beyond the first — 33% of machine time)._

- record wait and summary roundtrip
- summary is none without asks
- rework s counts attempts beyond the first
- manifest carries operator wait and totals ignore it

### test_privacy.py (7)

_privacy.py — the client-side secret scrub. Table-driven true positives (each provider's documented dummy format), false-positive fixtures matching our real traffic shape (UUIDs, git SHAs, base64 blobs), nested payloads, fail-closed behavior, and a hot-path perf bound._

- true positives
- false positives untouched
- nested json payload
- idempotent
- perf 100k prompt
- mask otel spans patches only dirty spans
- mask otel spans fail closed sentinel

### test_resume.py (12)

_Retry-by-resume: a revision continues the session that wrote the outputs (--resume, warm context) instead of launching a fresh-context rerun. Measured 2026-07-28: rework was ~40% of a run's wall-clock, almost all of it a cold agent re-reading everything; a resumed targeted edit took 12.7s vs 400s+ c_

- parse stream captures session id
- fresh run records session and revision resumes it
- resume error falls back to fresh revision
- CLI --model > AGENT.STAGE_MODELS[stage] > AGENT.DEFAULT_MODEL (the fast-write-leg knob).
- Patch pass (comparison-doc fix #1): free-form revisions on mechanical failures converged
- Patch revisions carry the one-Edit-per-failure contract and honor AGENT.STAGE_MODELS['patch'].
- A patch revision with no session to resume must NOT get the full build_prompt (which orders a
- Conditional resume (2026-08-11, TASK-29403): a revision resumed a 171,625-token session and paid
- load session tolerates old single line format
- v8 ground (2026-09-08): 5 lead-claim failures dragged 17 one-edit citation fixes into a
- The pre-pass must rotate its stream (witness evidence), rewrite guidance, and exclude generate
- v8/v9 field cost (2026-09-08): guidance APPENDED gate blocks per attempt, so revisers received

### test_resume_policy.py (15)

_Window-aware resume-cap policy (0.22.42): the cap scales with the VERIFIED context window, bounded by the configurable quality ceiling; anything unverifiable keeps the legacy 100K cap._

- unknown window keeps legacy cap
- window smaller than buffer keeps legacy cap
- verified 1m window yields quality ceiling
- config ceiling override respected
- old 200k regime reproduces original calibration
- detect by observation beats static checks
- detect ignores big sessions that compacted
- detect static 1m model
- detect static downgraded by env
- detect static official base url is not a gateway
- resumable 150k session resumes on verified window
- resumable fat session still goes fresh
- resumable unverified window uses legacy cap
- stream parser counts compaction events
- session file roundtrip with compactions

### test_runner.py (22)

_Pipeline contract tests with a fake engine — proves the engine seam + resume semantics from day 1._

- full pipeline green manifest
- rerun skips green stages
- stage subset reruns exactly that stage
- Review mode drives the pipeline one stage per invocation (--stages a, then --stages b), each a
- The manifest entry was overwritten per attempt, so a retried stage's earlier spend vanished — the
- Resume must be cheap in the plugin flow, which ALWAYS passes --stages: a green stage inside the
- missing requires fails loudly
- gate failure stops pipeline and records
- Typed error, never SystemExit — orchestration must stay embeddable (only cli.main exits).
- Audit 2026-08-19 gap #1: an EngineFailure used to skip finish_generation AND the manifest
- green skip is recorded as decision
- gate inert checks land in manifest
- Review finding #1 (2026-08-19): revision mode ROTATES the prior attempt's stream (witness
- Review finding #8: --force + crash used to leave status=engine_failed beside gate.passed=true
- Review finding #3: the crash path raised before ran_stages.add, so the one stage that spent
- manifest write is atomic and leaves no temp
- The breadcrumb exists only between attempt start and record — a green run and a live-recorded
- SIGKILL/power-loss recovery (0.23.2): an intent breadcrumb without a manifest record is
- A kill BETWEEN the live crash record and the breadcrumb clear must not double-bill: the
- 0.23.3: recovery must never fail a run. A breadcrumb with string/garbage fields is cleared
- 0.23.3: a dead GROUND attempt must not be eaten by a later `--pipeline ingest,extract`
- 0.23.3: started_at is derived from the breadcrumb's epoch — a reconciled crash entry can

### test_security.py (15)

_Security controls: read-only MCP grants and the write-guard hook._

- grants are per tool never whole server
- user scope aliases mirror but never widen grants
- unknown server is denied loudly
- no allowlist contains a write tool
- A write-capable tool may be granted ONLY with a PreToolUse constraint actually in place.
- write guard decisions
- The engine's PreToolUse matcher and the guard's _WRITE_TOOLS were duplicated literals: a new write
- No RFC_AGENT_RUN_DIR (someone's interactive session) => never interfere.
- A shard writing a scratch script in Claude Code's own throwaway /tmp/claude-*/ dir is legitimate
- CLI 2.1.224 decomposes compound Bash and auto-denies unlisted parts (13 denial round-trips in
- The ACI wrapper: agents' ONLY permitted git-history path. It may only run `git log` against a
- EXTRA_DISALLOWED_TOOLS reaches every stage's --disallowed-tools; empty pieces vanish cleanly.
- The prompt block is generated FROM _READONLY_BASH_ALLOW so prompt and enforcement can't drift,
- The batching nudge ships beside sandbox_affordances in run_claude: independent calls in ONE
- CLI 2.1.139 made PostToolUse exit-2 a silent drop unless continueOnBlock is set — reproduced

### test_stage_effort.py (5)

_AGENT.STAGE_EFFORT: per-stage `--effort` for headless children (Layer 2 of the extract redesign)._

- unconfigured stage runs as before and keeps operator env
- configured stage gets level and env override is stripped
- every cli level is accepted
- unknown level or shape fails loudly
- Source-level wiring check (same style as the dispatch-note test): the level reaches `--effort` after

### test_stage_tools.py (9)

_stage_tools broker: the sanctioned awk/sed/redirection replacement. Confinement is the contract — every destination resolves inside the run dir (the process cwd), traversal and symlinks included._

- slice line range and regex
- slice out lands inside run dir
- slice out traversal escape refused
- symlink escape refused
- hash prints sha256
- save image decodes b64 file inside run dir
- save image escape refused
- bad input is a usage error not a traceback
- help is self describing

### test_steer.py (21)

_Mid-run operator steering (0.24.0): inbox, per-child pump, and the stream-json spawn contract._

- post writes inbox and operator action
- pump delivers wrapped filters lanes and dedupes
- An uncapped steer could exceed the pipe buffer and block the engine's wait loop (which
- pump closes stdin on terminal result
- A record DELIVERED to stage N must not re-guide stage N+1 (the 0.24.0 grace window leaked
- 0.24.3 root fix: a steer targeting a LATER stage's lane, posted while another stage runs,
- A killed-pre-spend child (figma respawn, kickoff failure, Ctrl-C) whose pump never had a
- A resume->cold fallback passes the FAILED child's epoch: steering delivered (and acked) to
- 0.24.3: result detection is single-shot (byte offset), so close() must be retried off the
- 0.24.1: a steer racing the terminal result must NOT start a bonus turn on a finished stage
- End-to-end against a stub binary: kickoff arrives as the first stdin user turn (prompt is no
- Verdict fields latch on the FIRST result (a post-result bonus turn must not rewrite the
- The dead-child backstop (0.24.2): nonzero exit + no result event must raise with the stderr
- unwrap(TEMPLATE.format(text=x)) == x — the feed drain's 'read by the agent' ack depends on
- The wrapper itself ordered the 2026-08-21 mutiny: 'stop the run entirely' arrived under
- A feature-test steer must ride the real session (it tests nothing otherwise) — so it is
- --replay-user-messages echoes the injected steering turn into the stream; the drain turns
- 0.24.10: a steer posted mid-stage flushes to Langfuse within a tick of the serial wait loop
- Invocation-scope TTL (2026-08-21 research: control messages expire — MQ TTL+DLQ, Akka dead
- Field incident 2026-08-21: pumps read the consumed ledger ONCE at spawn, so a retraction
- A steer's terminal fate is a signal: delivered/skipped already reach the trace as

### test_streams.py (21)

- parse ok stream
- parse error stream keeps cost but no text
- The child inherits the operator's ambient model when --model isn't passed, so the manifest must
- parse garbage lines ignored
- The live feed: one readable line per tool call, incremental byte offsets, concurrent-safe appends.
- --include-partial-messages deltas become throttled progress heartbeats — the fix for 3-6 min
- Auto-denials cost a silent reformulation round-trip each (measured 2-9/run) — the feed must show
- CLI 2.1.224 added system/permission_denied events whose `message` is a plain STRING — the old
- parse stream survives string message
- The reasoning channel (operator ask 2026-08-07: 'stream the thinking, not just tool calls').
- Newer models ship thinking with display=omitted: empty text + signature + estimated_tokens
- v6 (2026-08-10): with the thinking tap on, display=summarized flows through to the CLI stream
- The resume-cost signal (2026-08-11, TASK-29403): resuming a 171,625-token session paid a 2m24s
- The SAME denial fires both signals on new CLIs (system permission_denied + the tool_result
- Fan-out honesty (audit 2026-08-19): usage used to be the FIRST-FINISHED worker's alone while
- Review finding #7: bare 'denied'/'permission' substrings counted a repo file's own EACCES as
- Review finding #9: an unguarded int() over a stream-parsed value would kill a fully
- P1 (0.24.50): thinking blocks with NO tool_use in the same assistant message are the
- 0.24.52 + 0.24.55: each deliberation block is prefixed [HH:MM:SS] in LOCAL time — the Langfuse
- The parallel-dispatch gauge: turns batching >1 tool_use are counted apart from single-call
- stream-json emits one assistant event PER CONTENT BLOCK sharing message.id — a parallel batch

### test_think_tap.py (10)

_Thinking tap: the sidecar that recovers reasoning text the headless CLI blanks._

- The tap intercepts AUTHENTICATED traffic, so it must never turn itself on.
- mutate body sets display and nothing else
- extractor survives arbitrary chunk boundaries
- extractor empty thinking stays silent
- start tap respects existing gateway
- start tap returns none on broken sidecar
- default base url is not a gateway
- tap handshake and loopback only
- Mirror of test_feed_server.test_steer_csrf_boundary for the local-process-only policy:
- Live round-trip: a no-preflight browser POST (Origin set) and a DNS-rebound GET (foreign

### test_witness_carry.py (1)

_Witness carry-over (0.22.59): a fresh revision inherits the compose session's reads, so a citation carried over verbatim isn't re-accused of fabrication when the compose stream is gone._

- The runner's hygiene contract: a from-scratch rerun regenerates from zero, so witnesses (and

## tests/harness/ — 236 tests

### test_engine_pi.py (42)

_PiEngine adapter tests — argv, env contract, translation, timeout, failure detection, doctor._

- argv carries mode hermeticity guard session and prompt
- tool lists are translated to pi names and mcp dropped
- Warm context: pi's headless resume primitive is `--session <path>`, and because the id was minted
- resume falls back cold with a log when the session file is gone
- unknown flag is refused because pi would ignore it
- `--` is passed precisely so a prompt may begin with `-`; validating past the terminator rejected
- a dash prompt survives the whole run
- unknown thinking level is refused
- missing binary names the fix
- every pi.* knob must be reachable from `--set`. The adapter reads them off the resolved Flags
- pi bin and guard path come from flags too
- env carries the policy contract and pins pi to the run
- `broker_hint` / `repos_root` / `exempt_marker` are what make a pinned broker exempt and make the
- the policy extra flag beats the environment
- no declared writable roots defaults to the run dir and says so
- hooks disabled means no guard extension and a loud note
- pi has no MCP and no upstream MCP extension, so such a stage cannot succeed. Refusing up front
- readable roots and steering are recorded as gaps
- A Claude-authored allowlist carries mcp__* names even when the stage declares no servers.
- run translates the recorded stream into canonical events
- cost is summed from the session jsonl with the category breakdown
- pi emits no terminal totals record and some providers report usage only at completion, so an
- denials come from the guards side channel
- exit zero with an errored stream is a failure
- a stream without agent settled is a failure
- extension error on stderr fails the stage
- timeout kills the whole process group
- The real shape of pi's bash tool: the leader dies politely on SIGTERM while a node server or `npm`
- a non positive timeout is a config error not an instant death
- budget kill is best effort but terminal
- `or None` turned a legitimate "spend nothing" into the opposite instruction.
- a negative budget is refused
- The guard fails safe, so an unreachable policy CLI presents as "no tool worked" AFTER the spend.
- A relative PYTHONPATH resolves against the GUARD's cwd (the run dir), where `src` does not exist —
- a torn non json stdout line is recorded not fatal
- doctor reports a missing binary with an install command
- doctor flags an old pi and an old node
- doctor reports missing credentials without printing any
- pi writes `auth.json` as `{}` on its first run, so existence is not a credential.
- doctor is healthy when everything checks out
- The escalation must not be conditional on the LEADER outliving the grace period. pi dies politely
- The contract says `--set pi.provider=anthropic` must reach the engine. It reaches PiEngine when a Flags

### test_engine_pi_guard.py (4)

_The pi guard extension — syntax, and its decision table exercised in Node._

- the guard and its manifest ship with the package
- pi's own shipped example guards with `path.includes(p)`. That is a convenience filter, not a
- node check parses the guard
- the guards decision table and its agreement with the python policy

### test_engine_pi_translate.py (26)

_Pi -> canonical event translation, with no subprocess anywhere._

- tool kind mapping
- normalise input lands paths patterns and commands where the ledger looks
- result text flattens the content block shape
- message cost breakdown distinguishes absent from zero
- the sample stream translates to the expected event shape
- pi's wire `message_update` is delta-only and its usage "may remain zero when a provider only
- thinking and text blocks become separate events
- tool output is clipped
- the error stream is detected as a failure
- every bad stop reason fails
- no agent settled is a failure even on a clean exit
- agent end with will retry is not terminal
- an unknown event type is recorded not swallowed
- The session file is where cost comes from, so a format bump matters. It is a LOG rather than a
- the researched session version produces no noise
- A header is guaranteed to be line 1 at 0.85.1, but losing events over a version change would be
- The translator does no start/end pairing, and `events.summarize` does not either — a tool_end with
- a non json stdout line becomes an unknown event not a crash
- The cursor advances by bytes on disk. Read through a TEXT handle, universal-newline translation
- denials are tailed incrementally and partial lines wait
- Measured against 0.85.1, an extension's console.log goes to stderr, so a denial cannot reach the
- session usage cost returns the summed breakdown
- finish prefers the session file breakdown and labels the source
- "no cost was reported" and "the cost was zero" are different facts. Emitting 0.0 for the first
- The mirror image: a provider that really did report {total: 0} has MEASURED zero, and that is a
- the stream sum accumulates every category

### test_harness_claude_engine.py (34)

_The claude-code adapter and its stream-json -> canonical-trace translator._

- tool kind mapping
- unknown tool is other never a guess
- init carries model session and bound tools
- seq is monotonic and schema version present
- every tool use becomes one tool start with a call id
- --include-partial-messages exists for the live feed; translating deltas would double every call.
- mcp calls carry server and tool
- tool end pairs by call id and records paths
- The CLI's first-class permission_denied event AND a guard-shaped errored tool_result.
- compaction and replayed user turns
- thinking and text are separate types
- result carries cost usage and session
- errored result keeps reason and blanks text
- unknown events are logged not dropped
- torn and garbage lines are skipped like parse stream
- output is clipped but shape survives
- A string message body is legal in the stream (pinned by test_streams) and must not crash.
- translate file writes a canonical trace and summarizes
- A re-translated attempt must replace its trace, never append to the previous one.
- The ledger and witness gates degrade to a loud WARN on an absent trace; a crashed child must
- engine is registered under its flag name
- engine satisfies the protocol
- doctor reports a missing binary with the fix
- doctor accepts a working binary
- doctor reports a failing binary
- run without native ctx says exactly what is missing
- The adapter's whole job: EngineRequest -> run_claude args, StreamResult -> trace + EngineResult.
- run_stage keeps manifests byte-identical by calling the SAME claude_engine as before, then
- hook settings default materialises every hook
- hooks master switch and per hook disable
- Only `assistant`/`user` lines carry a timestamp (verified on a live CLI 2.1.263 stream, 156
- `ts` is the ENGINE's clock and is deliberately not clamped — the CLI's own stamps can arrive a
- The wall-clock fallback: every event gets the same stamp, which is honest and still monotone.
- The strongest pin available: the canonical summary and the reference stream parser must report

### test_harness_cli.py (26)

_The `harness` CLI: the exit-code contract, the flag surface, and --json for agent callers._

- workflows lists the discovered workflows
- engines reports names and doctor status
- flags json is the machine readable registry
- flags workflow filter
- flags explain shows every layer and the winner
- flags explain human output marks the winner
- flags explain unknown names the fix
- 2 is GATE_FAILURE in the public contract; a typo'd subcommand must not read as a failed gate.
- unknown flag is rejected before anything runs
- bad stage engine syntax names the shape
- replay without a recording says how to make one
- stages subset requires a run dir
- record on a dir that is not a run
- record builds a recording and prints the replay command
- cli sugar folds into the one flag dict
- A workflow parser with no --set/--engine must not crash here.
- cli sets rejects bad stage engine syntax
- resolve run dir precedence
- a harness must not launch desktop apps, write memory or notify on someone else's behalf.
- run writes flags json and returns ok
- run halts before spend without an observability sink
- obs required can be disabled explicitly
- `--steer` at a live run must work on a machine with no config.json.
- exit codes map the runner failures
- json out gives an agent the whole result
- hooks fire in the documented order

### test_harness_layering.py (2)

_Layering guard: nothing under src/harness/ may import rfc_agent or workflows at RUNTIME. `TYPE_CHECKING`-guarded imports are tolerated (they never execute) — that is the one documented seam until config/streams move into the kernel._

- harness has no runtime dependency on workflow code
- A seam entry that no longer matches any import is stale — delete it so the allowlist shrinks honestly.

### test_harness_policy.py (19)

_harness.policy — the containment policy as a library, and its transport-neutral CLI._

- cli denies write outside writable roots
- cli allows write inside and relative paths resolve against cwd
- cli denies escaping redirect and egress
- cli allows provable readonly pipeline and asks otherwise
- cli deny reasons name the sanctioned alternative
- Containment.readonly_shell=False turns off the classifier but NOT the deny-list.
- Exit 0 and a verdict for every malformed input — a policy crash is worse than an 'ask'.
- cli help and unknown subcommand
- The extracted policy must work for any writable root, with no RFC_AGENT_* env set at all.
- dev sinks and agent scratchpad are not escapes
- segment readonly matrix
- No env, no subprocess: the same verdict the CLI gives, callable in-process by any adapter.
- broker allowlist matches exact forms
- A sanctioned broker form must reach 'allow' even though its argv looks like an interpreter.
- read confinement only fires when roots are declared
- unknown tool gets no opinion
- An engine that spells its shell tool differently is still contained.
- There is ONE allowlist. The Claude engine holds its own literal because its prompt text
- The point of the hook scripts must not hold a second copy of the regexes.

### test_harness_replay.py (18)

_The replay engine + `harness record`: a recorded run re-enacted with no model in the loop._

- record builds a versioned recording from a run dir
- The manifest is the run's own record of what each stage DECLARED; incidental files are not
- record ignores run level manifest summaries
- record can select labels
- record names the fix when asked for an unrecorded stage
- record refuses a dir that is not a run
- Recordings must be makeable from runs that predate the canonical trace.
- engine requires a recording and says how to make one
- doctor findings name the fix
- doctor is clean on a real recording
- engine is registered and satisfies the protocol
- run restores outputs and reemits the trace
- A replayed trace describes a run happening NOW; keeping the recording's clock would make the
- reemit is idempotent not append
- A replay that silently produced nothing would read downstream as a stage that legitimately
- run stage returns the recorded accounting
- run stage reports a recording that cannot satisfy the contract
- Same recording, two replays, identical outputs — the property the differential test rests on.

### test_harness_review_fixes.py (30)

_Fixes from the independent review of `harness/agnostic-kernel` (research/REVIEW.md)._

- a secret flags value is never serialised
- the kiwi credentials are marked secret
- A tripwire for flags added later: an env var named like a credential must be marked secret.
- Workflow discovery imports EVERY package under workflows/, so an unscoped dump put another
- `dump(only=None)` keeps working — it is what `harness flags --json` and debugging want.
- describe and explain redact a secret default
- engine from flags forwards flags to an adapter that takes them
- Adapters differ on purpose; passing `flags` unconditionally would TypeError on this one.
- get engine drops flags only for constructors that reject it
- The CLI half of the same bug: `pi.*` flags register on import, and nothing imported the engine
- One refusal fires two signals on CLI >= 2.1.224; counting events double-counts every denial,
- Side by side with the reference implementation, on the stream shape the review used.
- So a non-Claude engine can record the same `post_result_turn` decision the Claude path does.
- An engine that reports no call_id still gets an honest count.
- A7: a result with no cost must not become 0.0.
- `run_stage`'s loop runs AFTER a successful stage and outside the runner's except EngineFailure,
- write trace never raises
- The embedder path: `run_pipeline` called directly, no flags resolved. Before this, a config.json
- The promise is a list an agent can ACT on. A flag nothing reads by name must say so.
- The marker must not drift: a flag claiming `wired` needs a `ctx.flag("<name>")` reader.
- An existing config.json carrying AGENT.HOOK_CANARY / LANGFUSE.REQUIRED keeps working.
- boolean true words
- boolean false words
- `hooks`'s own help calls False 'removes containment — for benchmarks only'.
- enable disable still bypass the word list
- a bad boolean from the cli is a readable exit 1
- explain names the env layer as the winner
- explain exposes the bridge and the wired marker
- `harness flags` must stay usable on a machine with no config.json — where it is run first.
- harness uses exit 1 for the same usage error

### test_harness_runner_flags.py (25)

_run_pipeline's harness seams: the engine adapter, the resolved flags, and flags.json._

- Stages and fan-out engines must read the run's engine + toggles from ctx, never os.environ.
- flags json records what was in effect and which layer won
- A run that dies in stage one must still be reproducible from flags.json.
- Calling `run_pipeline(stages, ctx)` directly, with no flags or adapter, keeps working exactly.
- adapter runs stages that declare no engine
- A fan-out stage keeps its own engine and receives the adapter through ctx.engine.
- stage engines flag overrides per stage
- an engine without run stage fails with the fix
- disabled stages removes a stage and records the decision
- `b` requires `a`'s output; disabling `a` must fail b's requires check loudly.
- retries off fails on the first red gate
- retries on by default spends the stage budget
- green skip off reruns a green stage
- green skip on records the skip decision
- With cross-stage retry disabled, a red `b` never re-runs `a`.
- back jumps on by default reruns the earlier stage
- Revision mode keeps the previous outputs for targeted edits; with the flag off every attempt
- A warning is a courtesy; a stage must never fail to run because introspection did not work.
- A silent no-op would let an A/B believe a different engine ran the stage.
- Warning about a stage that HAS migrated would train operators to ignore the warning.
- The default IS claude-code, which is what an unmigrated call site binds — nothing to warn about.
- Every legacy caller and every hand-built RunContext keeps working: no adapter -> run_claude.
- The migrated call sites' contract: one EngineRequest per child, on the run's adapter.
- replay and pi have no StreamResult; the canonical summary is translated so the nine unmigrated
- engine stage falls back and routes

### test_harness_trace_postcondition.py (10)

_The canonical trace is a POSTCONDITION OF THE INVOCATION, not of the engine adapter._

- The regression: no adapter involved, and the trace must exist anyway.
- Two readings of one invocation must not disagree — the whole point of the translation.
- The claude-code adapter still writes the raw stream the ledger reads.
- Raw stream names are inconsistent across the call sites (`extract.stream.requote.jsonl`,
- A stage that did its work must not die because its accounting could not be written.
- write canonical trace returns none on failure
- Revision mode keeps the prior attempt's stream as witness evidence; its trace must survive too,
- A from-scratch regeneration must not leave traces witnessing reads the new run never made —
- When both exist, the recording carries the CANONICAL trace — it is the engine boundary, and a
- Recordings must remain makeable from runs that predate the canonical trace.

## tests/testing/ — 260 tests

### test_be_execute.py (20)

_The executor's assertion semantics._

- Generated suites express "exactly N slots" as an assertion that element N is null. Treating
- present but null also satisfies equals null
- an element that should be absent but is present still fails
- real contract drift still fails
- wrong value still fails
- happy path passes
- transport codes are never fail
- non json body is error not fail
- unmet precondition is blocked
- The CP contract documented "unchanged path/params" without listing them, so derived cases omitted
- a case that expects 400 still evaluates normally
- A mis-transcribed pattern used to raise re.error out of run_case, aborting the suite: 2026-08-26,
- an invalid pattern in a precondition is error too
- a valid pattern still judges normally
- parse curl extracts the envelope
- parse curl defaults to get and survives an unquotable line
- dig paths and misses
- A stale content-length from the captured curl would corrupt every request with a new body.
- The curl binary path and the request timeout are flags, not literals.
- a timeout is error with no http status

### test_be_spec.py (17)

_The spec deriver's step grammar. Every test below is one `check(...)`-style call with fixed expectations._

- canonical
- python ish form labs 29354 suite
- bare verb assert http 200 bare dotted path
- status code is transport never a body path
- prose field name must not become a field called the
- unsupported operator yields no assertion not a guess
- no route stated is dropped
- route but nothing checkable is dropped
- no numbered steps is dropped
- simple scalar params fold into the query string
- list valued params are dropped never guessed
- explicit query string in the path wins
- The generated TASK-29354 suite writes URLs this way. Stopping at the first `/` captured
- other escapes belong to the pattern and survive
- every documented verb parses
- `_status_only` is how derive() reports "passes on a 200 whatever the body says".
- require and assert land in different buckets

### test_evaluate_gate.py (14)

_The `evaluate` gate's schema half, driven directly._

- a well formed scorecard passes
- a markdown fence is rejected with the fix
- every dimension must be present
- a dimension outside 0 to 5 is rejected
- every one of the fourteen compliance checks must be present
- both presence checks must be present
- a status that is neither pass nor fail is rejected
- a missing dimension object is named
- coverage = mean(1-5), quality = mean(6-8), overall = mean of those two.
- quality is 14/3 = 4.6667 and overall (3.4 + 4.7) / 2 = 4.05 — both 4.0 and 4.1 are defensible
- "Treat 0 as a legitimate, expected score for a dimension that plainly doesn't apply".
- a missing mechanical file is reported
- The prompt quotes its counts, and the gate needs it whether or not the model stage succeeds.
- the prompt tells the model not to copy the mechanical file

### test_execute_be_stage.py (19)

_The `execute_be` stage: the run loop, the results file, and the exit semantics._

- every case is executed and recorded locally
- Results are written to both sinks: the run file and the store.
- `cohort_observed` is recorded only when a cohort path is configured. Absent flag -> the property is not
- a route map rewrite is applied and recorded
- A failed preflight creates no run and records nothing.
- Only ERROR is fatal. The question is reachability, not correctness.
- Nothing runnable means the suite is wrong, not the machine — so it is a gate failure.
- an empty spec is a gate failure
- a spec entry with no runnable case is skipped loudly
- a missing curl names what to pass
- a curl with no url is rejected
- missing env or build is rejected before any request
- dry run executes but writes nothing to the store
- Offline mode reads no store and fires no preflight.
- offline still validates the spec and the envelope
- the gate rejects a results row missing a field
- the gate rejects an unknown status
- the gate rejects a tally that does not match the rows
- It issues real requests with a captured identity, so it is opt-in.

### test_io_durability.py (22)

_Durability and concurrency for the workflow's artifacts: atomic writes, strict reads, locking._

- it writes the content and creates the parent
- The regression this exists for: `write_text` truncates first, so a crash mid-write published a
- A FIXED temp name let two invocations sharing a run dir interleave into one temp file — the exact
- os.replace alone only orders the rename; some filesystems commit it ahead of the data and publish
- a value that cannot be serialised does not truncate the old file
- an absent file and a torn file are reported differently
- an empty file reads as truncated not as wrong content
- A markdown fence or a preamble is a writer emitting the wrong thing, not damage — and the advice
- load json strict raises a gate failure naming the file
- `_build_prompt` runs before any gate and must not explode on a half-written inventory.
- read json gated names the torn file
- read json gated distinguishes the wrong shape
- One assertion per gate: a truncated input must come back as a failure string, never a traceback.
- the store writes through the atomic writer
- Before: `json.loads` raised out of `list_cases`, a traceback that repeated on every retry.
- a torn run file is also named
- locked serialises two threads
- `atomic_write_text` REPLACES the inode, so a lock held on the artifact itself would protect
- Blocker 5.2: unlocked, the later read-modify-write silently discarded the other's executions —
- Two invocations upserting different slices of one feature must union, not clobber.
- An execution for an unattached extid would sit in the run file reading as evidence for a case
- the lock sidecar is not mistaken for a run file

### test_json_store.py (21)

_The JSON case store: round-trip, the two attachment gates, and the no-network guarantee._

- it satisfies the protocol
- get store returns the json backend by default
- get store rejects an unknown backend
- upsert creates then updates in place
- The store_cases gate depends on this arithmetic.
- The same TC id under a different feature must never collide.
- an empty upsert reports an error rather than succeeding
- `requirement`, `qa_comment` and `type` are the three columns a store must not drop.
- list cases renders the step text the deriver reads
- list cases filters by status and tags
- a feature key with a slash cannot escape the store
- A run only holds CONFIRMED cases, and with require_tag only cases carrying that tag.
- an unclassified case is reported as having no exec tag
- extids restricts the run
- "Re-running the same build updates that run's executions instead of creating a second run".
- record execution stores the props and the detail
- recording against an unattached case raises
- recording against an unknown run raises
- health reports ok and no network
- Every socket constructor is poisoned for the duration: a backend that reached the network would
- `tcms_api` is imported lazily inside KiwiCaseStore._rpc, so the CLI can scan this workflow with

### test_kiwi_store.py (23)

_The Kiwi backend against a fake XML-RPC object._

- it satisfies the protocol
- There is no default password in code.
- the product is matched by exact prefixed name
- a legacy product created under another prefix still resolves
- `be_requirements.kiwi_cases` matched `feature in p["name"]`, so "RSE-1" resolved to
- the error names the flag that fixes it
- the product prefix is a flag
- upsert creates the product plan and cases
- upsert tags every case with exec and surface
- upsert matches an existing case on its extid within the feature
- `TestPlan.add_case` resets the status to NEED_UPDATE, so a final confirm pass is required or the
- the plan and run templates are flags
- an empty upsert reports an error
- list cases filters on status and tags
- list cases returns the case text the deriver reads
- The deriver is backend-agnostic: the same transcription off either store.
- create run applies the confirmed and tag gates
- the same summary reuses its run
- record execution sets the timestamps rather than actual duration
- a long property value is truncated
- recording for an unattached case raises
- an unknown status is rejected before any write
- health reports rather than raising

### test_mechanical.py (24)

_The evaluator's deterministic half._

- presence check passes on the clean suite
- an empty expected result is flagged
- a restated action is not an expected result
- an expected result copied from the steps is flagged
- implied setup without a precondition is flagged
- "A row that genuinely needs no setup is not a violation" (SKILL.md:28).
- Both sheets are in scope, "not just Test Cases" (SKILL.md:19).
- gate 1 passes when every case cites a requirement
- gate 1 flags the case with no reference
- gate 1 accepts a ticket key or a contract citation
- gate 2 passes on the clean suite
- gate 2 flags the empty field and the vague navigation
- gate 2 accepts a navigation step that names a deep link
- "...with no QA Comment explaining that the exact URL/route was unavailable" (gate 2's own wording).
- gate 2 flags a placeholder as well as a blank
- gate 2 rejects na in a field where it cannot be accurate
- gate 4 flags a declared surface no case covers
- gate 4 passes when every declared surface is covered
- gate 8 flags the identical title on the same surface
- "cases differing by surface or by distinct condition are NOT duplicates — do not flag those".
- the clean suite is a mechanical pass
- the sample suite fires every gate it was built to fire
- the scorecard counts are reported
- A gate failure becomes the model's guidance; "some case is wrong" is unactionable.

### test_pipeline.py (26)

_The whole workflow under the harness runner, with a fake engine._

- the default pipeline runs green end to end
- the python stages report themselves as python
- The SKILL.md is staged as the persona rather than named in a prompt for `Skill` to find, so no
- Parity with the run_chain.sh heredoc (:131-178), minus the storage call.
- a run without a contract says so in the prompt
- a second invocation skips every green stage
- a stage subset reruns exactly that stage
- The flow a plugin command drives: --stages sources, then --stages generate_cases, ...
- sources.json is the inputs digest for generate_cases, so editing a source must not leave a green
- a broken suite fails the generate gate after its retry
- revise_on_retry: the second attempt is a targeted edit, and the prompt carries the failures.
- an evaluator whose arithmetic is wrong fails the gate
- The whole point of computing it twice: a mismatch is surfaced, not resolved silently.
- an evaluator failing a check with no violations fails the gate
- an evaluator failing a check without naming an id fails the gate
- a compliance status that contradicts its checks fails the gate
- a missing source stops the run before any model is paid
- from cases skips the model but not the gate
- from cases reads a workbook with its execution column
- Slot compatibility: the pipeline cannot be composed with a hole in it.
- the workflow declares its flags and decisions
- every declared flag is registered
- compose pipeline defaults to the declared default
- compose pipeline honours the composition flags
- compose pipeline only ever names registered stages
- The flags and `--disable-stage` must end at the same place: a composed list is a valid pipeline.

### test_route_map.py (5)

_The route map. The rule: the map must MERGE query strings, never drop either side._

- route map parity
- A run without a route-map file returns the spec untouched rather than failing.
- no path and no mapping means no rewriting
- Every rewrite is reported so it is never silent — the stage turns each into a decision record.
- load map drops reviewer notes

### test_schema.py (23)

_The schema validators — every rule the generator SKILL states as prose, now checkable._

- well formed ids match
- malformed ids do not match
- The 14-code table is the vocabulary; a code outside it must fail the regex.
- type code maps scenario type names
- id segment helpers
- swap prefix round trips
- a case pointing at another scenario is a violation
- a blank mandatory field is a violation
- An explicit 'n/a' is populated; an empty cell is not. The schema check accepts 'n/a'.
- a missing key is named
- out of enum values are violations
- A scenario and its case always carry the same TYPE code — so must the `type` column.
- the clean suite passes every validator
- the sample suite fails the expected rules
- The SKILL's analytics-only exception is prose and cannot be detected mechanically, so it is a
- duplicate ids are caught
- a case linking to an unknown scenario is caught
- neg and edge are required per module
- an empty suite is a violation not a pass
- render text sections match kiwi writers shape
- case tags carry exec and surface
- Absent is not "runs by default" — it is "never runs and nobody notices".
- http cases selects only the executable subset

### test_secrets_and_tls.py (21)

_Credential handling and TLS scope._

- the password is declared a secret
- It is written to the Kiwi TestRun's `manager` field, where every account can already read it, and
- every declared secret is a flag this workflow registers
- A new `testing.*` flag whose name says credential must be added to SECRETS, not just registered.
- The password flag has no baked-in default.
- `_SECRET_SUPPORTED` is a probe, not an assumption: passing an unsupported kwarg would TypeError at
- the registered flag carries the secret marker
- `rfc_agent/runner.py` writes `flags.dump()` to `<run_dir>/flags.json` before any stage runs, and
- Redaction is for the serialised view only — the store must still be able to connect.
- a non secret flag is untouched
- the insecure context is unverified
- no context is built when verification stays on
- the context is attached to the transport
- Connecting with `insecure=True` must not leave `ssl._create_default_https_context` pointing at the
- the default is restored even when the connection raises
- a secure connection never touches the global default
- tcms-api connects inside its own constructor, so the context has to be reachable then too — the
- Which transport class tcms-api builds has moved between versions; failing to attach is a fallback,
- An operator reading `health()` must be able to see that verification is off.
- the flag help says it is insecure and how to stop being insecure
- Parity: flipping it would break every existing Kiwi deployment silently.

### test_stages_python.py (25)

_The python-only stages, each driven directly through its `Stage.engine` and then its gate._

- sources inventories an rfc run dir
- an explicit path overrides the rfc run dir
- the gate fails when the prd or rfc is missing
- an empty source file is blocked not present
- The operator needs to see the typo, not "no source given".
- a missing source is recorded as a decision
- store cases upserts and summarises
- store cases is idempotent
- store cases records the upsert decision
- dry run writes nothing to the store
- the gate catches arithmetic that does not add up
- the gate catches a store that holds fewer cases than the suite
- derive spec transcribes the stored http cases
- derive spec drops the unparseable case and records why
- derive spec flags a status only case as weak
- the gate says to fix the cases not the parser
- The tag that marks a case machine-executable is a flag: selecting on another tag needs no code edit.
- the render is skipped with a decision when openpyxl is absent
- the render is skipped when the flag is off
- `qa/<FEATURE>_QA_Suite.xlsx`, spaces to underscores (run_chain.sh:162).
- the render produces both sheets with the declared columns
- the gate catches a render that is not a faithful projection
- The `execution` column must survive a render and re-read: without it every imported case loses its
- scenarios round trip too
- The message must name the fix (`uv sync --extra testing`), per the harness's error convention.

## tests/parity/ — 18 tests

### test_differential.py (7)

_The same pipeline, two code paths, one manifest._

- manifests are equal modulo timestamps and durations
- Excluding a field from the comparison must not mean the field stopped being recorded.
- A manifest comparison is only meaningful if the artifacts match too.
- Three intended differences in the run-dir layout, and nothing else:
- flags json records the replay engine
- A recorded FAILURE must replay as the same failure — parity that only covers green runs would
- The recording format is a contract between `harness record` and the replay engine.

### test_manifest_golden.py (11)

_the manifest key set, the decision vocabulary and the run-dir layout are public surfaces._

- manifest key set per stage
- The two fields every consumer branches on.
- `cost_usd`/`duration_s` are LAST-ATTEMPT (the documented contract the eval harness reads);
- A recorded decision name that isn't in the enum doesn't exist (the decision-signals rule), and a
- recorded decisions are all in the vocabulary
- The file layout is this system's API (README). Every path a green run creates, pinned.
- stage outputs land at their declared paths
- The legacy call path resolves no flags and must not invent the file .
- An engine that reports NO cost must land `null` in the manifest, not 0.0.
- The convention has to hold through the replay engine too, or parity would launder a null into
- `run_totals` SUMS, so 0.0 is the honest answer once a costless stage has run — the null
