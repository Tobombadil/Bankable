#!/usr/bin/env bash
# The steps of .github/workflows/deploy-beta.yml: the one-server private beta (docs/64) deployed from
# a GitHub-hosted runner, from the GitHub Environment `production` (docs/64 §2, "One-click route").
# One subcommand per workflow step, so each step has its own log and runs here exactly as on the
# runner; infra/ci/test_beta.py drives them against PATH shims. infra/ci/beta.py holds the decisions.
#
#   check-inputs     every input present and well formed, DOMAIN = the tag's code (no cloud API)
#   images           the tag's api/web/worker images exist in GHCR
#   install-tofu     OpenTofu, pinned and checksummed     install-pmtiles   go-pmtiles, likewise
#   tofu-init        state in the R2 bucket beta-tfstate (infra/ci/backend_r2.tf)
#   tofu-apply       server type, plan, refuse deletions (beta.py plan-guard), apply
#   tiles            the tiles bucket's custom domain and CORS; is basemap.pmtiles there?
#   basemap          build and upload it (infra/scripts/build_basemap.sh)
#   outputs          read the state: mask secrets, write the SSH key and config, APP_HOST
#   wait-host        SSH answers, cloud-init finished, the tools deploy.sh and the backup need exist
#   render-env       the env file (beta.py render-env), 0600     deploy    infra/scripts/deploy.sh
#   health           health checks through the server, the gate, then public DNS; URLs to the summary
#   seed | preflight | status    the docs/64 §3 steps on the host
#   ghcr-logout | known-hosts-save | cleanup                     the always-run tail of the host job
#
# Never trace commands here (no xtrace): these steps handle the secrets. Values from the state are
# masked (beta.py mask) before anything else runs.
set -euo pipefail

# Pinned downloads: version and the SHA-256 of the exact archive (from the release's own checksum
# list, retrieved 2026-10-15). A mismatch stops the step before the binary runs.
TOFU_VERSION="1.12.7"
TOFU_SHA256="8b0967cc14715bfb03d5e04dd0da0a47c668dcb33cb3a84996413a48b1aa51c0" # tofu_1.12.7_linux_amd64.tar.gz
PMTILES_VERSION="1.31.2"
PMTILES_SHA256="3ed7dbf4ec2e6dfe5e25b6f70d1ffc932729f93c86db353bf514dd71010a312f" # go-pmtiles_1.31.2_Linux_x86_64.tar.gz
# Names without the product name (the product and domain may change; these may not).
STATE_BUCKET="beta-tfstate"                # created by hand once (docs/64 §2)
BUCKET_PREFIX="beta-object-storage"        # OpenTofu r2_bucket_name: <prefix>-production, <prefix>-tiles-production
IMAGE_PREFIX="ghcr.io/tobombadil/bankable" # .github/workflows/release.yml
HCLOUD_LOCATION="ash"                      # variables.tf hcloud_region's default (docs/20 A-3)
SEED_UNIT="infraque-seed-context"

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"
work="${RUNNER_TEMP:-${TMPDIR:-/tmp}}/deploy-beta"
mkdir -p "$work" && chmod 700 "$work"
summary_file="${GITHUB_STEP_SUMMARY:-/dev/null}"
github_env="${GITHUB_ENV:-/dev/null}"
github_output="${GITHUB_OUTPUT:-/dev/null}"
github_path="${GITHUB_PATH:-/dev/null}"
tofu_dir="infra/terraform"
remote_dir="/opt/infraque"
python="${PYTHON:-python3}"
# deploy.sh's compose invocation on the single host, at the tag it recorded (seed_single_host.sh's).
compose_remote="cd ${remote_dir}/compose && IMAGE_TAG=\$(cat ${remote_dir}/current-tag) INFRAQUE_ENV_FILE=${remote_dir}/secrets/.env docker compose -f docker-compose.yml -f compose.prod.yml -f compose.single.yml --env-file ${remote_dir}/secrets/.env"

