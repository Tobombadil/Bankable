#!/usr/bin/env bash
# Basemap tile build (docs/00-PLAN.md owner decision 2026-09-14/15; docs/adr/0007;
# docs/40-launch-runbook.md §2.7). Extracts a single PMTiles basemap covering the contiguous US
# plus Great Britain from the Protomaps weekly planet build, and uploads it to the tiles R2 bucket
# (infra/terraform/storage.tf's `cloudflare_r2_bucket.tiles`) under a dated key, then copies that
# key to the canonical `basemap.pmtiles` name the web app's MAP_TILE_URL points at — so a bad or
# half-finished build never replaces the live file (docs/60-deployment.md's "expand before
# restart" discipline, applied here to a static asset instead of a database migration).
#
# One file, not one-per-region: a single bbox spanning CONUS + GB at --maxzoom=12 measured at
# ~3.9 GB (see the sizing note below) — well inside the cost ceiling (R2 storage is
# $0.015/GB-month, so ~$0.06/month) and it keeps the owner's decided single URL
# (`https://tiles.{{DOMAIN}}/basemap.pmtiles`) intact, matching what the frontend lane is already
# wiring into `web/` via MAP_TILE_URL without needing a multi-source map style. PMTiles serves
# per-viewport tiles via HTTP range requests regardless of the archive's total size, so one larger
# file costs nothing at request time — only slightly more build time and storage than a two-file
# split would (measured, both real: CONUS alone ~1.9 GB, GB alone ~347 MB; the combined single
# bbox is ~3.9 GB because it also covers the ocean/Canada/Greenland area between the two regions,
# a real but small overhead at this size).
#
# Requires: go (>=1.21, for `go install`), curl, aws CLI (R2 is S3-compatible), python3 (date
# parsing only). Installs the pmtiles CLI itself if not already on PATH.
set -euo pipefail

: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
: "${R2_TILES_BUCKET:?set R2_TILES_BUCKET (infra/terraform output: tiles_bucket_name)}"

# --- Configuration ---

# North America + GB in one bbox (min_lon,min_lat,max_lon,max_lat). Chosen after measuring: a
# single bbox covering both regions is not "too large" (3.9 GB, ~28s to extract in this sandbox's
# bandwidth) so there is no need for the two-file fallback the task brief allows for.
BASEMAP_BBOX="${BASEMAP_BBOX:--125,24,1.85,60.9}"
BASEMAP_MAXZOOM="${BASEMAP_MAXZOOM:-12}"
BUILDS_INDEX_URL="${BUILDS_INDEX_URL:-https://build-metadata.protomaps.dev/builds.json}"
BUILD_SOURCE_BASE="${BUILD_SOURCE_BASE:-https://build.protomaps.com}"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

log() { echo "[build-basemap] $*"; }

# --- 1. Resolve BUILD_DATE ---
#
# https://build.protomaps.com/ alone 404s (it is a plain object-storage bucket with no index
# listing); the actual published list of build dates is the JSON the protomaps.com build-browser
# page itself fetches (https://maps.protomaps.com/builds/, whose bundled JS calls this URL) — not
# documented as a public API, so pin to a specific date via BUILD_DATE if this ever breaks.
if [[ -z "${BUILD_DATE:-}" ]]; then
  log "BUILD_DATE not set; resolving the latest build from ${BUILDS_INDEX_URL}"
  BUILD_DATE="$(curl -fsSL --max-time 30 "$BUILDS_INDEX_URL" \
    | python3 -c "import json,sys; d=json.load(sys.stdin); print(sorted(x['key'] for x in d)[-1].removesuffix('.pmtiles'))")"
  [[ -z "$BUILD_DATE" ]] && { echo "[build-basemap] could not resolve a build date" >&2; exit 1; }
  log "resolved latest build: ${BUILD_DATE}"
else
  log "using pinned BUILD_DATE=${BUILD_DATE}"
fi
source_url="${BUILD_SOURCE_BASE}/${BUILD_DATE}.pmtiles"

# --- 2. Install the pmtiles CLI (go-pmtiles) if not already present ---
if ! command -v go-pmtiles >/dev/null 2>&1; then
  log "go-pmtiles not on PATH; installing via 'go install' (GOFLAGS=-mod=mod)"
  : "${GOPATH:=$(go env GOPATH)}"
  GOFLAGS=-mod=mod go install github.com/protomaps/go-pmtiles@latest
  export PATH="${PATH}:${GOPATH}/bin"
  command -v go-pmtiles >/dev/null 2>&1 || {
    echo "[build-basemap] go-pmtiles install did not put a binary on PATH (checked \$GOPATH/bin)" >&2
    exit 1
  }
fi
log "pmtiles CLI: $(command -v go-pmtiles)"

# --- 3. Extract the region from the remote planet build (HTTP range requests; the full ~138 GB
#        planet file is never downloaded — only the tiles inside the bbox/zoom range) ---
output_file="${work_dir}/basemap.pmtiles"
log "extracting bbox=${BASEMAP_BBOX} maxzoom=${BASEMAP_MAXZOOM} from ${source_url}"
go-pmtiles extract "$source_url" "$output_file" \
  --bbox="$BASEMAP_BBOX" --maxzoom="$BASEMAP_MAXZOOM" --download-threads=8
[[ -s "$output_file" ]] || { echo "[build-basemap] extract produced an empty file" >&2; exit 1; }
log "extract done: $(du -h "$output_file" | cut -f1) ($(stat -c%s "$output_file" 2>/dev/null || stat -f%z "$output_file") bytes)"

# --- 4. Verify the archive is structurally sound before it ever reaches R2 ---
go-pmtiles verify "$output_file"
log "verify OK"

# --- 5. Upload to a dated key, then copy to the canonical name (idempotent: re-running with the
#        same BUILD_DATE re-uploads the same dated key and re-copies; never a partial live file) ---
r2_endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
dated_key="basemap-${BUILD_DATE}.pmtiles"
canonical_key="basemap.pmtiles"

log "uploading to r2://${R2_TILES_BUCKET}/${dated_key}"
AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
  aws s3 cp "$output_file" "s3://${R2_TILES_BUCKET}/${dated_key}" \
  --endpoint-url "$r2_endpoint" --content-type application/vnd.pmtiles

log "promoting ${dated_key} -> ${canonical_key} (server-side copy, no re-upload)"
AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
  aws s3 cp "s3://${R2_TILES_BUCKET}/${dated_key}" "s3://${R2_TILES_BUCKET}/${canonical_key}" \
  --endpoint-url "$r2_endpoint" --content-type application/vnd.pmtiles

log "done: ${canonical_key} now serves the ${BUILD_DATE} build. Verify with:"
log "  curl -sI https://tiles.\${DOMAIN}/${canonical_key} | grep -i 'accept-ranges\|content-length'"
