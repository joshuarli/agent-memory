SHELL := /bin/sh
.DEFAULT_GOAL := help

REPEATS ?= 5
SCENARIO ?=
RECORDS ?= 1000
OBJECTS ?= 200
OUT ?= .work/perf
CF ?= cf
CONFIRM_DATABASE ?=
WORKER_DIR := cloudflare
LOCAL_STATE := $(CURDIR)/.work/tmp/cf-state
LOCAL_DEV := $(CURDIR)/.work/local/dev
DATABASE_ID = $(shell node $(WORKER_DIR)/scripts/config.mjs database-id 2>/dev/null)
PYTHON := uv run --no-project --python '>=3.15' python -I
PERF := $(WORKER_DIR)/perf.sh
SUITES := $(sort $(wildcard tests/test_*.py eval/test_*.py))
LINT_PYTHON := $(sort $(wildcard src/agent_memory/*.py tests/*.py eval/*.py))
TYPED_PYTHON := src/agent_memory eval/bench_mirror.py tests/testkit.py
GIT_TEMPLATE_DIR ?= $(wildcard /opt/homebrew/share/git-core/templates)
ENV_GIT := $(if $(GIT_TEMPLATE_DIR),GIT_TEMPLATE_DIR=$(GIT_TEMPLATE_DIR))

.PHONY: help test test-python lint dist worker-deps worker-bundle worker-test worker-check deploy smoke-deployed migrate-local dev-local reset-local migrations-check migrations-production migrate-production integration-local cost-check check config-check perf-cost provision provision-access observe perf-local perf-worker perf-deployed perf-report

help: ## List targets
	@awk 'BEGIN {FS = ":.*## "} /^[a-z][a-z0-9.-]*:.*## / {printf "%-22s %s\n", $$1, $$2}' $(MAKEFILE_LIST) | sort

test: test-python worker-test ## Run every Python suite, eval selftest, and Worker suite

check: lint test-python worker-test migrations-check integration-local cost-check config-check ## Run the predeploy gate

config-check: ## Assert the parsed production config
	node $(WORKER_DIR)/scripts/config.mjs check

test-python: ## Run every Python suite and inference-free eval selftest
	@mkdir -p .work/tmp
	@set -e; for suite in $(SUITES); do \
		printf '== %s\n' "$$suite"; \
		$(ENV_GIT) TMPDIR="$(CURDIR)/.work/tmp" $(PYTHON) "$$suite"; \
	done

lint: ## Ruff, ty, shellcheck, and the Worker type check and lint
	uvx ruff check --no-cache $(LINT_PYTHON)
	uvx ty check $(TYPED_PYTHON)
	shellcheck -S warning -s sh $(WORKER_DIR)/*.sh $(WORKER_DIR)/scripts/*.sh
	cd $(WORKER_DIR) && npm run check && npm run lint

dist: ## Build the wheel and sdist into .work/dist
	TMPDIR="$(CURDIR)/.work/tmp" uv build --out-dir .work/dist

worker-deps: ## Install the pinned Worker toolchain from the lockfile
	cd $(WORKER_DIR) && npm ci

worker-bundle: ## Build the Worker bundle
	cd $(WORKER_DIR) && npm run build

worker-test: worker-bundle ## Build the bundle and exercise it with every Worker suite
	cd $(WORKER_DIR) && npm test

worker-check: worker-bundle ## Validate the production deploy plan without uploading
	cd $(WORKER_DIR) && $(CF) deploy --prebuilt --dry-run --mode production

deploy: ## Gate, check, deploy the checked bundle, and smoke the Worker
	CF_BIN="$(CF)" sh $(WORKER_DIR)/scripts/deploy-gate.sh
	$(MAKE) check
	@set -e; tested=$$(sh $(WORKER_DIR)/scripts/bundle-digest.sh); \
		printf 'deploy: tested bundle %s\n' "$$tested"; \
		cd $(WORKER_DIR) && $(CF) deploy --prebuilt --mode production; \
		deployed=$$(sh scripts/bundle-digest.sh); \
		[ "$$deployed" = "$$tested" ] || { >&2 printf 'deploy: bundle changed during deploy (%s != %s)\n' "$$deployed" "$$tested"; exit 1; }
	$(MAKE) smoke-deployed

smoke-deployed: ## Read-only probe of the live Worker
	sh $(WORKER_DIR)/scripts/smoke-deployed.sh

migrate-local: ## Apply D1 migrations to the local simulation state
	@[ -n "$(DATABASE_ID)" ] || { >&2 printf 'migrate-local: cloudflare.config.ts has no D1 database id\n'; exit 1; }
	@mkdir -p "$(LOCAL_STATE)"
	cd $(WORKER_DIR) && $(CF) d1 migrations apply $(DATABASE_ID) --local --persist-to "$(LOCAL_STATE)"

migrations-check: ## Fail when an applied migration was edited
	sh $(WORKER_DIR)/scripts/check-migrations.sh

dev-local: ## Run the persistent local stack on 127.0.0.1:8790
	@set -e; age=$$(sh $(WORKER_DIR)/dev-age.sh); \
	cd $(WORKER_DIR) && AGE_BIN="$$age" node local/cli.mjs serve --persistent --port 8790 \
		--state "$(LOCAL_DEV)" \
		--origins '[{"client_id":"origin-a.access","origin":"origin-a"},{"client_id":"origin-b.access","origin":"origin-b"}]'

reset-local: ## Remove the persistent local stack state
	@rm -rf "$(LOCAL_DEV)"

integration-local: worker-bundle ## Run the Python integration suites offline
	@sh $(WORKER_DIR)/dev-age.sh >/dev/null
	@mkdir -p .work/tmp
	@OFFLINE_FORBID_CF=1 UV_OFFLINE=1 TMPDIR="$(CURDIR)/.work/tmp" \
		sh $(WORKER_DIR)/scripts/offline-run.sh sh $(WORKER_DIR)/scripts/integration-local.sh

cost-check: worker-bundle ## Run the fast D1 row-budget tests
	cd $(WORKER_DIR) && node --test test/cost.test.mjs

perf-cost: worker-bundle ## Measure the mirror's local D1 and R2 cost
	@sh $(WORKER_DIR)/dev-age.sh >/dev/null
	@mkdir -p .work/perf .work/tmp
	@TMPDIR="$(CURDIR)/.work/tmp" $(PYTHON) eval/bench_cost.py

migrations-production: ## List pending production D1 migrations (remote, read-only)
	@[ -n "$(DATABASE_ID)" ] || { >&2 printf 'migrations-production: cloudflare.config.ts has no D1 database id\n'; exit 1; }
	cd $(WORKER_DIR) && $(CF) d1 migrations list $(DATABASE_ID)

migrate-production: ## Apply pending production D1 migrations; needs CONFIRM_DATABASE=<id>
	@[ -n "$(DATABASE_ID)" ] || { >&2 printf 'migrate-production: cloudflare.config.ts has no D1 database id\n'; exit 1; }
	@[ "$(CONFIRM_DATABASE)" = "$(DATABASE_ID)" ] || { >&2 printf 'migrate-production: set CONFIRM_DATABASE=%s to write production\n' "$(DATABASE_ID)"; exit 1; }
	$(CF) auth whoami
	cd $(WORKER_DIR) && $(CF) d1 migrations list $(DATABASE_ID)
	cd $(WORKER_DIR) && $(CF) d1 migrations apply $(DATABASE_ID)

provision: ## Create the R2 bucket and D1 database (production write)
	sh $(WORKER_DIR)/scripts/provision.sh

provision-access: ## Create the Worker-level Access app and a collector token (production write)
	sh $(WORKER_DIR)/scripts/provision.sh --access

observe: ## Assert the observability config and name the serving deployment
	@$(MAKE) config-check
	@printf '%s\n' 'dashboard: https://dash.cloudflare.com/?to=/:account/workers/services/view/usr-memories' 'logs: https://dash.cloudflare.com/?to=/:account/workers/services/view/usr-memories/production/observability'
	@cd $(WORKER_DIR) && $(CF) workers deployments list --worker usr-memories

perf-local: ## Collector benchmarks, no network
	@$(ENV_GIT) TMPDIR="$(CURDIR)/.work/tmp" REPEATS="$(REPEATS)" RECORDS="$(RECORDS)" OBJECTS="$(OBJECTS)" SCENARIO="$(SCENARIO)" OUT="$(OUT)" sh "$(PERF)" local

perf-worker: worker-bundle ## Benchmark the Miniflare bundle over HTTP
	@$(ENV_GIT) TMPDIR="$(CURDIR)/.work/tmp" REPEATS="$(REPEATS)" RECORDS="$(RECORDS)" OBJECTS="$(OBJECTS)" SCENARIO="$(SCENARIO)" OUT="$(OUT)" sh "$(PERF)" worker

perf-deployed: ## Benchmark the production origin with synthetic data
	@$(ENV_GIT) TMPDIR="$(CURDIR)/.work/tmp" REPEATS="$(REPEATS)" RECORDS="$(RECORDS)" OBJECTS="$(OBJECTS)" SCENARIO="$(SCENARIO)" OUT="$(OUT)" sh "$(PERF)" deployed

perf-report: ## Compare benchmark runs under OUT
	@TMPDIR="$(CURDIR)/.work/tmp" $(PYTHON) eval/bench_mirror.py report --dir "$(OUT)"
