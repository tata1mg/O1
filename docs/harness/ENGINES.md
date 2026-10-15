# Engines: selecting, running and adding them

An **engine** runs one headless agent invocation under a containment policy and writes a canonical trace. It
knows nothing about what a stage means. Three ship: `claude-code` (default), `pi`, `replay`.

---

## 1. Selecting an engine

Selection is by name, through flags. Never a code edit.

```bash
harness run testing --feature F                                       # default: claude-code
harness run testing --feature F --engine pi                           # whole run on pi
harness run testing --feature F --engine pi --stage-engine evaluate=claude-code
harness run testing --feature F --engine replay --set replay.recording=<path>   # offline: parity and benchmarks
harness engines                                                       # list, with each one's doctor verdict
harness engines --json                                                # the same, for an agent
```

Precedence: `stage_engines[<stage>]` beats `engine` beats the default. `flags.json` in the run directory
records which engine each stage actually used, so a finished run says what ran it.

**Per-stage overrides are the intended tool, not a workaround.** A cheap engine can run fan-out shards while a
strong one writes the deliverable; and a stage that needs a capability one engine lacks — MCP servers, in
practice — pins itself to the engine that has it.

---

## 2. `claude-code`

The `claude -p` primitive wrapped as an adapter. It is the default, and the engine the rest of the test suite
is built around.

### Prerequisites

| Requirement | Check | Fix |
|---|---|---|
| the `claude` binary | `claude --version` | install or update Claude Code |
| a login | `claude` runs interactively without prompting for auth | `claude` and log in, or set `AGENT.CLAUDE_CONFIG_DIR`, or provide an OAuth token file |
| MCP entries for every server any stage names | `harness doctor --workflow <name>` | add the entry to config; placeholders are accepted at materialise time |

`harness doctor` runs all three plus the hook canary.

The **hook canary** spawns one real `claude -p --model claude-haiku-4-5-20251001` the first time on a given CLI
version (about 20–40 s) to prove that `additionalContext` from a PostToolUse hook actually reaches the model.
Success caches in `~/.cache/rfc-agent/hooks_alive/<version>.json`. It never blocks a run: on failure it warns
and records a `hook_canary` decision. Disable it with `--disable hook_canary`.

### What the adapter does under the hood

1. **Stages the persona.** Copies the stage's `SKILL.md` (or `SKILL.<variant>.md`) into
   `<run_dir>/.claude/skills/<stage>/` so the CLI discovers exactly one skill.
2. **Materialises containment into settings.** Writes `logs/<label>.settings.json` with the four hook entries
   and the read-only `permissions.allow` prefix rules, and passes `--settings` plus `--setting-sources project`
   so the operator's own user-scope hooks and `CLAUDE.md` never load.
3. **Builds argv** from `Containment`: `--allowedTools` for MCP read grants, `--disallowed-tools` for the deny
   list, `--strict-mcp-config` unless the stage sets `inherit_user_mcp`, `--add-dir` for extra readable roots,
   `--permission-mode acceptEdits`, `--model`, `--max-budget-usd` when set, and the effort level.
4. **Spawns and streams.** `--output-format stream-json`, writes `logs/<label>.stream.jsonl` raw, translates
   each event into `logs/<label>.events.jsonl`, and tails both into `logs/activity.log` when `live_feed` is on.
5. **Waits with a timeout** (`stage.timeout_s`, else flag `timeout_s`, default 2400 s), killing the process
   group on expiry. There is deliberately **no per-stage cost cap by default**: runaway work is bounded by the
   timeout, and `--max-budget-usd` is a hard kill that leaves a stage dead mid-work, so it is opt-in.
6. **Resumes when asked.** On a revision with `resume_sessions` on, passes `--resume <session_id>` from
   `logs/<stage>.session` if the saved context is under the resume cap; otherwise runs cold and records
   `resume_skipped`.

### Known gaps and how they degrade

