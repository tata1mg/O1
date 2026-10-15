# `testing` workflow

QA suite generation, evaluation, storage and backend execution. This is `test_agent` (plugin `ai-sdlc`
1.1.0, HEAD `ca7d4ce`) re-expressed on the harness contracts.

What it replaces: one bash script (`tools/run_chain.sh`) that ran `rfc-agent` as a subprocess and then a
single headless `claude -p` turn which was trusted to build a spreadsheet at the right path and call a
storage MCP tool, with nothing checking either. There was no manifest for those steps, no resume, no cost
or latency accounting, and the evaluator skill was wired to nothing at all.

```
sources -> generate_cases -> render_xlsx -> evaluate -> store_cases -> derive_spec     [ -> execute_be ]
```

`execute_be` is registered but NOT in the default pipeline: it issues real requests against a live
service using a captured identity. Opt in with `--enable testing.execute` or `--stages execute_be`.

## Stages

| Stage | Engine | Requires | Outputs | Gate |
|---|---|---|---|---|
| `sources` | python | — | `qa/sources.json` | PRD and RFC present and non-empty; the inventory names a feature |
| `generate_cases` | model (`skills/qa-test-case-generator`) | `qa/sources.json` | `qa/cases.json`, `qa/scenarios.json` | every schema validator; every `execution: http` case's steps re-parsed through `be_spec.parse_case` with a route and a status. `retry_on_gate_failure=1`, `revise_on_retry=True` |
| `render_xlsx` | python | the two suite files | `qa/<FEATURE>_QA_Suite.xlsx` (conditional) | both sheets present, headers exactly the declared columns, row count equals `cases.json` |
| `evaluate` | python + model (`skills/qa-test-case-evaluator`) | the suite + `qa/sources.json` | `qa/eval.mechanical.json`, `qa/eval.json` | schema valid; aggregates recomputed; every FAIL carries a violation naming a real id; `compliance_status` derived not asserted; the model's presence verdict equals the one Python computed. `retry_on_gate_failure=1`, `revise_on_retry=True` |
| `store_cases` | python | `qa/cases.json` | `qa/store.json` | `created + updated == total`, and `total` equals the number of cases in the suite |
| `derive_spec` | python | `qa/store.json` | `qa/spec.json`, `qa/spec.report.json` | non-empty; every entry has a path and asserts something. Dropped and status-only cases become decisions |
| `execute_be` | python | `qa/spec.json` | `qa/results.json` | every row carries extid/status/detail/http_status/duration_s; status in PASS/FAIL/BLOCKED/ERROR; the tally matches the rows |

Decisions recorded (`logs/decisions.jsonl`): `source_missing`, `xlsx_skipped`,
`case_dropped_by_deriver`, `case_status_only`, `route_rewritten`, `case_not_attached`, `cases_stored`,
`presence_check_mismatch`.

## Inputs

`--feature` is required. Everything else is optional, and `--rfc-run-dir` is the normal way to supply the
sources.

| Input | Meaning |
|---|---|
| `--feature` | The store's product key, the run-dir name and the xlsx filename stem. NOT the ID prefix inside case ids — the generator picks that mnemonic itself |
| `--rfc-run-dir` | A completed RFC run dir. `sources` reads `raw/prd.md`, `out/rfc.md`, `out/contract.md`, `out/detail/plan.md`, `raw/figma.md` from it |
| `--prd` `--rfc` `--contract` `--plan` `--figma` `--test-data` | Explicit paths, each overriding the corresponding file in `--rfc-run-dir` |
| `--from-cases` | Skip generation: load an existing `cases.json` or `.xlsx` as the suite. It is still held to the full `generate_cases` gate |
| `--curl` `--env` `--build` `--route-map` | `execute_be` only |

## Flags

All 25 are `testing.*`, registered in `flags.py` with help text, and declared on `WORKFLOW.flags`. Today
they are introspectable through the registry — after workflow discovery (`harness.workflows.names()`,
which imports each workflow package and so registers its flags), `harness.flags.describe(owner="testing")`
returns the machine-readable rows, including type, default, env var and help. `harness flags --workflow testing
--json` is the intended CLI surface for the same data once the harness CLI lands.

