#!/usr/bin/env bash
# Generate a real per-environment age keypair for SOPS (ADR 0005). Never run this for an
# environment that already has a key without going through the "Rotate a secret" runbook
# (docs/60-deployment.md) — this always creates a *new* keypair.
set -euo pipefail

environment="${1:?Usage: bootstrap_age_key.sh <dev|staging|production>}"

case "$environment" in
  dev|staging|production) ;;
  *) echo "environment must be dev, staging or production" >&2; exit 1 ;;
esac

if ! command -v age-keygen >/dev/null 2>&1; then
  echo "age-keygen not found. Install age (https://github.com/FiloSottile/age)." >&2
  exit 1
fi

key_dir="${SOPS_AGE_KEY_DIR:-$HOME/.config/sops/age}"
mkdir -p "$key_dir"
chmod 700 "$key_dir"
key_file="$key_dir/${environment}-keys.txt"

if [[ -f "$key_file" ]]; then
  echo "Refusing to overwrite existing key: $key_file" >&2
  echo "Follow the 'Rotate a secret' runbook (docs/60-deployment.md) instead." >&2
  exit 1
fi

age-keygen -o "$key_file"
chmod 600 "$key_file"

public_key="$(grep -m1 '^# public key:' "$key_file" | sed 's/^# public key: //')"

cat <<EOF

Private key written to: $key_file (chmod 600; NOT under the repo; back it up to your password
manager and to the '$environment' GitHub Actions environment secret SOPS_AGE_KEY now).

Public key (paste into infra/sops/.sops.yaml, replacing REPLACE_WITH_${environment^^}_AGE_PUBLIC_KEY):
$public_key
EOF
