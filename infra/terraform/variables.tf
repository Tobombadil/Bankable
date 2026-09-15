# No secrets live in this file or anywhere under infra/*.tf (docs/04 E-19). Provider credentials
# come from environment variables the provider plugins read natively:
#   HCLOUD_TOKEN          — Hetzner Cloud API token
#   CLOUDFLARE_API_TOKEN  — Cloudflare API token (Zone:DNS:Edit + Account:R2:Edit scopes)
# Both are injected by CI from GitHub Actions encrypted secrets, or by hand from the operator's
# password manager for a local `tofu apply`. Never hard-code a value here.

variable "environment" {
  description = "Environment name: dev | staging | production. Drives resource naming and tags."
  type        = string
  validation {
    condition     = contains(["dev", "staging", "production"], var.environment)
    error_message = "environment must be one of dev, staging, production."
  }
}

variable "hcloud_region" {
  description = "Hetzner Cloud location. ash (Ashburn, VA, US) for a US region (docs/20 A-3)."
  type        = string
  default     = "ash"
}

variable "domain" {
  description = "Placeholder root domain (infraque.com in docs until the owner names the product, docs/04 §0.6)."
  type        = string
  default     = "example-infraque.test"
}

variable "cloudflare_zone_id" {
  description = "Cloudflare zone id for `domain`. Looked up once the real domain is registered; empty string skips DNS resource creation (dev/CI plans)."
  type        = string
  default     = ""
}

variable "cloudflare_account_id" {
  description = "Cloudflare account id that owns the R2 bucket."
  type        = string
  default     = ""
}

variable "ssh_public_key" {
  description = "Operator's SSH public key (OpenSSH format). The only key with SSH access to any VM (docs/20 §11: 'VMs accept SSH from the owner's key only')."
  type        = string
}

variable "app_server_type" {
  description = "Hetzner server type for the api+web VM. cx32 = 4 vCPU / 8 GB, matching docs/20 §14's app VM line."
  type        = string
  default     = "cx32"
}

variable "worker_server_type" {
  description = "Hetzner server type for the plain-worker + scheduler VM."
  type        = string
  default     = "cx32"
}

variable "browser_worker_server_type" {
  description = "Hetzner server type for the Chromium worker VM. Same size as the others; Playwright's 2 GB/3-page budget (docs/20 §4.1) fits inside 8 GB with headroom for the OS and the plain-HTTP token-bucket sidecar."
  type        = string
  default     = "cx32"
}

variable "vm_count_workers" {
  description = "Number of non-browser worker VMs (1 or 2, docs/20 §4.1 'Worker tier — 1-2 VMs'). Start at 1; the scaling path (docs/20 §13 step 1) adds a second only on a measured queue-age signal."
  type        = number
  default     = 1
  validation {
    condition     = var.vm_count_workers >= 1 && var.vm_count_workers <= 2
    error_message = "vm_count_workers must be 1 or 2 per docs/20 §4.1 and the cost ceiling."
  }
}

variable "r2_bucket_name" {
  description = "Cloudflare R2 bucket for raw snapshots, documents, exports and post media (docs/20 §4.4)."
  type        = string
  default     = "infraque-object-storage"
}

variable "backup_retention_days" {
  description = "Local backup-script retention before pruning old dumps on the app VM's backup volume (docs/60 §Backups; the authoritative backup is the managed Postgres provider's own PITR, this is the belt-and-braces copy in R2)."
  type        = number
  default     = 35
}
