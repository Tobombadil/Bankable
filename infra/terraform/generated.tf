# Secrets OpenTofu generates and keeps in its state, for the GitHub Actions deploy of the one-server
# beta (.github/workflows/deploy-beta.yml; docs/64 §2, "One-click route"). Off by default: a local
# `tofu apply` plans nothing here and keeps using var.ssh_public_key and the sops file (docs/60 §5).
#
# With `generate_secrets = true`:
#   - the four application secrets the beta's env file needs are random_password results, created
#     once and reused by every later run (deploy-beta.yml renders the env file from them);
#   - a fifth value seeds the gate's bcrypt salt, so the same SITE_ACCESS_PASSWORD keeps the same
#     SITE_ACCESS_HASH and a redeploy of the same tag changes nothing on the host;
#   - the operator SSH key every VM accepts is a generated ED25519 key instead of var.ssh_public_key.
#     The workflow reads its private half from the state to reach the VM. Changing the key later
#     replaces every server (hcloud_server.ssh_keys is create-only), so the workflow refuses any plan
#     that deletes or replaces a resource (infra/ci/beta.py plan-guard).
#
# The trade-off (decision drafted 2026-10-15): the state then holds these values in clear. It lives
# in the private R2 bucket `beta-tfstate` (infra/ci/backend_r2.tf), readable by the R2 token in the
# GitHub Environment `production` and by anyone who can run that environment's workflows. That is
# the same set of people who can already read the deployed env file through the workflow, and it
# spares the owner sops, age and a hand-kept secrets file. Rotating one value: `tofu taint
# 'random_password.generated["<KEY>"]'`, except POSTGRES_PASSWORD, which the database volume keeps
# from its first start (docs/60 §10.5).

variable "generate_secrets" {
  description = "Generate the beta's application secrets and the deploy SSH key in state (generated.tf). Only the GitHub Actions beta deploy sets it."
  type        = bool
  default     = false
}

locals {
  # Lengths, not values. Letters and digits only: POSTGRES_PASSWORD goes into DATABASE_URL
  # unescaped, and no value needs quoting in the env file. SESSION_SECRET must be >= 32 characters
  # (services/api/auth.py).
  generated_secret_lengths = {
    POSTGRES_PASSWORD  = 32
    SESSION_SECRET     = 48
    API_INTERNAL_TOKEN = 48
    AUDIT_HASH_PEPPER  = 48
    SITE_ACCESS_SALT   = 32
  }

  # trimspace: the provider's OpenSSH form ends in a newline, which would read as a changed key.
  operator_public_key = var.generate_secrets ? trimspace(tls_private_key.deploy[0].public_key_openssh) : var.ssh_public_key
}

resource "random_password" "generated" {
  for_each = var.generate_secrets ? local.generated_secret_lengths : {}
  length   = each.value
  special  = false
}

resource "tls_private_key" "deploy" {
  count     = var.generate_secrets ? 1 : 0
  algorithm = "ED25519"
}

output "generated_secrets" {
  description = "The generated application secrets by env-file key, plus SITE_ACCESS_SALT (empty unless generate_secrets)."
  sensitive   = true
  value       = { for key, secret in random_password.generated : key => secret.result }
}

output "deploy_ssh_private_key" {
  description = "Private half of the generated operator key, OpenSSH format (null unless generate_secrets)."
  sensitive   = true
  value       = try(tls_private_key.deploy[0].private_key_openssh, null)
}
