#!/usr/bin/env bash
# Rotate one key in an environment's SOPS-encrypted secrets file (docs/04 S-7), then redeploy so
# running containers pick it up. Does not rotate the age key itself — see
# infra/scripts/bootstrap_age_key.sh and infra/sops/README.md for that (a different, rarer
# rotation: the key that decrypts the file, not a value inside it).
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: rotate_secret.sh <environment> <KEY_NAME>

Opens $EDITOR on the decrypted secrets file so you can paste in the new value for KEY_NAME, then
re-encrypts on save. Appends a row to infra/secret-rotation-log.md and reminds you to redeploy.
EOF
}

environment="${1:-}"
key_name="${2:-}"
[[ -z "$environment" || -z "$key_name" ]] && { usage; exit 1; }

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
secrets_file="${repo_root}/infra/sops/secrets.${environment}.enc.yaml"
[[ -f "$secrets_file" ]] || { echo "no such file: $secrets_file" >&2; exit 1; }

echo "[rotate-secret] opening $secrets_file in \$EDITOR — update $key_name, save, and exit"
sops "$secrets_file"

read -r -p "Rotated by (name/email): " rotated_by
read -r -p "Reason (scheduled | personnel-change | suspected-exposure | vendor-incident): " reason

log_file="${repo_root}/infra/secret-rotation-log.md"
echo "| $(date -u +%F) | $key_name | $environment | $rotated_by | $reason |" >> "$log_file"

cat <<EOF

Logged in $log_file. This does NOT take effect until the next deploy — run:
  infra/scripts/deploy.sh $environment <current-image-tag>
or wait for the next scheduled deploy, per how urgent $reason is.
EOF
