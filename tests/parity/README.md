# Parity suite

"No regression" must be mechanical, not claimed. These tests prove that a pipeline run through the harness CLI
does exactly what the run loop does when called directly, and that the public surfaces other tools read have
not drifted. Run them before merging anything that touches the run loop, the manifest, the decision
vocabulary or a CLI surface.

```
make parity                       # the suite
make test                         # the suite plus everything else
PARITY_REGENERATE=1 make parity   # rewrite the goldens (see "Changing a golden" below)
```

## What each test proves

| File | Proves |
|---|---|
| `test_manifest_golden.py` | The per-stage manifest key set, the status and gate shape, the last-attempt-vs-totals accounting rule, the full decision vocabulary, the recording layout, and the run-dir file layout |
| `test_differential.py` | The **same three-stage pipeline** run twice, once by calling `run_pipeline(stages, ctx)` directly and once through `harness run <workflow> --engine replay` on a recording made from the first run, produces equal manifests, byte-identical declared outputs, and the same exit code on a red gate |

Everything runs under a **fake engine** (`conftest.fake_engine`) or the **replay engine**. No model is invoked,
nothing costs money, and every value is deterministic, which is the only way a manifest can be compared at all.

## What the differential test compares

Everything in each stage's manifest entry **except** `started_at`, `duration_s`, `duration_s_total` and
`rework_s`. Those four are properties of when the run happened and how fast the machine was. Compared:
`status`, `engine`, `cost_usd`, `cost_usd_total`, `usage`, `attempts`, `requires`, `outputs`,
`tool_call_count`, `multi_call_turn_pct`, `inputs_digest` and the whole `gate` object. A separate test asserts
the excluded fields are still *recorded*, so exclusion never becomes deletion.

Three files exist only on the harness path, each for a stated reason, and a fourth would fail the suite:

- `flags.json`: `harness run` resolves flags and records which layer won;
- `logs/<stage>.events.jsonl`: every engine adapter writes the canonical trace; the direct comparison run uses a
  bare stage callable, which is not an adapter;
- `logs/asks_skipped.jsonl`: the harness CLI records that a non-interactive run swallowed its asks.

## Changing a golden

A golden here is a **public surface**: `manifest.json` and the fixed artifact paths are the contract that
evaluation tooling reads, the exit codes are what the plugin command branches on, and the decision names are
what dashboards key off. None of those consumers lives in this repo, so nothing else will catch a rename.

1. Make the change and run `make parity`. Read the failure; it names the drifted file.
2. If the change is **intended**, run `PARITY_REGENERATE=1 make parity` and `git diff` the goldens.
3. **The diff is the review artefact.** Put it in the commit message and say who downstream is affected. A
   golden regenerated without reading its diff proves nothing.

## What this suite does NOT cover

- **Live behaviour.** A real `claude -p` run is non-deterministic; parity here is structural.
- **The Pi engine.** It is proven separately against a fake `pi` executable emitting recorded events
  (`tests/harness/test_engine_pi.py`).
- **Stage prompt content.** Covered by the stage-specific tests, not by parity.
