# Top-level developer/operator entrypoints (docs/60-deployment.md, ADR 0005). Every target below
# except `deploy-stub` must work locally with no cloud account, per this sprint's task brief.
#
# `web/`, `services/` and `pipeline/` code is owned by other agents this sprint (see
# docs/CHANGELOG.md); this file only orchestrates existing entrypoints in those trees, it does not
# add logic to them. Where a target's real output is a moving target because those trees are
# being edited concurrently in this shared session, that is called out in docs/60-deployment.md
# §9, not hidden here.

SHELL := /usr/bin/env bash
PYTHON ?= .venv/bin/python
PIP ?= .venv/bin/pip

.PHONY: help venv dev reparse test test-core test-perf test-web browsers caddy coverage-gate coverage-floors lint typecheck build deploy-stub backup restore-drill clean

help:
	@echo "Targets: venv dev reparse hooks test test-core test-perf test-web browsers caddy coverage-gate coverage-floors lint typecheck build deploy-stub backup restore-drill clean"

## One-time local setup (docs/00-PLAN.md install command). Re-runs only when requirements.txt
## changes (the stamp file), not on every target invocation.
venv: .venv/.stamp

.venv/.stamp: requirements.txt
	python3 -m venv .venv
	$(PIP) install -q --upgrade pip
	$(PIP) install -q -r requirements.txt
	touch .venv/.stamp

## dev: SQLite store loaded from data/, api + web running locally (task brief). Ctrl+C stops both.
## Delegates to web/dev_up.py (frontend-developer already built this exact target — its own
## docstring says so: "`make dev` equivalent... docs/00-PLAN.md Sprint 2 task item 1") rather than
## a second, narrower loader here: it loads every connector's real `data/normalized/*` output
## (richer than the `data/eval/` snapshot) through the same `services.ingest.loader` gate this
## document's dev-loader would otherwise have reimplemented, and already starts api+web as the
## two real subprocesses a production deploy uses. `--preview` bypasses the publish delay so
## today's rows are visible immediately, matching the interactive point of `make dev`.
## Install the commit-msg guard (CLAUDE.md: no model identifiers in commits; owner decision 2026-09-18).
hooks:
	git config core.hooksPath .githooks
	@echo "commit-msg hook installed (.githooks/commit-msg)"

dev: venv
	$(PYTHON) -m web.dev_up --preview

## reparse: restate every stored snapshot under the current parsers, no fetch (docs/61 §4a, docs/22
## §8.4). Idempotent: a source whose latest snapshot the current parser already read is skipped.
## Restatements emit no change events. DATA_DIR overrides the data root (default data/).
reparse: venv
	$(PYTHON) -m pipeline.connectors run --all --reparse $(if $(DATA_DIR),--data-dir $(DATA_DIR),)

## ---------------------------------------------------------------------------------------- tests
## One definition of the suite (audit 2026-10-07 QA-6). CI runs these targets verbatim
## (.github/workflows/ci.yml: `make test-core`, `make test-perf`, `make test-web`), with
## `PYTHON=python VENV=` because the runner installs into its own interpreter. Core paths are
## pyproject.toml's `testpaths`, so a bare `pytest` collects the same tests (serially, latency included).
##   test-core  everything but web/ and the latency tests: parallel (pytest-xdist), branch coverage
##              of production code (pytest-cov; config in pyproject.toml [tool.coverage]).
##   test-perf  the wall-clock budget tests (conftest.py LATENCY_TESTS): serial, no coverage, so the
##              timings mean what the budgets (docs/04 D-13, E-16) say.
##   test-web   web/ on its own (ci.yml header: never in one process with core), parallel, with the
##              two browser modules on one worker (conftest.py BROWSER_TESTS). Needs `make browsers`.
## `XDIST=` runs serially; `COV=` runs without coverage, e.g. `make test-core XDIST= COV=`.
## Every run names its skips (`-rs` in pyproject addopts); docs/04 E-12 lists the ones that run nowhere.
CORE_PATHS := tests pipeline services infra
XDIST ?= -n auto --dist loadgroup
COV ?= --cov --cov-report=
VENV ?= venv
CADDY_VERSION := 2.8.4
CADDY_SHA512 := b8bec15d14fb033562af9f207850027bcbaa1f891edc9efe00d38bf39e1bf9944f8b6b8eba041ddd4c171cd70c905174c704d705be2f23bc678fe1eaf37a2485
CADDY_DIR ?= $(HOME)/.cache/bankable-caddy-$(CADDY_VERSION)
# infra/test_caddyfile.py skips 37 of its 42 tests without a Caddy binary; use the pinned one
# `make caddy` fetched, unless the caller already set CADDY_BIN.
export CADDY_BIN ?= $(wildcard $(CADDY_DIR)/caddy)

test: test-core test-perf test-web

test-core: $(VENV)
	$(PYTHON) -m pytest $(CORE_PATHS) -m "not latency" $(XDIST) $(COV)

test-perf: $(VENV)
	$(PYTHON) -m pytest $(CORE_PATHS) -m latency

test-web: $(VENV)
	$(PYTHON) -m pytest web $(XDIST) $(COV)

browsers: $(VENV)
	$(PYTHON) -m playwright install --with-deps chromium

