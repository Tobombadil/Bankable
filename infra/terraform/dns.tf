# DNS stays on Cloudflare (see versions.tf header note). Records are only created once a real
# zone exists (`cloudflare_zone_id` non-empty) so `tofu plan`/`validate` still work with placeholder
# variables before the domain is registered (docs/00-PLAN.md open question 1 / D6).

locals {
  create_dns = var.cloudflare_zone_id != ""
}

resource "cloudflare_record" "app" {
  count   = local.create_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = var.environment == "production" ? "@" : var.environment
  content = hcloud_server.app.ipv4_address
  type    = "A"
  proxied = true # edge CDN/WAF in front of the app VM (docs/20 §2 edge tier)
  ttl     = 1    # ignored when proxied
}

resource "cloudflare_record" "www" {
  count   = local.create_dns && var.environment == "production" ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = "www"
  content = var.domain
  type    = "CNAME"
  proxied = true
  ttl     = 1
}

resource "cloudflare_record" "admin" {
  count   = local.create_dns ? 1 : 0
  zone_id = var.cloudflare_zone_id
  name    = var.environment == "production" ? "admin" : "admin-${var.environment}"
  content = hcloud_server.app.ipv4_address
  type    = "A"
  proxied = true # Cloudflare Access can be layered on this hostname later (docs/20 §7 [A-10])
  ttl     = 1
}
