#!/usr/bin/env bash
# Deploy runbook, automated (docs/04 O-4; docs/60-deployment.md §10.1 is the human-readable
# version of these same steps). Run from CI or by hand from the operator's machine with the SSH
# access OpenTofu granted (infra/terraform/variables.tf `ssh_public_key`).
#
# Order (the point of this script, not an implementation detail):
#   1. ship the target compose files, Caddyfile, backup script and decrypted secrets to EVERY host
#      (so no step ever runs against an unsynced host);
#   2. pull the target images on every host (the release workflow pushed them:
#      ghcr.io/tobombadil/bankable-{api,web,worker,browser-worker}:<tag>);
#   3. stop workers and the scheduler (nothing mid-fetch while the schema changes);
#   4. run migrations ONCE, in a one-off container of the same api image (expand phase, E-11);
#   5. start caddy/api/web on the app VM and wait, bounded, until every api/web replica is healthy
#      and /v1/health answers — on failure roll back to the previously deployed tag;
#   6. start the workers; 7. start the scheduler last (it never enqueues work no worker is up for).
# Re-running with the same tag is a no-op at every step (pull, migrate, up -d are idempotent).
set -Eeuo pipefail # -E: the ERR trap below must fire inside functions too

usage() {
  cat <<EOF
Usage: deploy.sh <environment> [image-tag]

  environment   staging | production
  image-tag     tag pushed by .github/workflows/release.yml (default: \$IMAGE_TAG, then "latest";
                prefer the immutable sha-<short sha> tag for anything but a rehearsal)

Required environment variables:
  APP_HOST, WORKER_HOSTS (space-separated), BROWSER_WORKER_HOST   — from 'tofu output' (infra/terraform)
  SOPS_AGE_KEY      the environment's private age key (see infra/sops/README.md)
Optional:
  SSH_USER          (default: root, matching the docker-ce marketplace image)
  DEPLOY_REF        git ref whose infra/compose files are shipped (default: the working tree)
  GHCR_USER/GHCR_READ_TOKEN   log the VMs into ghcr.io first (needed while the packages are private)
  HEALTH_TIMEOUT_SECONDS (default 180), HEALTH_INTERVAL_SECONDS (default 5)
  INFRAQUE_NO_AUTO_ROLLBACK=1 disables the automatic rollback (rollback.sh sets it to avoid recursion)
EOF
}

environment="${1:-}"
image_tag="${2:-${IMAGE_TAG:-latest}}"
[[ -z "$environment" ]] && { usage; exit 1; }
[[ "$environment" != "staging" && "$environment" != "production" ]] && {
  echo "environment must be staging or production" >&2; exit 1;
}

: "${APP_HOST:?set APP_HOST (tofu output app_ipv4)}"
: "${WORKER_HOSTS:?set WORKER_HOSTS (space-separated tofu output worker_ipv4s)}"
: "${BROWSER_WORKER_HOST:?set BROWSER_WORKER_HOST (tofu output browser_worker_ipv4)}"
: "${SOPS_AGE_KEY:?set SOPS_AGE_KEY to the private age key contents for this environment}"
ssh_user="${SSH_USER:-root}"
remote_dir="/opt/infraque"
env_file="${remote_dir}/secrets/.env"
compose_files="-f docker-compose.yml -f compose.prod.yml"
health_timeout="${HEALTH_TIMEOUT_SECONDS:-180}"
health_interval="${HEALTH_INTERVAL_SECONDS:-5}"
deploy_ref="${DEPLOY_REF:-}"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
deploy_log="${DEPLOY_LOG:-${repo_root}/infra/deploy-log.md}"

log() { echo "[deploy $environment] $*"; }

# ---------------------------------------------------------------- staging the files to ship
stage_dir="$(mktemp -d)"
trap 'rm -rf "$stage_dir"' EXIT
mkdir -p "$stage_dir/compose" "$stage_dir/scripts"
stage_file() { # <repo path> <staged path>
  if [[ -n "$deploy_ref" ]]; then
    git show "${deploy_ref}:$1" > "$2"
  else
    cp "$1" "$2"
  fi
}
for f in docker-compose.yml compose.prod.yml Caddyfile; do
  stage_file "infra/compose/$f" "$stage_dir/compose/$f"
done
stage_file infra/scripts/backup.sh "$stage_dir/scripts/backup.sh"
chmod +x "$stage_dir/scripts/backup.sh"
commit="$(git rev-parse --short "${deploy_ref:-HEAD}" 2>/dev/null || echo unknown)"

# ---------------------------------------------------------------- remote helpers
remote() { local host="$1"; shift; ssh "${ssh_user}@${host}" "$@"; }

remote_compose() { # <host> <compose args...>; runs in the host's compose dir with the env file
  local host="$1"; shift
  remote "$host" "cd ${remote_dir}/compose && IMAGE_TAG=${image_tag} INFRAQUE_ENV_FILE=${env_file} docker compose ${compose_files} --env-file ${env_file} $*"
}

