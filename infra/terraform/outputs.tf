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

output "private_network_id" {
  value = hcloud_network.app.id
}