`testing.evaluate`, `testing.store` and `testing.execute` compose the pipeline rather than gating a stage
from inside it: `compose_pipeline(ctx.flags)` maps them to a stage list. Turning `testing.store` off also
drops `derive_spec` and `execute_be`, since both read from the store. The harness-level equivalent is
`--disable-stage <name>` or `--stages <names>`, and both routes end at the same list.

| Flag | Type | Default | What it is for |
|---|---|---|---|
| `testing.storage` | `json`\|`kiwi` | `json` | Case store backend. `json` lives under `<run_dir>/store/` and never opens a socket |
| `testing.feature` | str | `""` | Pinned feature key when `--feature` is not passed on the CLI |
| `testing.product_prefix` | str | `"Diagno / "` | Store product-name prefix |
| `testing.plan_template` | str | `"{feature} — imported test cases"` | Test-plan name |
| `testing.run_template` | str | `"{feature} · {env} · {build}"` | Test-run summary, which is also the run's reuse key |
| `testing.exec_tag` | str | `"exec:http"` | The tag a case must carry to be machine-executable |
| `testing.evaluate` | bool | `True` | Run the evaluator stage |
| `testing.store` | bool | `True` | Run `store_cases` |
| `testing.execute` | bool | `False` | Run `execute_be` |
| `testing.xlsx` | bool | `True` | Render the workbook (needs the optional extra) |
| `testing.require_case_per_scenario` | bool | `True` | Gate every scenario to at least one case |
| `testing.dry_run` | bool | `False` | Execute but write nothing to the store |
| `testing.offline` | bool | `False` | Refuse every network call: parse and validate only |
| `testing.insecure_tls` | bool | `True` | Skip TLS verification when connecting to the store. **Default `True` to match today's behaviour** (see below) |
| `testing.curl_path` | str | `/usr/bin/curl` | curl binary |
| `testing.timeout_s` | int | `30` | Per-request timeout |
| `testing.curl` / `.env` / `.build` / `.route_map` | str | `""` | `execute_be` inputs, also settable as CLI inputs |
| `testing.cohort_path` | str | `""` | Dotted response path recorded as the `cohort_observed` execution property. Empty = not recorded |
| `testing.kiwi_url` / `.kiwi_user` / `.kiwi_password` | str | `""` | Kiwi credentials (env `TCMS_API_URL` / `TCMS_USERNAME` / `TCMS_PASSWORD`). Empty = fall back to `~/.tcms.conf` |
| `testing.kiwi_web` | str | `https://localhost:8443` | Base URL used only to build the printed plan/run links (env `KIWI_WEB`) |

### `testing.insecure_tls`

It defaults to `True` so a run behaves exactly as it does today, and that default is the only thing about
it that is unchanged. Disabling verification lets any machine on the network path read and rewrite
everything on the wire, credentials included. The fix is to trust the deployment's CA — add its
certificate to the system trust store, or point `SSL_CERT_FILE` at it — and then
`--disable testing.insecure_tls`. Flipping the default here would break every existing Kiwi deployment
silently, so the flag's help text carries the warning and the remedy.

What did change is the **scope**. `test_agent` implemented it as
`ssl._create_default_https_context = ssl._create_unverified_context`, in four modules, at import time.
That assignment is process-global and permanent: once any Kiwi code had run, every later HTTPS call in
the same process — Atlassian, Figma, an engine adapter fetching anything — stopped verifying
certificates, for the life of the process. Here an `ssl.SSLContext` with `check_hostname=False` and
`verify_mode=CERT_NONE` is built per connection and attached to that transport; the process default is
swapped only across `tcms-api`'s own constructor (which handshakes inside itself) and restored in a
`finally`, including when the connection raises. `health()` reports `tls_verified` so an operator can
see the posture, and `tests/testing/test_secrets_and_tls.py` asserts the global default is left as it was
found.

### Credentials

`testing.kiwi_password` is declared in `flags.SECRETS` and registered with `Flag(secret=True)` so it is
redacted from `flags.json`, `harness flags --json` and `--explain` tables. The marker is applied behind a
`dataclasses.fields` probe: the kernel's `secret` field is still landing, and passing an unsupported
kwarg would raise at import time — which, since workflow discovery imports this module, would break every
CLI invocation rather than just a Kiwi run. `SECRETS` is the workflow's declaration either way, and a
test fails if any `testing.*` flag whose name says credential is missing from it.

