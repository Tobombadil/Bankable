#!/usr/bin/env bash
# Restore drill (docs/04 O-7: "a monthly restore into a scratch database runs the integration
# fixtures and records date, duration, restored point and outcome in the runbook"). Restores the
# most recent R2 backup (infra/scripts/backup.sh) into a throwaway local Postgres container,
# proves it with a handful of row-count sanity checks, and prints a ready-to-paste runbook row.
#
# This is NOT a substitute for a real PITR restore drill against the managed provider — that is a
# separate, provider-specific exercise the "Restore from backup" runbook in
# docs/60-deployment.md's step 1 covers. This script proves the *R2 copy* is actually restorable,
# which the provider's own snapshot mechanism does not.
set -euo pipefail

: "${R2_BUCKET:?set R2_BUCKET}"
: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"

r2_endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
scratch_container="infraque-restore-drill"
scratch_port="${SCRATCH_PG_PORT:-55432}"
work_dir="$(mktemp -d)"
trap 'docker rm -f "$scratch_container" >/dev/null 2>&1 || true; rm -rf "$work_dir"' EXIT

start_time="$(date -u +%s)"

echo "[restore-drill] finding the latest backup in r2://${R2_BUCKET}/postgres/"
latest="$(AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
  aws s3 ls "s3://${R2_BUCKET}/postgres/" --endpoint-url "$r2_endpoint" \
  | awk '{print $4}' | sort | tail -n1)"
[[ -z "$latest" ]] && { echo "[restore-drill] no backups found" >&2; exit 1; }
echo "[restore-drill] latest backup: $latest"

AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
  aws s3 cp "s3://${R2_BUCKET}/postgres/${latest}" "${work_dir}/${latest}" --endpoint-url "$r2_endpoint"

echo "[restore-drill] starting scratch Postgres+PostGIS on port ${scratch_port}"
docker run -d --rm --name "$scratch_container" \
  -e POSTGRES_DB=restore_drill -e POSTGRES_USER=drill -e POSTGRES_PASSWORD=drill \
  -p "${scratch_port}:5432" postgis/postgis:16-3.4 >/dev/null

echo "[restore-drill] waiting for scratch Postgres to accept connections"
for _ in $(seq 1 30); do
  docker exec "$scratch_container" pg_isready -U drill -d restore_drill >/dev/null 2>&1 && break
  sleep 2
done

echo "[restore-drill] restoring dump"
docker cp "${work_dir}/${latest}" "${scratch_container}:/tmp/restore.dump"
docker exec "$scratch_container" pg_restore --no-owner --no-privileges -U drill -d restore_drill /tmp/restore.dump

echo "[restore-drill] sanity checks: row counts on core tables (docs/21 §2)"
docker exec "$scratch_container" psql -U drill -d restore_drill -c \
  "select 'proposal', count(*) from proposal
   union all select 'opportunity', count(*) from opportunity
   union all select 'organization', count(*) from organization
   union all select 'event', count(*) from event;"

end_time="$(date -u +%s)"
duration=$((end_time - start_time))

cat <<EOF

--- paste into docs/60-deployment.md "Restore from backup" runbook's Last executed line ---
Last executed: $(date -u +%FT%TZ), devops-engineer, restored ${latest}, duration ${duration}s, outcome: OK
--------------------------------------------------------------------------------------------
EOF
