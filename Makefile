# homelab-mcp — developer convenience targets.
#
# The pocketid-mcp-as contract work (Part A conform + Part B host) is
# driven by the scripts under scripts/. These targets are thin wrappers.

RUN ?= uv run --extra dev
ORIGIN ?= http://127.0.0.1:9200

.PHONY: help test lint typecheck fmt check \
        contract-pull conformance contract-verify conformance-ci

help:
	@echo "Targets:"
	@echo "  test            run the pytest suite"
	@echo "  lint            ruff format --check + ruff check"
	@echo "  typecheck       mypy src"
	@echo "  check           lint + typecheck + test"
	@echo "  contract-pull   bump/refresh the pinned contract ref + stage content"
	@echo "                  (REF=<tag|branch|sha>, default: keep current pin)"
	@echo "  conformance     clone the pinned harness fresh + run against ORIGIN (/mcp)"
	@echo "  contract-verify assert served contract.json == upstream@pinned (ORIGIN)"
	@echo "  conformance-ci  boot the server locally + run both checks end-to-end"

test:
	$(RUN) pytest -q

lint:
	$(RUN) ruff format --check .
	$(RUN) ruff check .

typecheck:
	$(RUN) mypy src

check: lint typecheck test

# Deliberate, reviewable bump/refresh of the pinned contract ref. GitHub is
# the single source of truth; this updates contract/PINNED.json and stages the
# fetched content (gitignored). After running, review the diff + `make conformance-ci`.
contract-pull:
	bash scripts/pull-contract.sh $(REF)

# Run the UPSTREAM conformance harness (cloned fresh at the pinned tag,
# unpatched) against a live AS. Part A: pocketid-mcp-as v1.1, profile
# jwt-refresh, scope mcp-only, MCP path /mcp.
conformance:
	@ref=$$(jq -r '.ref' contract/PINNED.json); \
	dir=$$(mktemp -d); \
	git init --quiet $$dir; \
	git -C $$dir remote add origin https://github.com/carpenike/mcp-as-contract; \
	git -C $$dir fetch --quiet --depth 1 origin $$ref; \
	git -C $$dir checkout --quiet FETCH_HEAD; \
	bash $$dir/conformance/check.sh $(ORIGIN) jwt-refresh mcp-only --mcp-path /mcp; \
	rc=$$?; rm -rf $$dir; exit $$rc

# Part B drift guard: served contract.json must deep-equal upstream@pinned.
contract-verify:
	bash scripts/verify-served-contract.sh $(ORIGIN)

# Boot the server locally and run both checks (what CI runs).
conformance-ci:
	bash scripts/conformance-ci.sh