`testing.kiwi_user` is deliberately not redacted. It is not a secret on its own, it is already written to
the Kiwi TestRun's `manager` field where every account can read it, and "which account did this run use"
is the first question when a run's writes land in the wrong place.

## Artifact durability

Every file this workflow declares canonical goes through `_io.atomic_write_text` (unique temp, `fsync`,
`os.replace`) rather than `Path.write_text`, which truncates before it writes. The repo had already paid
for this twice: a torn `manifest.json` bricked a run dir because every later invocation crashed at load,
and a fixed temp name then let two invocations interleave into one temp file. `qa/cases.json` is exactly
as load-bearing, so it gets the same treatment.

Readers are strict where a gate can report and tolerant where it cannot. `read_json_gated` returns
failures naming the file, and distinguishes an absent artifact from a truncated one from content that is
not JSON at all — the advice differs in each case. `read_json` stays tolerant, because prompt building
runs before any gate and must not explode on a half-written inventory. The JSON store raises a
`GateFailure` naming the file instead of letting `JSONDecodeError` escape out of `list_cases` and repeat
on every retry.

Every read-modify-write on the store — `upsert_cases`, `create_run`, `record_execution` — runs under an
advisory `flock` on a sidecar `<name>.lock`. The sidecar rather than the artifact itself, because the
atomic write replaces the inode and a lock on the old file would protect nothing once the rename landed.
Without it, two invocations sharing a run dir silently lost each other's executions, and nothing
downstream noticed because `qa/results.json` is written by the stage independently of the store.

TODO(harness): `atomic_write_text` and `locked` are not workflow-specific and belong beside the manifest
writer in the kernel, with `rfc_agent.runner._atomic_write` becoming a re-export. The implementation here
is deliberately identical so the move is a delete-and-import.

## What changed, and why

1. **JSON is canonical; the xlsx is a render** (D8). The generator writes `qa/cases.json` and
   `qa/scenarios.json` and the workbook is produced by deterministic Python. Before, the workbook was the
   only artifact and the model built it itself with unrestricted `Bash`, so no gate could check the sheet
   names, the columns, the enums, the ID convention or the step grammar.
2. **The evaluator is a stage.** Its deterministic half runs in Python and its model half is checked against it.
3. **The presence check is computed twice.** The evaluator SKILL calls it "the one thing that's never
   subjective" and then asks a model to do it. Python computes it independently; a disagreement fails the
   stage and shows both verdicts, because one of the two is wrong and which one matters.
4. **The http step grammar has one implementation.** It was prose in the SKILL, restated in the shell
   heredoc, and encoded a third time as regexes in `be_spec.py`. The `generate_cases` gate now runs
   `be_spec.parse_case` — the same parser `derive_spec` will use — so "executable" is decided once, at
   generation time, instead of silently at derivation time weeks later.
5. **The Excel importer keeps the `Execution` column.** `import_cases.parse_cases` had no `execution` key
   in its column map, so every case imported from a workbook lost its classification, never got an
   `exec:` tag, and was reported `skipped_wrong_execution` at run time. `xlsx.parse_cases` maps it.
6. **No password default in code.** `kiwi_runner.py:28-30` and `be_requirements.py:135-137` shipped the
   admin password as a literal, and `make import` wrote it into `~/.tcms.conf`. Credentials come from
   flags (env `TCMS_*`) or `~/.tcms.conf`; an incomplete triple falls back to `~/.tcms.conf` rather than
   guessing. **This is a behaviour change**: a deployment that relied on the literal must now set
   `TCMS_PASSWORD` (or configure `~/.tcms.conf`).
7. **One product-matching rule instead of three.** `kiwi_writer` matched the product by exact name,
   `kiwi_runner` and `be_spec` by `name.endswith("/ <feature>") or name.endswith(feature)`, and
   `be_requirements` by substring `feature in name` — so a product that resolved for the writer could
   resolve to a *different* product for a reader. The single rule is **exact match on
   `<product_prefix><feature>`** — the name the writer creates — with the `endswith("/ <feature>")` form
   kept only as a fallback for products created before the prefix was configurable. The substring rule is
   gone entirely; it was the loosest of the three and the only one that could match an unrelated product.
8. **Storage is a protocol** (`store/__init__.py`), with a JSON backend as the default. Docker plus a
   running Kiwi is no longer a prerequisite for testing generation, evaluation, derivation or execution.
