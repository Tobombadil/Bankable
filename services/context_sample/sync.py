"""Fill a server's prospect directory from R2: `python -m services.context_sample.sync [--region conus]`.

The GitHub Actions build (`.github/workflows/prospect-layers.yml`, `infra/scripts/build_prospect_layers.sh`)
writes to the private bucket (`R2_BUCKET`; the raw snapshots bucket, never the public tiles bucket, because
NLR's notice keeps the solar rasters out of public redistribution):

    prospect/builds/<region>/<build_id>/layers.json     the manifest (pipeline/context/prospect/build.py)
    prospect/builds/<region>/<build_id>/<file>          one file per layer, plus a layer's extra files
    prospect/current/<region>.json                      {"build_id": ..., "manifest_sha256": ...}, last

This module reads the pointer and, when the local `layers.json` already names that build, does nothing.
Otherwise it downloads the build into `<dest>/<build_id>/`, checks the manifest's sha256 against the
pointer and every file's sha256 against the manifest, rewrites each layer's `file` to
`<build_id>/<file>`, and replaces `<dest>/layers.json` last (an atomic rename). A running sampler reopens
when the manifest's mtime changes, so it never reads a half-written build. The previous build's
directory is kept for one rollback (by role, so a pointer rolled back to an older build keeps it);
other build directories are deleted.

Environment (the names the beta deploy already renders, infra/ci/beta.py): `R2_BUCKET`,
`R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, and `R2_ENDPOINT` or `R2_ACCOUNT_ID`. The destination is
`PROSPECT_DATA_DIR` (or `<data root>/normalized/prospect`, `services.context_sample.paths`).
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
from collections.abc import Mapping
from typing import Any

from services.context_sample.paths import MANIFEST_NAME, prospect_dir

PREFIX = "prospect/"
#: A build id is a UTC timestamp token, optionally suffixed (`20261101T054100Z`, `20261101T054100Z-r2`).
BUILD_ID = re.compile(r"^\d{8}T\d{6}Z(?:-[a-z0-9]+)?$")
#: A layer file is a bare name: no directory part, so nothing in a manifest can write outside `dest`.
FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class SyncError(RuntimeError):
    """The pointer, manifest or a file failed a check; nothing local was changed."""


def _sha256(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            digest.update(block)
    return digest.hexdigest()


def _get_json(client: Any, bucket: str, key: str) -> tuple[dict[str, Any], bytes]:
    body: bytes = client.get_object(Bucket=bucket, Key=key)["Body"].read()
    doc = json.loads(body)
    if not isinstance(doc, dict):
        raise SyncError(f"{key} is not a JSON object")
    return doc, body


def _files(manifest: dict[str, Any]) -> list[tuple[str, str]]:
    """(file name, expected sha256) for every layer file and extra file the manifest lists."""
    out: list[tuple[str, str]] = []
    for layer_id, entry in sorted((manifest.get("layers") or {}).items()):
        name, digest = entry.get("file"), entry.get("sha256")
        if not (isinstance(name, str) and FILE_NAME.match(name) and isinstance(digest, str)):
            raise SyncError(f"layer {layer_id}: bad file entry {name!r}")
        out.append((name, digest))
        for extra in entry.get("extra_files") or []:
            extra_digest = (entry.get("extra_sha256") or {}).get(extra)
            if not (isinstance(extra, str) and FILE_NAME.match(extra) and isinstance(extra_digest, str)):
                raise SyncError(f"layer {layer_id}: bad extra file entry {extra!r}")
            out.append((extra, extra_digest))
    return out


def local_build_id(dest: pathlib.Path) -> str | None:
    path = dest / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text()).get("build_id")
    except (OSError, ValueError):
        return None
    return value if isinstance(value, str) else None


def sync(
    dest: pathlib.Path, *, region: str, client: Any, bucket: str, prefix: str = PREFIX
) -> dict[str, Any]:
    """Bring `dest` to the build `prospect/current/<region>.json` names. Returns what was done."""
    pointer, _raw = _get_json(client, bucket, f"{prefix}current/{region}.json")
    build_id = pointer.get("build_id")
    if not (isinstance(build_id, str) and BUILD_ID.match(build_id)):
        raise SyncError(f"pointer for {region} names no valid build_id: {build_id!r}")
    previous = local_build_id(dest)
    if previous == build_id:
        return {"status": "current", "build_id": build_id}
    base = f"{prefix}builds/{region}/{build_id}/"
    manifest, manifest_raw = _get_json(client, bucket, base + MANIFEST_NAME)
    expected = pointer.get("manifest_sha256")
    if expected and hashlib.sha256(manifest_raw).hexdigest() != expected:
        raise SyncError(f"{base}{MANIFEST_NAME} does not match the pointer's manifest_sha256")
    if manifest.get("region") != region:
        raise SyncError(f"{base}{MANIFEST_NAME} is for region {manifest.get('region')!r}, not {region!r}")
    files = _files(manifest)

    dest.mkdir(parents=True, exist_ok=True)
    staging = dest / f".staging-{build_id}"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir()
    total = 0
    try:
        for name, digest in files:
            target = staging / name
            client.download_file(bucket, base + name, str(target))
            if _sha256(target) != digest:
                raise SyncError(f"{base}{name}: sha256 does not match the manifest")
            total += target.stat().st_size
        final = dest / build_id
        shutil.rmtree(final, ignore_errors=True)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    local = dict(manifest)
    local["layers"] = {}
    for layer_id, entry in (manifest.get("layers") or {}).items():
        e = dict(entry)
        e["file"] = f"{build_id}/{entry['file']}"
        if entry.get("extra_files"):
            e["extra_files"] = [f"{build_id}/{x}" for x in entry["extra_files"]]
        local["layers"][layer_id] = e
    local["build_id"] = build_id
    local["synced_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    tmp = dest / f".{MANIFEST_NAME}.part"
    tmp.write_text(json.dumps(local, indent=1, sort_keys=True, ensure_ascii=False))
    os.replace(tmp, dest / MANIFEST_NAME)

    # Keep the build now served and the one it replaced (a rollback may point at an older build id, so
    # the choice is by role, not by name order).
    keep = {build_id, previous} if previous else {build_id}
    removed = sorted(
        p.name for p in dest.iterdir() if p.is_dir() and BUILD_ID.match(p.name) and p.name not in keep
    )
    for name in removed:
        shutil.rmtree(dest / name, ignore_errors=True)
    return {
        "status": "updated",
        "build_id": build_id,
        "files": len(files),
        "bytes": total,
        "removed": removed,
    }


def client_from_env(env: Mapping[str, str] | None = None) -> tuple[Any, str]:
    """(boto3 S3 client, bucket) from the R2 variables; raises SyncError naming any that are missing."""
    from pipeline.connectors.objectstore import build_s3_client

    env = os.environ if env is None else env
    bucket = (env.get("R2_BUCKET") or "").strip()
    endpoint = (env.get("R2_ENDPOINT") or "").strip()
    account = (env.get("R2_ACCOUNT_ID") or "").strip()
    if not endpoint and account:
        endpoint = f"https://{account}.r2.cloudflarestorage.com"
    key_id = (env.get("R2_ACCESS_KEY_ID") or "").strip()
    secret = (env.get("R2_SECRET_ACCESS_KEY") or "").strip()
    missing = [
        n
        for n, v in (
            ("R2_BUCKET", bucket),
            ("R2_ENDPOINT or R2_ACCOUNT_ID", endpoint),
            ("R2_ACCESS_KEY_ID", key_id),
            ("R2_SECRET_ACCESS_KEY", secret),
        )
        if not v
    ]
    if missing:
        raise SyncError(f"missing {', '.join(missing)}")
    return build_s3_client(endpoint, key_id, secret), bucket


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--region", default="conus")
    parser.add_argument(
        "--dest", type=pathlib.Path, help="default: PROSPECT_DATA_DIR or <data>/normalized/prospect"
    )
    args = parser.parse_args(argv)
    try:
        client, bucket = client_from_env()
        result = sync(args.dest or prospect_dir(), region=args.region, client=client, bucket=bucket)
    except SyncError as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)  # noqa: T201
        return 1
    print(json.dumps(result))  # noqa: T201 - CLI summary line
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