## caddy: the Caddy release compose.prod.yml pins, checksum-verified (the release's own SHA-512)
## before unpacking; linux amd64, as in CI.
caddy:
	mkdir -p $(CADDY_DIR)
	curl -fsSL -o $(CADDY_DIR)/caddy.tar.gz \
	  https://github.com/caddyserver/caddy/releases/download/v$(CADDY_VERSION)/caddy_$(CADDY_VERSION)_linux_amd64.tar.gz
	echo "$(CADDY_SHA512)  $(CADDY_DIR)/caddy.tar.gz" | sha512sum -c -
	tar -xzf $(CADDY_DIR)/caddy.tar.gz -C $(CADDY_DIR) caddy
	@echo "CADDY_BIN=$(CADDY_DIR)/caddy"

## coverage-gate: docs/04 E-7 over the data `make test-core` left in .coverage: >= 80 % of production
## lines and branches in pipeline/, services/ and infra/ (test modules are omitted in pyproject.toml),
## and the visibility predicate at 100 % with nothing excluded.
COVERAGE_FLOOR := 80
coverage-gate:
	$(PYTHON) -m coverage report --include="pipeline/*,services/*,infra/*" --fail-under=$(COVERAGE_FLOOR)
	$(PYTHON) -m coverage report --include="services/api/visibility.py" -m --fail-under=100

## coverage-floors: per-package floors (docs/04 E-7 "reported per package"; a drop blocks merge).
## Each floor is the package's figure measured on 2026-10-07 (production code, lines + branches,
## `make test-core` data) rounded down, minus one point of slack so a small unrelated change does not
## block a merge. These are ratchets: raise a floor when its package improves, never lower one. A
## floor here never excuses a package from the 80 % standard. pipeline/*.py (resolve, normalize, diff,
## link_dockets, backfill_ferc: 76.9 %) and personal_data.py (77.9 %) are below it and are listed so
## they can only go up. The gate modules' standard is 100 % (E-7); only visibility.py meets it, and
## coverage-gate enforces it. `pipeline/*.py` matches top-level files only; `dir/*` matches recursively.
COVERAGE_FLOORS := \
  "pipeline/*.py=75" \
  "pipeline/connectors/*=88" \
  "pipeline/context/*=80" \
  "services/api/*=94" \
  "services/ingest/*=85" \
  "services/resolve/*=88" \
  "services/alerts/*=92" \
  "services/social/*=90" \
  "services/db/*=84" \
  "infra/*=83" \
  "services/ingest/personal_data.py=76" \
  "services/personal_names.py=87" \
  "services/api/withheld_names.py=95" \
  "services/api/merged_redirect.py=96" \
  "services/api/serialize.py=95" \
  "services/visibility_audit/run.py=89"
coverage-floors:
	@status=0; for entry in $(COVERAGE_FLOORS); do \
	  pattern="$${entry%=*}"; floor="$${entry##*=}"; \
	  pct=$$($(PYTHON) -m coverage report --include="$$pattern" --format=total 2>/dev/null) || pct=0; \
	  verdict=ok; awk "BEGIN{exit !($$pct < $$floor)}" && { verdict="BELOW FLOOR"; status=1; }; \
	  printf '%-40s %6s%%  floor %3s%%  %s\n' "$$pattern" "$$pct" "$$floor" "$$verdict"; \
	done; exit $$status

## lint: ruff check + format --check, repo-wide (docs/04 E-5). The extend-per-file-ignore matches
## the convention pyproject.toml already uses for other CLI-reporting test files (T20 pattern);
## infra/scheduler/test_cadence.py just isn't in that file yet — see its own header comment.
lint: venv
	$(PYTHON) -m ruff check . --extend-per-file-ignores "infra/scheduler/test_cadence.py:S101"
	$(PYTHON) -m ruff format --check .

## typecheck: mypy --strict on pipeline/services/web (pyproject.toml's configured scope) plus infra/.
typecheck: venv
	$(PYTHON) -m mypy
	$(PYTHON) -m mypy --strict infra/

## build: build every service image (needs a running Docker daemon — absent in some sandboxes,
## including the one this task was authored in; see docs/60-deployment.md §9).
build:
	docker build -f infra/docker/Dockerfile --target api -t infraque-api:local .
	docker build -f infra/docker/Dockerfile --target web -t infraque-web:local .
	docker build -f infra/docker/Dockerfile --target worker -t infraque-worker:local .
	docker build -f infra/docker/Dockerfile.browser-worker -t infraque-browser-worker:local .

## deploy-stub: the only target that cannot work locally (task brief) — no cloud account exists
## yet (docs/60-deployment.md §11 item 1). Prints what a real deploy needs instead of pretending.
deploy-stub:
	@echo "No Hetzner/Cloudflare/managed-Postgres account exists yet (docs/60-deployment.md §11)."
	@echo "Once one does: infra/scripts/deploy.sh <staging|production> <image-tag>"
	@echo "See docs/60-deployment.md §10.1 for the full runbook."

## backup / restore-drill: thin wrappers so the runbooks (docs/60-deployment.md §10.3) are
## discoverable from the same place as everything else; the scripts themselves need real
## R2/Postgres credentials to do anything (docs/60-deployment.md §11).
backup:
	infra/scripts/backup.sh

restore-drill:
	infra/scripts/restore_drill.sh

clean:
	rm -rf web/.data
