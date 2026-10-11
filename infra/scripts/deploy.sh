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
#   4. dump the database (the nightly backup unit, run now: backup.sh to R2) and refuse to migrate
#      if that fails, restarting what step 3 stopped; then run migrations ONCE, in a one-off
#      container of the same api image (expand phase, E-11), then install or upgrade Procrastinate's
#      job-queue schema, once, from the worker image (infra/scheduler/queue_schema.py: idempotent,
#      version-recorded, guarded by an advisory lock);
#   5. start caddy/api/web on the app VM and wait, bounded, until every api/web replica is healthy
#      and /v1/health answers with `checks.queue: true` — on failure roll back to the previous tag;
#   6. start the workers; 7. start the scheduler last (it never enqueues work no worker is up for).
# With SINGLE_HOST=1 (the private beta, docs/64) every service runs on APP_HOST: compose.single.yml
# joins the compose files, the in-stack Postgres starts and must be healthy before step 4, the
# worker starts on APP_HOST and no browser-worker starts at all.
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
                    (APP_HOST alone with SINGLE_HOST=1)
  SOPS_AGE_KEY      the environment's private age key (see infra/sops/README.md)
Optional:
  SINGLE_HOST=1     one VM runs everything, its own Postgres included (compose.single.yml, docs/64)
  SSH_USER          (default: root, matching the docker-ce marketplace image)
  DEPLOY_REF        git ref whose infra/compose files are shipped (default: the working tree)
  GHCR_USER/GHCR_READ_TOKEN   log the VMs into ghcr.io first (needed while the packages are private)
  HEALTH_TIMEOUT_SECONDS (default 180), HEALTH_INTERVAL_SECONDS (default 5)
  INFRAQUE_NO_AUTO_ROLLBACK=1 disables the automatic rollback (rollback.sh sets it to avoid recursion)
  SKIP_PRE_MIGRATION_DUMP=1   no dump before the migrations (set it by hand only knowing the last dump)
  INFRAQUE_ROLLBACK=1         set by rollback.sh: no dump and no `alembic upgrade` (the schema stays)
EOF
}

environment="${1:-}"
image_tag="${2:-${IMAGE_TAG:-latest}}"
[[ -z "$environment" ]] && { usage; exit 1; }
[[ "$environment" != "staging" && "$environment" != "production" ]] && {
  echo "environment must be staging or production" >&2; exit 1;
}

: "${APP_HOST:?set APP_HOST (tofu output app_ipv4)}"
single_host="${SINGLE_HOST:-}"
[[ -z "$single_host" || "$single_host" == "1" ]] || { echo "SINGLE_HOST must be 1 or unset" >&2; exit 1; }
if [[ -n "$single_host" ]]; then
  # Asked for, never inferred: a three-VM deploy that forgot WORKER_HOSTS must not start a
  # database on the app VM.
  [[ -z "${WORKER_HOSTS:-}${BROWSER_WORKER_HOST:-}" ]] || {
    echo "SINGLE_HOST=1 runs everything on APP_HOST: unset WORKER_HOSTS and BROWSER_WORKER_HOST" >&2; exit 1;
  }
  WORKER_HOSTS="$APP_HOST"
  BROWSER_WORKER_HOST=""
else
  : "${WORKER_HOSTS:?set WORKER_HOSTS (space-separated tofu output worker_ipv4s)}"
  : "${BROWSER_WORKER_HOST:?set BROWSER_WORKER_HOST (tofu output browser_worker_ipv4)}"
fi
: "${SOPS_AGE_KEY:?set SOPS_AGE_KEY to the private age key contents for this environment}"
ssh_user="${SSH_USER:-root}"
remote_dir="/opt/infraque"
env_file="${remote_dir}/secrets/.env"
compose_files="-f docker-compose.yml -f compose.prod.yml${single_host:+ -f compose.single.yml}"
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
for f in docker-compose.yml compose.prod.yml compose.single.yml Caddyfile; do
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
if [[ -n "$single_host" ]]; then
  # One host: fetch and load share the connector_data volume, so `local` works too; and the
  # database is the in-stack container, which DATABASE_URL must name with its password.
  case "$(dotenv_value SNAPSHOT_STORE)" in
    local|s3) ;;
    *) refuse "SNAPSHOT_STORE must be local or s3 on a single host, not '$(dotenv_value SNAPSHOT_STORE)'" ;;
  esac
  db_password="$(dotenv_value POSTGRES_PASSWORD)"
  [[ -n "$db_password" ]] || refuse "POSTGRES_PASSWORD is empty or missing: SINGLE_HOST=1 runs its own database (docs/64)"
  [[ "$(dotenv_value DATABASE_URL)" == "postgresql+psycopg://infraque:${db_password}@postgres:5432/infraque" ]] \
    || refuse "DATABASE_URL must be postgresql+psycopg://infraque:<POSTGRES_PASSWORD>@postgres:5432/infraque on a single host"
else
  [[ "$(dotenv_value SNAPSHOT_STORE)" == "s3" ]] || refuse "SNAPSHOT_STORE must be s3: fetch and load run on different hosts (docs/60 §5)"
