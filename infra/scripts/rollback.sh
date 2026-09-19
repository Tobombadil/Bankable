#!/usr/bin/env bash
# Rollback runbook, automated (docs/04 O-5: "re-deploy the previous digest"). Re-runs deploy.sh
# with an older image tag; migrations are NOT downgraded by default (E-11: downgrade only if the
# migration's docstring says reversible — the normal path is expand/contract, where the old image
# simply runs unmodified against the new-but-compatible schema).
#
# deploy.sh calls this itself when the health check fails after the schema step; the
# INFRAQUE_NO_AUTO_ROLLBACK guard below stops that from recursing if the rollback also fails.
set -euo pipefail

usage() {
  cat <<EOF
Usage: rollback.sh <environment> <previous-image-tag> [--downgrade-migration]

  --downgrade-migration   only pass this if the migration being rolled back from documents
                          itself as reversible (docs/04 E-11); otherwise leave the schema as-is.
EOF
}

environment="${1:-}"
previous_tag="${2:-}"
downgrade="${3:-}"
[[ -z "$environment" || -z "$previous_tag" ]] && { usage; exit 1; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
remote_dir="/opt/infraque"
env_file="${remote_dir}/secrets/.env"

if [[ "$downgrade" == "--downgrade-migration" ]]; then
  : "${APP_HOST:?set APP_HOST}"
  echo "[rollback] downgrading one migration step on $APP_HOST (confirmed reversible by the operator)"
  ssh "${SSH_USER:-root}@${APP_HOST}" \
    "cd ${remote_dir}/compose && IMAGE_TAG=${previous_tag} INFRAQUE_ENV_FILE=${env_file} docker compose -f docker-compose.yml -f compose.prod.yml --env-file ${env_file} run --rm --no-deps -T api alembic -c services/db/migrations/alembic.ini downgrade -1"
fi

echo "[rollback] redeploying previous image tag: $previous_tag"
INFRAQUE_NO_AUTO_ROLLBACK=1 "$(dirname "${BASH_SOURCE[0]}")/deploy.sh" "$environment" "$previous_tag"

echo "[rollback] done. If entity tables need repair after a bad deploy, run the docs/21 §6.5 'replay' procedure next (docs/04 O-5)."
