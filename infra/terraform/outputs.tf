output "app_ipv4" {
  description = "Public IPv4 of the api+web VM. Point DNS here manually until cloudflare_zone_id is set."
  value       = hcloud_server.app.ipv4_address
}

output "worker_ipv4s" {
  description = "Public IPv4s of the worker VM(s)."
  value       = hcloud_server.worker[*].ipv4_address
}

output "browser_worker_ipv4" {
  description = "Public IPv4 of the browser-worker VM."
  value       = hcloud_server.browser_worker.ipv4_address
}

output "r2_bucket_name" {
  description = "Name of the R2 bucket created for this environment (empty if cloudflare_account_id was not set)."
  value       = try(cloudflare_r2_bucket.object_storage[0].name, null)
}

output "tiles_bucket_name" {
  description = "Name of the public basemap-tiles R2 bucket for this environment (docs/adr/0007; empty if cloudflare_account_id was not set)."
  value       = try(cloudflare_r2_bucket.tiles[0].name, null)
}

output "tile_url" {
  description = <<-EOT
    Public URL the web app's MAP_TILE_URL should point at, once the custom domain step in
    storage.tf's comment block is done by hand (the cloudflare_r2_custom_domain resource is not
    created by this module — see that file). Null until cloudflare_zone_id is set, since the
    custom domain needs a real zone; construct it manually against the tiles bucket's own R2 URL
    in the meantime if you need a tile URL before the domain step runs.
  EOT
  value = var.cloudflare_zone_id != "" ? (
    var.environment == "production"
    ? "https://tiles.${var.domain}/basemap.pmtiles"
    : "https://tiles-${var.environment}.${var.domain}/basemap.pmtiles"
  ) : null
}

output "private_network_id" {
  value = hcloud_network.app.id
}
