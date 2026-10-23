# ruff: noqa: S603 - asserts are the point (docs/04 E-6); fixed argv, no shell
"""infra/scripts/build_parcels_layer.sh end to end, offline: a fake `aws` maps each bucket to a
directory, the recorded catalogue answers stand in for TxGIO's catalogue, and the county zips are the
synthetic county written by GDAL (`pipeline/context/test_txgio_parcels.py` describes it). The plan, two
county batches (one county has no zip, so it fails and is reported), the publish gate and a server's
pull (with object-store credentials) run in turn, and the pulled layer is read back. Everything lands in
the private object store's `data/normalized/context/us.tx.txgio.parcels/` prefix and nothing in the
public tiles bucket: the source is derived_only, and the per-parcel file is its table less the owner
columns.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import zipfile
from typing import Any

import numpy as np
import pytest

from pipeline.context import txgio_parcels as tp

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "infra" / "scripts" / "build_parcels_layer.sh"
FIX = pathlib.Path(__file__).parent / "fixtures" / "txgio_parcels"
BASH = shutil.which("bash")

FAKE_AWS = """#!{python}
import pathlib, shutil, sys
root = pathlib.Path({root!r})
args = sys.argv[1:]
with (root / "calls.log").open("a") as log:
    log.write(" ".join(args) + "\\n")
assert args[:2] == ["s3", "cp"], args
skip = {{"--endpoint-url", "--content-type", "--cache-control"}}
pos, i = [], 2
while i < len(args):
    if args[i] in skip:
        i += 2
        continue
    if not args[i].startswith("--"):
        pos.append(args[i])
    i += 1
src, dst = pos
def local(p):
    return root / "bucket" / p[len("s3://"):] if p.startswith("s3://") else pathlib.Path(p)
s, d = local(src), local(dst)
if "--recursive" in args:
    if s.is_dir():
        shutil.copytree(s, d, dirs_exist_ok=True)
    sys.exit(0)
if not s.is_file():
    print(f"fatal error: (404) HeadObject: {{src}}", file=sys.stderr)
    sys.exit(1)
d.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(s, d)
"""


def _county_zip(path: pathlib.Path, fips: str) -> None:
    """The synthetic county in TxGIO's shapefile layout, its FIPS column set to `fips`."""
    pyogrio = pytest.importorskip("pyogrio")
    import shapely

    feats = [
        f
        for f in json.loads((FIX / "synthetic_county.geojson").read_text(encoding="utf-8"))["features"]
        if f["geometry"]
    ]
    geoms = np.asarray([shapely.to_wkb(shapely.geometry.shape(f["geometry"])) for f in feats], dtype=object)
    names = [
        "PROP_ID",
        "GEO_ID",
        "OWNER_NAME",
        "GIS_AREA",
        "GIS_AREA_UNIT",
        "STAT_LAND_USE",
        "LAND_VALUE",
        "MKT_VALUE",
        "TAX_YEAR",
        "FIPS",
    ]
    data = []
    for name in names:
        values = [fips if name == "FIPS" else f["properties"].get(name) for f in feats]
        if name in ("GIS_AREA", "LAND_VALUE", "MKT_VALUE"):
            data.append(np.asarray([np.nan if v is None else float(v) for v in values], dtype=np.float64))
        else:
            data.append(np.asarray(["" if v is None else str(v) for v in values], dtype=object))
    stage = path.parent / f"stage-{fips}"
    stage.mkdir()
    pyogrio.raw.write(
        str(stage / f"stratmap26-landparcels_{fips}_lp.shp"),
        geoms,
        data,
        names,
        driver="ESRI Shapefile",
        geometry_type="Polygon",
        crs="EPSG:4326",
    )
    with zipfile.ZipFile(path, "w") as zf:
        for p in sorted(stage.iterdir()):
            zf.write(p, p.name)
    shutil.rmtree(stage)


