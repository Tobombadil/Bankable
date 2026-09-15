#!/usr/bin/env bash
# Backup script (docs/04 O-7). The managed Postgres provider's own daily snapshot + PITR is the
# primary backup (RPO <= 1h, ADR 0003) and needs no script — this is the belt-and-braces copy into
# a *different* account (Cloudflare R2, not the Postgres provider) so a compromised or suspended
# provider account is not also a total-loss event. Run daily by cron on the app VM (see
# docs/60-deployment.md "Backups").
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL (postgresql://... — pg_dump wants a plain libpq URL, not the +psycopg SQLAlchemy form)}"
: "${R2_BUCKET:?set R2_BUCKET}"
: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"

retention_days="${BACKUP_RETENTION_DAYS:-35}"
backup_dir="${BACKUP_DIR:-/opt/infraque/backups}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="${backup_dir}/infraque-${timestamp}.dump"
r2_endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"

mkdir -p "$backup_dir"

echo "[backup] pg_dump -> $dump_file"
pg_dump --format=custom --no-owner --no-privileges --dbname="${DATABASE_URL/postgresql+psycopg/postgresql}" \
  --file="$dump_file"

echo "[backup] uploading to r2://${R2_BUCKET}/postgres/"
AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
  aws s3 cp "$dump_file" "s3://${R2_BUCKET}/postgres/$(basename "$dump_file")" \
  --endpoint-url "$r2_endpoint"

echo "[backup] pruning local dumps older than ${retention_days}d"
find "$backup_dir" -name 'infraque-*.dump' -mtime "+${retention_days}" -print -delete

echo "[backup] done: $(basename "$dump_file") ($(du -h "$dump_file" | cut -f1))"
