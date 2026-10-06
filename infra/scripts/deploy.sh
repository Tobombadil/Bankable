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
#   4. run migrations ONCE, in a one-off container of the same api image (expand phase, E-11),
#      then install or upgrade Procrastinate's job-queue schema, once, from the worker image
#      (infra/scheduler/queue_schema.py: idempotent, version-recorded, guarded by an advisory lock);
#   5. start caddy/api/web on the app VM and wait, bounded, until every api/web replica is healthy
#      and /v1/health answers with `checks.queue: true` — on failure roll back to the previous tag;
#   6. start the workers; 7. start the scheduler last (it never enqueues work no worker is up for).
# Re-running with the same tag is a no-op at every step (pull, migrate, queue schema, up -d are
# idempotent). Before any of it, the decrypted secrets must carry a non-empty API_INTERNAL_TOKEN:
# without it every call `web` makes shares the anonymous 60/hour bucket and pages turn into 429s
# (docs/60 §11 item 9), so the deploy refuses rather than starting a `web` that fails after a few views.
# The same goes for every other key in REQUIRED_KEYS below (the list infra/sops/secrets.example.plain.yaml
# marks `required`; devops audit 2026-09-30 F3): a missing DOMAIN took Caddy down and a missing
# PLATFORM_POSTURE changed what was published, with no error. The environment argument is written
# into the shipped env file as ENVIRONMENT, so staging no longer reports itself as production.
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

# ---------------------------------------------------------------- secrets, decrypted once, checked first
# sops converts the YAML secrets file to KEY=VALUE lines: that is what `--env-file`, `env_file:`
# and the systemd backup unit's EnvironmentFile all read. Held in memory only (never written
# locally) and piped to each host below, exactly as the per-host `sops -d | ssh` pipe did before.
secrets_dotenv="$(SOPS_AGE_KEY="$SOPS_AGE_KEY" sops -d --input-type yaml --output-type dotenv "infra/sops/secrets.${environment}.enc.yaml")"
dotenv_value() { # <KEY>: the last assignment of KEY in the decrypted secrets, quotes stripped
  local line
  line="$(grep -E "^${1}=" <<<"$secrets_dotenv" | tail -n 1 || true)"
  line="${line#"${1}"=}"
  line="${line%\"}"; line="${line#\"}"; line="${line%\'}"; line="${line#\'}"
  printf '%s' "$line"
}
refuse() { echo "[deploy $environment] refusing to deploy: $*" >&2; exit 1; }
if [[ -z "$(dotenv_value API_INTERNAL_TOKEN)" ]]; then
  echo "[deploy $environment] refusing to deploy: API_INTERNAL_TOKEN is empty or missing in infra/sops/secrets.${environment}.enc.yaml." >&2
  echo "[deploy $environment] web shares the anonymous 60/hour API bucket without it and starts failing after a few page views (docs/60 §11 item 9)." >&2
  exit 1
fi
# Keep in step with the keys marked `required` in infra/sops/secrets.example.plain.yaml
# (infra/test_scripts.py compares the two).
REQUIRED_KEYS=(
  API_INTERNAL_TOKEN SESSION_SECRET AUDIT_HASH_PEPPER DATABASE_URL
  DOMAIN PLATFORM_POSTURE MAP_TILE_URL
  SNAPSHOT_STORE R2_ACCOUNT_ID R2_BUCKET R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
  SENDER_LEGAL_NAME SENDER_POSTAL_ADDRESS PRODUCT_NAME PRODUCT_URL PRODUCT_CONTACT_EMAIL
)
missing=()
for key in "${REQUIRED_KEYS[@]}"; do
  [[ -n "$(dotenv_value "$key")" ]] || missing+=("$key")
done
(( ${#missing[@]} == 0 )) || refuse "empty or missing in infra/sops/secrets.${environment}.enc.yaml: ${missing[*]} (infra/sops/secrets.example.plain.yaml lists every key)"
case "$(dotenv_value PLATFORM_POSTURE)" in
  commercial|noncommercial) ;;
  *) refuse "PLATFORM_POSTURE must be commercial or noncommercial, not '$(dotenv_value PLATFORM_POSTURE)' (docs/60 §5.1)" ;;
esac
[[ "$(dotenv_value SNAPSHOT_STORE)" == "s3" ]] || refuse "SNAPSHOT_STORE must be s3: fetch and load run on different hosts (docs/60 §5)"
declared_environment="$(dotenv_value ENVIRONMENT)"
if [[ -n "$declared_environment" && "$declared_environment" != "$environment" ]]; then
  refuse "infra/sops/secrets.${environment}.enc.yaml says ENVIRONMENT=${declared_environment}"
fi
# The environment is the argument, not something the secrets file can get wrong: one ENVIRONMENT
# line, last, is what compose.prod.yml interpolates and every container receives.
secrets_dotenv="$(grep -v -E '^ENVIRONMENT=' <<<"$secrets_dotenv" || true)"
secrets_dotenv+=$'\n'"ENVIRONMENT=${environment}"

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
  printf '%s\n' "$secrets_dotenv" | remote "$host" "umask 077 && cat > ${env_file} && chmod 600 ${env_file}"
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

require_queue_ready() { # /v1/health from inside the network must answer AND report the queue schema
  local body
  body="$(remote_compose "$APP_HOST" run --rm --no-deps -T api curl -sf --max-time 5 http://api:8000/v1/health)" || return 1
  if [[ "$body" != *'"queue":true'* ]]; then
    log "/v1/health answered but checks.queue is not true: the job-queue schema is missing (workers would crash-loop)"
    return 1
  fi
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
log "4/7 installing or upgrading the job-queue schema (Procrastinate, from the ${image_tag} worker image)"
# `python -m infra.scheduler.queue_schema ensure`, except that an image built before that module
# existed (a rollback target from before 2026-09-27) says so and changes nothing instead of failing
# the rollback: the schema a newer image installed stays, which is what that image needs anyway.
queue_schema_py='import importlib.util as u, runpy, sys; m = "infra.scheduler.queue_schema"; sys.exit(print("queue schema step skipped: this image predates " + m) if u.find_spec(m) is None else runpy.run_module(m, run_name="__main__"))'
remote_compose "$APP_HOST" run --rm --no-deps -T scheduler python -c "'${queue_schema_py}'" ensure

log "5/7 starting caddy/api/web on $APP_HOST (behind the Cloudflare edge cache) and waiting for health"
remote_compose "$APP_HOST" up -d --no-build caddy api web
wait_healthy "$APP_HOST" api web
require_queue_ready

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