class _BucketClient:
    """`pull`'s S3 calls, answered from the fake bucket's directory (what the object store holds)."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root
        self.buckets: set[str] = set()

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        self.buckets.add(Bucket)
        data = (self.root / Bucket / Key).read_bytes()

        class Body:
            def read(self) -> bytes:
                return data

        return {"Body": Body()}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        self.buckets.add(bucket)
        shutil.copyfile(self.root / bucket / key, filename)


WORKFLOW = ROOT / ".github" / "workflows" / "parcels-layer.yml"


def test_the_workflow_is_dispatched_by_a_person_with_a_dry_run_and_needs_no_new_secret() -> None:
    import re

    import yaml

    doc = yaml.safe_load(WORKFLOW.read_text())
    on = doc["on"] if "on" in doc else doc[True]
    assert set(on) == {"workflow_dispatch", "pull_request"}  # no schedule: a person starts a build
    inputs = on["workflow_dispatch"]["inputs"]
    assert inputs["counties"]["default"] == "all" and inputs["dry_run"]["type"] == "boolean"
    assert doc["permissions"] == {"contents": "read"}
    jobs = doc["jobs"]
    assert jobs["county-batch"]["strategy"]["max-parallel"] <= 2
    assert jobs["county-batch"]["strategy"]["fail-fast"] is False
    assert jobs["publish"]["needs"] == ["plan", "county-batch"] and "always()" in jobs["publish"]["if"]
    assert "inputs.dry_run" in jobs["dry-run"]["if"] and "environment" not in jobs["dry-run"]
    assert "secrets." not in json.dumps(jobs["dry-run"]) and "secrets." not in json.dumps(jobs["test"])
    text = WORKFLOW.read_text()
    secrets = set(re.findall(r"secrets\.([A-Z0-9_]+)", text))
    # The R2 values flood-layer.yml already reads (account, token, the private bucket): nothing new.
    flood = (WORKFLOW.parent / "flood-layer.yml").read_text()
    assert secrets <= set(re.findall(r"secrets\.([A-Z0-9_]+)", flood)), secrets
    # The private object store, never the public tiles bucket or host.
    assert "R2_TILES_BUCKET" not in text and "tiles." not in text
    bucket = "R2_BUCKET: ${{ secrets.R2_BUCKET || vars.R2_BUCKET || 'beta-object-storage-production' }}"
    assert bucket in flood
    for job in ("plan", "county-batch", "publish"):
        assert jobs[job]["env"]["R2_BUCKET"] == bucket.split(": ", 1)[1], job
    steps = [s for j in jobs.values() for s in j["steps"]]
    assert all(len(s["uses"].split("@")[1].split()[0]) == 40 for s in steps if "uses" in s)
    runs = " ".join(str(s.get("run", "")) for s in steps)
    for mode in ("dry-run", "plan", "batch", "publish"):
        assert f"build_parcels_layer.sh {mode}" in runs
    for owned in (
        "pipeline/context/txgio_parcels.py",
        "infra/scripts/build_parcels_layer.sh",
        ".github/workflows/parcels-layer.yml",
    ):
        assert owned in on["pull_request"]["paths"]


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_build_plans_builds_reports_failures_publishes_and_a_server_pulls_it(
    tmp_path: pathlib.Path,
) -> None:
    pytest.importorskip("pyogrio")
    root = tmp_path / "s3"
    (root / "bucket" / "objects").mkdir(parents=True)
    (root / "bucket" / "tiles").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    aws = bindir / "aws"
    aws.write_text(FAKE_AWS.format(python=sys.executable, root=str(root)))
    aws.chmod(0o755)
    zips = tmp_path / "zips"
    zips.mkdir()
    _county_zip(zips / "48351.zip", "48351")
    _county_zip(zips / "48371.zip", "48371")
    output = tmp_path / "gh_output"
    report = tmp_path / "gh_summary"
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "R2_ACCOUNT_ID": "acct",
        "R2_ACCESS_KEY_ID": "key",
        "R2_SECRET_ACCESS_KEY": "secret",
        "R2_BUCKET": "objects",
        # Set, as on a deploy that also builds the grid layers: the parcels must not go there.
        "R2_TILES_BUCKET": "tiles",
        "PYTHON": sys.executable,
        "PARCELS_CATALOGUE_DIR": str(FIX),
        "PARCELS_ZIP_DIR": str(zips),
        "COUNTIES": "48351,48371,48241",
        "BATCHES": "2",
        "DOWNLOAD_GAP_S": "0",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(report),
    }

    def run(*args: str, extra: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        assert BASH is not None
        return subprocess.run(
            [BASH, str(SCRIPT), *args],
            env={**env, **(extra or {})},
            capture_output=True,
            text=True,
            check=False,
            timeout=600,
            cwd=ROOT,
        )

    planned = run("plan")
    assert planned.returncode == 0, planned.stderr
    outputs = dict(line.split("=", 1) for line in output.read_text().splitlines())
    version = outputs["version"]
    assert version.startswith("v1/20261001-")
    batches = json.loads(outputs["batches"])
    assert sorted(",".join(batches).split(",")) == ["48241", "48351", "48371"] and len(batches) == 2
    for batch in batches:
        done = run("batch", batch, extra={"PARCELS_VERSION": version})
        assert done.returncode == 0, done.stderr
    published = run("publish", extra={"PARCELS_VERSION": version})
    # Jasper (48241) had no zip: it fails, the others are promoted, and the run ends red with the table.
    assert published.returncode == 1, published.stderr
    assert "promoted" in published.stderr
    summary = report.read_text()
    assert "| Jasper | 48241 |" in summary and "| Newton | 48351 | 16 | 12 |" in summary
    bucket = root / "bucket" / "objects" / "data" / "normalized" / "context" / "us.tx.txgio.parcels"
    assert not list((root / "bucket" / "tiles").rglob("*")), "nothing may reach the public tiles bucket"
    assert sorted(p.name for p in (root / "bucket" / "objects").iterdir()) == ["data"]
    latest = json.loads((bucket / "latest.json").read_text())
    assert latest["version"] == version and sorted(latest["counties"]) == ["48351", "48371"]
    assert [f["fips"] for f in latest["failures"]] == ["48241"]
    assert (bucket / version / "README.txt").read_text().startswith("Texas land parcels")
    # Nothing an owner wrote reaches the bucket.
    owners = {"SYNTH OWNER", "SYNTH SITUS", "SYNTH MAIL", "SYNTH CARE"}
    for path in bucket.rglob("*"):
        if path.suffix == ".json" or path.suffix == ".txt":
            assert not any(o in path.read_text().upper() for o in owners), path
    # A server pulls one county with its object-store credentials and reads it.
    dest = tmp_path / "server"
    client = _BucketClient(root / "bucket")
    pulled = tp.pull(client=client, bucket="objects", counties={"48351"}, out_dir=dest)
    assert pulled["status"] == "pulled" and pulled["counties"] == 1 and client.buckets == {"objects"}
    from services.context_sample.parcels import ParcelLayer

    layer = ParcelLayer(dest)
    assert set(layer.counties) == {"48351"} and layer.counties["48351"].parcels == 16
    table = layer.read_bbox((-98.1, 30.9, -97.9, 31.1))
    assert table is not None and table.num_rows == 16


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_the_build_refuses_the_public_tiles_bucket(tmp_path: pathlib.Path) -> None:
    """A deploy that set R2_BUCKET to the tiles bucket by mistake writes nothing: the per-parcel table of a
    derived-only source is never mirrored where anyone can download it."""
    assert BASH is not None
    root = tmp_path / "s3"
    (root / "bucket" / "tiles").mkdir(parents=True)
    bindir = tmp_path / "bin"
    bindir.mkdir()
    aws = bindir / "aws"
    aws.write_text(FAKE_AWS.format(python=sys.executable, root=str(root)))
    aws.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bindir}{os.pathsep}{os.environ['PATH']}",
        "R2_ACCOUNT_ID": "acct",
        "R2_ACCESS_KEY_ID": "key",
        "R2_SECRET_ACCESS_KEY": "secret",
        "R2_BUCKET": "tiles",
        "R2_TILES_BUCKET": "tiles",
        "PYTHON": sys.executable,
        "PARCELS_CATALOGUE_DIR": str(FIX),
        "COUNTIES": "48351",
        "GITHUB_OUTPUT": str(tmp_path / "gh_output"),
    }
    for args in (["plan"], ["publish"]):
        done = subprocess.run(
            [BASH, str(SCRIPT), *args],
            env={**env, "PARCELS_VERSION": "v1/20261001-20261023T0900Z"},
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
            cwd=ROOT,
        )
        assert done.returncode == 2 and "tiles bucket" in done.stderr, done.stderr
    assert not list((root / "bucket").rglob("*.json")) and not (root / "calls.log").exists()
