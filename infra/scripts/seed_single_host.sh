#!/usr/bin/env bash
# Seed a single-host deploy (SINGLE_HOST=1 infra/scripts/deploy.sh; docs/64 §4) from a local data root:
# the one built by docs/61 §4 (`python -m pipeline.connectors run --all`, the pipeline.context
# builders), so the server starts with the same connector runs and context layers instead of an
# empty store. Run once, after the first deploy, from the operator's checkout:
#
#   APP_HOST=<ip> infra/scripts/seed_single_host.sh [data-root]   # default: data/
#
# Order (each step is a one-off container of the deployed images, through the same compose files
# and env file deploy.sh wrote):
#   1. ship runs/, snapshots/, normalized/ (with normalized/context/) and held/ into the
#      `connector_data` volume, owned by the image's appuser;
#   2. queue one load per source at its latest promoted run (infra/scheduler/bootstrap.py `load`),
#      which the running worker takes, each chaining into resolve and enrich;
#   3. wait, bounded, until no load/resolve/enrich job is queued or running;
#   4. load what the scheduler never loads: the context asset layers, the EIA-860M retirements
#      onto them, and proposal-opportunity matches (`python -m web.dev_up --context-only`);
#   5. queue one fetch per source (bootstrap `fetch`), so every source gets its `source_run` row
#      and freshness now rather than at its bucket's next tick.
# Safe to repeat: a re-shipped file overwrites its copy, a repeated load is idempotent, and a
# source whose job is still queued is reported `already_queued`.
set -euo pipefail

data_root="${1:-data}"
: "${APP_HOST:?set APP_HOST (tofu output app_ipv4)}"
ssh_user="${SSH_USER:-root}"
remote_dir="/opt/infraque"
env_file="${remote_dir}/secrets/.env"
compose_files="-f docker-compose.yml -f compose.prod.yml -f compose.single.yml"
volume="infraque_connector_data" # `name: infraque` (docker-compose.yml) + the `connector_data` volume
data_dir="/var/lib/infraque/data" # INFRAQUE_DATA_DIR in infra/docker/Dockerfile
drain_timeout="${SEED_DRAIN_TIMEOUT_SECONDS:-3600}"
drain_interval="${SEED_DRAIN_INTERVAL_SECONDS:-15}"

log() { echo "[seed] $*"; }
remote() { ssh "${ssh_user}@${APP_HOST}" "$@"; }
remote_compose() { # IMAGE_TAG: whatever deploy.sh last recorded on the host
  remote "cd ${remote_dir}/compose && IMAGE_TAG=\$(cat ${remote_dir}/current-tag) INFRAQUE_ENV_FILE=${env_file} docker compose ${compose_files} --env-file ${env_file} $*"
}

[[ -d "$data_root" ]] || { echo "[seed] no data root at ${data_root}" >&2; exit 1; }
parts=()
for part in runs snapshots normalized held; do
  [[ -d "${data_root}/${part}" ]] && parts+=("$part")
done
(( ${#parts[@]} > 0 )) || { echo "[seed] ${data_root} holds none of runs/ snapshots/ normalized/ held/" >&2; exit 1; }
[[ -d "${data_root}/normalized/context" ]] || log "warning: ${data_root}/normalized/context is absent; no asset layer will load"

log "1/5 shipping ${parts[*]} from ${data_root} into ${volume} on ${APP_HOST}"
# -h follows symlinked directories (a checkout whose data/ links to another root).
tar -C "$data_root" -czh "${parts[@]}" \
  | remote_compose "run --rm --no-deps -T --user root --entrypoint sh worker -c 'tar -xz -C ${data_dir} && chown -R appuser:appuser ${data_dir}'"

log "2/5 queueing one load per source at its latest promoted run"
remote_compose "run --rm --no-deps -T worker python -m infra.scheduler.bootstrap load"

log "3/5 waiting for the loads, resolve and enrich to finish (at most ${drain_timeout}s)"
pending_sql="select count(*) from procrastinate_jobs where status in ('todo', 'doing') and task_name in ('load_source', 'resolve_tick', 'enrich_tick')"
deadline=$(( $(date +%s) + drain_timeout ))
while :; do
  pending="$(remote_compose "exec -T postgres psql -U infraque -d infraque -tAc \"${pending_sql}\"" | tr -d '[:space:]')"
  [[ "$pending" == "0" ]] && break
  if (( $(date +%s) >= deadline )); then
    echo "[seed] ${pending:-?} load/resolve jobs still pending after ${drain_timeout}s; rerun this script once they finish" >&2
    exit 1
  fi
  log "   ${pending:-?} pending"
  sleep "$drain_interval"
done

log "4/5 loading the context layers, retirements and matches (web image, ${volume} mounted)"
remote_compose "run --rm --no-deps -T -v ${volume}:${data_dir} web python -m web.dev_up --context-only --data-dir ${data_dir}"

log "5/5 queueing one fetch per source"
remote_compose "run --rm --no-deps -T worker python -m infra.scheduler.bootstrap fetch"

log "done. The site serves the seeded store; the scheduler keeps it fresh from here."
