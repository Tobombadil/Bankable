#!/usr/bin/env bash
# Rollback runbook, automated (docs/04 O-5: "re-deploy the previous digest"). Re-runs deploy.sh
# with an older image tag; migrations are NOT downgraded by default (E-11: downgrade only if the
# migration's docstring says reversible — the normal path is expand/contract, where the old image
# simply runs unmodified against the new-but-compatible schema).
#
# With --downgrade-migration (docs/60 §10.2), in this order:
#   1. read the revision the previous image expects: `alembic heads` IN THE PREVIOUS IMAGE (exactly
#      one head, or it refuses);
#   2. stop the workers and the scheduler, so no job's transaction holds a lock the DDL waits on;
#   3. dump the database (the nightly backup unit, run now) and refuse without a dump;
#   4. downgrade to that revision IN THE NEWER IMAGE, the one being rolled back from. Only it has the
#      newer revision's script, and so its `downgrade()`: run in the previous image, alembic refused
#      with "Can't locate revision" (reproduced 2026-10-10, docs/60 §10.2);
#   5. redeploy the previous tag (deploy.sh, no migration), which starts the workers and scheduler.
# If step 3 or 4 fails, what step 2 stopped is started again as it was and nothing is redeployed. The
# newer tag is the host's `current-tag` (what deploy.sh last recorded), or --from <tag> when the host
# already runs the previous tag (an automatic rollback has happened since).
#
# deploy.sh calls this itself when the health check fails after the schema step; the
# INFRAQUE_NO_AUTO_ROLLBACK guard below stops that from recursing if the rollback also fails.
set -euo pipefail

usage() {
  cat <<EOF
Usage: rollback.sh <environment> <previous-image-tag> [--downgrade-migration [--from <newer-tag>]]

  --downgrade-migration   only pass this if the migration being rolled back from documents
                          itself as reversible (docs/04 E-11); otherwise leave the schema as-is.
  --from <newer-tag>      the image whose migration is undone (default: the host's current-tag).
                          Needed when the host already runs <previous-image-tag>.
Hosts and keys as deploy.sh (APP_HOST, WORKER_HOSTS, BROWSER_WORKER_HOST or SINGLE_HOST=1, SOPS_AGE_KEY).
EOF
}

environment="${1:-}"
previous_tag="${2:-}"
[[ -z "$environment" || -z "$previous_tag" ]] && { usage; exit 1; }
shift 2
downgrade=""
from_tag=""
while (( $# )); do
  case "$1" in
    --downgrade-migration) downgrade=1 ;;
    --from) from_tag="${2:-}"; [[ -n "$from_tag" ]] || { usage; exit 1; }; shift ;;
    *) usage; exit 1 ;;
  esac
  shift
