#!/usr/bin/env bash
# Runs ON the beta host, as the transient systemd unit `infraque-seed-context` that
# `infra/ci/beta.sh seed` starts (.github/workflows/deploy-beta.yml, action `seed`; docs/64 §3, the
# path for a server seeded with no local data). The workflow queues `bootstrap fetch` itself and
# returns; this waits here, bounded, until every fetch and the load, resolve, enrich and match jobs
# the fetches chained have finished, then queues `bootstrap context --build`. The context build reads
# the EIA-860M, EIA-860 and GHGRP snapshots those fetches store, and its `context_load` writes
# organisations, so it starts after the loads rather than beside them (seed_single_host.sh's rule).
#
#   journalctl -u infraque-seed-context      progress (or the workflow's `status` action)
#
# Exits 1, queueing nothing, if jobs are still pending after SEED_WAIT_SECONDS (default 4 h): run
# the workflow's `seed` again once `status` shows an empty queue; fetches already queued are reported
# `already_queued` and the waiter starts over.
set -euo pipefail

remote_dir="/opt/infraque"
wait_seconds="${SEED_WAIT_SECONDS:-14400}"
interval="${SEED_INTERVAL_SECONDS:-60}"

log() { echo "[seed] $*"; }
compose() { # the compose files and env file deploy.sh wrote, at the tag it recorded
  (cd "${remote_dir}/compose" && IMAGE_TAG="$(cat "${remote_dir}/current-tag")" \
    INFRAQUE_ENV_FILE="${remote_dir}/secrets/.env" docker compose -f docker-compose.yml \
    -f compose.prod.yml -f compose.single.yml --env-file "${remote_dir}/secrets/.env" "$@")
}
sql() { compose exec -T postgres psql -U infraque -d infraque -tAc "$1"; }

pending_sql="select count(*) from procrastinate_jobs where status in ('todo', 'doing') and task_name in ('run_connector', 'load_source', 'resolve_tick', 'enrich_tick', 'match_tick', 'context_build', 'context_load')"
deadline=$(( $(date +%s) + wait_seconds ))
while :; do
  pending="$(sql "$pending_sql" | tr -d '[:space:]')"
  [[ "$pending" == "0" ]] && break
  if (( $(date +%s) >= deadline )); then
    log "${pending:-?} fetch/load/resolve jobs still pending after ${wait_seconds}s; the context build was not queued." >&2
    log "Run the workflow's seed action again once its status action shows an empty queue." >&2
    exit 1
  fi
  log "waiting: ${pending:-?} fetch/load/resolve/match jobs queued or running"
  sleep "$interval"
done

log "fetches by outcome:"
sql "select status, count(*) from procrastinate_jobs where task_name = 'run_connector' group by status order by status"
log "queueing the context build (bootstrap context --build; it chains into context_load)"
compose run --rm --no-deps -T worker python -m infra.scheduler.bootstrap context --build
log "done: the worker builds and loads the context layers next (about 10 minutes, docs/64 §7); then run preflight."