9. **`--dry-run` and `--offline` are different things.** `be_execute --dry-run` still read Kiwi to derive
   a spec and still fired a live preflight request. `testing.dry_run` means "write nothing";
   `testing.offline` means "open no socket".
10. **The feature-specific field is a flag.** `cohort_observed` was hardcoded to
    `data.mix_panel_data.labs_careplan_variant` in the generic executor. It is `testing.cohort_path`, and
    empty means the property is not written.
11. **Results are persisted locally.** `qa/results.json` records every case's extid, status, detail, HTTP
    status and duration. Before, results went only into Kiwi and to stdout, so a run whose terminal
    scrolled away left no local evidence.
12. **Every step is in the manifest.** Status, attempts, gate verdict, cost, duration and inputs digest
    per stage, which is what makes the chain resumable and green-skippable. `--skip-rfc` was the only
    resume knob before.
13. **Hardcoded constants became flags.** curl path, request timeout, `EXEC_TAG`, product prefix, plan and
    run name templates, route-map path. The keep-awake `caffeinate` wrapper is the harness's `keep_awake`
    flag rather than a self-re-exec inside a workflow script.
14. **Two rules the port deliberately did not mechanise.** The generator SKILL grants a prose exception
    letting pure analytics scenarios stay scenario-only, and that cannot be detected from the artifact —
    so the gate requires a case per scenario and `testing.require_case_per_scenario` is the documented
    escape hatch. Likewise nine of the evaluator's fourteen compliance gates (3, 5, 6, 7, 9, 10, 12, 13,
    14) and all eight rubric dimensions need a requirement read and an opinion formed; they stay with the
    model.

## What still needs Kiwi, Docker or a live environment

Nothing in the default pipeline. With `testing.storage=json` the six default stages run entirely offline,
which is how the test suite exercises them.

| Needs | For what |
|---|---|
| A reachable Kiwi + `tcms-api` installed | `testing.storage=kiwi` for `store_cases`, `derive_spec` and `execute_be`'s run/executions. The backend is unit-tested against a fake XML-RPC object (`tests/testing/test_kiwi_store.py`), which covers the logic but not whether `tcms-api`'s real client accepts the calls, nor the read-only `actual_duration` behaviour the start/stop-date trick works around |
| Docker | Only to *host* Kiwi. `test_agent`'s `docker-compose.yml` and `make build-arm64` are not ported; point `testing.kiwi_url` at whatever instance you have |
| A captured curl with live cookies, plus network reach to the target host | `execute_be`. Capture it from the browser's network tab; a hand-trimmed cookie jar returns 403 Bad CSRF where the full captured one returns 200 |
| `--env` and `--build` | `execute_be` records both on the run and on every execution property |
| `openpyxl` (extra `testing`) | `render_xlsx`. Absent, the stage records `xlsx_skipped` and moves on |
| A model engine (`ctx.engine`) | `generate_cases` and `evaluate`. Use `--from-cases` to run the rest of the chain over an existing suite with no model at all |

Not ported (and not needed by any stage): `tools/be_requirements.py` and the
`config/be-requirements/*.yaml` it produced. Nothing ever read those YAMLs back — `be_execute` did not
consume them, and two incompatible schemas coexisted in the repo. The MCP server (`mcp_server/kiwi_mcp.py`)
is also not ported: `store_cases` writes to the store directly, so there is no tool for a model to call.

## Parity with test_agent

