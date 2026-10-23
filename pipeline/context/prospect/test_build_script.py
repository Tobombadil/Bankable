# ruff: noqa: S101, S603 - asserts are the point (docs/04 E-6); fixed argv, no shell
"""infra/scripts/build_prospect_layers.sh end to end, offline: a fake `aws` maps the bucket to a directory.

The wind source is the only one that builds offline (from a file in the bucket's inbox), which is the
manual route the Global Wind Atlas Terms require. The test then follows the pointer with
`services.context_sample.sync` and samples the result, so the build -> R2 -> server path is one test.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
from typing import Any

import pytest

from services.context_sample.sampler import Sampler
from services.context_sample.sync import sync

ROOT = pathlib.Path(__file__).resolve().parents[3]
SCRIPT = ROOT / "infra" / "scripts" / "build_prospect_layers.sh"
FIX = pathlib.Path(__file__).parent / "fixtures" / "global_wind_atlas"
BASH = shutil.which("bash")
AMARILLO = (-101.85, 35.22)

FAKE_AWS = """#!{python}
import pathlib, shutil, sys
root = pathlib.Path({root!r})
args = sys.argv[1:]
with (root / "calls.log").open("a") as log:
    log.write(" ".join(args) + "\\n")
assert args[:2] == ["s3", "cp"], args
skip = {{"--endpoint-url", "--content-type"}}
pos, i = [], 2
while i < len(args):
    if args[i] in skip:
        i += 2
        continue
    if not args[i].startswith("--"):
        pos.append(args[i])
    i += 1
src, dst = pos
recursive = "--recursive" in args
def local(p):
    return root / "bucket" / p[len("s3://"):] if p.startswith("s3://") else pathlib.Path(p)
s, d = local(src), local(dst)
if src.startswith("s3://") and "/prospect/current/" in src and (root / "pointer_error").exists():
    print((root / "pointer_error").read_text(), file=sys.stderr)
    sys.exit(1)
if recursive:
    if s.is_dir():
        shutil.copytree(s, d, dirs_exist_ok=True)
    sys.exit(0)
if not s.is_file():
    print(f"fatal error: (404) HeadObject: {{src}}", file=sys.stderr)
    sys.exit(1)