| Gap | Behaviour |
|---|---|
| `AskUserQuestion` in a non-interactive run | Auto-denied by the CLI headless; the question lands in `logs/asks_skipped.jsonl` for the operator relay |
| MCP servers that need OAuth | A materialised server definition has no stored token headless. `inherit_user_mcp` drops `--strict-mcp-config` so the operator's already-authed servers load; inherited servers stay uncallable unless their tools are in the read-only allowlist |
| Inherited MCP write tools | Present but not explicitly denied: containment there is the CLI's headless auto-deny only |
| Bash containment | A deny-list over a read-only allow-list, not a sandbox |

---

## 3. `pi`

A `pi --mode json` subprocess adapter. It is a structural match for the loop the harness already runs (spawn a
child, read JSONL off stdout, parse events into a ledger), so adapting is a parser swap rather than an
architecture change.

### Prerequisites

| Requirement | Value | Note |
|---|---|---|
| Node | `>= 22.19.0` (flag `pi.min_node`) | Every Pi package declares it |
| install | `npm install -g --ignore-scripts @earendil-works/pi-coding-agent` or `curl -fsSL https://pi.dev/install.sh \| sh` | `--ignore-scripts` is the documented default; Pi needs no lifecycle scripts |
| **npm scope** | `@earendil-works/*` | The scope moved from `@mariozechner/*`; pinning the old one installs an abandoned pre-rename snapshot |
| **repo** | `earendil-works/pi` | |
| auth | a provider API key | **API keys, not OAuth.** `/login` is interactive-only and needs a browser loopback callback |
| version pin | `>= 0.85.0` (flag `pi.min_version`) | `doctor()` reports anything older |
| binary | flag `pi.bin` (default `pi`) | Accepts an absolute path; `doctor()` checks `PATH` and executability |

`harness doctor` checks four things for Pi and names the fix for each: the binary is findable, its `--version`
meets `pi.min_version`, Node meets `pi.min_node`, and **some** provider credential is reachable.

That last check is deliberately more than "does `auth.json` exist": Pi creates it as an empty `{}` on its first
run, so a file-existence test would report a healthy run that then fails at the first provider call. The check
requires a non-empty credential map, or one of `ANTHROPIC_API_KEY`, `ANTHROPIC_OAUTH_TOKEN`, `OPENAI_API_KEY`,
`GEMINI_API_KEY`, `OPENROUTER_API_KEY` in the environment. **Credential values are never read or printed**,
only counted.

**Providers.** About 35 API-key providers, each with an env var, plus local models via Ollama, LM Studio, vLLM
and llama.cpp through `models.json`. The canonical list is `packages/ai/src/env-api-keys.ts` in the Pi source.

**Two auth footguns the adapter closes for you.** Pi's resolution order is CLI `--api-key` > `auth.json` >
environment, so **`auth.json` beats the environment** and a stale file silently overrides the key the harness
exports. And the `auth.json` `key` field supports a leading `!` that executes a shell command, which is a
secret-exfiltration surface. The adapter gives every run a private `PI_CODING_AGENT_DIR` inside the run
directory, so no ambient `auth.json` is ever read.

**Commercial note.** A Claude Pro/Max subscription used through Pi is **billed per token, not against plan
limits**.

### What the adapter does under the hood

Source of truth: `src/harness/engines/pi.py`, `pi_translate.py` and `pi_ext/README.md`. The invocation, with
every flag load-bearing:

```
PI_CODING_AGENT_DIR=<run_dir>/.pi-home       # flag pi.private_home
PI_CODING_AGENT_SESSION_DIR=<session dir>    # belt and braces; --session-dir wins
PI_OFFLINE=1  PI_SKIP_VERSION_CHECK=1        # flag pi.offline
PI_TELEMETRY=0                               # flag pi.telemetry (default off)
HARNESS_POLICY_CMD / _JSON / _DENIALS_PATH   # the guard's contract with harness.policy
<PROVIDER>_API_KEY=...

pi --mode json -p \
   --no-approve --no-skills --no-prompt-templates --no-themes --no-context-files \
   --no-extensions -e <harness>/engines/pi_ext/harness-guard.js \
   --provider <p> --model <pinned-id> --thinking <level> \
   --system-prompt <stage_dir>/SKILL.md \
   --tools read,grep,find,ls,write \
   --session-dir <run_dir>/logs/pi-sessions \
   --session-id <uuid the harness minted>          # or --session <saved file> on a resume \
   --name "<label>" \
   -- "<stage prompt>"
```

