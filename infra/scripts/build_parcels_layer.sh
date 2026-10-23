#!/usr/bin/env bash
# Texas land parcels for the site finder (owner decisions 2026-10-23: Site finder Pro gets land parcels,
# and Inframation serves the owner's own powered-land deals first; docs/25-siting-parcels.md; source
# us.tx.txgio.parcels in data/sources.yaml). Run by .github/workflows/parcels-layer.yml, one mode per
# job, the way build_flood_layer.sh builds the flood layer:
#
#   dry-run         read TxGIO's catalogue and ask every chosen county zip its size (HEAD); download and
#                   upload nothing; print the per-county report (the step summary when GITHUB_STEP_SUMMARY
#                   is set). Needs no bucket and no secret.
#   plan            the newest vintage's county zips -> plan.json, uploaded under the build's version key;
#                   writes `version` and the county batches (`batches`, a JSON list) to GITHUB_OUTPUT
#   batch <F,F,..>  for each county: download its zip (one at a time, five seconds apart), group and
#                   write its files, upload them, delete the zip. A county that fails writes its reason to
#                   counties/<fips>.json and the batch goes on; the publish job reports every failure.
#   publish         the county summaries -> manifest.json (a county not built or failed keeps its published
#                   entry), README.txt, then latest.json. Refused (latest.json unchanged, exit 1) when a
#                   published county would be lost or a county's parcels fell by more than half. A run with
#                   failed counties still promotes what was built and then exits 1, so it shows red with
#                   the per-county failure table.
#
# Keys (bucket R2_BUCKET, the private object store the flood and prospect builds use; never the public
# tiles bucket: us.tx.txgio.parcels is derived_only, and parcels/<fips>.parquet is the source table less
# its owner columns, so it is not mirrored where anyone can download it. The public reads parcels only
# through the API, behind the register gate):
#   data/normalized/context/us.tx.txgio.parcels/v1/<publication>-<build>/{plan.json,
#     parcels/<fips>.parquet,holdings/<fips>.parquet,counties/<fips>.json,manifest.json,README.txt}
#   data/normalized/context/us.tx.txgio.parcels/latest.json   (a copy of the newest promoted manifest;
#     servers pull from it with their own object-store credentials:
#     python -m pipeline.context.txgio_parcels pull)
# The manifest names each county's files under the version that built them, so never expire a version
# prefix latest.json still names. The script refuses to run when R2_BUCKET is the tiles bucket.
#
# Offline (tests): PARCELS_CATALOGUE_DIR holds recorded catalogue answers (collections.json,
# resources.json) and PARCELS_ZIP_DIR the county zips (<fips>.zip); nothing then touches TxGIO.
#
# Requires: python3 with requirements.txt, plus pyogrio (GDAL, reads the county geodatabases) and
# pyproj (any county not in EPSG:4326), installed below if missing; the aws CLI (R2 is S3-compatible).
# Disk: one county zip at a time (Harris, the largest, 430 MB).
set -euo pipefail

mode="${1:?usage: build_parcels_layer.sh dry-run|plan|batch <fips,...>|publish}"
PYTHON="${PYTHON:-python3}"
PREFIX="data/normalized/context/us.tx.txgio.parcels"
GEO_PACKAGES="${GEO_PACKAGES:-pyogrio==0.11.1 pyproj==3.7.2}"
COUNTIES="${COUNTIES:-all}"
BATCHES="${BATCHES:-16}"
#: Seconds between two county downloads (TxGIO's file host; the polite session spaces its own requests too).
DOWNLOAD_GAP_S="${DOWNLOAD_GAP_S:-5}"
work_dir="$(mktemp -d)"
trap 'rm -rf "$work_dir"' EXIT

log() { echo "[build-parcels] $*" >&2; }
parcels() { "$PYTHON" -m pipeline.context.txgio_parcels "$@"; }
catalogue_args=()
if [[ -n "${PARCELS_CATALOGUE_DIR:-}" ]]; then
  catalogue_args=(--catalogue "${PARCELS_CATALOGUE_DIR}/collections.json" --resources "${PARCELS_CATALOGUE_DIR}/resources.json")
fi
summary() {
  if [[ -n "${GITHUB_STEP_SUMMARY:-}" ]]; then cat >> "$GITHUB_STEP_SUMMARY"; else cat >&2; fi
}

if [[ "$mode" == "dry-run" ]]; then
  log "dry run: catalogue and a HEAD per county zip (${COUNTIES}); nothing is downloaded or uploaded"
  parcels plan --counties "$COUNTIES" --out "${work_dir}/plan.json" --check ${catalogue_args[@]+"${catalogue_args[@]}"} >&2
  parcels report --checks "${work_dir}/checks.json" | summary
  "$PYTHON" - "${work_dir}/checks.json" <<'PY'
import json, sys
checks = json.load(open(sys.argv[1]))
bad = [c for c in checks if not c.get("ok")]
for c in bad:
    print(f"::warning::{c.get('name') or ''} ({c['fips']}): {c.get('reason') or 'size differs from the catalogue'}")