done
[[ -z "$from_tag" || -n "$downgrade" ]] || { echo "[rollback] --from only goes with --downgrade-migration" >&2; exit 1; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
remote_dir="/opt/infraque"
env_file="${remote_dir}/secrets/.env"
single_host="${SINGLE_HOST:-}"
compose_files="-f docker-compose.yml -f compose.prod.yml${single_host:+ -f compose.single.yml}"
alembic="alembic -c services/db/migrations/alembic.ini"

if [[ -n "$downgrade" ]]; then
  # What deploy.sh needs to redeploy afterwards, checked before the schema changes rather than after.
  : "${APP_HOST:?set APP_HOST}"
  : "${SOPS_AGE_KEY:?set SOPS_AGE_KEY (deploy.sh redeploys the previous tag after the downgrade)}"
  if [[ -n "$single_host" ]]; then
    worker_hosts="$APP_HOST"
  else
    : "${WORKER_HOSTS:?set WORKER_HOSTS, or SINGLE_HOST=1}"
    : "${BROWSER_WORKER_HOST:?set BROWSER_WORKER_HOST, or SINGLE_HOST=1}"
    worker_hosts="$WORKER_HOSTS"
  fi
  compose_on() { # <host> <tag> <compose args...>: in the host's compose dir, with its env file
    local host="$1" tag="$2"; shift 2
    ssh "${SSH_USER:-root}@${host}" "cd ${remote_dir}/compose && IMAGE_TAG=${tag} INFRAQUE_ENV_FILE=${env_file} docker compose ${compose_files} --env-file ${env_file} $*"
  }
  pipeline() { # stop|start: the workers and the scheduler, as deploy.sh steps 3 and 6-7 do
    local host
    for host in $worker_hosts; do compose_on "$host" "$from_tag" "$1" worker; done
    [[ -n "$single_host" ]] || compose_on "$BROWSER_WORKER_HOST" "$from_tag" "$1" browser-worker
    compose_on "$APP_HOST" "$from_tag" "$1" scheduler
  }
  give_up() { # nothing was downgraded: put back what step 2 stopped and stop here
    echo "[rollback] $*; not downgrading" >&2
    pipeline start || true
    echo "[rollback] restarted the workers and the scheduler as they were; the schema and the running tag are unchanged" >&2
    exit 1
  }

  if [[ -z "$from_tag" ]]; then
    from_tag="$(ssh "${SSH_USER:-root}@${APP_HOST}" "cat ${remote_dir}/current-tag 2>/dev/null" || true)"
  fi
  [[ -n "$from_tag" ]] || { echo "[rollback] no current-tag on ${APP_HOST}: name the newer image with --from <tag>" >&2; exit 1; }
  [[ "$from_tag" != "$previous_tag" ]] || {
    echo "[rollback] ${APP_HOST} already runs ${previous_tag}; name the image whose migration to undo with --from <tag>" >&2
    exit 1
  }

  # 1. The previous image's code expects its own head; `heads` reads the scripts only (no database).
  heads="$(compose_on "$APP_HOST" "$previous_tag" run --rm --no-deps -T api "$alembic heads")" \
    || { echo "[rollback] could not read the migration head of ${previous_tag}; not downgrading" >&2; exit 1; }
  mapfile -t target < <(awk '$2 == "(head)" { print $1 }' <<<"$heads")
  (( ${#target[@]} == 1 )) || {
    echo "[rollback] ${previous_tag} must have exactly one migration head, found: ${heads:-none}; not downgrading" >&2
    exit 1
  }

  # 2. Nothing writes while the schema changes (deploy.sh step 3); each stop waits for running jobs.
  echo "[rollback] stopping the workers and the scheduler"
  pipeline stop

  # 3. A downgrade drops what the migration added: dump first, as deploy.sh does before an upgrade
  # (the nightly backup unit, run now; its `last-success` stamp must move), and refuse without one.
  echo "[rollback] dumping the database before the downgrade (infraque-backup.service)"
  stamp="${remote_dir}/backups/last-success"
  ssh "${SSH_USER:-root}@${APP_HOST}" \
    "before=\$(cat ${stamp} 2>/dev/null || true); systemctl start infraque-backup.service && after=\$(cat ${stamp} 2>/dev/null || true) && [ -n \"\$after\" ] && [ \"\$after\" != \"\$before\" ]" \
    || give_up "the dump failed (journalctl -u infraque-backup on ${APP_HOST})"

  # 4. In the newer image, which has the scripts between the two heads. services/db/migrations/env.py
  # runs it in one transaction (no migration uses an autocommit block), so a failure changes nothing.
  echo "[rollback] downgrading to ${target[0]} (the head of ${previous_tag}) in ${from_tag}, which has the newer scripts (confirmed reversible by the operator)"
  compose_on "$APP_HOST" "$from_tag" run --rm --no-deps -T api "$alembic downgrade ${target[0]}" \
    || give_up "the downgrade in ${from_tag} failed"
fi

echo "[rollback] redeploying previous image tag: $previous_tag"
# INFRAQUE_ROLLBACK: no dump and no `alembic upgrade` (deploy.sh step 4). The older image's upgrade
# fails outright once the newer migration has run, and a rollback must not depend on R2 answering.
# A downgrade above took its own dump.
INFRAQUE_NO_AUTO_ROLLBACK=1 INFRAQUE_ROLLBACK=1 "$(dirname "${BASH_SOURCE[0]}")/deploy.sh" "$environment" "$previous_tag"

echo "[rollback] done. If entity tables need repair after a bad deploy, run the docs/21 §6.5 'replay' procedure next (docs/04 O-5)."