fi
# The access gate (Caddyfile `gate_*`, docs/64): `basic` needs its login, and the hash must be the
# base64 of a bcrypt hash (`caddy hash-password`), which carries no `$` into the env file.
case "$(dotenv_value SITE_ACCESS)" in
  ""|open) ;;
  basic)
    [[ -n "$(dotenv_value SITE_ACCESS_USER)" ]] || refuse "SITE_ACCESS=basic needs SITE_ACCESS_USER"
    printf '%s' "$(dotenv_value SITE_ACCESS_HASH)" | base64 -d 2>/dev/null | grep -qE '^\$2[aby]\$' \
      || refuse "SITE_ACCESS_HASH must be the base64 of a bcrypt hash: caddy hash-password | base64 -w0 (docs/64)"
    ;;
  *) refuse "SITE_ACCESS must be open or basic, not '$(dotenv_value SITE_ACCESS)'" ;;
esac
# The content security policy's mode (Caddyfile `csp_*`, docs/64 §5): Caddy refuses anything else.
case "$(dotenv_value CSP_MODE)" in
  ""|report|enforce) ;;
  *) refuse "CSP_MODE must be report or enforce, not '$(dotenv_value CSP_MODE)'" ;;
esac
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

# The dump before the schema changes (docs/51 §2.9 item 4; docs/60 §10.1): the nightly backup
# unit itself (cloud-init app.yaml `infraque-backup.service`: backup.sh with the env file step 1
# shipped, pg_dump 16, the upload to R2), run now. `systemctl start` on a oneshot unit returns when
# backup.sh exits, with its status. The `last-success` stamp backup.sh writes last must have moved,
# so a unit skipped by its conditions, or a run that dumped but failed to upload, cannot pass.
pre_migration_dump() {
  local stamp="${remote_dir}/backups/last-success"
  remote "$APP_HOST" "before=\$(cat ${stamp} 2>/dev/null || true); systemctl start infraque-backup.service && after=\$(cat ${stamp} 2>/dev/null || true) && [ -n \"\$after\" ] && [ \"\$after\" != \"\$before\" ]"
}

restart_stopped() { # what step 3 stopped, as it was: `start` reuses the stopped containers (old image)
  local host
  for host in $WORKER_HOSTS; do
    remote_compose "$host" start worker || true
  done
  [[ -n "$single_host" ]] || remote_compose "$BROWSER_WORKER_HOST" start browser-worker || true
  remote_compose "$APP_HOST" start scheduler || true
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
hosts=("$APP_HOST")
[[ -n "$single_host" ]] || hosts+=($WORKER_HOSTS "$BROWSER_WORKER_HOST")
for host in "${hosts[@]}"; do
  sync_host "$host"
done

log "2/7 pulling images on every host"
remote_compose "$APP_HOST" pull --quiet caddy api web scheduler${single_host:+ worker postgres}
if [[ -z "$single_host" ]]; then
  for host in $WORKER_HOSTS; do
    remote_compose "$host" pull --quiet worker
  done
  remote_compose "$BROWSER_WORKER_HOST" pull --quiet browser-worker
fi

log "3/7 stopping workers and the scheduler"
rollback_armed=1
for host in $WORKER_HOSTS; do
  remote_compose "$host" stop worker
done
[[ -n "$single_host" ]] || remote_compose "$BROWSER_WORKER_HOST" stop browser-worker
remote_compose "$APP_HOST" stop scheduler
if [[ -n "$single_host" ]]; then
  log "4/7 starting the single host's database and waiting for it"
  remote_compose "$APP_HOST" up -d --no-build postgres
  wait_healthy "$APP_HOST" postgres
fi

if [[ -n "${INFRAQUE_ROLLBACK:-}" ]]; then
  # A rollback's target image is older than the schema, or at it: its `alembic upgrade head` either
  # changes nothing or, once the newer migration has run, fails ("Can't locate revision", checked
  # 2026-10-10 with alembic 1.x), which stopped the automatic rollback at this step. Expand/contract
  # (docs/04 E-11) is what lets the older image run on the newer schema; a downgrade is rollback.sh's.
  log "4/7 rollback: no dump and no migration; the schema stays as it is (expand/contract, docs/04 E-11)"
elif [[ -n "${SKIP_PRE_MIGRATION_DUMP:-}" ]]; then
  log "4/7 no dump before the migrations (SKIP_PRE_MIGRATION_DUMP is set)"
else
  log "4/7 dumping the database before the migrations (infraque-backup.service: backup.sh, upload to R2)"
  if ! pre_migration_dump; then
    # Nothing has changed yet: no migration ran and the old api/web still serve. Put back what
    # step 3 stopped and stop here, without a rollback (there is nothing to roll back).
    rollback_armed=0
    log "FAILED: the pre-migration dump did not complete (journalctl -u infraque-backup on ${APP_HOST}); not migrating"
    restart_stopped
    log "restarted the stopped workers and scheduler as they were; the database and the running tag are unchanged"
    exit 1
  fi
fi
if [[ -z "${INFRAQUE_ROLLBACK:-}" ]]; then
  log "4/7 running migrations once (expand phase, docs/04 E-11) in a one-off container of ${image_tag}"
  remote_compose "$APP_HOST" run --rm --no-deps -T api alembic -c services/db/migrations/alembic.ini upgrade head
fi
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
[[ -n "$single_host" ]] || remote_compose "$BROWSER_WORKER_HOST" up -d --no-build browser-worker

log "7/7 starting the scheduler (singleton) last"
remote_compose "$APP_HOST" up -d --no-build scheduler

remote "$APP_HOST" "printf '%s\n' '${image_tag}' > ${remote_dir}/current-tag"
{
  echo "| $(date -u +%FT%TZ) | $environment${single_host:+ (single host)} | $image_tag | $(whoami) | $commit |"
} >> "$deploy_log"

log "done. Recorded in $deploy_log. Run the E-10 smoke suite against $environment next."