print(f"[build-parcels] dry run: {len(checks) - len(bad)} of {len(checks)} county files answer as catalogued", file=sys.stderr)
PY
  exit 0
fi

: "${R2_ACCOUNT_ID:?set R2_ACCOUNT_ID}"
: "${R2_ACCESS_KEY_ID:?set R2_ACCESS_KEY_ID}"
: "${R2_SECRET_ACCESS_KEY:?set R2_SECRET_ACCESS_KEY}"
: "${R2_BUCKET:?set R2_BUCKET (infra/terraform: the object storage bucket, not the tiles bucket)}"
if [[ -n "${R2_TILES_BUCKET:-}" && "${R2_BUCKET}" == "${R2_TILES_BUCKET}" ]]; then
  log "R2_BUCKET is the public tiles bucket: the parcels never go there (derived_only)"
  exit 2
fi
r2() {
  AWS_ACCESS_KEY_ID="$R2_ACCESS_KEY_ID" AWS_SECRET_ACCESS_KEY="$R2_SECRET_ACCESS_KEY" \
    aws s3 "$@" --endpoint-url "https://${R2_ACCOUNT_ID}.r2.cloudflarestorage.com"
}
output() {
  if [[ -n "${GITHUB_OUTPUT:-}" ]]; then echo "$1" >> "$GITHUB_OUTPUT"; else echo "$1"; fi
}

case "$mode" in
  plan)
    parcels plan --counties "$COUNTIES" --out "${work_dir}/plan.json" ${catalogue_args[@]+"${catalogue_args[@]}"} >&2
    version="${PARCELS_VERSION:-$("$PYTHON" -c 'import json,sys; from pipeline.context.txgio_parcels import Plan, version_key; print(version_key(Plan.from_dict(json.load(open(sys.argv[1])))))' "${work_dir}/plan.json")}"
    [[ "$version" =~ ^v[0-9]+/[0-9A-Za-z._-]+$ ]] || { log "bad version key: $version"; exit 2; }
    batches="$("$PYTHON" -c 'import json,math,sys; p=json.load(open(sys.argv[1])); f=[c["fips"] for c in p["counties"]]; n=max(1,int(sys.argv[2])); s=max(1,math.ceil(len(f)/n)); print(json.dumps([",".join(f[i:i+s]) for i in range(0,len(f),s)]))' "${work_dir}/plan.json" "$BATCHES")"
    r2 cp "${work_dir}/plan.json" "s3://${R2_BUCKET}/${PREFIX}/${version}/plan.json" --content-type application/json --only-show-errors
    log "plan ${version}: $("$PYTHON" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["counties"]))' "${work_dir}/plan.json") counties"
    output "version=${version}"
    output "batches=${batches}"
    ;;

  batch)
    list="${2:?usage: build_parcels_layer.sh batch <fips,fips,...>}"
    : "${PARCELS_VERSION:?set PARCELS_VERSION (the version the plan job printed)}"
    if ! "$PYTHON" -c "import pyogrio" >/dev/null 2>&1; then
      log "installing ${GEO_PACKAGES}"
      # shellcheck disable=SC2086 # a space-separated package list on purpose
      "$PYTHON" -m pip install --quiet ${GEO_PACKAGES} >&2
    fi
    r2 cp "s3://${R2_BUCKET}/${PREFIX}/${PARCELS_VERSION}/plan.json" "${work_dir}/plan.json" --only-show-errors
    out="${work_dir}/out"
    first=1
    for fips in ${list//,/ }; do
      [[ "$fips" =~ ^48[0-9]{3}$ ]] || { log "not a Texas county FIPS code: $fips"; exit 2; }
      [[ $first -eq 1 ]] || sleep "$DOWNLOAD_GAP_S"
      first=0
      zip_args=()
      if [[ -n "${PARCELS_ZIP_DIR:-}" ]]; then
        zip_args=(--zip "${PARCELS_ZIP_DIR}/${fips}.zip")
      fi
      # The county command writes counties/<fips>.json whatever happens and never exits non-zero for a
      # county's own failure; a crash of the interpreter itself still leaves the loop going.
      parcels county --plan "${work_dir}/plan.json" --fips "$fips" --out-dir "$out" --work-dir "${work_dir}/zips" \
        ${zip_args[@]+"${zip_args[@]}"} >&2 || log "${fips}: the build process failed"
      [[ ! -e "${work_dir}/zips" ]] || [[ -z "$(ls -A "${work_dir}/zips")" ]] \
        || { log "${fips}: a downloaded zip was not deleted"; exit 1; }
      if [[ ! -s "${out}/counties/${fips}.json" ]]; then
        mkdir -p "${out}/counties"
        printf '{"fips": "%s", "status": "failed", "reason": "the build process ended without a summary"}\n' "$fips" > "${out}/counties/${fips}.json"
      fi
      status="$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1]))["status"])' "${out}/counties/${fips}.json")"
      if [[ "$status" == "ok" ]]; then
        for kind in parcels holdings; do
          r2 cp "${out}/${kind}/${fips}.parquet" "s3://${R2_BUCKET}/${PREFIX}/${PARCELS_VERSION}/${kind}/${fips}.parquet" \
            --content-type application/vnd.apache.parquet --only-show-errors
          rm -f "${out}/${kind}/${fips}.parquet"
        done
      fi
      # The summary goes last: publish counts a county as built only when its summary says ok.
      r2 cp "${out}/counties/${fips}.json" "s3://${R2_BUCKET}/${PREFIX}/${PARCELS_VERSION}/counties/${fips}.json" \
        --content-type application/json --only-show-errors
      log "${fips}: $("$PYTHON" -c 'import json,sys; s=json.load(open(sys.argv[1])); print(s["status"], s.get("parcels", ""), "parcels", s.get("holdings", ""), "holdings", (s.get("seconds") or {}).get("total"), "s", s.get("reason") or "")' "${out}/counties/${fips}.json")"
    done
    ;;

  publish)
    : "${PARCELS_VERSION:?set PARCELS_VERSION (the version the plan job printed)}"
    out="${work_dir}/out"
    mkdir -p "${out}/counties"
    r2 cp "s3://${R2_BUCKET}/${PREFIX}/${PARCELS_VERSION}/plan.json" "${work_dir}/plan.json" --only-show-errors
    r2 cp "s3://${R2_BUCKET}/${PREFIX}/${PARCELS_VERSION}/counties/" "${out}/counties/" --recursive --only-show-errors || true
    expected="$("$PYTHON" -c 'import json,sys; print(" ".join(c["fips"] for c in json.load(open(sys.argv[1]))["counties"]))' "${work_dir}/plan.json")"
    previous=()
    if r2 cp "s3://${R2_BUCKET}/${PREFIX}/latest.json" "${work_dir}/previous.json" --only-show-errors 2>/dev/null; then
      previous=(--previous "${work_dir}/previous.json")
    else
      log "no published latest.json yet; this is the first build"
    fi
    scope=()
    if [[ "${COUNTIES}" != "all" ]]; then
      scope=(--partial) # a chosen set of counties: the others carry forward from latest.json
    fi
    set +e
    parcels manifest --plan "${work_dir}/plan.json" --out-dir "$out" --version "$PARCELS_VERSION" \
      --expected "$expected" --allow-dropped "${ALLOW_DROPPED_COUNTIES:-}" \
      ${scope[@]+"${scope[@]}"} ${previous[@]+"${previous[@]}"} > "${work_dir}/gate.json"
    gate=$?
    set -e
    cat "${work_dir}/gate.json" >&2
    parcels report --manifest "${out}/manifest.json" | summary
    if [[ $gate -ne 0 ]]; then
      log "not promoted: $(cat "${work_dir}/gate.json")"
      r2 cp "${out}/manifest.json" "s3://${R2_BUCKET}/${PREFIX}/${PARCELS_VERSION}/manifest.json" \
        --content-type application/json --only-show-errors
      exit 1
    fi
    cat > "${out}/README.txt" <<EOF
