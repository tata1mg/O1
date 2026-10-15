# Harness

The harness is the agent-agnostic, workflow-agnostic kernel in `src/harness/`. It owns the stage lifecycle,
engine adapters, the containment policy, feature flags and the canonical trace format. A **workflow** in
`src/workflows/<name>/` owns only its stages, gates and vocabulary. One workflow ships today: `testing` (QA
suite generation, evaluation, storage and backend execution — see
[src/workflows/testing/README.md](../../src/workflows/testing/README.md)).

The RFC pipeline (`rfc`, Scope → Summarize; see [../HLD.md](../HLD.md)) is a separate package, `rfc_agent`. It
shares some modules with the harness (the run loop, the ledger and observability live in `rfc_agent` and the
harness calls them through documented seams) but does not run as a harness workflow.

```
$ uv run harness --help
usage: harness [-h] {run,flags,engines,workflows,doctor,record} ...
```

| Document | One line |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | The shape: layers, the run directory as the API, the stage lifecycle, canonical events, containment, flags, observability, and how to add a workflow. |
| [ENGINES.md](ENGINES.md) | Operator guide to engines: selecting one, what each needs, what each adapter does under the hood, known gaps, and how to add an adapter. |
| [BENCHMARKS.md](BENCHMARKS.md) | What the benchmark suites measure and how to run them. |

---

## Reading orders

### You are an operator, running a workflow

1. The **Harness** section of the top-level [README](../../README.md): the CLI, worked examples, and the
   exit-code contract.
2. [ENGINES.md](ENGINES.md) §1–2: selecting an engine, and what `claude-code` needs. Skip §3 unless you use Pi.
3. Run `uv run harness doctor` before anything else. It checks config, both engines and the MCP servers each
   stage references, and every finding names its fix.
4. [ARCHITECTURE.md](ARCHITECTURE.md) §2–3: the run directory and the stage lifecycle. This lets you read a
   `manifest.json` and know what happened.
5. When a run fails: `<run_dir>/flags.json` (what was in effect), `manifest.json` (per-stage status, cost, gate
   failures), `logs/decisions.jsonl` (what the harness decided and why).

### You are an agent driving the harness

You need machine-readable surfaces and stable contracts.

1. `uv run harness flags --json` — the full flag registry with type, default, scope, owner, env var and help.
   **This is the authoritative list of what you may toggle.** Do not infer flags from documentation.
2. `uv run harness workflows --json` and `uv run harness engines --json` — what exists and whether it is
   healthy.
3. [ARCHITECTURE.md](ARCHITECTURE.md) §2 and §7 — the run-directory contract and flag precedence
   (`CLI > env > config > workflow > default`).
4. `--json` on `run` for a structured summary; `flags --explain <name>` when you need to know which layer won.

Two rules. **Exit codes are the contract**: 0 ok or operator quit, 1 pipeline/config error, 2 gate failure,
3 engine failure — never continue past a non-zero. And **errors name their fix**: read the message before
retrying, because a retry without the named change will fail identically.

### You are a contributor adding a workflow or an engine

1. [ARCHITECTURE.md](ARCHITECTURE.md) in full, especially §9 (the folder recipe for a new workflow) and §10
   (what is deliberately not in the kernel, so you know where your code belongs).
2. The kernel spec itself: `src/harness/contracts.py` (Workflow, Stage, RunContext, StageResult),
   `events.py` (the canonical trace schema), `engines/__init__.py` (the Engine protocol), `flags.py`,
   `workflows.py`. The docstrings state *why*, not just what.
3. **Adding an engine:** [ENGINES.md](ENGINES.md) §5 — the five steps (protocol, registry, translator, doctor,
   fake-binary test). Read `engines/pi.py` as the worked example.
4. **Adding a workflow:** `src/workflows/testing/` is the reference. Expose one `WORKFLOW`, namespace your
   flags `<name>.*` with help text, declare your decisions.
5. Before opening a PR, run `make parity` (see [tests/parity/README.md](../../tests/parity/README.md)).

House rules that catch most review comments in advance: every new toggle is a `harness.flags.Flag` with help
text and never a bare `os.environ` read; nothing under `harness/` imports `workflows`; and every hardcoded
constant you touch becomes a flag or gains a comment saying why it has that value.
