# `pi_ext` — the containment transport for the Pi engine

Pi ships **no permission system**. Its own docs say so three times, including a section titled "No Built-in
Sandbox": *"By default, it runs with the permissions of the user and process that launched it."* The single
interception point is the extension event `pi.on("tool_call")`, which fires after `tool_execution_start`
and before the tool runs, can block, can mutate the tool input, and **fails safe** — a handler that throws
blocks the call.

`harness-guard.js` is that handler. It is the Pi counterpart of the Claude adapter's `PreToolUse` hooks
(D6): the **policy** lives once, in Python (`harness.policy`), and each engine only transports it.

```
pi child (Node)                          harness (Python)
  tool_call event
     └─ harness-guard.js  ──spawnSync──▶  python -m harness.policy check
            ▲                                  │  {"decision": "...", "reason": "..."}
            └──────────── verdict ◀────────────┘
     block: true + reason            ──▶  logs/<label>.denials.jsonl  ──▶  pi_translate ──▶ DENIAL event
```

## Files

| File | Role |
|---|---|
| `harness-guard.js` | the whole guard: tool-name mapping, request building, verdict mapping, denial recording. Plain ESM, no dependencies, no build step. |
| `harness-guard.ts` | two-line re-export, kept as a fallback. pi discovers extensions as `*.ts`, but `-e <path>.js` **does** load at 0.85.1 (measured — see below), so the `.js` is what the adapter passes. Point `--set pi.guard_path=<abs>/harness-guard.ts` at the shim if that ever changes. |
| `package.json` | declares `"type": "module"` only, so plain `node` can import the guard (which is how the tests exercise `decide()`). No dependencies, ever. |

## The policy contract

The guard writes one JSON object to the policy CLI's stdin and reads one back from stdout. The CLI always
exits 0; the verdict channel is stdout, because a policy check that crashes a run is worse than one that
says "ask".

```json
{ "tool": "Write", "native_tool": "write", "input": {"path": "/abs/resolved/path"},
  "cwd": "/abs/run/dir", "writable_roots": ["/abs/run/dir"], "readable_roots": ["/abs/repos"],
  "readonly_shell": true }
```
```json
{ "decision": "allow" | "deny" | "ask", "reason": "..." }
```

Environment, all set by `harness/engines/pi.py`:

| Var | Meaning |
|---|---|
| `HARNESS_POLICY_CMD` | JSON array argv, e.g. `["/abs/venv/bin/python","-m","harness.policy","check"]`. The interpreter path is explicit because pi's Node host inherits no virtualenv. |
| `HARNESS_POLICY_JSON` | `{cwd, writable_roots, readable_roots, readonly_shell, disabled_tools}` plus, when declared, `{broker_hint, repos_root, exempt_marker}`. Those three are what the Claude transport also passes: without them the policy's teaching strings interpolate an empty broker path and a literal `<repos_root>`, and a pinned broker stops being exempt — so a bash stage that works on `claude-code` has its sanctioned alternative denied on pi. **The kernel cannot derive them** (naming a workflow's broker script inside `harness/` is the D2/D3 layering violation the layering test pins), so a workflow declares them: export `HARNESS_POLICY_BROKER_HINT`, `HARNESS_POLICY_REPOS_ROOT`, `HARNESS_POLICY_EXEMPT_MARKER` for its stages, or set the `pi.policy_extra` flag (which wins). Until one of those is set, the policy simply has no exemption to apply. |
| `HARNESS_DENIALS_PATH` | JSONL file the guard appends one record per block to |
| `HARNESS_DENIALS_ECHO` | `"1"` also `console.log`s the record. **pi routes extension console output to stderr in `--mode json`** (measured), so the echo lands in `logs/<label>.pi.stderr`, never in the event stream. Flag `pi.denials_echo`. |

### Tool-name mapping

`harness.policy` routes on engine-neutral, Claude-spelled names, so the guard translates first. The
mapping is a decision, not a detail:

| Pi tool | Sent as | Why |
|---|---|---|
| `write`, `edit` | `Write`, `Edit` | path confinement against `writable_roots` |
| `read` | `Read` | read confinement against `readable_roots` + `writable_roots` |
| `grep`, `find`, `ls` | `Read` | what the policy must decide about a search is whether the root it walks is readable. With no root in the input, the guard substitutes the cwd, which is the root pi would actually walk. |
| `bash`, `powershell` | `Bash` | mutation/egress deny-list, then the read-only classifier |
| anything else | its own native name | the policy answers "no policy rule for tool X" (`ask`), which the guard turns into a block — **default-deny for unrecognised tools, deliberately** |

### Verdict mapping

| Verdict | Guard | Why |
|---|---|---|
| `allow` | proceed | provably safe |
| `deny` | block, reason relayed **verbatim** | the reason strings are tuned prompt-facing text that teaches the sanctioned alternative in one turn; paraphrasing costs denial round-trips |
| `ask` | **block**, with "nobody to ask" appended | `--mode json` sets `ctx.hasUI === false`; there is no operator to prompt, so no opinion is a denial |
| `ask` on a read-family tool **when no `readable_roots` were declared** | proceed | read confinement the harness never declared is not the guard's to invent. The policy answers "not enforced" here and the Claude transport defers to the engine's own `--add-dir`; blocking made a pi stage unable to read anything outside the run dir under an identical policy. Decided from the envelope, never from the reason string. |
| any verdict carrying `enforced: false` | proceed | an explicit "I am not enforcing this class" is an allowance. The policy does not send this field today; honouring it means the day it does, pi stops inventing a denial. |
| `ask` on `bash`/`powershell` **when `readonly_shell` is false** | proceed | the harness explicitly switched the read-only classifier off for this stage. `ask` then means "the mutation deny-list passed and I was told not to classify" — the same allowance a Claude run gets with the approver hook off. This is the one place the guard interprets rather than obeys a verdict. |
| the policy call throws | **block** | fail safe. pi already blocks on a throwing handler; the guard makes the reason explicit instead of relying on that. |

## Preflight

`run()` sends one probe request through the policy CLI before it spawns pi, and fails with a named
`EngineFailure` if no verdict comes back. This exists because the guard **fails safe**: an unreachable
policy CLI otherwise presents as "the model mysteriously could not use any tool", visible only as a pile of
DENIAL events after a full stage of spend. The usual cause is `harness` not being importable by that
interpreter — a relative `PYTHONPATH` like `src` resolves against the guard's cwd, which is the run dir, so
the adapter absolutises every relative entry before handing the environment over.

## Path handling

Every path is `path.resolve(cwd, p)`d before the policy sees it, and the Python side resolves again
(`Path.resolve()`, which also follows symlinks). Pi's own shipped `protected-paths.ts` example uses
`path.includes(".env")`; that is a convenience filter, not a boundary — `../` traversal, symlinks and
relative paths all defeat substring matching. A test pins the absence of `.includes(p)` in this file.

Decisions are a pure function of `(toolName, input)`. Nothing consults sibling tool results, because pi
preflights siblings from one assistant message sequentially and then executes them **concurrently**: a
guard that reasoned across a batch would be unsound by construction.

## Running a stage on Pi

```bash
harness doctor --engine pi                      # pi on PATH, version, Node version, credential presence
harness run rfc --engine pi --set pi.provider=anthropic --set model=claude-sonnet-5
harness run rfc --stage-engine review=pi        # one stage on pi, the rest on claude-code
```

The invocation the adapter builds, for reference:

```
pi --mode json -p --no-approve --no-skills --no-prompt-templates --no-themes --no-context-files \
   --no-extensions -e <this dir>/harness-guard.js \
   [--provider …] [--model …] [--thinking …] \
   [--system-prompt <stage>/SKILL.md] [--append-system-prompt logs/<label>.append-system.md] \
   [--tools read,grep,find] [--exclude-tools bash] \
   --session-dir <run>/logs/pi-sessions --session-id <uuid we minted> --name <label> \
   -- "<kickoff prompt>"
```

with `cwd=<run dir>`, `PI_CODING_AGENT_DIR=<run>/.pi-home` (private config root, `compaction.enabled:
false`), `PI_OFFLINE=1`, `PI_TELEMETRY=0`, and stdout/stderr on **separate** pipes — pi writes non-JSON
diagnostics to stderr and merging them corrupts the JSONL parse.

## Gaps versus the Claude adapter

| Capability | Pi | What the adapter does |
|---|---|---|
| **Resume** | better than Claude's. `--session <path\|partial-id>` continues headlessly; `--session-id` lets us mint the id **before** launch; `--fork` copies a session, leaving the original immutable. (`-r`/`--resume` is an interactive picker — never used.) | `supports_resume()` is True. A resume passes `--session <path to the saved session file>`, whose path is derivable because the id was minted with `--session-id` on the first pass. If that file is gone the run falls back COLD with the same id (`--session-id`) and a `LOG` event saying the previous context is not carried over. `--fork` for revision passes is not wired yet. |
| **MCP** | absent by design ("No MCP. Build CLI tools with READMEs, or build an extension"). No MCP extension ships upstream. | A stage whose `Containment.mcp_servers` is non-empty is **refused before any spend** with a `PipelineConfigError` naming the stage, the servers, and the `stage_engines` override that fixes it. MCP tool names are dropped from the tool lists with a `LOG` event. Fetch remote inputs in the harness before the stage and hand it local files — better for both engines: identical ingest, cacheable and replayable. |
| **Steering** | none. `--mode json` has no stdin channel for user turns, and extension UI is a no-op (`ctx.hasUI === false`). | `EngineRequest.steer_epoch` is dropped, with a `LOG` event saying so. The upgrade path, if a stage ever must ask a human, is `--mode rpc`, which has a full Extension UI Protocol. |
| **`--add-dir`** | no flag, and no `--cwd` either. | cwd comes from `Popen(cwd=…)`. `readable_roots` are enforced by **this guard**, not by pi: pi never restricted paths, so extra roots were already reachable — the grant is free and the guard is what has to be written. |
| **Permission modes** (`acceptEdits`, `plan`) | none. | Policy is this guard, which is more explicit and more testable than a mode enum. A "plan mode" stage is just `--tools read,grep,find,ls`. |
| **Hooks by name** | no named hook system. | `Containment.disabled_hooks` has no equivalent and is logged as ignored; the guard transports one policy check. `hooks_enabled=False` (run flag `hooks`) unloads the guard entirely, which removes **all** containment. |
| **`allowed-tools` in SKILL.md frontmatter** | parsed and silently ignored (pi keeps `name`, `description`, `disable-model-invocation`). | Non-issue for our stages: all five RFC stage skills use only `name` and `description`. Tool restriction stays in the runner, via `--tools`. |
| **Terminal cost event** | none. Per-message `Usage.cost` is richer (per-category, cache split, plus tool-nested usage) but nobody sums it. | Summed from the session JSONL after exit, with the in-stream sum as fallback. The RESULT event carries the per-category breakdown in `extra.cost` (input / output / cacheRead / cacheWrite) and names the source in `extra.cost_source` (`session_file`, `event_stream`, or `none`). When NO message reported a cost, `cost_usd` is **absent (None), never 0.0** — an unmeasured stage must not look free. A provider that really reported `{total: 0}` still reports 0.0. |
| **Non-zero exit on failure** | broken: `--mode json` exits 0 even on agent error or abort. | Success is derived from the stream (`agent_settled` terminal, `stopReason`, `auto_retry_end.success`, `compaction_end.aborted`), never from the exit code. |
| **Unknown-flag rejection** | none: unknown flags are collected for extensions, so a typo silently no-ops. | `_assert_known_flags` refuses any flag not pinned in `KNOWN_FLAGS`, and an unknown `--thinking` level is refused rather than passed. |
| **OS sandbox** | none, by pi's own choice ("real isolation needs to come from the operating system or a virtualization/container boundary"). | Unchanged from Claude: this guard is defence in depth, not a sandbox. Pair it with a container in CI (DEFERRED #15 stands). |