sync_host() { # compose files + backup script + decrypted secrets, before anything runs there
  local host="$1"
  log "syncing compose files (${commit}) and secrets to $host"
  remote "$host" "mkdir -p ${remote_dir}/compose ${remote_dir}/secrets ${remote_dir}/scripts ${remote_dir}/backups"
  scp -q "$stage_dir"/compose/* "${ssh_user}@${host}:${remote_dir}/compose/"
  scp -q "$stage_dir/scripts/backup.sh" "${ssh_user}@${host}:${remote_dir}/scripts/backup.sh"
  # sops converts the YAML secrets file to KEY=VALUE lines: that is what `--env-file`, `env_file:`
  # and the systemd backup unit's EnvironmentFile all read.
  SOPS_AGE_KEY="$SOPS_AGE_KEY" sops -d --input-type yaml --output-type dotenv "infra/sops/secrets.${environment}.enc.yaml" \
    | remote "$host" "umask 077 && cat > ${env_file} && chmod 600 ${env_file}"
  if [[ -n "${GHCR_READ_TOKEN:-}" ]]; then
    printf '%s' "$GHCR_READ_TOKEN" | remote "$host" "docker login ghcr.io -u ${GHCR_USER:?set GHCR_USER with GHCR_READ_TOKEN} --password-stdin"
  fi
}

wait_healthy() { # <host> <service...>: every replica reports a healthy healthcheck, bounded
  local host="$1"; shift
  local deadline=$(( $(date +%s) + health_timeout ))
  while :; do
    local statuses
    statuses="$(remote_compose "$host" "ps -q $* | xargs docker inspect -f '{{.State.Health.Status}}' 2>/dev/null | sort -u | tr '\n' ' '" || true)"
    if [[ "${statuses//[[:space:]]/}" == "healthy" ]]; then
      return 0
    fi
    if (( $(date +%s) >= deadline )); then
      log "health check timed out after ${health_timeout}s (statuses: ${statuses:-none})"
      return 1
    fi
    sleep "$health_interval"
  done
}

previous_tag="$(remote "$APP_HOST" "cat ${remote_dir}/current-tag 2>/dev/null" || true)"
rollback_armed=0
on_error() {
  local rc=$?
  trap - ERR
  if (( rollback_armed )) && [[ -n "$previous_tag" && "$previous_tag" != "$image_tag" && -z "${INFRAQUE_NO_AUTO_ROLLBACK:-}" ]]; then
    log "FAILED after the schema step; rolling back to the previous tag ${previous_tag}"
    INFRAQUE_NO_AUTO_ROLLBACK=1 "$repo_root/infra/scripts/rollback.sh" "$environment" "$previous_tag" || true
  else
    log "FAILED (rc=$rc); nothing to roll back automatically (previous tag: '${previous_tag:-none}')"
  fi
  exit "$rc"
}
trap on_error ERR

# ---------------------------------------------------------------- the deploy
log "1/7 syncing files and secrets to every host (target tag ${image_tag}, compose files from ${commit})"
for host in "$APP_HOST" $WORKER_HOSTS "$BROWSER_WORKER_HOST"; do
  sync_host "$host"
done

log "2/7 pulling images on every host"
remote_compose "$APP_HOST" pull --quiet caddy api web scheduler
for host in $WORKER_HOSTS; do
  remote_compose "$host" pull --quiet worker
done
remote_compose "$BROWSER_WORKER_HOST" pull --quiet browser-worker

log "3/7 stopping workers and the scheduler"
rollback_armed=1
for host in $WORKER_HOSTS; do
  remote_compose "$host" stop worker
done
remote_compose "$BROWSER_WORKER_HOST" stop browser-worker
remote_compose "$APP_HOST" stop scheduler

log "4/7 running migrations once (expand phase, docs/04 E-11) in a one-off container of ${image_tag}"
remote_compose "$APP_HOST" run --rm --no-deps -T api alembic -c services/db/migrations/alembic.ini upgrade head

log "5/7 starting caddy/api/web on $APP_HOST (behind the Cloudflare edge cache) and waiting for health"
remote_compose "$APP_HOST" up -d --no-build caddy api web
wait_healthy "$APP_HOST" api web
remote_compose "$APP_HOST" run --rm --no-deps -T api curl -sf --max-time 5 http://api:8000/v1/health >/dev/null

log "6/7 starting workers"
for host in $WORKER_HOSTS; do
  remote_compose "$host" up -d --no-build worker
done
remote_compose "$BROWSER_WORKER_HOST" up -d --no-build browser-worker

log "7/7 starting the scheduler (singleton) last"
remote_compose "$APP_HOST" up -d --no-build scheduler

remote "$APP_HOST" "printf '%s\n' '${image_tag}' > ${remote_dir}/current-tag"
{
  echo "| $(date -u +%FT%TZ) | $environment | $image_tag | $(whoami) | $commit |"
} >> "$deploy_log"

log "done. Recorded in $deploy_log. Run the E-10 smoke suite against $environment next."