d.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(s, d)
"""


class DirS3:
    """The two calls sync makes, over the fake bucket directory."""

    def __init__(self, root: pathlib.Path) -> None:
        self.root = root

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3's names
        import io

        return {"Body": io.BytesIO((self.root / Bucket / Key).read_bytes())}

    def download_file(self, bucket: str, key: str, filename: str) -> None:
        shutil.copyfile(self.root / bucket / key, filename)


@pytest.fixture()
def fake_aws(tmp_path: pathlib.Path) -> tuple[pathlib.Path, dict[str, str]]:
    root = tmp_path / "s3"
    (root / "bucket").mkdir(parents=True)
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
        "R2_BUCKET": "private",
        "PROSPECT_REGION": "texas",
        "PROSPECT_SOURCES": "wind",
        "PYTHON": sys.executable,
    }
    return root, env


def _run(env: dict[str, str], build_id: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run(
        [BASH, str(SCRIPT)],
        env={**env, "PROSPECT_BUILD_ID": build_id},
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
        cwd=ROOT,
    )


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_build_publishes_a_dated_build_then_the_pointer_and_a_server_follows_it(
    fake_aws: tuple[pathlib.Path, dict[str, str]], tmp_path: pathlib.Path
) -> None:
    root, env = fake_aws
    bucket = root / "bucket" / "private"
    inbox = bucket / "prospect" / "inbox" / "global_wind_atlas"
    inbox.mkdir(parents=True)
    shutil.copyfile(FIX / "gwa_format_synthetic_amarillo.tif", inbox / "USA_wind-speed_100m.tif")
    (inbox / "USA_wind-speed_100m.json").write_text(json.dumps({"downloaded_at": "2026-10-20T09:00:00Z"}))

    first = _run(env, "20261103T064100Z")
    assert first.returncode == 0, first.stdout + first.stderr
    build = bucket / "prospect" / "builds" / "texas" / "20261103T064100Z"
    manifest = json.loads((build / "layers.json").read_text())
    assert sorted(manifest["layers"]) == ["wind_speed_100m"]
    assert manifest["layers"]["wind_speed_100m"]["provenance"]["retrieved_at"] == "2026-10-20T09:00:00Z"
    assert (build / "wind_speed_100m.tif").is_file()
    pointer = json.loads((bucket / "prospect" / "current" / "texas.json").read_text())
    assert pointer["build_id"] == "20261103T064100Z" and pointer["layers"] == ["wind_speed_100m"]
    # calls.log holds each `aws` argv: "s3 cp SRC DST ..."; an upload is a call whose DST is in the bucket.
    uploads = [
        c.split()[3]
        for c in (root / "calls.log").read_text().splitlines()
        if c.split()[3].startswith("s3://")
    ]
    pointer_put = uploads.index("s3://private/prospect/current/texas.json")
    build_puts = [i for i, dst in enumerate(uploads) if "/prospect/builds/texas/20261103T064100Z/" in dst]
    calls = (root / "calls.log").read_text().splitlines()
    assert build_puts and max(build_puts) < pointer_put, "the pointer moves only after the build is uploaded"
    assert list((bucket / "data" / "runs").rglob("*.json")), "run records copied to the bucket"
    assert not any("tiles" in c for c in calls), "never the public tiles bucket"

    # A second run without a new wind file keeps the served layer (seeded) and publishes again.
    shutil.rmtree(inbox)
    second = _run(env, "20261203T064100Z")
    assert second.returncode == 0, second.stdout + second.stderr
    assert "no Global Wind Atlas file" in second.stdout
    again = json.loads(
        (bucket / "prospect" / "builds" / "texas" / "20261203T064100Z" / "layers.json").read_text()
    )
    assert again["layers"]["wind_speed_100m"]["sha256"] == manifest["layers"]["wind_speed_100m"]["sha256"]

    dest = tmp_path / "server"
    assert sync(dest, region="texas", client=DirS3(root / "bucket"), bucket="private")["status"] == "updated"
    wind = Sampler(dest).sample(*AMARILLO, ["wind_speed_100m"])["layers"][0]
    assert wind["status"] == "ok" and wind["value"] == pytest.approx(6.4)


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_pointer_that_cannot_be_read_stops_the_run_and_keeps_the_served_build(
    fake_aws: tuple[pathlib.Path, dict[str, str]],
) -> None:
    """Only a 404 means "nothing served". A 403 or a transient error reading the pointer was taken for
    "no served build", which set the served layer count to 0 and let a smaller build replace it."""
    root, env = fake_aws
    bucket = root / "bucket" / "private"
    inbox = bucket / "prospect" / "inbox" / "global_wind_atlas"
    inbox.mkdir(parents=True)
    shutil.copyfile(FIX / "gwa_format_synthetic_amarillo.tif", inbox / "USA_wind-speed_100m.tif")
    first = _run(env, "20261103T064100Z")
    assert first.returncode == 0, first.stdout + first.stderr
    pointer = bucket / "prospect" / "current" / "texas.json"
    served = pointer.read_text()
    (root / "pointer_error").write_text(
        "fatal error: An error occurred (403) when calling the HeadObject operation: Forbidden"
    )
    shutil.copyfile(FIX / "gwa_format_synthetic_amarillo.tif", inbox / "USA_wind-speed_100m.tif")
    result = _run(env, "20261203T064100Z")
    assert result.returncode != 0
    assert "cannot read prospect/current/texas.json" in result.stderr and "(403)" in result.stderr
    assert pointer.read_text() == served
    assert not (bucket / "prospect" / "builds" / "texas" / "20261203T064100Z").exists()


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_a_pad_us_file_in_the_inbox_is_built_with_its_download_date(
    fake_aws: tuple[pathlib.Path, dict[str, str]],
) -> None:
    """PAD-US has no permitted automated route today (ScienceBase's catalogue route answers 404, its file
    manager is a JavaScript application; 2026-10-23), so a person's download in the inbox is built, and its
    recorded download date becomes `retrieved_at`."""
    root, env = fake_aws
    bucket = root / "bucket" / "private"
    inbox = bucket / "prospect" / "inbox" / "padus"
    inbox.mkdir(parents=True)
    shutil.copyfile(FIX.parent / "padus" / "padus_tx_sample.geojson", inbox / "PADUS4_1_State_TX.geojson")
    (inbox / "PADUS4_1_State_TX.json").write_text(json.dumps({"downloaded_at": "2026-10-23T08:00:00Z"}))
    result = _run({**env, "PROSPECT_SOURCES": "protected"}, "20261103T064100Z")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "using the padus file(s)" in result.stdout
    manifest = json.loads(
        (bucket / "prospect" / "builds" / "texas" / "20261103T064100Z" / "layers.json").read_text()
    )
    entry = manifest["layers"]["protected_lands"]
    assert entry["provenance"]["retrieved_at"] == "2026-10-23T08:00:00Z"
    assert entry["coverage"]["states"] == ["Texas"]


#: Requires-Python of each build-only pin in prospect-layers.yml, from PyPI's JSON API (read 2026-10-23;
#: https://pypi.org/pypi/pyproj/3.8.0/json says ">=3.12" and lists cp312 and later wheels only). A new pin
#: is recorded here first, so a pin with no wheel for the workflow's Python fails here, not in Actions.
REQUIRES_PYTHON = {
    ("pyogrio", "0.13.0"): (3, 10),
    ("pyproj", "3.7.2"): (3, 11),
    ("pyproj", "3.8.0"): (3, 12),
}


def test_the_workflow_pins_install_on_the_workflow_python() -> None:
    import re

    import yaml

    workflow = yaml.safe_load((ROOT / ".github" / "workflows" / "prospect-layers.yml").read_text())
    steps = workflow["jobs"]["build-prospect-layers"]["steps"]
    setup = next(s for s in steps if str(s.get("uses", "")).startswith("actions/setup-python"))
    python = tuple(int(p) for p in str(setup["with"]["python-version"]).split("."))
    install = next(s for s in steps if s.get("name") == "install the build dependencies")
    pins = re.findall(r'"(pyogrio|pyproj)==([0-9.]+)"', install["run"])
    assert {name for name, _ in pins} == {"pyogrio", "pyproj"}
    for pin in pins:
        assert pin in REQUIRES_PYTHON, f"record the Requires-Python of {pin} from PyPI first"
        assert python >= REQUIRES_PYTHON[pin], f"{pin} has no wheel for Python {python}"


@pytest.mark.skipif(BASH is None, reason="bash not available")
def test_nothing_is_published_when_the_build_has_no_layers(
    fake_aws: tuple[pathlib.Path, dict[str, str]],
) -> None:
    root, env = fake_aws
    result = _run(env, "20261103T064100Z")  # no wind file, nothing served: zero layers
    assert result.returncode != 0
    assert "refusing to publish 0 layers" in result.stderr
    assert not (root / "bucket" / "private" / "prospect" / "current").exists()