| test_agent behaviour | Where it lives now |
|---|---|
| `run_chain.sh` investigate (`rfc-agent` as a subprocess) | Gone. The RFC run is a separate `harness run rfc`; `--rfc-run-dir` points at its output |
| `run_chain.sh:31-34` `caffeinate` self-re-exec | Harness flag `keep_awake` |
| `run_chain.sh:43-66` hand-rolled re-declaration of every rfc-agent flag | Gone. Each workflow owns its own inputs |
| `run_chain.sh:102-104` "no RFC at ..." exit 2 | `sources` gate |
| `run_chain.sh:124-129` prefer `raw/prd.md` / `raw/figma.md` over a URL | `stages/sources._resolve` |
| `run_chain.sh:131-178` the stage-2 prompt | `stages/generate_cases._build_prompt`, minus the storage call and asking for JSON |
| `run_chain.sh:174` "call kiwi_upsert_test_cases exactly once" | `store_cases`, in Python, with a checked summary |
| `run_chain.sh:162` `qa/<FEATURE>_QA_Suite.xlsx` | `stages/render_xlsx.workbook_rel` (same name, same space-to-underscore rule) |
| `skills/qa-test-case-generator/SKILL.md` | `skills/qa-test-case-generator/SKILL.md`, verbatim + a "Harness output contract" section |
| `skills/qa-test-case-evaluator/SKILL.md` | `skills/qa-test-case-evaluator/SKILL.md`, verbatim + a "Harness output contract" section |
| The ID convention, TYPE table, enums, mandatory columns, TS↔TC linkage, NEG+EDGE per module | `schema.py` validators, run by the `generate_cases` gate |
| Evaluator presence check + gates 1/2/4/8 | `mechanical.py`, run by the `evaluate` stage |
| Evaluator rubric + gates 3/5/6/7/9/10/12/13/14 | Still the model's, checked for shape and arithmetic by the `evaluate` gate |
| `be_spec.parse_case` grammar (`RE_SEND`, `RE_STATUS`, `RE_MATCHES`, `_undelimit`, drop reasons) | `be_spec.py`, unchanged |
| `be_spec.confirmed_http_cases` (direct XML-RPC scan) | `be_spec.confirmed_http_cases(store, ...)` via `CaseStore.list_cases` |
| `be_spec.derive` / `report` (stdout) | `stages/derive_spec`, writing `qa/spec.json` + `qa/spec.report.json` and recording decisions |
| `be_execute.run_case` classification order and every measured rule behind it | `be_execute.run_case`, unchanged |
| `be_execute.parse_curl`, `dig` | `be_execute.parse_curl`, `dig`, unchanged |
| `be_execute.apply_route_map` | `route_map.apply_route_map` (map path now always explicit) |
| `be_execute` preflight, exit 3 | `stages/execute_be`, raising `EngineFailure` |
| `be_execute` "nothing to run", exit 4 | `stages/execute_be`, raising `GateFailure` |
| `be_execute` per-case stdout + tally | `qa/results.json` + the stage's `final_text` |
| `EXEC_TAG`, `/usr/bin/curl`, timeout 30, `cohort_observed` path | Flags `testing.exec_tag`, `.curl_path`, `.timeout_s`, `.cohort_path` |
| `kiwi_writer.upsert_cases` (ensure-by-name walk, extid idempotency, re-CONFIRM after `add_case`) | `store/kiwi.py:upsert_cases`, unchanged apart from the four documented changes |
| `kiwi_writer.render_text`, `case_tags`, `PRIORITY_MAP` | `schema.render_text`, `schema.case_tags`, `store/kiwi.PRIORITY_MAP` |
| `kiwi_runner.create_run` (CONFIRMED gate + `require_tag` gate, run reuse by summary) | `store/kiwi.py:create_run` and `store/json_store.py:create_run` |
| `kiwi_runner.record_execution` (start/stop dates because `actual_duration` is read-only, 500/900 truncation) | `store/kiwi.py:record_execution`, unchanged |
| `import_cases.py` stdlib xlsx reader + header aliasing | `xlsx.read_sheets` / `parse_cases` / `parse_scenarios`, with `execution` added |
| `import_cases.py --dry-run` preview | `--from-cases` plus the `generate_cases` gate, which checks more than the preview showed |
| `tests/test_be_spec.py` (14 checks) | `tests/testing/test_be_spec.py`, same assertions, source lines cited |
| `tests/test_be_execute.py` (17 checks) | `tests/testing/test_be_execute.py`, same assertions and mocks |
| `tests/test_route_map.py` (6 checks) | `tests/testing/test_route_map.py`, same assertions |

The three converted files hold 37 original assertions, not the 39 the survey reports: `test_be_spec.py`
has 14 `check(...)` calls, not 16. Counted at `ca7d4ce` with `grep -c '^check(' tests/test_be_spec.py`.
| `commands/{ai-sdlc,be-execute,generate-test-cases,evaluate-test-cases}.md` | Not ported. The in-session slash commands remain a human affordance in `test_agent` (D14); the headless path is this workflow |
| `tools/be_requirements.py`, `config/be-requirements/*.yaml` | Not ported — nothing read them back (see above) |
| `mcp_server/kiwi_mcp.py`, `docker-compose*.yml`, `Makefile` | Not ported — deployment, not workflow |
