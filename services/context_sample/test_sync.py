"""`services.context_sample.sync`: a server's prospect directory follows the R2 pointer, checks every hash,
swaps the manifest last, and never writes outside its directory."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
from typing import Any

import pytest

from services.context_sample.sampler import Sampler
from services.context_sample.sync import SyncError, client_from_env, sync

BUCKET = "private"
PECOS = (-103.5, 31.4)


class FakeS3:
    """The two calls sync makes, over a dict of key -> bytes."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.downloads: list[str] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        assert Bucket == BUCKET
        return {"Body": io.BytesIO(self.objects[Key])}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        assert bucket == BUCKET
        self.downloads.append(key)
        pathlib.Path(filename).write_bytes(self.objects[key])


def publish(s3: FakeS3, build_dir: pathlib.Path, build_id: str, *, region: str = "texas") -> None:
    """What infra/scripts/build_prospect_layers.sh uploads: the build's files, then the pointer."""
    for path in build_dir.iterdir():
        if path.is_file():
            s3.objects[f"prospect/builds/{region}/{build_id}/{path.name}"] = path.read_bytes()
    manifest = (build_dir / "layers.json").read_bytes()
    s3.objects[f"prospect/current/{region}.json"] = json.dumps(
        {"build_id": build_id, "region": region, "manifest_sha256": hashlib.sha256(manifest).hexdigest()}
    ).encode()


def test_sync_downloads_checks_and_serves_the_build(
    fixture_prospect_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    s3 = FakeS3()
    publish(s3, fixture_prospect_dir, "20261101T054100Z")
    dest = tmp_path / "srv"
    result = sync(dest, region="texas", client=s3, bucket=BUCKET)
    assert result["status"] == "updated" and result["build_id"] == "20261101T054100Z"
    manifest = json.loads((dest / "layers.json").read_text())
    assert manifest["build_id"] == "20261101T054100Z"
    assert manifest["layers"]["solar_ghi"]["file"] == "20261101T054100Z/solar_ghi.tif"
    assert manifest["layers"]["oil_gas_reserves_state"]["extra_files"] == [
        "20261101T054100Z/oil_gas_reserves_by_state.csv"
    ]
    ghi = next(i for i in Sampler(dest).sample(*PECOS)["layers"] if i["layer"] == "solar_ghi")
    assert ghi["status"] == "ok" and ghi["value"] == 5.81
    assert not any(p.name.startswith(".staging") for p in dest.iterdir())

    s3.downloads.clear()
    assert sync(dest, region="texas", client=s3, bucket=BUCKET)["status"] == "current"
    assert s3.downloads == [], "an unchanged pointer downloads nothing"


def test_a_file_that_fails_its_hash_changes_nothing(
    fixture_prospect_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    s3 = FakeS3()
    publish(s3, fixture_prospect_dir, "20261101T054100Z")
    dest = tmp_path / "srv"
    sync(dest, region="texas", client=s3, bucket=BUCKET)
    before = (dest / "layers.json").read_bytes()

    publish(s3, fixture_prospect_dir, "20261201T054100Z")
    s3.objects["prospect/builds/texas/20261201T054100Z/solar_ghi.tif"] = b"corrupted"
    with pytest.raises(SyncError, match="sha256"):
        sync(dest, region="texas", client=s3, bucket=BUCKET)
    assert (dest / "layers.json").read_bytes() == before
    assert sorted(p.name for p in dest.iterdir()) == ["20261101T054100Z", "layers.json"]


def test_a_manifest_naming_a_path_outside_the_directory_is_refused(
    fixture_prospect_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    s3 = FakeS3()
    publish(s3, fixture_prospect_dir, "20261101T054100Z")
    key = "prospect/builds/texas/20261101T054100Z/layers.json"
    manifest = json.loads(s3.objects[key])
    manifest["layers"]["solar_ghi"]["file"] = "../../etc/x.tif"
    s3.objects[key] = json.dumps(manifest).encode()
    s3.objects["prospect/current/texas.json"] = json.dumps({"build_id": "20261101T054100Z"}).encode()
    with pytest.raises(SyncError, match="bad file entry"):
        sync(tmp_path / "srv", region="texas", client=s3, bucket=BUCKET)
    s3.objects["prospect/current/texas.json"] = json.dumps({"build_id": "../x"}).encode()
    with pytest.raises(SyncError, match="no valid build_id"):
        sync(tmp_path / "srv", region="texas", client=s3, bucket=BUCKET)


def test_a_rollback_keeps_the_build_it_points_at(
    fixture_prospect_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    s3 = FakeS3()
    dest = tmp_path / "srv"
    for build_id in ("20261101T054100Z", "20261201T054100Z", "20270101T054100Z"):
        publish(s3, fixture_prospect_dir, build_id)
        sync(dest, region="texas", client=s3, bucket=BUCKET)
    assert sorted(p.name for p in dest.iterdir() if p.is_dir()) == ["20261201T054100Z", "20270101T054100Z"]
    publish(s3, fixture_prospect_dir, "20261101T054100Z")  # the owner points back at the oldest build
    sync(dest, region="texas", client=s3, bucket=BUCKET)
    assert sorted(p.name for p in dest.iterdir() if p.is_dir()) == ["20261101T054100Z", "20270101T054100Z"]
    assert json.loads((dest / "layers.json").read_text())["build_id"] == "20261101T054100Z"


def test_client_from_env_names_what_is_missing() -> None:
    with pytest.raises(SyncError, match=r"R2_BUCKET.*R2_ENDPOINT or R2_ACCOUNT_ID"):
        client_from_env({"R2_ACCESS_KEY_ID": "k", "R2_SECRET_ACCESS_KEY": "s"})
