# Object storage stays on Cloudflare R2 (see versions.tf header note): zero egress fees for raw
# snapshots, documents, exports and post media (docs/20 §4.4, ADR 0003). One bucket per
# environment so a preview or staging run can never overwrite a production snapshot.

resource "cloudflare_r2_bucket" "object_storage" {
  count      = var.cloudflare_account_id != "" ? 1 : 0
  account_id = var.cloudflare_account_id
  name       = "${var.r2_bucket_name}-${var.environment}"
  location   = "ENAM" # Eastern North America — matches the US hosting region (docs/20 A-3)
}
