# One base image, per-process entrypoints (docs/20 §4.1). Build with `--target api|web|worker`.
# Build context is the repo root (see infra/compose/*.yml `build.context: ../..`).
#
# Base Python: 3.11 to match the runtime every test suite in this repo has actually been run
# against (services/README.md, pipeline/README.md, web/README.md all report "Python 3.11.15");
# docs/20 §15 recommends 3.12 for production and ADR 0002 has no pinned patch version — bumping to
# 3.12 is a one-line FROM change once the team has run the suite there, not a DevOps decision to
# make silently here.

FROM python:3.11-slim-bookworm AS base

# libpq5 is the runtime half of the Postgres client library that psycopg (below) links against;
# curl is used by the healthchecks in infra/compose/*.yml.
# Unpinned Debian package versions are deliberate: pinning to the snapshot available on the base
# image's release date breaks the next `docker build` after a security update lands in bookworm;
# the base image itself is pinned by digest and scanned in CI (docs/04 E-20).
# hadolint ignore=DL3008
# `apt-get upgrade` pulls the Debian security point releases the slim base image lags behind
# (first CI image scan, 2026-09-15: libpcre2 10.42-1 flagged HIGH, fixed in 10.42-1+deb12u1).
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && apt-get install -y --no-install-recommends libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./requirements.txt
COPY infra/scheduler/requirements.txt ./infra-scheduler-requirements.txt

# requirements.txt has no Postgres driver — every test suite in this repo runs against SQLite
# (services/README.md "Postgres vs SQLite" section). psycopg is added here, at the image layer,
# so a production container can actually reach the managed Postgres ADR 0003 specifies; the
# durable fix is a pyproject.toml/requirements.txt entry from whoever owns that lockfile
# (docs/CHANGELOG.md line records this as a flagged gap, not silently worked around). Procrastinate
# (ADR 0004) is installed from infra/scheduler/requirements.txt for the same reason — only the
# `worker` target actually runs it, but one shared base layer keeps the image count at two
# (docs/20 §4.1 "one container image with different entrypoints").
RUN pip install --no-cache-dir -r requirements.txt -r infra-scheduler-requirements.txt "psycopg[binary]>=3.1,<4"
# The CI image scan flags wheel 0.45.1 and jaraco.context 5.3.0 (CVE-2026-24049, CVE-2026-23949).
# Those are the copies vendored inside setuptools 78.1.1 (`setuptools/_vendor/*.dist-info`), not
# top-level installs, so upgrading the top-level packages changes nothing (tried, 2026-09-15). They
# move only when setuptools does, and gridstatus 0.36.0 pins setuptools<79; see .trivyignore.

COPY pyproject.toml ./pyproject.toml
COPY pipeline ./pipeline
COPY services ./services
COPY data/sources.yaml ./data/sources.yaml
COPY api/openapi.yaml ./api/openapi.yaml
COPY infra/__init__.py ./infra/__init__.py
COPY infra/entrypoint.py infra/logging_config.py infra/observability.py ./infra/
COPY infra/scheduler ./infra/scheduler

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BANKABLE_DOMAIN=example-infraque.test

RUN useradd --create-home --uid 10001 appuser
USER appuser

# Every process starts through infra/entrypoint.py, which wires JSON logging
# (infra/logging_config.py) and env-gated error tracking (infra/observability.py, SENTRY_DSN)
# before importing the service; uvicorn for api/web is started from there (2 workers for api,
# 1 for web, override with WEB_CONCURRENCY).

# ---- api -------------------------------------------------------------------
FROM base AS api
EXPOSE 8000
CMD ["python", "-m", "infra.entrypoint", "api"]

# ---- web ---------------------------------------------------------------------
FROM base AS web
COPY web ./web
EXPOSE 8001
CMD ["python", "-m", "infra.entrypoint", "web"]

# ---- worker (scheduler / worker-plain / worker-model / social dry-run) -------
FROM base AS worker
# No network-facing port; the command is set per-service in infra/compose/*.yml
# (`infra.entrypoint scheduler` for the scheduler, `infra.entrypoint worker --queues ...` for
# job consumers).
CMD ["python", "-m", "infra.entrypoint", "worker", "--queues", "fetch,diff,normalise,resolve,alert,post_draft,publish_post,sor_sync,webhook"]
