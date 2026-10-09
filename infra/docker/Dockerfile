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

# The Postgres driver (`psycopg[binary]`) comes from requirements.txt since 2026-09-26; until then
# this line installed it a second time as a stopgap (docs/60 §11 item 4). Procrastinate (ADR 0004)
# is also pinned there (==3.9.0) and bounded again in infra/scheduler/requirements.txt — only the
# `worker` target actually runs it, but one shared base layer keeps the image count at two
# (docs/20 §4.1 "one container image with different entrypoints").
# `--mount=type=secret,id=ca_bundle` is a build-time hook for sandboxes whose egress re-terminates
# TLS with a private CA (first real build, 2026-09-26, behind such a proxy): pass the bundle with
# `docker buildx build --secret id=ca_bundle,src=<file>` (or Compose `build.secrets`) and pip trusts
# it for this step only. Nothing is copied into the image, and when no secret is supplied the mount
# is absent and the `if` is a no-op, so a plain `docker build` installs exactly what it did before.
RUN --mount=type=secret,id=ca_bundle,target=/run/secrets/ca_bundle \
    if [ -s /run/secrets/ca_bundle ]; then export PIP_CERT=/run/secrets/ca_bundle; fi \
    && pip install --no-cache-dir -r requirements.txt -r infra-scheduler-requirements.txt 'setuptools>=83'
# `setuptools>=83`: the base image ships setuptools 79.0.1, whose vendored copies of wheel 0.45.1 and
# jaraco.context 5.3.0 (`setuptools/_vendor/*.dist-info`) the CI image scan flags (CVE-2026-24049,
# CVE-2026-23949; fixed in wheel 0.46.2 / jaraco.context 6.1.0, vendored from setuptools 82.0.0 on);
# 83.0.0 also fixes PYSEC-2026-3447. Upgrading the top-level wheel changes nothing (tried,
# 2026-09-15). gridstatus 0.36.0 held setuptools below 79 until its parsers were vendored
# (pipeline/vendor/gridstatus, 2026-10-07). Nothing imports setuptools or pkg_resources at runtime.

COPY pyproject.toml ./pyproject.toml
COPY pipeline ./pipeline
COPY services ./services
COPY data/sources.yaml ./data/sources.yaml
# Reference data the code reads at runtime from the app tree (not the connector_data volume):
# vocabularies, vendored regions/organisations/suppressions, match rules and three label files.
# Pinned by infra/test_image_data.py, which fails when code reads a data path this image lacks.
COPY data/vocabulary ./data/vocabulary
COPY data/vendored ./data/vendored
COPY data/match_rules.yaml ./data/match_rules.yaml
COPY data/eval/ethanol_match_labels.csv data/eval/gleif_matches.csv data/eval/match_labels.csv ./data/eval/
COPY api/openapi.yaml ./api/openapi.yaml
COPY infra/__init__.py ./infra/__init__.py
COPY infra/entrypoint.py infra/logging_config.py infra/observability.py ./infra/
COPY infra/scheduler ./infra/scheduler

# The commit this image was built from, read by services/api/build_info.py for `/v1/health`'s
# `build.commit` and the site footer. The image has no `.git` and no `git` binary, so without this
# stamp the footer reads "Build unknown" (first local stack run, 2026-09-26). release.yml passes
# `GIT_SHA=${{ github.sha }}`; Compose passes `${GIT_SHA:-}`. Declared after the dependency layers
# so a new commit rebuilds only the layers below this line; empty means "unavailable", as before.
ARG GIT_SHA=""
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    BANKABLE_DOMAIN=example-infraque.test \
    GIT_SHA=${GIT_SHA}

# Connector output (snapshots, normalised parquet, events, run records; pipeline/connectors/
# store.py) goes to INFRAQUE_DATA_DIR, not /app/data: /app is root-owned and read-only to the
# process, and /app/data holds sources.yaml, which a volume mounted there would hide. The directory
# is created owned by appuser so the named volume Compose mounts on it (`connector_data`) starts
# out writable by uid 10001; before 2026-09-27 every scheduled run ended in `PermissionError:
# '/app/data/runs'` (docs/60 §11 item 9). This is the single-host local mode; the object-storage
# mode docs/20 §2 specifies is a separate backend (lane E10b).
ENV INFRAQUE_DATA_DIR=/var/lib/infraque/data
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p "$INFRAQUE_DATA_DIR" \
    && chown appuser:appuser "$INFRAQUE_DATA_DIR"
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
