"""infra/scripts/build_grid_layers.sh against PATH shims (no network, no R2, no pyosmium): every
build is uploaded under its own version key; `grid/osm-power/latest.json` moves only for a build of
every state that passes the promotion gate (`pipeline.context.osm_power check`). Lane grid-osm,
2026-10-22. Checks use `expect` instead of bare `assert` (ruff S101, see infra/test_scripts.py)."""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "infra" / "scripts" / "build_grid_layers.sh"
BASH = shutil.which("bash") or "/bin/bash"
VERSION = "v1/20261021T202102Z-20261102T064100Z"

#: Records every call; serves the published `latest.json` when the test provides one.
AWS_SHIM = r"""#!/usr/bin/env bash
printf 'aws %s\n' "$*" >> "$FAKE_LOG"
if [[ "$1 $2" == "s3 cp" && "$3" == s3://*/latest.json ]]; then
  [[ -n "${FAKE_PREVIOUS:-}" ]] || exit 1
  cp "$FAKE_PREVIOUS" "$4"
fi
exit 0
"""
#: `python3` as the script calls it: pip and the pyosmium version are answered here, the build writes
#: the test's prepared files, and everything else (the manifest summary, `check`) runs for real.
PYTHON_SHIM = r"""#!/usr/bin/env bash
if [[ "$1 $2" == "-m pip" ]]; then exit 0; fi
if [[ "$1" == "-c" && "$2" == *osmium* ]]; then echo 4.3.2; exit 0; fi
if [[ "$1 $2 $3" == "-m pipeline.context.osm_power build" ]]; then
  printf 'build %s\n' "$*" >> "$FAKE_LOG"
  shift 3
  while [[ $# -gt 0 ]]; do
    if [[ "$1" == "--out-dir" ]]; then mkdir -p "$2"; cp "$FAKE_BUILD"/* "$2"/; fi
    shift
  done
  echo '{}'
  exit 0
fi
exec "$REAL_PYTHON" "$@"
"""


def expect(condition: bool, message: str = "") -> None:
    if not condition:
        raise AssertionError(message)


def _manifest(regions: dict[str, tuple[int, int]]) -> dict[str, object]:
    return {
        "version": VERSION,
        "regions": [
            {"region": r, "lines_kept": lines, "substations_kept": subs, "timestamp": "2026-10-21T20:21:02Z"}
            for r, (lines, subs) in regions.items()
        ],
        "files": {"lines.parquet": {"rows": 1}, "substations.parquet": {"rows": 1}},
    }


def _run(
    tmp_path: pathlib.Path, *, states: str = "all", previous: dict[str, object] | None = None
) -> tuple[subprocess.CompletedProcess[str], list[str]]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name, body in (("aws", AWS_SHIM), ("python-shim", PYTHON_SHIM)):
        path = bin_dir / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
    build = tmp_path / "build"
    build.mkdir()
    (build / "manifest.json").write_text(json.dumps(_manifest({"US-OK": (900, 400), "US-TX": (5000, 2000)})))
    (build / "lines.parquet").write_bytes(b"PAR1")
    (build / "substations.parquet").write_bytes(b"PAR1")
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ.get('PATH', '')}",
        "PYTHON": str(bin_dir / "python-shim"),
        "REAL_PYTHON": sys.executable,
        "FAKE_LOG": str(tmp_path / "calls.log"),
        "FAKE_BUILD": str(build),
        "GRID_STATES": states,
        "R2_ACCOUNT_ID": "fake-account",
        "R2_ACCESS_KEY_ID": "fake-key-id",
        "R2_SECRET_ACCESS_KEY": "fake-secret",
        "R2_TILES_BUCKET": "fake-tiles",
    }
    if previous is not None:
        prev = tmp_path / "previous.json"
        prev.write_text(json.dumps(previous))
        env["FAKE_PREVIOUS"] = str(prev)
    result = subprocess.run(  # noqa: S603 -- fixed script path under the repo, no shell
        [BASH, str(SCRIPT)], capture_output=True, text=True, cwd=ROOT, env=env, check=False
    )
    calls = (tmp_path / "calls.log").read_text().splitlines() if (tmp_path / "calls.log").exists() else []
    return result, calls


def _upload_targets(calls: list[str]) -> list[str]:
    """The `s3://` destination of every local-to-bucket `aws s3 cp`, in call order."""
    out = []
    for call in calls:
        words = call.split()
        if words[:3] == ["aws", "s3", "cp"] and not words[3].startswith("s3://"):
            out.append(words[4])
    return out


def test_a_first_full_build_uploads_its_version_and_promotes_it(tmp_path: pathlib.Path) -> None:
    result, calls = _run(tmp_path)
    expect(result.returncode == 0, result.stderr)
    targets = _upload_targets(calls)
    prefix = f"s3://fake-tiles/grid/osm-power/{VERSION}/"
    expect(
        targets
        == [
            f"{prefix}lines.parquet",
            f"{prefix}substations.parquet",
            f"{prefix}manifest.json",
            f"{prefix}README.txt",
            "s3://fake-tiles/grid/osm-power/latest.json",
        ],
        str(targets),
    )
    expect(
        any(c.startswith("build -m pipeline.context.osm_power build --states all") for c in calls), str(calls)
    )
    expect("promoted" in result.stdout, result.stdout)


def test_a_subset_build_is_uploaded_but_never_promoted(tmp_path: pathlib.Path) -> None:
    result, calls = _run(tmp_path, states="texas")
    expect(result.returncode == 0, result.stderr)
    expect(not any("latest.json" in c for c in calls), str(calls))
    expect(any(f"grid/osm-power/{VERSION}/lines.parquet" in c for c in calls), str(calls))


def test_a_build_that_loses_a_state_or_a_tenth_of_its_lines_is_not_promoted(tmp_path: pathlib.Path) -> None:
    previous = _manifest({"US-LA": (800, 300), "US-OK": (900, 400), "US-TX": (6000, 2000)})
    result, calls = _run(tmp_path, previous=previous)
    expect(result.returncode != 0, "the gate should fail the job")
    expect("US-LA: in the published build, missing from this one" in result.stdout, result.stdout)
    expect("US-TX: lines_kept fell from 6000 to 5000" in result.stdout, result.stdout)
    promoted = [c for c in calls if c.startswith("aws s3 cp") and c.split()[4].endswith("/latest.json")]
    expect(promoted == [], str(promoted))


def test_a_steady_build_replaces_the_published_one(tmp_path: pathlib.Path) -> None:
    previous = _manifest({"US-OK": (905, 401), "US-TX": (5010, 1990)})
    result, calls = _run(tmp_path, previous=previous)
    expect(result.returncode == 0, result.stderr + result.stdout)
    expect(any(c.split()[4:5] == ["s3://fake-tiles/grid/osm-power/latest.json"] for c in calls), str(calls))
