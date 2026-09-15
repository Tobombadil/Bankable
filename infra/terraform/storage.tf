# Object storage stays on Cloudflare R2 (see versions.tf header note): zero egress fees for raw
# snapshots, documents, exports and post media (docs/20 §4.4, ADR 0003). One bucket per
# environment so a preview or staging run can never overwrite a production snapshot.

resource "cloudflare_r2_bucket" "object_storage" {
  count      = var.cloudflare_account_id != "" ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "${var.r2_bucket_name}-${var.environment}"
  location   = "ENAM" # Eastern North America — matches the US hosting region (docs/20 A-3)
}

# --- Basemap tiles bucket (docs/00-PLAN.md owner decision 2026-09-14/15; docs/adr/0007) ---
#
# Public static map tiles (a Protomaps PMTiles basemap, self-hosted, replacing the prototype's
# tile.openstreetmap.org raster whose usage policy forbids production apps — docs/40 §2.7).
# This MUST be a separate bucket from `object_storage` above: that bucket holds raw snapshots and
# exports under `docs/13`'s licence/attribution rules and is never public; this one is a public,
# read-only static asset with no licence-gated content at all (PMTiles files serve OpenStreetMap-
# derived rendering data via HTTP range requests — the platform's own database is not exposed
# through it, ADR 0007's licence note). Same per-environment naming as the bucket above so a
# staging build can never overwrite the production tiles.
resource "cloudflare_r2_bucket" "tiles" {
  count      = var.cloudflare_account_id != "" ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "${var.r2_bucket_name}-tiles-${var.environment}"
  location   = "ENAM"
}

# --- Custom domain (tiles.infraque.com) for the tiles bucket ---
#
# NOT created here: the `cloudflare_r2_custom_domain` resource does not exist in the pinned
# provider (`versions.tf`: cloudflare ~> 4.36). Confirmed against the provider's published schema
# docs up to and including the latest 4.x release (4.52.0) — only `cloudflare_r2_bucket` exists in
# the v4 series; `cloudflare_r2_custom_domain` (and R2 bucket CORS) ship only in the v5 provider,
# which is a separate upgrade (`versions.tf`'s pin is an explicit ADR 0005 choice, not something
# to bump silently for one feature). Do this step by hand instead, once per environment, after
# `tofu apply` has created the bucket above and the zone for `var.domain` exists:
#
#   1. Cloudflare dashboard -> R2 -> the `${var.r2_bucket_name}-tiles-${var.environment}` bucket
#      -> Settings -> Custom Domains -> Connect Domain -> enter `tiles.${var.domain}` (or
#      `tiles-${var.environment}.${var.domain}` for non-production).
#   2. Cloudflare provisions the DNS record itself (no manual `cloudflare_record` needed — R2
#      custom domains are not a plain CNAME to a customer-visible target).
#   3. Confirm `curl -I https://tiles.${var.domain}/basemap.pmtiles` returns 200 with
#      `accept-ranges: bytes` once `infra/scripts/build_basemap.sh` has uploaded a file.
#
# Left here, commented, as the resource to uncomment on the v5 upgrade (schema per the v5 docs;
# verify field names against the provider version actually pinned at that time before uncommenting):
#
# resource "cloudflare_r2_custom_domain" "tiles" {
#   count      = var.cloudflare_account_id != "" && var.cloudflare_zone_id != "" ? 1 : 0
#   account_id = var.cloudflare_account_id
#   bucket_name = cloudflare_r2_bucket.tiles[0].name
#   zone_id    = var.cloudflare_zone_id
#   domain     = var.environment == "production" ? "tiles.${var.domain}" : "tiles-${var.environment}.${var.domain}"
#   enabled    = true
#   min_tls    = "1.2"
# }

# --- CORS for the tiles bucket ---
#
# NOT created here for the same reason: no CORS resource exists for R2 buckets in the pinned v4
# provider (confirmed against the same 4.52.0 schema dump — `cloudflare_r2_bucket`'s only
# arguments are `account_id`, `name`, `location`). PMTiles are read via HTTP range requests
# (`Range`/`Accept-Ranges`) directly from the browser (`docs/adr/0007`), so the bucket needs a CORS
# policy allowing `GET, HEAD` with the `Range` header from the site origin, or every map load fails
# cross-origin. Set it by hand once per environment via the Cloudflare API (there is no dashboard
# UI field for this yet either, per Cloudflare's own R2 CORS docs) — this is the exact call,
# credentials from the operator's env, never committed:
#
#   curl -X PUT "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/r2/buckets/${var.r2_bucket_name}-tiles-${var.environment}/cors" \
#     -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" -H "Content-Type: application/json" \
#     -d '{"rules":[{"allowed":{"methods":["GET","HEAD"],"origins":["https://'"${var.domain}"'"],"headers":["Range"]},"exposeHeaders":["Content-Range","Content-Length","Accept-Ranges"],"maxAgeSeconds":3600}]}'
#
# Left here, commented, as the resource to uncomment on the v5 upgrade (verify the exact resource
# name and schema against the provider version actually pinned at that time):
#
# resource "cloudflare_r2_bucket_cors" "tiles" {
#   count       = var.cloudflare_account_id != "" ? 1 : 0
#   account_id  = var.cloudflare_account_id
#   bucket_name = cloudflare_r2_bucket.tiles[0].name
#   rules {
#     allowed {
#       methods = ["GET", "HEAD"]
#       origins = ["https://${var.domain}"]
#       headers = ["Range"]
#     }
#     expose_headers  = ["Content-Range", "Content-Length", "Accept-Ranges"]
#     max_age_seconds = 3600
#   }
# }
