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
  # A downgrade drops what the migration added: dump first, as deploy.sh does before an upgrade
  # (the nightly backup unit, run now; its `last-success` stamp must move), and refuse without one.
  echo "[rollback] dumping the database before the downgrade (infraque-backup.service)"
  stamp="${remote_dir}/backups/last-success"
  ssh "${SSH_USER:-root}@${APP_HOST}" \
    "before=\$(cat ${stamp} 2>/dev/null || true); systemctl start infraque-backup.service && after=\$(cat ${stamp} 2>/dev/null || true) && [ -n \"\$after\" ] && [ \"\$after\" != \"\$before\" ]" \
    || { echo "[rollback] the dump failed (journalctl -u infraque-backup on ${APP_HOST}); not downgrading" >&2; exit 1; }
  echo "[rollback] downgrading one migration step on $APP_HOST (confirmed reversible by the operator)"
  ssh "${SSH_USER:-root}@${APP_HOST}" \
    "cd ${remote_dir}/compose && IMAGE_TAG=${previous_tag} INFRAQUE_ENV_FILE=${env_file} docker compose -f docker-compose.yml -f compose.prod.yml --env-file ${env_file} run --rm --no-deps -T api alembic -c services/db/migrations/alembic.ini downgrade -1"
fi

echo "[rollback] redeploying previous image tag: $previous_tag"
# INFRAQUE_ROLLBACK: no dump and no `alembic upgrade` (deploy.sh step 4). The older image's upgrade
# fails outright once the newer migration has run, and a rollback must not depend on R2 answering.
# A downgrade above took its own dump.
INFRAQUE_NO_AUTO_ROLLBACK=1 INFRAQUE_ROLLBACK=1 "$(dirname "${BASH_SOURCE[0]}")/deploy.sh" "$environment" "$previous_tag"

echo "[rollback] done. If entity tables need repair after a bad deploy, run the docs/21 §6.5 'replay' procedure next (docs/04 O-5)."