## Not verified at 0.85.1

Everything below is read from source by the research report unless marked resolved. There is no API key on
this machine, so **no live agent turn has ever run** — the live probes below used `npx` with an invalid key
and only exercised pi's startup path.

1. ~~Whether `-e` accepts a `.js` path.~~ **Resolved live, 2026-09-08:** it does. A probe extension's
   factory ran under `npx @earendil-works/pi-coding-agent@0.85.1 --mode json -p --no-extensions -e
   probe.js`. `harness-guard.ts` stays as a cheap fallback.
2. ~~Whether an extension's `console.log` lands in the `--mode json` stream.~~ **Resolved live, and the
   answer is no:** the probe's `console.log` line appeared on **stderr**, with stdout untouched. pi keeps
   stdout pure JSONL. The denials side file is therefore the only channel, not merely the safer one.
3. **The thinking-content block's field name** (`thinking` vs `text`) on the wire. The translator accepts
   both.
3b. **The session-format version.** `SESSION_VERSION = 3` is what the translator was written against. A
   different value in the header produces a loud `LOG` event rather than a failure, because the parse may
   still be correct and the session file is only load-bearing for the cost sum.
4. **`--system-prompt` reading a file path** is an emergent property of pi's shared `resolvePromptInput`
   helper rather than a documented promise. It works at 0.85.1; an upstream change would break it loudly
   (the prompt would arrive as a literal path string), not silently.
5. **`tool_execution_end.result.details.paths`** — the shape the translator reads for touched paths is
   inferred from the session format's `details?: any`; when absent, paths still come from `tool_start`.

### Measured live on 2026-09-08 (npx, invalid key, no agent turn)

| Probe | Result |
|---|---|
| `npx -y @earendil-works/pi-coding-agent@0.85.1 --version` | `0.85.1`. Runs on Node 22.14 despite `engines.node >= 22.19.0`; npm only warns `EBADENGINE`. `doctor()` still reports the old Node, because a warning is not a guarantee. |
| `-e <abs>/probe.js` with `--mode json -p --no-extensions` | the extension factory ran — a plain `.js` extension loads. |
| a `console.log` from that extension | arrived on **stderr**; stdout stayed empty. |
| the `{"type":"session",...}` header as line 1 | **not** observed: with an invalid API key pi produced no stdout at all before it was killed at 120 s, so the header may be written after provider setup rather than at process start. The translator tolerates a missing header for exactly this reason. |

## rfc-tools.js (axon only)

Loaded next to the guard by the `axon` adapter and by rfc-agent's Axon backend. Configured by one 0600 JSON
file named in `RFC_TOOLS_CONFIG`, it does three things:

- registers the stage's MCP servers for the one session (`pi.registerMcpServer`, `direct` exposure, so the
  tools keep their `mcp__<server>__<tool>` names);
- runs rfc-agent's `investigate/hook.py` with a Claude-shaped payload: `pre` on write/edit `tool_call`
  (exit 2 blocks with the hook's message), `post` on `tool_result` (its `additionalContext` is appended to
  the result);
- cancels Axon's threshold compaction unless the config allows it.

Failure policy matches the guard. A write check that can't run blocks the write; a post check that can't run
adds nothing. `tests/fixtures/axon/rfc_tools_cases.mjs` drives it under plain `node`, including once against
the real `hook.py`.
