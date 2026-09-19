#!/usr/bin/env bash
# Backup script (docs/04 O-7). The managed Postgres provider's own daily snapshot + PITR is the
# primary backup (RPO <= 1h, ADR 0003) and needs no script — this is the belt-and-braces copy into
# a *different* account (Cloudflare R2, not the Postgres provider) so a compromised or suspended
# provider account is not also a total-loss event.
#
# Runs nightly on the app VM from the systemd timer infra/terraform/cloud-init/app.yaml installs
# (`infraque-backup.timer`, EnvironmentFile=/opt/infraque/secrets/.env); deploy.sh ships this
# file to /opt/infraque/scripts/backup.sh. Tools: `postgresql-client-16` (pg_dump must be >= the
# server major, checked below) and `awscli`, both installed by that cloud-init file.
#
# Retention (docs/60 §8): local dumps for BACKUP_RETENTION_DAYS (default 35); on R2, every daily
# dump for 14 days plus the Sunday dump for 8 weeks — pruned here, since the pinned Cloudflare
# provider has no R2 lifecycle resource (docs/60 §11 item 8).
set -euo pipefail

: "${DATABASE_URL:?set DATABASE_URL (postgresql://... — pg_dump wants a plain libpq URL, not the +psycopg SQLAlchemy form)}"
: "${R2_BUCKET:?set R2_BUCKET}"
: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"

retention_days="${BACKUP_RETENTION_DAYS:-35}"
r2_daily_days="${BACKUP_R2_DAILY_DAYS:-14}"
r2_weekly_weeks="${BACKUP_R2_WEEKLY_WEEKS:-8}"
backup_dir="${BACKUP_DIR:-/opt/infraque/backups}"
timestamp="$(date -u +%Y%m%dT%H%M%SZ)"
dump_file="${backup_dir}/infraque-${timestamp}.dump"
r2_endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
plain_url="${DATABASE_URL/postgresql+psycopg/postgresql}"
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"

mkdir -p "$backup_dir"

# pg_dump refuses a server newer than itself; fail with the reason instead of pg_dump's own.
server_major="$(psql "$plain_url" -tAc 'show server_version' | cut -d. -f1 | tr -d '[:space:]')"
client_major="$(pg_dump --version | awk '{print $3}' | cut -d. -f1)"
if (( client_major < server_major )); then
  echo "[backup] pg_dump ${client_major} is older than the server (${server_major}); install postgresql-client-${server_major}" >&2
  exit 1
fi

echo "[backup] pg_dump (client ${client_major}, server ${server_major}) -> $dump_file"
pg_dump --format=custom --no-owner --no-privileges --dbname="$plain_url" --file="$dump_file"

echo "[backup] uploading to r2://${R2_BUCKET}/postgres/"
aws s3 cp "$dump_file" "s3://${R2_BUCKET}/postgres/$(basename "$dump_file")" --endpoint-url "$r2_endpoint"

echo "[backup] pruning local dumps older than ${retention_days}d"
find "$backup_dir" -name 'infraque-*.dump' -mtime "+${retention_days}" -print -delete

echo "[backup] pruning R2: keep ${r2_daily_days} daily + ${r2_weekly_weeks} weekly (Sunday) dumps"
now_epoch="$(date -u +%s)"
aws s3 ls "s3://${R2_BUCKET}/postgres/" --endpoint-url "$r2_endpoint" | awk '{print $4}' \
  | grep -E '^infraque-[0-9]{8}T[0-9]{6}Z\.dump$' | while read -r object; do
    day="${object#infraque-}"; day="${day:0:8}"
    day_epoch="$(date -u -d "${day:0:4}-${day:4:2}-${day:6:2}" +%s)"
    age_days=$(( (now_epoch - day_epoch) / 86400 ))
    weekday="$(date -u -d "@${day_epoch}" +%u)" # 7 = Sunday
    if (( age_days <= r2_daily_days )); then continue; fi
    if [[ "$weekday" == "7" ]] && (( age_days <= r2_weekly_weeks * 7 )); then continue; fi
    echo "[backup] deleting r2://${R2_BUCKET}/postgres/${object} (${age_days}d old)"
    aws s3 rm "s3://${R2_BUCKET}/postgres/${object}" --endpoint-url "$r2_endpoint"
  done

date -u +%FT%TZ > "${backup_dir}/last-success" # docs/60 §7: backup-age alert reads this (> 26 h, O-7)
echo "[backup] done: $(basename "$dump_file") ($(du -h "$dump_file" | cut -f1))"