log() { echo "[deploy-beta] $*"; }
fail() { echo "::error::$*"; exit 1; }
warn() { echo "::warning::$*"; }
md() { printf '%s\n' "$@" >> "$summary_file"; }
need() { # the step's env must carry these (the workflow maps them from the Environment)
  local name
  for name in "$@"; do
    [[ -n "${!name:-}" ]] || fail "${name} is not set for this step"
  done
}
tofu() { if [[ -x "$work/bin/tofu" ]]; then "$work/bin/tofu" "$@"; else command tofu "$@"; fi; }
with_r2() ( # <command...> with the R2 S3 token as AWS credentials (the S3 backend, the AWS CLI)
  export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
  "$@"
)
r2_endpoint() { echo "https://${CLOUDFLARE_ACCOUNT_ID}.r2.cloudflarestorage.com"; }
# shellcheck disable=SC2029 # remote commands are built here on purpose, expanded on this side
host_ssh() { ssh "root@${APP_HOST}" "$@"; }

install_binary() { # <url> <sha256> <file in the archive> <name on PATH>
  local archive="$work/download.tar.gz"
  mkdir -p "$work/bin"
  curl -fsSL --retry 3 --max-time 300 -o "$archive" "$1"
  echo "$2  $archive" | sha256sum -c --quiet - || fail "checksum mismatch for $1: not running it"
  tar -xzf "$archive" -C "$work/bin" "$3"
  [[ "$3" == "$4" ]] || mv "$work/bin/$3" "$work/bin/$4"
  rm -f "$archive"
  echo "$work/bin" >> "$github_path"
  log "installed $4 from $1 (sha256 verified)"
}

cf() { # <method> <url> [json]: true on HTTP 2xx with "success": true; the answer is in $work/cf.json
  local args=(-sS --retry 3 --max-time 60 -X "$1" -o "$work/cf.json" -w '%{http_code}'
    -H "Authorization: Bearer ${CLOUDFLARE_API_TOKEN}" -H "Content-Type: application/json")
  [[ -z "${3:-}" ]] || args+=(--data "$3")
  local code
  code="$(curl "${args[@]}" "$2" || echo 000)"
  [[ "$code" == 2* ]] && jq -e '.success == true' "$work/cf.json" >/dev/null 2>&1
}
cf_errors() { jq -r '[.errors[]? | "\(.code): \(.message)"] | join("; ")' "$work/cf.json" 2>/dev/null || echo "no answer"; }
hcloud_get() { curl -fsS --retry 3 --max-time 60 -H "Authorization: Bearer ${HCLOUD_TOKEN}" "https://api.hetzner.cloud/v1/$1"; }

# ------------------------------------------------------------------------------------- inputs job
default_tag() { # sha-<7> of the newest commit in this run's history that release-images built, else of this run's
  # commit. A run started right after a merge then deploys the previous build instead of failing on images that
  # release-images publishes only once ci passes on the new commit (about 12 minutes).
  local sha
  if [[ -n "${GITHUB_TOKEN:-}" && -n "${GITHUB_REPOSITORY:-}" ]]; then
    for sha in $(curl -fsS --retry 3 --max-time 30 -H "Authorization: Bearer ${GITHUB_TOKEN}" \
      -H "Accept: application/vnd.github+json" \
      "https://api.github.com/repos/${GITHUB_REPOSITORY}/actions/workflows/release.yml/runs?branch=main&status=success&per_page=30" \
      | jq -r '.workflow_runs[]?.head_sha' 2>/dev/null || true); do
      if [[ "$sha" =~ ^[0-9a-f]{40}$ ]] && git merge-base --is-ancestor "$sha" HEAD 2>/dev/null; then
        [[ "${sha:0:7}" == "${GITHUB_SHA:0:7}" ]] \
          || log "no images for ${GITHUB_SHA:0:7} yet: deploying sha-${sha:0:7}, the newest build in this run's history" >&2
        echo "sha-${sha:0:7}"
        return
      fi
    done
  fi
  echo "sha-${GITHUB_SHA:0:7}"
}

