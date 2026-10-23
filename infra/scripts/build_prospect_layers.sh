#!/usr/bin/env bash
# Prospecting layers build (docs/00-PLAN.md decision row of 2026-10-22 "Prospecting layers: resource,
# constraint and incentive data join the base map and the site finder, screening grade only"; docs/25
# "Prospecting layers"). Run by .github/workflows/prospect-layers.yml; the national inputs (a 1.5 GB PAD-US
# geodatabase, a CONUS wind grid) are never downloaded on a development machine.
#
# What it does, in the basemap build's discipline (infra/scripts/build_basemap.sh: a dated key first, the
# canonical name last, so a failed build never replaces what is served):
#
#   1. Seed the output directory with the build now served (prospect/current/<region>.json names it), so
#      a source that fails this time keeps its previous layer: pipeline.context.prospect.build merges
#      into an existing manifest and never drops an entry for a failed source.
#   2. Take the files a person downloaded by hand from prospect/inbox/ (the Global Wind Atlas Terms forbid
#      automated access, data/sources.yaml global.dtu.global_wind_atlas). With GDAL on PATH the wind
#      GeoTIFF is clipped to the region and re-encoded as tiled Deflate first, so the pure-Python reader
#      never decodes a national LZW file. PAD-US and Aqueduct zips may arrive the same way (2b), because
#      neither has a permitted automated route today.
#   3. Run `python -m pipeline.context.prospect.build` (every other source fetches politely and re-reads
#      its terms page first; a changed clause stops that source).
#   4. Refuse to publish a manifest with fewer layers than the one served.
#   5. Upload the manifest and its files to prospect/builds/<region>/<build_id>/ in the PRIVATE bucket
#      (R2_BUCKET, never the public tiles bucket: NLR's notice keeps the solar rasters out of public
#      redistribution, data/sources.yaml us.nrel.nsrdb.psm3_annual), then write the pointer
#      prospect/current/<region>.json last. Servers follow the pointer with
#      `python -m services.context_sample.sync`, which checks every sha256.
#   6. Copy the run records (data/runs/...) to the bucket's data/ prefix, where the connectors' run
#      records live (pipeline/connectors/objectstore.py DEFAULT_PREFIX).
#
# Exit status: the build's (1 when any source failed). A partial build is still promoted when it holds
# at least as many layers as the served one, so a red run means "a source needs a look", never "the
# layers went missing".
#
# Requires: python3 with requirements.txt plus pyogrio and pyproj (geodatabase reading), the aws CLI, and
# optionally gdal_translate (gdal-bin) for the wind file.
set -euo pipefail

: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
: "${R2_BUCKET:?set R2_BUCKET (the private bucket, infra/terraform output r2_bucket_name; not the tiles bucket)}"

REGION="${PROSPECT_REGION:-conus}"
SOURCES="${PROSPECT_SOURCES:-}" # empty: every source in pipeline.context.prospect.build.SOURCES
PYTHON="${PYTHON:-python3}"
PREFIX="prospect"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

r2_endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
export AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY"
export AWS_DEFAULT_REGION="${AWS_DEFAULT_REGION:-auto}"
s3() { aws s3 "$@" --endpoint-url "$r2_endpoint" --only-show-errors; }
log() { echo "[prospect-layers] $*"; }

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
out="$work/out"
snaps="$work/snapshots"
mkdir -p "$out" "$snaps" "$work/tmp" "$work/inbox" "$work/upload"
export TMPDIR="$work/tmp"           # the builders' raw downloads and extracted geodatabases land here
export INFRAQUE_DATA_DIR="$work/data" # run records and the small snapshots (pipeline.connectors.store)
build_id="${PROSPECT_BUILD_ID:-$(date -u +%Y%m%dT%H%M%SZ)}" # override only in tests
pointer_key="${PREFIX}/current/${REGION}.json"
log "region=${REGION} build_id=${build_id} sources=${SOURCES:-all}"

# --- 1. Seed from the build now served ---
# Only "the pointer does not exist" (HTTP 404 on the HeadObject that `aws s3 cp` makes first) means nothing
# is served. Any other failure (403, a timeout, a reset connection) stops the run: taking it for "nothing
# served" would set served_layers to 0 and let a partial build replace the served one past step 4.
served_layers=0
if pointer_err="$(s3 cp "s3://${R2_BUCKET}/${pointer_key}" "$work/current.json" 2>&1)"; then
  current="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["build_id"])' "$work/current.json")"
  log "seeding from the served build ${current}"
  s3 cp "s3://${R2_BUCKET}/${PREFIX}/builds/${REGION}/${current}/" "$out/" --recursive
  served_layers="$("$PYTHON" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["layers"]))' "$out/layers.json")"
elif grep -qE '\(404\)|Not Found|NoSuchKey' <<<"$pointer_err"; then
  log "no served build for ${REGION}; starting empty"
else
  echo "[prospect-layers] cannot read ${pointer_key}, so the served build is unknown; nothing built or published: ${pointer_err}" >&2
  exit 1
fi

