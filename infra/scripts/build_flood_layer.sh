#!/usr/bin/env bash
# Flood layer build for the powered-land site finder (docs/00-PLAN.md owner decision 2026-10-22;
# docs/25 section 5; source us.fema.nfhl in data/sources.yaml). Run by .github/workflows/flood-layer.yml,
# one mode per job, the way build_basemap.sh refreshes the basemap:
#
#   plan            build the areas of interest (buffers around 69 kV-and-up lines), upload them under
#                   the version's key, and print the states to build (JSON list) on stdout
#   state <ST>      fetch one state's NFHL layers from FEMA (only the two layers used, by HTTP range),
#                   clip, upload sfha/, zones_other/, coverage/ and states/<ST>.json; the raw download
#                   is deleted whether the build succeeds or not
#   publish         collect the state summaries into manifest.json, upload it, then copy it to latest.json.
#                   Refused (latest.json unchanged): a state whose SFHA rows fell by more than half since
#                   the last build, and, in a full build, a state of the last build that was not rebuilt
#                   (ALLOW_DROPPED_STATES names any that may go). A partial build (STATES set) carries
#                   every state it did not rebuild forward from latest.json, under that state's own
#                   version key, so latest.json always lists every state.
#
# Keys (bucket R2_BUCKET, the object store the pipeline already uses; never the public tiles bucket):
#   data/normalized/context/us.fema.nfhl/<version>/{aoi.parquet,aoi.json,sfha/<ST>.parquet,
#     zones_other/<ST>.parquet,coverage/<ST>.parquet,states/<ST>.json,manifest.json}
#   data/normalized/context/us.fema.nfhl/latest.json   (a copy of the newest complete manifest)
# The manifest names each state's file keys, which may be under an earlier version: never expire a
# version prefix that latest.json still names. Every file of a version is written before latest.json
# moves, so a failed build never changes what the finder reads (the "expand before restart" discipline
# of build_basemap.sh).
#
# Requires: python3 with requirements.txt plus shapely and pyogrio (installed below if missing), and the
# aws CLI (R2 is S3-compatible). Disk: one state's two layers at a time (Texas: 1.3 GB written).
set -euo pipefail

mode="${1:?usage: build_flood_layer.sh plan|state <ST>|publish}"
: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
: "${R2_BUCKET:?set R2_BUCKET (infra/terraform: the object storage bucket, not the tiles bucket)}"
: "${FLOOD_VERSION:?set FLOOD_VERSION (the workflow computes one per run, e.g. 20261022T0441Z)}"

PYTHON="${PYTHON:-python3}"
PREFIX="data/normalized/context/us.fema.nfhl"
#: The build-only geometry stack. shapely is also what the finder reads these files with; pyogrio
#: bundles GDAL, which reads FEMA's file geodatabases.
GEO_PACKAGES="${GEO_PACKAGES:-shapely==2.1.2 pyogrio==0.11.1}"
MIN_KV="${MIN_KV:-69}"
BUFFER_MILES="${BUFFER_MILES:-2}"
#: Refuse to publish when a state's SFHA rows fell by more than this share (a truncated download).
MAX_DROP_PCT="${MAX_DROP_PCT:-50}"
#: STATES (plan, publish): the states of a partial rebuild, comma separated; empty = a full build.
#: ALLOW_DROPPED_STATES (publish): states of the previous build a full build may leave out.
r2_endpoint="https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

log() { echo "[build-flood] $*" >&2; }
r2() {
  AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws s3 "$@" --endpoint-url "$r2_endpoint"
}
key() { echo "${PREFIX}/${FLOOD_VERSION}/$1"; }

[[ "$FLOOD_VERSION" =~ ^[0-9]{8}T[0-9]{4}Z$ ]] || { echo "[build-flood] FLOOD_VERSION must look like 20261022T0441Z" >&2; exit 2; }

needs="shapely"
[[ "$mode" != state ]] || needs="shapely, pyogrio" # GDAL reads the geodatabases; plan and publish do not
if ! "$PYTHON" -c "import ${needs}" >/dev/null 2>&1; then
  log "installing ${GEO_PACKAGES}"
  # shellcheck disable=SC2086 # a space-separated package list on purpose
  "$PYTHON" -m pip install --quiet ${GEO_PACKAGES} >&2 # stdout carries plan's state list
fi
nfhl() { "$PYTHON" -m pipeline.context.fema_nfhl "$@"; }

