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

.PHONY: help venv dev test test-core test-web lint typecheck build deploy-stub backup restore-drill clean

help:
	@echo "Targets: venv dev test test-core test-web lint typecheck build deploy-stub backup restore-drill clean"

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
dev: venv
	$(PYTHON) -m web.dev_up --preview

## test: the full suite this task can validate in one shared-session snapshot (docs/60 §9 explains
## why web/ is a separate job from pipeline/services rather than one combined run).
test: test-core test-web

test-core: venv
	$(PYTHON) -m pytest tests pipeline services infra/scheduler --ignore=tests/test_web_default_view.py

test-web: venv
	$(PYTHON) -m playwright install --with-deps chromium
	$(PYTHON) -m pytest web -v

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