# --- 2. Files downloaded by hand (Global Wind Atlas) ---
s3 cp "s3://${R2_BUCKET}/${PREFIX}/inbox/global_wind_atlas/" "$work/inbox/" --recursive || true
gwa="$(find "$work/inbox" -maxdepth 1 -name '*.tif' | sort | tail -n 1)"
if [[ -n "$gwa" ]]; then
  name="$(basename "$gwa" .tif)"
  mkdir -p "$snaps/global_wind_atlas"
  if command -v gdal_translate >/dev/null 2>&1; then
    read -r west north east south < <("$PYTHON" -c \
      "from pipeline.context.prospect.common import REGIONS; w, s, e, n = REGIONS['${REGION}']; print(w, n, e, s)")
    log "clipping ${name}.tif to ${REGION} and re-encoding as tiled Deflate (gdal_translate)"
    gdal_translate -q -projwin "$west" "$north" "$east" "$south" \
      -co TILED=YES -co COMPRESS=DEFLATE -co PREDICTOR=3 -co BIGTIFF=IF_SAFER \
      "$gwa" "$snaps/global_wind_atlas/${name}.tif"
    rm -f "$gwa"
  else
    mv "$gwa" "$snaps/global_wind_atlas/${name}.tif"
  fi
  # The provenance's retrieved_at is the download date the person recorded beside the file.
  [[ -f "$work/inbox/${name}.json" ]] && cp "$work/inbox/${name}.json" "$snaps/global_wind_atlas/${name}.json"
else
  log "no Global Wind Atlas file in ${PREFIX}/inbox/global_wind_atlas/: the wind layer keeps its served build, if any"
fi

# --- 2b. PAD-US and Aqueduct files downloaded by hand, when there is no permitted automated route ---
# Measured 2026-10-23: ScienceBase's catalogue route for the PAD-US zips answers 404 and its file manager
# serves a JavaScript application, and files.wri.org's robots.txt is "User-agent: * / Disallow: /", which the
# Aqueduct builder honours. A person downloads the zip in a browser and uploads it (with an optional
# <name>.json {"downloaded_at": "..."}) to prospect/inbox/<padus|wri_aqueduct>/; the builder reads it
# instead of fetching.
for sub in padus wri_aqueduct; do
  mkdir -p "$work/inbox_${sub}"
  s3 cp "s3://${R2_BUCKET}/${PREFIX}/inbox/${sub}/" "$work/inbox_${sub}/" --recursive || true
  if compgen -G "$work/inbox_${sub}/*.zip" >/dev/null || compgen -G "$work/inbox_${sub}/*.geojson" >/dev/null; then
    mkdir -p "$snaps/${sub}"
    mv "$work/inbox_${sub}"/* "$snaps/${sub}/"
    log "using the ${sub} file(s) from ${PREFIX}/inbox/${sub}/"
  else
    log "no ${sub} file in ${PREFIX}/inbox/${sub}/: the builder tries its automated route"
  fi
done

# --- 3. Build ---
args=(--region "$REGION" --out-dir "$out" --snapshots "$snaps")
[[ -n "$SOURCES" ]] && args+=(--sources "$SOURCES")
set +e
"$PYTHON" -m pipeline.context.prospect.build "${args[@]}" > "$work/report.json"
status=$?
set -e
cat "$work/report.json"

# --- 4. Check the manifest before anything is published ---
new_layers="$("$PYTHON" - "$out" "$work/upload" <<'PY'
import hashlib, json, os, pathlib, sys

out, upload = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
manifest = json.loads((out / "layers.json").read_text())
for layer_id, entry in manifest["layers"].items():
    files = [(entry["file"], entry["sha256"])]
    files += [(x, entry.get("extra_sha256", {}).get(x)) for x in entry.get("extra_files", [])]
    for name, digest in files:
        path = out / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise SystemExit(f"{layer_id}: {name} does not match its sha256 in layers.json")
        os.link(path, upload / name)
os.link(out / "layers.json", upload / "layers.json")
print(len(manifest["layers"]))
PY
)"
log "layers in the new manifest: ${new_layers} (served: ${served_layers})"
if [[ "$new_layers" -eq 0 || "$new_layers" -lt "$served_layers" ]]; then
  echo "[prospect-layers] refusing to publish ${new_layers} layers over the ${served_layers} served" >&2
  exit 1
fi

# --- 5. Upload to the dated key, then move the pointer ---
s3 cp "$work/upload/" "s3://${R2_BUCKET}/${PREFIX}/builds/${REGION}/${build_id}/" --recursive
"$PYTHON" - "$out/layers.json" "$build_id" "$REGION" "$status" > "$work/pointer.json" <<'PY'
import datetime as dt, hashlib, json, pathlib, sys

manifest_path, build_id, region, status = sys.argv[1], sys.argv[2], sys.argv[3], int(sys.argv[4])
raw = pathlib.Path(manifest_path).read_bytes()
manifest = json.loads(raw)
print(json.dumps({
    "build_id": build_id,
    "region": region,
    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
    "published_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
    "layers": sorted(manifest["layers"]),
    "failures": manifest.get("failures") or {},
    "build_exit_status": status,
}, indent=1))
PY
s3 cp "$work/pointer.json" "s3://${R2_BUCKET}/${pointer_key}" --content-type application/json
log "published ${PREFIX}/builds/${REGION}/${build_id}/ and moved ${pointer_key} to it"

# --- 6. Run records ---
if [[ -d "$INFRAQUE_DATA_DIR/runs" ]]; then
  s3 cp "$INFRAQUE_DATA_DIR/runs/" "s3://${R2_BUCKET}/data/runs/" --recursive
fi

exit "$status"
