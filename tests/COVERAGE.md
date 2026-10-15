# Coverage

Measured on the full suite (1150 passed, 5 skipped), with branch coverage and the hook subprocesses counted.

| Measure | Result |
|---|---|
| Statements | 10,718 of 12,718 (82%) |
| Branches | 3,415 of 4,542 (75%) |
| Combined (what `coverage report` prints) | 82% |

Branch coverage counts each outcome of an `if`, loop or `try` separately, so it reads lower than statement coverage.
It shows that a line ran, not that a test checked the result.

## By area

| Area | Statements | Branches |
|---|---|---|
| Harness kernel and engines | 91% | 84% |
| `testing` workflow | 87% | 81% |
| Scope | 78% | 61% |
| Investigate | 83% | 73% |
| Draft | 93% | 87% |
| Review | 97% | 89% |
| Revise | 96% | 83% |
| Summarize | 95% | 92% |
| Run loop, Langfuse tracing, hooks, config | 88–94% | 74–90% |
| Engine (`engine.py`), Figma snapshot, gates | 62–78% | 56–66% |

Largest untaken branches: `engine.py` (106), `scope/cli.py` (91), `gates.py` (84), `figma_snapshot.py` (77),
`xlsx.py` (46, needs `openpyxl`), `harness/cli.py` (41), `investigate/cli.py` (31).

## Reproducing the number

Hooks run as subprocesses and some tests pass a stripped `env=`, which drops coverage's subprocess switch. Inject it
for the measurement only:

```bash
# cov.rc
[run]
branch = True
parallel = True
patch = subprocess
source = src

# covenv.py: a pytest plugin that copies COVERAGE_PROCESS_START into any explicit env= passed to Popen
PYTHONPATH=. COVERAGE_PROCESS_START=cov.rc \
  uv run --with pytest-cov --with 'coverage>=7.10' pytest tests -p covenv --cov --cov-config=cov.rc
```

## Documented behaviour with no test

Found by checking every flag, config key, file and constant named in `docs/` against the tests. Gaps closed since
(`--fresh-clone`, `RFC_AGENT_CONFIG`, Revise `MINORS` and its settings, Investigate presets and overrides,
`LANGFUSE.MASK_SECRETS`) are not listed.

| Doc | Behaviour | Status |
|---|---|---|
| SCOPE | `--figma-wait`, `--reuse-figma ask\|always\|never` | not tested (needs the Figma exporter faked) |
| SCOPE | `RFC_AGENT_REPO_CACHE` env override | not tested (read once at import) |
| SCOPE | `SCOPE.DISCOVERY_MODEL`, `SCOPE.SUMMARY_MODEL`, `STEERING.REPO_ALIASES` | not tested |
| INVESTIGATE / DRAFT | model, effort and timeout settings actually reaching the `claude` command line | settings resolution is tested, the hand-off is not |
| REVISE | `LEAD_TIMEOUT_S`, `CONFIRM_TIMEOUT_S` reaching the agent | resolution tested, hand-off not |
| OBSERVABILITY | `LANGFUSE_TRACING_ENVIRONMENT` | not tested |
| harness ENGINES | `--disable hook_canary`, Claude flag set (`--permission-mode`, `--setting-sources`) | not asserted |

Everything else named in the docs appears in at least one test, which is a name match and not a proof of behaviour.
