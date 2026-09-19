# OpenTofu provider pinning. ADR 0005: OpenTofu (Terraform-compatible) for cloud resources.
#
# Providers:
#   hetzner (hcloud)  — compute VMs, private network, firewall, SSH key (docs/20 §14/§15, ADR 0005)
#   cloudflare        — DNS records and the R2 object-storage bucket. Kept on Cloudflare rather
#                       than moved to Hetzner/DigitalOcean because ADR 0003 (Accepted) and ADR 0005
#                       (Accepted, see status line) already name Cloudflare R2 for zero-egress
#                       object storage and Cloudflare for DNS/CDN/WAF — the same account that
#                       already fronts bankablehq.com (.github/workflows/blank.yml). Moving those
#                       two resource kinds to the compute provider would silently reopen two
#                       Accepted ADRs; recorded here rather than done quietly (docs/00-PLAN.md P-6).
#                       Compute is the resource docs/20 §14 actually prices against Hetzner, so
#                       compute is what moves.

terraform {
  required_version = ">= 1.6.0"

  required_providers {
    hcloud = {
      source  = "hetznercloud/hcloud"
      version = "~> 1.48"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.36"
    }
  }
}
