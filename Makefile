.PHONY: test lint parity smoke security security-deps install-hooks

test:
	uv run pytest -q

lint:
	uv run ruff check src tests

# The no-regression gate (docs: tests/parity/README.md). Run this before merging anything that touches
# the run loop, the manifest, the decision vocabulary or a CLI surface.
parity:
	uv run pytest -q tests/parity

# Real end-to-end run against a frozen fixture case (manual, costs real tokens; not CI).
# Usage: make smoke PRD=<path-or-url>
smoke:
	@test -n "$(PRD)" || { echo "usage: make smoke PRD=<path-or-url>"; exit 1; }
	uv run rfc --prd "$(PRD)" --run-dir runs/smoke --yes

.PHONY: bench bench-json bench-after

# Small, structural, reproducible benchmarks (docs/harness/BENCHMARKS.md). Prints tables for every suite;
# does not write JSON (use bench-json for that). engine_pong makes real, tiny-cost `claude` calls.
bench:
	uv run python -m bench all

# Same suites, written as JSON envelopes (one per suite) under bench/results/after/ — the AFTER pass once
# the kernel + engine adapters land. Re-run per suite with a fresh --json path to avoid overwriting a prior
# AFTER snapshot you still want (e.g. bench/results/after/harness_overhead.json).
bench-json:
	@mkdir -p bench/results/after
	for s in harness_overhead gate_latency cli_startup test_suite engine_pong stream_translate; do \
	  uv run python -m bench $$s --json bench/results/after/$$s.json; \
	done

# Alias documented in docs/harness/BENCHMARKS.md as "the exact command" for the AFTER pass.
bench-after: bench-json

# Security gate (scripts/security/scan.py). `security` = whole history + committed tree + deps; the pre-push hook
# runs the same script on just the commits being pushed. install-hooks points git at .githooks/.
security:
	python3 scripts/security/scan.py all

security-deps:
	python3 scripts/security/scan.py deps

install-hooks:
	git config core.hooksPath .githooks
	chmod +x .githooks/pre-push
	@echo "pre-push hook installed (git config core.hooksPath = .githooks)"
