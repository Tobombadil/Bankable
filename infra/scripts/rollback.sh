#!/usr/bin/env bash
# Rollback runbook, automated (docs/04 O-5: "re-deploy the previous digest"). Re-runs the same
# deploy.sh with an older image tag; migrations are NOT downgraded by default (E-11: downgrade
# only if the migration's docstring says reversible — the normal path is expand/contract, where
# the old image simply runs unmodified against the new-but-compatible schema).
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

if [[ "$downgrade" == "--downgrade-migration" ]]; then
  : "${APP_HOST:?set APP_HOST}"
  echo "[rollback] downgrading one migration step on $APP_HOST (confirmed reversible by the operator)"
  ssh "${SSH_USER:-root}@${APP_HOST}" \
    "cd /opt/infraqueue/compose && docker compose -f docker-compose.yml -f compose.prod.yml run --rm api alembic -c services/db/migrations/alembic.ini downgrade -1"
fi

echo "[rollback] redeploying previous image tag: $previous_tag"
"$(dirname "${BASH_SOURCE[0]}")/deploy.sh" "$environment" "$previous_tag"

echo "[rollback] done. If entity tables need repair after a bad deploy, run the docs/21 §6.5 'replay' procedure next (docs/04 O-5)."