case "$mode" in
  plan)
    lines="${LINES_PARQUET:-}"
    if [[ -z "$lines" ]]; then
      # Until the OpenStreetMap lines land, the LBNL x HIFLD subset (CC BY 4.0, docs/25 section 2).
      # data.openei.org rate-limits hard (docs/25 section 2.3); one request.
      lines="${work_dir}/lines.parquet"
      log "fetching the LBNL transmission lines"
      "$PYTHON" -m pipeline.context.lbnl_transmission --out "$lines" >&2
    fi
    log "building areas of interest: ${BUFFER_MILES} mi around lines of ${MIN_KV} kV and up"
    nfhl aoi --lines "$lines" --min-kv "$MIN_KV" --buffer-miles "$BUFFER_MILES" \
      --states "${STATES:-}" --out "${work_dir}/aoi.parquet" > "${work_dir}/aoi.json"
    "$PYTHON" -c 'import json,sys; d=json.load(open(sys.argv[1])); print(json.dumps(sorted(d["states"])))' \
      "${work_dir}/aoi.json" > "${work_dir}/states.json"
    r2 cp "${work_dir}/aoi.parquet" "s3://${R2_BUCKET}/$(key aoi.parquet)" --only-show-errors
    r2 cp "${work_dir}/aoi.json" "s3://${R2_BUCKET}/$(key aoi.json)" --only-show-errors
    log "areas of interest: $(cat "${work_dir}/states.json")"
    cat "${work_dir}/states.json"
    ;;

  state)
    st="${2:?usage: build_flood_layer.sh state <ST>}"
    [[ "$st" =~ ^[A-Z]{2}$ ]] || { echo "[build-flood] not a state code: $st" >&2; exit 2; }
    r2 cp "s3://${R2_BUCKET}/$(key aoi.parquet)" "${work_dir}/aoi.parquet" --only-show-errors
    out="${work_dir}/out"
    log "${st}: fetch, clip, write"
    nfhl state --state "$st" --aoi "${work_dir}/aoi.parquet" --out-dir "$out" --work-dir "${work_dir}/raw" \
      ${PRODUCT_FILE:+--product-file "$PRODUCT_FILE"} > "${work_dir}/summary.json"
    [[ ! -e "${work_dir}/raw" ]] || [[ -z "$(ls -A "${work_dir}/raw")" ]] \
      || { echo "[build-flood] ${st}: the raw download was not deleted" >&2; exit 1; }
    for layer in sfha zones_other coverage; do
      r2 cp "${out}/${layer}/${st}.parquet" "s3://${R2_BUCKET}/$(key "${layer}/${st}.parquet")" --only-show-errors
    done
    # The summary goes last: publish counts a state as built only when its summary exists.
    r2 cp "${out}/states/${st}.json" "s3://${R2_BUCKET}/$(key "states/${st}.json")" --only-show-errors
    log "${st}: $("$PYTHON" -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s["files"]["sfha"]["rows"], "SFHA rows,", s["sfha_area_km2"], "km2,", s["seconds"]["total"], "s")' "${work_dir}/summary.json")"
    ;;

  publish)
    out="${work_dir}/out"
    mkdir -p "${out}/states"
    r2 cp "s3://${R2_BUCKET}/$(key states/)" "${out}/states/" --recursive --only-show-errors
    r2 cp "s3://${R2_BUCKET}/$(key aoi.json)" "${work_dir}/aoi.json" --only-show-errors
    expected="${EXPECTED_STATES:-}"
    if [[ -n "$expected" ]]; then
      for st in $expected; do
        [[ -s "${out}/states/${st}.json" ]] || { echo "[build-flood] ${st} has no summary: not publishing" >&2; exit 1; }
      done
    fi
    previous=()
    if r2 cp "s3://${R2_BUCKET}/${PREFIX}/latest.json" "${work_dir}/previous.json" --only-show-errors 2>/dev/null; then
      previous=(--previous "${work_dir}/previous.json")
    else
      log "no previous manifest (first build)"
    fi
    scope=()
    if [[ -n "${STATES:-}" ]]; then
      scope=(--partial) # a rebuild of chosen states: the others carry forward from latest.json
      log "partial build (${STATES}): states not rebuilt keep their previous version"
    fi
    nfhl manifest --out-dir "$out" --version "$FLOOD_VERSION" --aoi-summary "${work_dir}/aoi.json" \
      --max-drop-pct "$MAX_DROP_PCT" --allow-dropped "${ALLOW_DROPPED_STATES:-}" \
      ${scope[@]+"${scope[@]}"} ${previous[@]+"${previous[@]}"} >&2
    r2 cp "${out}/manifest.json" "s3://${R2_BUCKET}/$(key manifest.json)" --only-show-errors --content-type application/json
    # Server-side copy; --copy-props none because R2 answers GetObjectTagging with NotImplemented
    # (build_basemap.sh, deploy-beta #12).
    r2 cp "s3://${R2_BUCKET}/$(key manifest.json)" "s3://${R2_BUCKET}/${PREFIX}/latest.json" \
      --copy-props none --content-type application/json --only-show-errors
    log "published ${PREFIX}/${FLOOD_VERSION}; latest.json now points at it"
    ;;

  *)
    echo "usage: build_flood_layer.sh plan|state <ST>|publish" >&2
    exit 2
    ;;
esac
