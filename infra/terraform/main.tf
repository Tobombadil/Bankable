# ADR 0005: 2-3 small Hetzner Cloud VMs + Docker Compose. One image per process family
# (docs/20 §4.1), so three server *roles* here, not three different images.
#
#   vm-app     — api + web containers (Compose), behind Caddy for TLS
#   vm-worker  — scheduler + worker-plain + worker-model (+ services/social dry-run publisher)
#   vm-browser — worker-browser only, isolated so a Chromium OOM cannot take the API down with it
#                (docs/adr/0005 "Single VM ... a browser worker OOM takes the API down with it")
#
# No secrets here (docs/04 E-19): the cloud-init script only installs Docker and drops the compose
# files; actual .env values are decrypted from infra/sops/*.enc.yaml by the deploy script (O-4) at
# deploy time, never baked into the image or this configuration.

provider "hcloud" {
  # token via HCLOUD_TOKEN env var
}

provider "cloudflare" {
  # token via CLOUDFLARE_API_TOKEN env var
}

locals {
  name_prefix = "infraque-${var.environment}"
  common_labels = {
    project     = "infraque" # placeholder name, docs/00-PLAN.md D6
    environment = var.environment
    managed_by  = "opentofu"
  }
}

resource "hcloud_ssh_key" "operator" {
  name       = "${local.name_prefix}-operator"
  public_key = var.ssh_public_key
  labels     = local.common_labels
}

# One private network so Postgres-bound and inter-VM traffic (queue polling, admin-to-API) never
# needs to leave the datacentre network, and the browser VM's egress can be scoped separately from
# the plain-worker VM (docs/20 §4.3, §11 "workers may only reach the hosts listed for their source").
resource "hcloud_network" "app" {
  name     = "${local.name_prefix}-net"
  ip_range = "10.0.0.0/16"
  labels   = local.common_labels
}

resource "hcloud_network_subnet" "app" {
  network_id   = hcloud_network.app.id
  type         = "cloud"
  network_zone = "us-east"
  ip_range     = "10.0.1.0/24"
}

# Public HTTP(S) from the edge only; SSH from the operator only (docs/20 §11).
resource "hcloud_firewall" "web" {
  name   = "${local.name_prefix}-fw-web"
  labels = local.common_labels

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = ["0.0.0.0/0", "::/0"] # tighten to the operator's IP/CIDR once known; SSH key auth only regardless
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "80"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "443"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
}

# Workers take no inbound traffic at all except SSH (they poll out; nothing calls in).
resource "hcloud_firewall" "worker" {
  name   = "${local.name_prefix}-fw-worker"
  labels = local.common_labels

  rule {
    direction  = "in"
    protocol   = "tcp"
    port       = "22"
    source_ips = ["0.0.0.0/0", "::/0"]
  }
}

resource "hcloud_server" "app" {
  name         = "${local.name_prefix}-app"
  server_type  = var.app_server_type
  image        = "docker-ce" # Hetzner App Marketplace image: Docker + Compose plugin preinstalled
  location     = var.hcloud_region
  ssh_keys     = [hcloud_ssh_key.operator.id]
  firewall_ids = [hcloud_firewall.web.id]
  labels       = merge(local.common_labels, { role = "app" })

  network {
    network_id = hcloud_network.app.id
    ip         = "10.0.1.10"
  }

  user_data = file("${path.module}/cloud-init/app.yaml")

  depends_on = [hcloud_network_subnet.app]
}

resource "hcloud_server" "worker" {
  count        = var.vm_count_workers
  name         = "${local.name_prefix}-worker-${count.index + 1}"
  server_type  = var.worker_server_type
  image        = "docker-ce"
  location     = var.hcloud_region
  ssh_keys     = [hcloud_ssh_key.operator.id]
  firewall_ids = [hcloud_firewall.worker.id]
  labels       = merge(local.common_labels, { role = "worker" })

  network {
    network_id = hcloud_network.app.id
    ip         = "10.0.1.${20 + count.index}"
  }

  user_data = file("${path.module}/cloud-init/worker.yaml")

  depends_on = [hcloud_network_subnet.app]
}

resource "hcloud_server" "browser_worker" {
  name         = "${local.name_prefix}-browser-worker"
  server_type  = var.browser_worker_server_type
  image        = "docker-ce"
  location     = var.hcloud_region
  ssh_keys     = [hcloud_ssh_key.operator.id]
  firewall_ids = [hcloud_firewall.worker.id]
  labels       = merge(local.common_labels, { role = "browser-worker" })

  network {
    network_id = hcloud_network.app.id
    ip         = "10.0.1.30"
  }

  user_data = file("${path.module}/cloud-init/browser-worker.yaml")

  depends_on = [hcloud_network_subnet.app]
}