with `cwd=<run_dir>`, `start_new_session=True` (its own process group, so a timeout kills the bash tool's
grandchildren too), and **stdout and stderr on separate pipes**, because Pi writes non-JSON diagnostics to
stderr and merging them corrupts the JSONL parse.

Side files, all under `<run_dir>/logs/`: `<label>.pi.jsonl` (raw native stream), `<label>.pi.stderr`,
`<label>.denials.jsonl` (the guard appends, the translator tails), `<label>.append-system.md` when a
system-prompt append is set, and the canonical `<label>.events.jsonl`.

Why each piece:

- `--mode json -p` — JSONL event stream, non-interactive.
- The five hermetic flags (`pi.hermetic`, default on). `--no-approve` is the load-bearing one: non-interactive
  modes never prompt for project trust, and without it a repo under analysis could inject its own
  `.pi/SYSTEM.md` into the stage.
- `--no-extensions -e …/harness-guard.js` — discovery off, exactly one extension: ours. A CLI `-e` extension
  loads **before** the trust decision, so the guard is active even in an untrusted repo.
- `--system-prompt <path>` — passed as a **file path**, not inline text, which avoids any argv-size limit on a
  large `SKILL.md`.
- `--session-dir` + `--session-id` — the harness mints the id before launch, so the session path is known
  without parsing anything, and nothing lands in the operator's `~/.pi`.
- `--` always, so a prompt beginning with `-` cannot be parsed as flags.

**Provider default is a trap.** Pi's own default provider is **google**, so leaving both `pi.provider` and the
model empty does *not* give you Anthropic. Set one.

**Compaction is off by default** (`pi.compaction`), written into `<run_dir>/.pi-home/settings.json` because Pi
exposes compaction only through settings, never a flag. Compaction is lossy, and a stage that silently
summarises its evidence away makes every trace-backed gate lie; off means an overflow fails loudly instead.

Reading the result:

1. **Line 1 of stdout is normally the session header** `{"type":"session","version":3,...}`. If its `version`
   differs, the translator emits a loud `LOG` event and proceeds. A stream that starts with no header at all is
   also tolerated.
2. **`agent_settled` is the terminal event**, not `agent_end`. A stream that ends without `agent_settled` is
   recorded as a failure (truncated, crashed, or killed).
3. **Tool events**: `tool_execution_start` carries full `args`, `tool_execution_end` the `result`, keyed by
   `toolCallId`. Never read tool arguments from `message_update`.
4. **Cost** is summed from the session JSONL after exit, with the in-stream sum as fallback.
5. **Denials** come from the guard's JSONL side file, not the event stream, and become canonical `DENIAL`
   events.

**Every known non-equivalence with the Claude adapter is recorded in the trace as a `LOG` event**: missing
`writable_roots`, dropped MCP tool names, `readable_roots` being guard-enforced rather than engine-enforced,
the guard being off, `disabled_hooks` having no equivalent, steering being dropped, hermetic mode being off,
and a cold-fallback resume.

### Known gaps and how they degrade

