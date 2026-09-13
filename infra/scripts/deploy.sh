#!/usr/bin/env bash
# Deploy runbook, automated (docs/04 O-4; see docs/60-deployment.md "Deploy" for the human-readable
# version of these same steps). Run from CI (a tag push to main) or by hand from the operator's
# machine with the same SSH access OpenTofu granted (infra/terraform/variables.tf
# `ssh_public_key`).
#
# Order matters and is the point of this script, not an implementation detail: workers stop first
# (so nothing is mid-fetch when the schema changes), migrations run once against the shared
# database, then api/web restart behind the Cloudflare edge cache (which keeps serving public
# pages throughout, docs/20 §12), and the scheduler restarts last (so it never enqueues work the
# new workers aren't up to consume yet).
set -euo pipefail

usage() {
  cat <<EOF
Usage: deploy.sh <environment> <image-tag>

  environment   staging | production
  image-tag     the git tag / digest already pushed to the registry by CI

Required environment variables:
  APP_HOST, WORKER_HOSTS (space-separated), BROWSER_WORKER_HOST   — from 'tofu output' (infra/terraform)
  SSH_USER          (default: root, matching the docker-ce marketplace image)
  SOPS_AGE_KEY      the environment's private age key (see infra/sops/README.md)
EOF
}

environment="${1:-}"
image_tag="${2:-}"
[[ -z "$environment" || -z "$image_tag" ]] && { usage; exit 1; }
[[ "$environment" != "staging" && "$environment" != "production" ]] && {
  echo "environment must be staging or production" >&2; exit 1;
}

: "${APP_HOST:?set APP_HOST (tofu output app_ipv4)}"
: "${WORKER_HOSTS:?set WORKER_HOSTS (space-separated tofu output worker_ipv4s)}"
: "${BROWSER_WORKER_HOST:?set BROWSER_WORKER_HOST (tofu output browser_worker_ipv4)}"
: "${SOPS_AGE_KEY:?set SOPS_AGE_KEY to the private age key contents for this environment}"
ssh_user="${SSH_USER:-root}"
compose_files="-f infra/compose/docker-compose.yml -f infra/compose/compose.prod.yml"
remote_dir="/opt/infraqueue"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

log() { echo "[deploy $environment] $*"; }

decrypt_secrets_to() {
  local host="$1"
  log "decrypting infra/sops/secrets.${environment}.enc.yaml"
  SOPS_AGE_KEY="$SOPS_AGE_KEY" sops -d "infra/sops/secrets.${environment}.enc.yaml" \
    | ssh "${ssh_user}@${host}" "mkdir -p ${remote_dir}/secrets && cat > ${remote_dir}/secrets/.env && chmod 600 ${remote_dir}/secrets/.env"
}

sync_compose_files() {
  local host="$1"
  scp -q infra/compose/docker-compose.yml infra/compose/compose.prod.yml infra/compose/Caddyfile \
    "${ssh_user}@${host}:${remote_dir}/compose/"
}

remote_compose() {
  local host="$1"; shift
  ssh "${ssh_user}@${host}" \
    "cd ${remote_dir}/compose && DOMAIN=\${DOMAIN:-} IMAGE_TAG=${image_tag} docker compose ${compose_files} --env-file ${remote_dir}/secrets/.env $*"
}

log "1/5 draining and stopping worker services on: $WORKER_HOSTS $BROWSER_WORKER_HOST"
for host in $WORKER_HOSTS "$BROWSER_WORKER_HOST"; do
  sync_compose_files "$host"
  decrypt_secrets_to "$host"
  remote_compose "$host" stop worker browser-worker social scheduler
done

log "2/5 running migrations (expand phase, docs/04 E-11) once against the shared database"
remote_compose "$APP_HOST" run --rm api alembic -c services/db/migrations/alembic.ini upgrade head

log "3/5 restarting api/web on $APP_HOST (behind the Cloudflare edge cache)"
sync_compose_files "$APP_HOST"
decrypt_secrets_to "$APP_HOST"
remote_compose "$APP_HOST" up -d --pull always caddy api web

log "4/5 restarting workers"
for host in $WORKER_HOSTS; do
  remote_compose "$host" up -d --pull always worker social
done
remote_compose "$BROWSER_WORKER_HOST" up -d --pull always browser-worker

log "5/5 restarting the scheduler (singleton) last"
remote_compose "$APP_HOST" up -d --pull always scheduler

deploy_log="infra/deploy-log.md"
{
  echo "| $(date -u +%FT%TZ) | $environment | $image_tag | $(whoami) | $(git rev-parse --short HEAD 2>/dev/null || echo unknown) |"
} >> "$deploy_log"

log "done. Recorded in $deploy_log. Run the E-10 smoke suite against $environment next."
