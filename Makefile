.PHONY: help test mutate e2e

# bandit-wannabe's checkout: its Vagrant VM is the game box the end-to-end test plays.
BANDIT ?= ../bandit-wannabe
VENV   := .venv
PY     := $(VENV)/bin/python

help: ## List targets
	@grep -E '^[a-z0-9-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-6s %s\n", $$1, $$2}'

# CI pins aiohttp to the host's version (3.9.1, Ubuntu 24.04); locally any 3.9+
# works, since a newer Python may have no 3.9.1 wheel.
$(PY):
	python3 -m venv $(VENV)
	$(PY) -m pip install -q "pytest==9.1.1" "pytest-cov==5.0.0" "ruff==0.6.9" "aiohttp>=3.9.1"

test: $(PY) ## Lint + unit tests with coverage (what CI runs; seconds)
	$(PY) -m ruff check --select F,E9 spawner scripts tests
	$(PY) -m pytest -q tests/test_*.py --cov=spawner --cov=scripts --cov-report=term-missing --cov-fail-under=64

mutate: $(PY) ## Plant known bugs one at a time; the unit tests must catch each (~1 min)
	PY=$(PY) tests/mutations.sh

e2e: $(PY) ## End to end against the Vagrant game VM: real spawner, ttyd, tmux (~3 min)
	@cd $(BANDIT) && vagrant status handover 2>/dev/null | grep -q 'running' \
	  || { echo "starting the game VM (make local-up in $(BANDIT))"; $(MAKE) -C $(BANDIT) local-up; }
	PYTHON=$(abspath $(PY)) BANDIT=$(abspath $(BANDIT)) tests/vm/run.sh </dev/null
