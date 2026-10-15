# Tests

```bash
uv run pytest            # the whole suite: offline, no model calls, no network
make parity              # only the no-regression suite (tests/parity)
uv run pytest tests/rfc  # one area
```

Every test runs against a fake `claude` binary, a recorded stream, or a throwaway git remote. Nothing spends
money. `tests/conftest.py` sets `RFC_AGENT_NO_REAL_CLAUDE=1` so a test that forgets to stub the model fails
instead of calling it, and turns Langfuse tracing off (`tests/rfc/test_observe.py` turns it on with a fake
client).

## Layout

| Folder | Covers |
|---|---|
| `rfc/` | The RFC pipeline: Scope (parsing, confirm, snapshots, discovery, and the whole flow end to end in `test_scope_main.py`), Investigate, Draft and its gate, Review, Revise, Summarize, the `rfc` runner, and its Langfuse tracing |
| `infra/` | What the pipeline runs on: the run loop and its retry, back-jump and resume rules; the Claude engine's streams and tool ledger; containment hooks and the write guard; secret scrubbing; config; identity |
| `harness/` | The harness kernel: CLI, flags, policy, replay, layering rules, and the `claude-code` and `pi` engine adapters (including the Pi guard under Node) |
| `testing/` | The `testing` workflow: schema, gates, Kiwi and JSON stores, backend execution, spec derivation |
| `parity/` | Golden files for the run-dir layout, manifest keys, decision vocabulary and recording layout, plus the differential replay run. Run it before merging anything that touches the run loop. See [parity/README.md](parity/README.md) |
| `fixtures/` | Shared recorded streams (`stream_*.jsonl`), a fake repo, and Pi fixtures |

`panel.test.tsx` is the Figma plugin panel's test and `fixtures/pi/guard_cases.mjs` drives the Pi guard under
Node; neither is collected by pytest.

## Conventions

- A new stage test uses a fake `claude` (see `rfc/test_investigate.py` for the pattern) and asserts on the files
  the stage writes, not on prompts.
- Anything that needs git builds a bare origin in `tmp_path` (see `rfc/test_scope_main.py`).
- Hooks are tested as subprocesses, the way Claude Code runs them; coverage tools do not see inside them.