cmd_check_inputs() {
  local tag="${INPUT_TAG:-}" ref="" commit=""
  [[ -n "$tag" ]] || tag="$(default_tag)"
  if [[ "$tag" =~ ^sha-([0-9a-f]{7})$ ]]; then
    ref="${BASH_REMATCH[1]}"
  elif [[ "$tag" =~ ^v[0-9A-Za-z._-]+$ ]]; then
    ref="refs/tags/${tag}"
  fi
  [[ -z "$ref" ]] || commit="$(git rev-parse --verify --quiet "${ref}^{commit}" 2>/dev/null || true)"
  : > "$work/common.py"
  : > "$work/editorial.py"
  if [[ -n "$commit" ]]; then # the code the tag's images were built from, not the workflow's ref
    git show "${commit}:services/api/common.py" > "$work/common.py" 2>/dev/null || true
    git show "${commit}:services/social/editorial.py" > "$work/editorial.py" 2>/dev/null || true
  fi
  "$python" infra/ci/beta.py check-inputs --tag "$tag" --commit "$commit" \
    --common "$work/common.py" --editorial "$work/editorial.py"
}

# ------------------------------------------------------------------------------------- infra job
cmd_images() {
  need TAG GHCR_USER GHCR_TOKEN
  printf '%s' "$GHCR_TOKEN" | docker login ghcr.io -u "$GHCR_USER" --password-stdin >/dev/null
  local missing=() service image
  for service in api web worker; do # the single host runs no browser-worker
    image="${IMAGE_PREFIX}-${service}:${TAG}"
    if docker manifest inspect "$image" >/dev/null 2>&1; then log "found ${image}"; else missing+=("$image"); fi
  done
  docker logout ghcr.io >/dev/null 2>&1 || true
  (( ${#missing[@]} == 0 )) || fail "not in GHCR: ${missing[*]}. release-images publishes sha-<7> for each main commit whose ci run passed: wait for it, or choose another tag"
}

cmd_install_tofu() {
  install_binary "https://github.com/opentofu/opentofu/releases/download/v${TOFU_VERSION}/tofu_${TOFU_VERSION}_linux_amd64.tar.gz" \
    "$TOFU_SHA256" tofu tofu
}

cmd_install_pmtiles() { # build_basemap.sh uses `go-pmtiles` when it is on PATH instead of `go install ...@latest`
  install_binary "https://github.com/protomaps/go-pmtiles/releases/download/v${PMTILES_VERSION}/go-pmtiles_${PMTILES_VERSION}_Linux_x86_64.tar.gz" \
    "$PMTILES_SHA256" pmtiles go-pmtiles
}

cmd_tofu_init() {
  need CLOUDFLARE_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
  # A one-key listing, not HeadBucket: an "Object Read & Write" R2 token may list objects but not buckets.
  with_r2 aws s3api list-objects-v2 --bucket "$STATE_BUCKET" --max-keys 1 --endpoint-url "$(r2_endpoint)" >/dev/null 2>&1 \
    || fail "cannot read the R2 bucket ${STATE_BUCKET}: create it by hand (Cloudflare > R2, docs/64 §2), and check that R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY are an R2 token for all buckets in account ${CLOUDFLARE_ACCOUNT_ID}"
  cp infra/ci/backend_r2.tf "${tofu_dir}/backend_ci.tf"
  (umask 077 && printf 'bucket    = "%s"\nendpoints = { s3 = "%s" }\n' "$STATE_BUCKET" "$(r2_endpoint)" > "$work/r2.tfbackend")
  with_r2 tofu -chdir="$tofu_dir" init -input=false -no-color -backend-config="$work/r2.tfbackend"
}

cmd_tofu_apply() {
  need DOMAIN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_ZONE_ID HCLOUD_TOKEN CLOUDFLARE_API_TOKEN R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
  local server_type rc=0
  # The token's two scopes, before anything is created: a missing one stops here, not halfway through the apply.
  cf GET "https://api.cloudflare.com/client/v4/zones/${CLOUDFLARE_ZONE_ID}/dns_records?per_page=1" \
    || fail "CLOUDFLARE_API_TOKEN cannot read the DNS records of zone ${CLOUDFLARE_ZONE_ID} ($(cf_errors)): give it Zone · DNS · Edit on ${DOMAIN} (docs/64 §2 step D)"
  cf GET "https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/r2/buckets?per_page=1" \
    || fail "CLOUDFLARE_API_TOKEN cannot use R2 in account ${CLOUDFLARE_ACCOUNT_ID} ($(cf_errors)): give it Account · Workers R2 Storage · Edit, with Account Resources including that account (docs/64 §2 step D)"
  # The type of the server the state already holds; empty before the first one exists. Not `output -raw`:
  # on a state without outputs it prints its "No outputs found" warning on stdout and exits 0.
  server_type="$(with_r2 tofu -chdir="$tofu_dir" show -json -no-color 2>/dev/null \
    | jq -r '[.values.root_module.resources[]? | select(.address == "hcloud_server.app") | .values.server_type][0] // empty' \
    || true)"
  [[ -z "$server_type" || "$server_type" =~ ^[a-z0-9-]+$ ]] || fail "the state's server type is not a Hetzner type name: ${server_type:0:80}"
  if [[ -n "$server_type" ]]; then
    log "server type ${server_type}, from the state: a later run never changes it"
  else
    hcloud_get "server_types?per_page=50" > "$work/server_types.json"
    server_type="$("$python" infra/ci/beta.py pick-server-type "$work/server_types.json" --location "$HCLOUD_LOCATION")" \
      || { echo "$server_type"; exit 1; } # its ::error:: line, not swallowed by $(...)
  fi
  (umask 077 && "$python" infra/ci/beta.py tfvars --server-type "$server_type" --bucket-prefix "$BUCKET_PREFIX" > "$work/beta.tfvars.json")
  with_r2 tofu -chdir="$tofu_dir" plan -input=false -no-color -var-file="$work/beta.tfvars.json" -out="$work/plan.bin"
  (umask 077 && with_r2 tofu -chdir="$tofu_dir" show -json -no-color "$work/plan.bin" > "$work/plan.json")
  "$python" infra/ci/beta.py plan-guard "$work/plan.json" || rc=$?
  rm -f "$work/plan.json" # it carries the prior state, generated secrets included
  (( rc == 0 )) || exit "$rc"
  with_r2 tofu -chdir="$tofu_dir" apply -input=false -no-color "$work/plan.bin"
  rm -f "$work/plan.bin"
}

cmd_tiles() {
  need DOMAIN CLOUDFLARE_ACCOUNT_ID CLOUDFLARE_ZONE_ID CLOUDFLARE_API_TOKEN R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
  local bucket host api cors
  bucket="$(with_r2 tofu -chdir="$tofu_dir" output -raw tiles_bucket_name)"
  host="tiles.${DOMAIN}"
  api="https://api.cloudflare.com/client/v4/accounts/${CLOUDFLARE_ACCOUNT_ID}/r2/buckets/${bucket}"
  echo "tiles_bucket=${bucket}" >> "$github_output"
  # The pinned Cloudflare provider has no R2 custom-domain or CORS resource (infra/terraform/storage.tf),
  # so these two are API calls, each idempotent. A failure leaves the map blank, not the site down.
  if cf GET "${api}/domains/custom" \
    && jq -e --arg d "$host" '[.result.domains[]? | select(.domain == $d)] | length > 0' "$work/cf.json" >/dev/null; then
    log "${host} is already the custom domain of ${bucket}"
  elif cf POST "${api}/domains/custom" "$(jq -nc --arg d "$host" --arg z "$CLOUDFLARE_ZONE_ID" '{domain: $d, zoneId: $z, enabled: true, minTLS: "1.2"}')"; then
    log "connected ${host} to ${bucket}; Cloudflare adds its DNS record and certificate within minutes"
  else
    warn "could not connect ${host} to ${bucket} ($(cf_errors)); the map stays blank until it is: Cloudflare > R2 > ${bucket} > Settings > Custom Domains"
  fi
  cors="$(jq -nc --arg o "https://${DOMAIN}" '{rules: [{allowed: {methods: ["GET", "HEAD"], origins: [$o], headers: ["Range"]}, exposeHeaders: ["Content-Range", "Content-Length", "Accept-Ranges", "ETag"], maxAgeSeconds: 3600}]}')"
  if cf PUT "${api}/cors" "$cors"; then
    log "CORS on ${bucket}: GET and HEAD with Range from https://${DOMAIN}"
  else
    warn "could not set CORS on ${bucket} ($(cf_errors)); browsers will refuse the basemap's range requests"
  fi
  if with_r2 aws s3api head-object --bucket "$bucket" --key basemap.pmtiles --endpoint-url "$(r2_endpoint)" >/dev/null 2>&1; then
    echo "basemap=present" >> "$github_output"
    log "basemap.pmtiles is in ${bucket}"
  else
    echo "basemap=missing" >> "$github_output"
    warn "basemap.pmtiles is not in ${bucket} yet: the basemap job builds it (about 3.9 GB) while the deploy goes on; the map is blank until it is uploaded"
  fi
}

# ------------------------------------------------------------------------------------- basemap job
cmd_basemap() {
  need DOMAIN R2_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_TILES_BUCKET
  export PATH="$work/bin:$PATH"
  command -v go-pmtiles >/dev/null || fail "go-pmtiles is not installed (install-pmtiles)"
  local start=$SECONDS url code deadline
  infra/scripts/build_basemap.sh
  log "built and uploaded in $(( SECONDS - start ))s"
  url="https://tiles.${DOMAIN}/basemap.pmtiles"
  deadline=$(( $(date +%s) + ${URL_WAIT_SECONDS:-600} ))
  until code="$(curl -sS -o /dev/null -w '%{http_code}' -r 0-15 --max-time 20 "$url" 2>/dev/null)" && [[ "$code" == 206 ]]; do
    if (( $(date +%s) >= deadline )); then
      warn "${url} answers ${code:-nothing} to a range request, not 206: check the custom domain (Cloudflare > R2 > ${R2_TILES_BUCKET} > Settings)"
      md "### Basemap uploaded; ${url} not answering range requests yet (${code:-no answer})"
      return 0
    fi
    sleep "${URL_INTERVAL_SECONDS:-15}"
  done
  md "### Basemap: ${url} answers range requests (built in $(( SECONDS - start ))s)"
}

# ------------------------------------------------------------------------------------- host job
cmd_outputs() {
  need R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY CLOUDFLARE_ACCOUNT_ID
  local host
  (umask 077 && with_r2 tofu -chdir="$tofu_dir" output -json > "$work/outputs.json")
  "$python" infra/ci/beta.py mask "$work/outputs.json"
  host="$("$python" infra/ci/beta.py output "$work/outputs.json" app_ipv4)" || { echo "$host"; exit 1; }
  [[ -n "$host" ]] || fail "the state has no server address: run the workflow with action deploy first"
  "$python" infra/ci/beta.py write-key "$work/outputs.json" "$work/ssh/id_deploy"
  mkdir -p "$HOME/.ssh" && chmod 700 "$HOME/.ssh"
  cat >> "$HOME/.ssh/config" <<EOF
Host ${host}
  User root
  IdentityFile $work/ssh/id_deploy
  IdentitiesOnly yes
  UserKnownHostsFile $work/ssh/known_hosts
  StrictHostKeyChecking accept-new
  BatchMode yes
  ConnectTimeout 15
  ServerAliveInterval 30
  ServerAliveCountMax 10
EOF
  chmod 600 "$HOME/.ssh/config"
  # The host key seen on the first run, kept in the state bucket: later runs refuse a changed key.
  if with_r2 aws s3 cp "s3://${STATE_BUCKET}/ssh/known_hosts" "$work/ssh/known_hosts" --endpoint-url "$(r2_endpoint)" --quiet 2>/dev/null; then
    log "using the host key recorded by an earlier run"
  else
    : > "$work/ssh/known_hosts"
    log "no host key recorded yet: this run records the server's on first contact"
  fi
  echo "APP_HOST=${host}" >> "$github_env"
  log "server ${host}"
}

cmd_wait_host() {
  need APP_HOST
  local deadline=$(( $(date +%s) + ${HOST_WAIT_SECONDS:-600} ))
  until host_ssh true 2>/dev/null; do
    (( $(date +%s) < deadline )) || fail "no SSH answer from ${APP_HOST} within ${HOST_WAIT_SECONDS:-600}s (Hetzner console: is the server running?)"
    log "waiting for SSH on ${APP_HOST}"
    sleep "${HOST_WAIT_INTERVAL:-10}"
  done
  log "waiting for cloud-init's first boot (packages, the AWS CLI, the backup timer)"
  # shellcheck disable=SC2016 # expands on the host
  host_ssh 'timeout 900 cloud-init status --wait >/dev/null 2>&1; rc=$?; [ "$rc" = 0 ] || { echo "cloud-init status: exit $rc"; cloud-init status --long; }; true'
  if ! host_ssh 'command -v docker && docker compose version && command -v pg_dump && command -v aws && systemctl cat infraque-backup.service >/dev/null'; then
    host_ssh 'cloud-init status --long; tail -n 60 /var/log/cloud-init-output.log' || true
    fail "${APP_HOST} lacks a tool deploy.sh or the nightly backup needs (cloud-init output above)"
  fi
}

cmd_render_env() {
  need DOMAIN SITE_ACCESS_PASSWORD SITE_ACCESS_USER SENDER_POSTAL_ADDRESS PRODUCT_NAME CLOUDFLARE_ACCOUNT_ID R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY
  local py="${RENDER_PYTHON:-}"
  if [[ -z "$py" ]]; then
    python3 -m venv "$work/venv"
    "$work/venv/bin/pip" install --quiet --disable-pip-version-check --require-hashes --only-binary=:all: \
      --no-deps -r infra/ci/requirements-deploy.txt
    py="$work/venv/bin/python"
  fi
  "$py" infra/ci/beta.py render-env "$work/outputs.json" "$work/beta.env"
  echo "DEPLOY_ENV_FILE=$work/beta.env" >> "$github_env"
}

cmd_deploy() {
  need APP_HOST TAG DEPLOY_ENV_FILE GHCR_USER GHCR_TOKEN
  env -u WORKER_HOSTS -u BROWSER_WORKER_HOST -u SOPS_AGE_KEY \
    SINGLE_HOST=1 APP_HOST="$APP_HOST" DEPLOY_ENV_FILE="$DEPLOY_ENV_FILE" \
    GHCR_USER="$GHCR_USER" GHCR_READ_TOKEN="$GHCR_TOKEN" DEPLOY_LOG="$work/deploy-log.md" \
    infra/scripts/deploy.sh production "$TAG"
}

wait_for() { # <url> <address to pin, or ""> <text the body must hold, or "">
  local url="$1" pin="$2" want="$3" host deadline
  host="${url#https://}"
  host="${host%%/*}"
  local args=(-fsS --max-time 15 -o "$work/health.body")
  [[ -z "$pin" ]] || args+=(--resolve "${host}:443:${pin}")
  deadline=$(( $(date +%s) + ${URL_WAIT_SECONDS:-900} ))
  until curl "${args[@]}" "$url" 2>"$work/health.err" && { [[ -z "$want" ]] || grep -qF "$want" "$work/health.body"; }; do
    (( $(date +%s) < deadline )) \
      || fail "${url}${pin:+ (at ${pin})} was not healthy within ${URL_WAIT_SECONDS:-900}s: $(tr '\n' ' ' < "$work/health.err")$(head -c 300 "$work/health.body" 2>/dev/null || true)"
    sleep "${URL_INTERVAL_SECONDS:-15}"
  done
  log "healthy: ${url}${pin:+ (at ${pin})}"
}

cmd_health() {
  need DOMAIN APP_HOST
  local site="https://${DOMAIN}/health" api="https://api.${DOMAIN}/v1/health" code
  log "1/3 at the server itself, whatever DNS caches say (Caddy fetches its certificates first)"
  wait_for "$site" "$APP_HOST" ""
  wait_for "$api" "$APP_HOST" '"status":"ok"'
  log "2/3 the beta gate asks for the login"
  code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 15 --resolve "${DOMAIN}:443:${APP_HOST}" "https://${DOMAIN}/" || true)"
  [[ "$code" == 401 ]] || fail "https://${DOMAIN}/ answered ${code} without the login: the gate (SITE_ACCESS=basic) must answer 401"
  log "3/3 through public DNS, as testers reach it"
  wait_for "$site" "" ""
  wait_for "$api" "" '"status":"ok"'
  md "### Deployed ${TAG:-} to ${DOMAIN}" "" \
    "| | |" "|---|---|" \
    "| Site (shared login \`${SITE_ACCESS_USER:-}\`) | https://${DOMAIN}/ |" \
    "| Admin | https://admin.${DOMAIN}/admin |" \
    "| API health (no login) | ${api} |" \
    "| Site health (no login) | ${site} |" \
    "| Basemap | https://tiles.${DOMAIN}/basemap.pmtiles |" \
    "| Server | ${APP_HOST} |" "" \
    "Next: run this workflow with action \`seed\`, then \`status\` until the queue is empty, then \`preflight\`."
}

cmd_ghcr_logout() {
  [[ -n "${APP_HOST:-}" ]] || return 0
  host_ssh 'docker logout ghcr.io' >/dev/null 2>&1 || warn "could not log ${APP_HOST} out of ghcr.io (the token expires with this run anyway)"
}

require_deployed() {
  host_ssh "test -s ${remote_dir}/current-tag" || fail "nothing is deployed on ${APP_HOST}: run the workflow with action deploy first"
}

cmd_seed() {
  need APP_HOST
  require_deployed
  local started
  log "1/2 queueing one fetch per source (bootstrap fetch, in the worker image)"
  host_ssh "${compose_remote} run --rm --no-deps -T worker python -m infra.scheduler.bootstrap fetch" | tee "$work/fetch.jsonl"
  log "2/2 starting the waiter that queues the context build once the fetches are loaded (unit ${SEED_UNIT})"
  scp -q infra/ci/seed_on_host.sh "root@${APP_HOST}:${remote_dir}/scripts/seed_on_host.sh"
  started="$(host_ssh "if systemctl is-active --quiet ${SEED_UNIT}; then echo 'already running'; else systemctl reset-failed ${SEED_UNIT} >/dev/null 2>&1 || true; systemd-run --quiet --unit=${SEED_UNIT} --description='deploy-beta seed: context build once the fetches are loaded' /bin/bash ${remote_dir}/scripts/seed_on_host.sh && echo started; fi")"
  log "waiter: ${started}"
  md "### Seed queued on ${APP_HOST}" "" \
    "- fetches: \`$(tail -n 1 "$work/fetch.jsonl")\`" \
    "- context build waiter (\`${SEED_UNIT}\`): ${started}. It queues \`bootstrap context --build\` once no fetch, load, resolve or match job is left (at most 4 h)." "" \
    "Progress: run this workflow with action \`status\` (queue depths and the waiter's log). The rehearsal (docs/64 §4, §7) fetched 18 sources in about 4 minutes, drained the loads in under 2 and built and loaded the context layers in about 8; one worker on the server will take longer. Then run \`preflight\`."
}

cmd_preflight() {
  need APP_HOST
  require_deployed
  local rc=0
  # The hand-check worksheet folder, mounted when it exists (docs/64 §3, check 12).
  host_ssh "${compose_remote} run --rm --no-deps -T \$([ -d ${remote_dir}/handcheck ] && echo -v ${remote_dir}/handcheck:/out:ro) worker python -m infra.scheduler.bootstrap preflight --json" > "$work/preflight.out" || rc=$?
  "$python" infra/ci/beta.py preflight-summary "$work/preflight.out" --exit-code "$rc"
}

cmd_status() {
  need APP_HOST DOMAIN
  # Read-only: ps, a SELECT, systemctl show, journalctl, cat.
  host_ssh 'bash -s' > "$work/status.txt" 2>&1 <<'REMOTE' || true
cd /opt/infraque/compose 2>/dev/null || { echo "nothing deployed: /opt/infraque/compose is missing"; exit 0; }
tag="$(cat /opt/infraque/current-tag 2>/dev/null || echo none)"
c() { IMAGE_TAG="$tag" INFRAQUE_ENV_FILE=/opt/infraque/secrets/.env docker compose -f docker-compose.yml -f compose.prod.yml -f compose.single.yml --env-file /opt/infraque/secrets/.env "$@"; }
echo "== image tag: $tag"
echo "== services"; c ps
echo "== jobs queued or running, by queue and task"
c exec -T postgres psql -U infraque -d infraque -c "select queue_name, task_name, status, count(*) from procrastinate_jobs where status in ('todo', 'doing') group by 1, 2, 3 order by 1, 2, 3"
echo "== failed jobs, by task"
c exec -T postgres psql -U infraque -d infraque -c "select task_name, count(*) from procrastinate_jobs where status = 'failed' group by 1 order by 2 desc"
echo "== seed waiter (infraque-seed-context)"
systemctl show infraque-seed-context -p ActiveState -p SubState -p Result 2>/dev/null
journalctl -u infraque-seed-context -n 20 --no-pager -o short-iso 2>/dev/null
echo "== last backup: $(cat /opt/infraque/backups/last-success 2>/dev/null || echo none)"
echo "== host"; uptime; df -h / | tail -n 1; free -m | head -n 2
REMOTE
  cat "$work/status.txt"
  local health
  health="$(curl -sS --max-time 15 "https://api.${DOMAIN}/v1/health" 2>&1 || true)"
  echo "== https://api.${DOMAIN}/v1/health: ${health}"
  md "### Status of ${APP_HOST} (${DOMAIN})" '```' "$(cat "$work/status.txt")" "== https://api.${DOMAIN}/v1/health: ${health}" '```'
}

cmd_known_hosts_save() {
  need R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY CLOUDFLARE_ACCOUNT_ID
  [[ -s "$work/ssh/known_hosts" ]] || { log "no host key to keep"; return 0; }
  with_r2 aws s3 cp "$work/ssh/known_hosts" "s3://${STATE_BUCKET}/ssh/known_hosts" --endpoint-url "$(r2_endpoint)" --quiet
  log "kept the server's host key for the next run"
}

cmd_cleanup() { # the runner is discarded anyway; the key, env file and state outputs go first
  rm -rf "$work" "${tofu_dir}/backend_ci.tf" "${tofu_dir}/.terraform"
}

case "${1:-}" in
  check-inputs) cmd_check_inputs ;;
  images) cmd_images ;;
  install-tofu) cmd_install_tofu ;;
  install-pmtiles) cmd_install_pmtiles ;;
  tofu-init) cmd_tofu_init ;;
  tofu-apply) cmd_tofu_apply ;;
  tiles) cmd_tiles ;;
  basemap) cmd_basemap ;;
  outputs) cmd_outputs ;;
  wait-host) cmd_wait_host ;;
  render-env) cmd_render_env ;;
  deploy) cmd_deploy ;;
  health) cmd_health ;;
  ghcr-logout) cmd_ghcr_logout ;;
  seed) cmd_seed ;;
  preflight) cmd_preflight ;;
  status) cmd_status ;;
  known-hosts-save) cmd_known_hosts_save ;;
  cleanup) cmd_cleanup ;;
  *) sed -n '2,24p' "${BASH_SOURCE[0]}" >&2; exit 2 ;;
esac
