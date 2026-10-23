#!/usr/bin/env bash
# Grid layers build (owner decisions 2026-10-22: the powered-land site finder; substations from
# OpenStreetMap, public now; docs/25 §2.7). Builds the US high-voltage network -- power lines and
# cables of 69 kV and up (and those with no voltage tag), and substations -- from Geofabrik's
# OpenStreetMap state extracts, and uploads two compact GeoParquet files and their manifest to the
# tiles R2 bucket the basemap uses (`infra/scripts/build_basemap.sh`), under a versioned key, then
# promotes that version to `grid/osm-power/latest.json` -- the file the server's monthly
# `context_build` reads (`python -m pipeline.context.osm_power pull`).
#
# Layout on the bucket (public through tiles.<DOMAIN>, like basemap.pmtiles):
#   grid/osm-power/<version>/lines.parquet        GeoParquet 1.1, WKB LineString, zstd
#   grid/osm-power/<version>/substations.parquet  GeoParquet 1.1, WKB Point + outline Polygon
#   grid/osm-power/<version>/manifest.json        per-state extract timestamps, counts, sha256
#   grid/osm-power/<version>/README.txt           the ODbL notice for the derived database
#   grid/osm-power/latest.json                    a copy of the promoted version's manifest
# <version> is `v1/<data timestamp>-<build time>` (`pipeline.context.osm_power.version_key`): a
# rebuild never overwrites a published version. These files are the derived database the API's
# lines and substations come from, offered under the ODbL as they are (docs/13 §6, docs/25 §2.7).
#
# One state at a time, so the runner's disk holds one extract (California, the largest, is about
# 1.3 GB) plus a few megabytes of filtered output: download, filter with pyosmium, delete the raw
# extract, read. Sequential downloads five seconds apart; robots.txt honoured (PoliteSession).
#
# Promotion is gated (`osm_power check`): a build missing a state the published one has, or whose
# lines or substations fell by more than 10% and at least 50 rows in any state (or to none), counted
# per state as a one-state server would load them, is uploaded under its version but not promoted,
# and the job fails. A build of a subset of states (GRID_STATES other than `all`) is
# never promoted: the server would withdraw nothing outside it, but the map would lose coverage.
#
# Requires: python3 with requirements.txt installed (pandas, pyarrow, requests, pyyaml), pip,
# aws CLI (R2 is S3-compatible). Installs pyosmium itself.
set -euo pipefail

: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
: "${R2_TILES_BUCKET:?set R2_TILES_BUCKET (infra/terraform output: tiles_bucket_name)}"

GRID_STATES="${GRID_STATES:-all}"
OSMIUM_VERSION="${OSMIUM_VERSION:-4.3.2}"
PYTHON="${PYTHON:-python3}"
PREFIX="grid/osm-power"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

log() { echo "[build-grid-layers] $*"; }
r2() {
  AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws "$@" --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
}

# --- 1. pyosmium: only this build reads OSM files, so it is not in requirements.txt ---
"$PYTHON" -m pip install --quiet "osmium==${OSMIUM_VERSION}"
log "pyosmium $("$PYTHON" -c 'import osmium.version as v; print(v.pyosmium_release)')"

# --- 2. build: per state, download -> filter -> delete -> read; GeoParquet + manifest ---
out_dir="${work_dir}/out"
log "building states=${GRID_STATES}"
"$PYTHON" -m pipeline.context.osm_power build --states "$GRID_STATES" \
  --out-dir "$out_dir" --work-dir "${work_dir}/osm" > "${work_dir}/build.json"
version="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "${out_dir}/manifest.json")"
log "built ${version}: $(du -h "${out_dir}/lines.parquet" | cut -f1) lines, $(du -h "${out_dir}/substations.parquet" | cut -f1) substations"
"$PYTHON" - "${out_dir}/manifest.json" <<'PY'
import json, sys
m = json.load(open(sys.argv[1]))
for r in m["regions"]:
    print(f"[build-grid-layers]   {r['region']}: {r['lines_kept']} lines, {r['substations_kept']} substations, data {r['timestamp']}")
PY

# --- 3. the ODbL notice that travels with the files ---
cat > "${out_dir}/README.txt" <<EOF
OpenStreetMap power network, United States -- ${version}

Contains information from OpenStreetMap (https://www.openstreetmap.org/copyright),
which is made available here under the Open Database License (ODbL) 1.0:
https://opendatacommons.org/licenses/odbl/1-0/

(c) OpenStreetMap contributors.

These files are a Derivative Database of OpenStreetMap: power=line and power=cable
ways with a highest stated voltage of 69 kV or more (or no voltage tag), and
power=substation nodes and areas except substation=traction and
substation=minor_distribution, filtered from Geofabrik's US state extracts and
reshaped by Inframation. They are offered under the same licence, ODbL 1.0.
The method is pipeline/context/osm_power.py; manifest.json lists each state
extract's data timestamp, the rules applied and every file's sha256.
EOF

# --- 4. upload the version (immutable key) ---
for name in lines.parquet substations.parquet; do
  r2 s3 cp "${out_dir}/${name}" "s3://${R2_TILES_BUCKET}/${PREFIX}/${version}/${name}" \
    --content-type application/vnd.apache.parquet
done
r2 s3 cp "${out_dir}/manifest.json" "s3://${R2_TILES_BUCKET}/${PREFIX}/${version}/manifest.json" \
  --content-type application/json
r2 s3 cp "${out_dir}/README.txt" "s3://${R2_TILES_BUCKET}/${PREFIX}/${version}/README.txt" \
  --content-type "text/plain; charset=utf-8"
log "uploaded r2://${R2_TILES_BUCKET}/${PREFIX}/${version}/"

# --- 5. promote, if the gate passes ---
if [[ "$GRID_STATES" != "all" ]]; then
  log "GRID_STATES=${GRID_STATES} is a subset: ${version} uploaded, latest.json not changed"
  exit 0
fi
previous="${work_dir}/previous.json"
if ! r2 s3 cp "s3://${R2_TILES_BUCKET}/${PREFIX}/latest.json" "$previous" >/dev/null 2>&1; then
  log "no published latest.json yet; this is the first build"
  rm -f "$previous"
fi
"$PYTHON" -m pipeline.context.osm_power check --manifest "${out_dir}/manifest.json" --previous "$previous"
r2 s3 cp "${out_dir}/manifest.json" "s3://${R2_TILES_BUCKET}/${PREFIX}/latest.json" \
  --content-type application/json --cache-control "max-age=300"
log "promoted ${version} -> ${PREFIX}/latest.json"
