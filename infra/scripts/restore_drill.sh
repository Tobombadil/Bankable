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

# Over TCP, not the unix socket: the image's entrypoint runs its init scripts on a temporary
# socket-only server and then restarts it, so a socket probe says "ready" too early and the restore
# is cut off mid-way ("terminating connection due to administrator command"; local drill,
# 2026-09-26). The final server is the first one listening on TCP.
echo "[restore-drill] waiting for scratch Postgres to accept TCP connections"
ready=0
for _ in $(seq 1 30); do
  if docker exec "$scratch_container" pg_isready -h 127.0.0.1 -U drill -d restore_drill >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
(( ready )) || { echo "[restore-drill] scratch Postgres never accepted TCP connections" >&2; exit 1; }

# Into a database cloned from template0, not the image's POSTGRES_DB: that one already has the
# postgis/topology/tiger extensions its init script installs, so the dump's own CREATE SCHEMA
# statements fail and pg_restore exits 1 ("errors ignored on restore: 3") even though every row
# restored (local drill, 2026-09-26).
echo "[restore-drill] restoring dump into a fresh database (template0)"
docker cp "${work_dir}/${latest}" "${scratch_container}:/tmp/restore.dump"
docker exec "$scratch_container" createdb -h 127.0.0.1 -U drill -T template0 restore_target
docker exec "$scratch_container" pg_restore --no-owner --no-privileges -h 127.0.0.1 -U drill -d restore_target /tmp/restore.dump

echo "[restore-drill] sanity checks: row counts on core tables (docs/21 §2)"
docker exec "$scratch_container" psql -h 127.0.0.1 -U drill -d restore_target -c \
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
