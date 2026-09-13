# Secrets: SOPS + age

ADR 0005. No secret value is ever committed in plaintext (docs/04 E-19); every `secrets.<env>.enc.yaml`
file here is safe to commit because it is SOPS-encrypted, and only the matching age *private* key
(never committed, `infra/sops/keys/example-age-key.txt` excepted — see below) can read it.

## Layout

```
infra/sops/.sops.yaml                    creation rules: which age public key encrypts which file
infra/sops/secrets.dev.example.enc.yaml  a real, working example — decryptable by anyone who clones this repo
infra/sops/secrets.dev.example.plain.yaml  its plaintext source (regenerate the .enc file from this)
infra/sops/keys/example-age-key.txt      the THROWAWAY private key that decrypts the example only
infra/sops/secrets.dev.enc.yaml          not created yet — the real dev environment's secrets
infra/sops/secrets.staging.enc.yaml      not created yet
infra/sops/secrets.production.enc.yaml   not created yet
```

## Try it (safe — decrypts only the placeholder example)

```bash
cd infra/sops
SOPS_AGE_KEY_FILE=keys/example-age-key.txt sops -d secrets.dev.example.enc.yaml
```

## Bootstrapping a real environment (dev, staging or production)

1. Generate a real age keypair — never reuse the example key for anything real:
   ```bash
   infra/scripts/bootstrap_age_key.sh <environment>
   ```
   This writes the private key to `~/.config/sops/age/<environment>-keys.txt` (not under the repo)
   and prints the public key.
2. Paste the public key into `infra/sops/.sops.yaml`'s `REPLACE_WITH_<ENV>_AGE_PUBLIC_KEY` line.
3. Create the secrets file:
   ```bash
   sops infra/sops/secrets.<environment>.enc.yaml   # opens $EDITOR on a decrypted tmpfile, encrypts on save
   ```
4. Store the *private* key in two places, per docs/60-deployment.md's "Rotate a secret" runbook:
   the operator's password manager, and this repo's GitHub Actions environment secret
   `SOPS_AGE_KEY` (for the CI/CD deploy job to decrypt at deploy time) — never as a repo file.
5. On the target VM, the deploy script (`infra/scripts/deploy.sh`) decrypts the file into
   `/opt/infraqueue/secrets/.env` immediately before `docker compose up`, and the plaintext never
   touches disk outside that one root-owned, `0600` file.

## Rotation

Key and secret rotation is a `docs/04` S-7 requirement (≤ 90 days, immediately on personnel change
or suspected exposure). Follow the "Rotate a secret" runbook in `docs/60-deployment.md`; every
rotation is logged under `infra/secret-rotation-log.md` (date, secret, rotated by, reason).

## What this is not

This is not a KMS. There is no audit log of *who decrypted what and when* beyond git history of
`.sops.yaml` and whoever has the private key. For a solo operator (docs/20 [A-2]) that is an
accepted trade-off (ADR 0005); revisit if a second engineer with less-than-full trust joins.


> **Note (2026-09-13):** the throwaway age private key that once accompanied `secrets.dev.example.enc.yaml` was removed from the repository; private keys are gitignored under `infra/sops/keys/`. The example file therefore cannot be decrypted from a fresh clone. Generate your own key with `infra/scripts/bootstrap_age_key.sh dev`, re-encrypt the example, and treat the recipient in `.sops.yaml` as a placeholder to replace.