| Gap | Degradation | What you see |
|---|---|---|
| **No MCP at all** | A stage whose `Containment.mcp_servers` is non-empty is **refused before any model spend**, naming the stage, the servers and the override that fixes it | `PipelineConfigError`, exit 1: "…the `pi` engine has no MCP support… pin the stage to an engine that has MCP: `--stage-engine <stage>=claude-code`". MCP tool names are also dropped from the tool lists with a `LOG` event |
| **`--mode json` exits 0 even on agent error or abort** | The adapter **ignores the exit code** and derives success from the stream: `agent_settled` terminal, `stopReason`, `auto_retry_end.success`, `compaction_end.aborted` | `EngineFailure`, exit 3, naming the stream signal |
| **Unknown flags are silently ignored**, not rejected | `_assert_known_flags` validates the adapter's own argv against `KNOWN_FLAGS` (a frozenset pinned to the supported version) **before launch**. An unknown `--thinking` value is refused the same way | `EngineFailure` naming the offending flag, rather than a silently unhermetic run |
| **No terminal cost event** | Summed from the session JSONL after exit, in-stream sum as fallback. The RESULT event carries the per-category breakdown in `extra.cost` and its origin in `extra.cost_source` (`session_file`, `event_stream`, or `none`) | When **no** message reported a cost, `cost_usd` is **`None`, never `0.0`**: an unmeasured stage must not look free |
| **Reading back Pi's effective tool set** | Not implemented. `pi.getAllTools()` exists in Pi's extension API, but its lifecycle timing is unverified | The tool allowlist is trusted as passed. Argv validation catches a misspelled *flag*; it cannot catch Pi resolving `--tools` differently than intended |
| **No `--add-dir`, no `--cwd`** | cwd via `Popen(cwd=…)`. `readable_roots` are enforced by **the guard**, not by Pi | A `LOG` event saying so |
| **No permission system, no plan mode** | Policy is entirely the guard extension. A read-only stage is `--tools read,grep,find,ls` | — |
| **Hooks by name** | `Containment.disabled_hooks` has no equivalent. `hooks_enabled=False` (flag `hooks`) unloads the guard entirely, which removes **all** containment | A `LOG` event saying it was ignored |
| **No mid-run steering** (`ctx.hasUI === false`) | `EngineRequest.steer_epoch` is dropped | A `LOG` event. If a stage must ever ask a human, the upgrade is `--mode rpc` |
| **A thrown guard is silent on stdout** | stderr is captured and scanned for `Extension error`; a hit fails the stage. Without this, a broken guard presents as tools mysteriously ceasing to work | `EngineFailure`: "tools were failing safe to BLOCKED; treat this stage as unrun" |
| **Hard budget kill** | No Pi flag; enforced harness-side by killing the process group when the in-stream cost sum crosses `max_budget_usd` | `EngineFailure` naming it as **best-effort** |

**Resume works.** `supports_resume()` is `True`. A resume passes **`--session <path to the saved session
file>`**, whose path is derivable because the id was minted with `--session-id` on the first pass. **If that
file is gone, the run falls back cold under the same `--session-id`**, with a `LOG` event, not a crash. Pi's
`-r`/`--resume` is an interactive picker and is never used. `--fork` is not wired.

**Skills are portable unchanged.** Pi implements the same Agent Skills standard, and stage skills use only
`name` and `description`, the subset Pi's frontmatter parser keeps. Pi silently ignores `allowed-tools`
frontmatter, which is fine because tool restriction lives in the runner.

**Parallel invocations are safe.** `settings.json`, `auth.json` and `trust.json` are `proper-lockfile`
protected upstream, and session files are created with the exclusive `wx` flag. The adapter mints a unique id
per child anyway.

### The guard extension

`src/harness/engines/pi_ext/harness-guard.js` — plain ESM, **no dependencies and no build step**. Pi loads a
`.js` extension via `-e <abs path>`. A `harness-guard.ts` two-line re-export ships beside it as a fallback;
point `--set pi.guard_path=<abs>/harness-guard.ts` at it if needed.

The policy is not reimplemented in TypeScript. The handler writes one JSON request to
`python -m harness.policy check` and reads one verdict back, because two copies of a containment classifier
would drift.

```
pi child (Node)                          harness (Python)
  tool_call event
     └─ harness-guard.js  ──spawnSync──▶  python -m harness.policy check
            ▲                                  │  {"decision": ..., "reason": ...}
            └──────────── verdict ◀────────────┘
     block: true + reason            ──▶  logs/<label>.denials.jsonl ──▶ DENIAL events
```

Four decisions in it worth knowing:

1. **Verdicts map conservatively.** `allow` proceeds; `deny` blocks and relays the policy's reason
   **verbatim**, because those strings teach the sanctioned alternative in one turn; `ask` **blocks** with
   "nobody to ask" appended, since `--mode json` has no operator. The single exception is `ask` on `bash` when
   `readonly_shell` is false: the harness switched the classifier off, so that proceeds.
2. **Unrecognised tools are default-denied.**
3. **Paths are resolved, not substring-matched.** `path.resolve(cwd, p)` in the guard and `Path.resolve()`
   again in Python, which also follows symlinks.
4. **Failures block.**

Two constraints inherited from Pi's execution model. Sibling tool calls in one assistant message are
preflighted sequentially and then executed **concurrently**, so every verdict is a pure function of
`(toolName, input)`. And extension `console.log` output goes to **stderr** in `--mode json`, so the denials
JSONL is the only usable channel; `pi.denials_echo` adds an echo that lands in `<label>.pi.stderr`.

**The guard is not a sandbox.** Bash deny-listing is unsound in principle (`eval`, base64, `$IFS`, variable
indirection), and Pi has no OS-level fallback. Pair the guard with a container when isolation matters.

---

## 4. `replay`

Re-emits a recorded canonical trace and writes a fixture output tree. Three jobs and no others: the
differential parity run, the structural benchmarks, and offline tests of runner behaviour.

```bash
harness record <run_dir> <recording_dir>                                # build a recording from a finished run
harness run testing --feature F --engine replay --set replay.recording=<recording_dir> --run-dir /tmp/x
```

It implements the same `Engine` protocol as the real adapters, which is the point: a bespoke fake does not
catch a protocol change.

---

## 5. Adding an adapter

Five steps.

**1. Implement the protocol** (`src/harness/engines/<name>.py`).

```python
class MyEngine:
    name = "my-engine"

    def run(self, req: EngineRequest) -> EngineResult: ...
    def supports_resume(self) -> bool: ...
    def doctor(self) -> list[str]: ...   # [] = healthy
```

`run` must, without exception: write **every** output the stage declared; write the canonical trace to
`req.trace_path`; and return an `EngineResult` whose `summary` is a `TraceSummary`. Honour `req.containment`:
if your engine cannot express part of it, **fail loudly rather than silently running uncontained**. That is
the rule the MCP-on-Pi refusal follows.

**2. Register it.** Add `"my-engine": "harness.engines.my_engine:MyEngine"` to `_BUILTIN` in
`engines/__init__.py`, or call `register(name, factory)` for an out-of-tree adapter. Resolution is lazy, so
importing the kernel must not import your dependencies.

**3. Write the translator.** Your engine's native events → the ten canonical types, with tool names mapped to
`ToolKind`. The two hazards, both learned from Pi: identify the **true terminal event** (not merely the first
"done"-looking one), and identify where **authoritative** tool arguments and results live (not a delta
stream). Write the translator as a pure function from native events to `Event` objects, so it is testable
against a recorded fixture with no subprocess.

**4. Add `doctor()` findings.** Binary present, version against the pin, auth configured, runtime
prerequisites. Each finding names the fix. `harness doctor` surfaces them, and a run refuses to start on a hard
finding rather than failing 40 minutes in.

**5. Write the fake-binary test.** A stub executable that emits recorded native events, driven through one
real stage. This is how both shipped adapters are verified: the Claude pattern is `tests/infra/test_steer.py` (a stub
Python executable), and Pi's is `tests/fixtures/pi/fake_pi.py` driving `tests/harness/test_engine_pi.py`.
`tests/harness/test_engine_pi_translate.py` covers the translator as a pure function over recorded events, and
`tests/harness/test_engine_pi_guard.py` exercises the guard's `decide()` under plain `node`. Assert the canonical
trace field by field, the `StageResult` accounting, the declared outputs on disk, and the failure path (a stub
that errors must produce `EngineFailure`).

Then add the adapter's row, including its gaps, to this document. An adapter whose gaps are undocumented will
be discovered by an operator at 2 a.m., which is the expensive way to find them.