Texas land parcels for Inframation's site finder -- ${PARCELS_VERSION}

Source: Texas Geographic Information Office (TWDB), StratMap Land Parcels, https://geographic.texas.gov;
data from Texas appraisal districts. TxGIO's catalogue lists the collection under Creative Commons Zero
v1.0 Universal (CC0 1.0). Neither Inframation nor this information, as presented on Inframation, is
endorsed by the State of Texas or any state agency. Not survey grade; not for legal purposes.

Changes by Inframation: no owner name, care-of name, mailing address or situs address is kept; parcels
of one owner that touch or lie within 30 m of each other share a holding id, drawn at random for each
build; acres are measured from the outlines in an equal-area projection, each outline once (accounts
drawn on one outline are one tract) and overlapping outlines as their union; outlines are simplified
by about a metre; the geographic id, legal description and every address are not kept. The method is
pipeline/context/txgio_parcels.py; manifest.json lists each county's retrieval time, counts and every
file's sha256. Private: these files stay in the object store and are never published as they are.
EOF
    r2 cp "${out}/manifest.json" "s3://${R2_BUCKET}/${PREFIX}/${PARCELS_VERSION}/manifest.json" \
      --content-type application/json --only-show-errors
    r2 cp "${out}/README.txt" "s3://${R2_BUCKET}/${PREFIX}/${PARCELS_VERSION}/README.txt" \
      --content-type "text/plain; charset=utf-8" --only-show-errors
    r2 cp "${out}/manifest.json" "s3://${R2_BUCKET}/${PREFIX}/latest.json" \
      --content-type application/json --only-show-errors
    log "promoted ${PARCELS_VERSION} -> ${PREFIX}/latest.json"
    failures="$("$PYTHON" -c 'import json,sys; print(len(json.load(open(sys.argv[1]))["failures"]))' "${out}/manifest.json")"
    if [[ "$failures" != "0" ]]; then
      log "${failures} counties failed (see the table); their published versions, if any, carry forward"
      exit 1
    fi
    ;;

  *)
    echo "usage: build_parcels_layer.sh dry-run|plan|batch <fips,...>|publish" >&2
    exit 2
    ;;
esac
