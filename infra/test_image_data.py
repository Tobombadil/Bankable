"""The images carry every piece of reference data the code reads from the app tree at runtime.

`infra/docker/Dockerfile` (api, web, worker) and `Dockerfile.browser-worker` copy code directories
whole but `data/` piecemeal, because most of `data/` is connector output that lives on the
`connector_data` volume instead. Until 2026-10-06 they copied only `data/sources.yaml`, so
`/methodology`, `/v1/lifecycle-states`, the region lookups, the organisation tree, the match rules
and the ICIS-Air suppression list all raised FileNotFoundError inside the image while every test
passed on a checkout. This test reads the code for `ROOT / "data" / ...` chains, keeps the ones
that name committed reference data, and fails when a Dockerfile does not copy them.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCKERFILES = (REPO / "infra/docker/Dockerfile", REPO / "infra/docker/Dockerfile.browser-worker")
RUNTIME_PACKAGES = ("pipeline", "services", "infra", "web")

#: Paths under data/ that are volume-backed, written at runtime, or read only by offline scripts
#: (evaluation and backfill tools that never run in a container).
NOT_IN_IMAGE = (
    "data/normalized",
    "data/snapshots",
    "data/runs",
    "data/quarantine",
    "data/exports",
    "data/documents",
    "data/probes",
    "data/eval/raw",
    "data/eval/normalized.parquet",
)
#: A bare `ROOT / "data" / "eval"` is an offline script's output directory, not something to ship.
BARE_DIRS_NOT_SHIPPED = ("data/eval",)

_CHAIN = re.compile(r'"data"((?:\s*/\s*"[A-Za-z0-9_.\-]+")+)')
_SEGMENT = re.compile(r'"([A-Za-z0-9_.\-]+)"')


def _in_checkout(path: str) -> bool:
    """Reference data is committed; the volume-backed paths are excluded above before this runs."""
    return (REPO / path).exists()


def _code_data_paths() -> set[str]:
    found: set[str] = set()
    for package in RUNTIME_PACKAGES:
        for py in (REPO / package).rglob("*.py"):
            if py.name.startswith("test_") or "/tests/" in py.as_posix():
                continue
            for match in _CHAIN.finditer(py.read_text(encoding="utf-8")):
                found.add("/".join(["data", *_SEGMENT.findall(match.group(1))]))
    return found


def _required_paths() -> set[str]:
    required = set()
    for path in _code_data_paths():
        if path in BARE_DIRS_NOT_SHIPPED or any(
            path == skip or path.startswith(skip + "/") for skip in NOT_IN_IMAGE
        ):
            continue
        if _in_checkout(path):
            required.add(path)
    return required


def _copied_sources(dockerfile: Path) -> list[str]:
    sources: list[str] = []
    for line in dockerfile.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[0] == "COPY" and not parts[1].startswith("--"):
            sources.extend(p.rstrip("/") for p in parts[1:-1])
    return sources


def test_the_scan_finds_the_known_runtime_reads() -> None:
    required = _required_paths()
    for path in (
        "data/sources.yaml",
        "data/vocabulary/lifecycle_states.yaml",
        "data/vendored/regions",
        "data/match_rules.yaml",
    ):
        assert path in required, f"scan no longer sees {path}; the regex or the code moved"


@pytest.mark.parametrize("dockerfile", DOCKERFILES, ids=lambda p: p.name)
def test_every_runtime_data_path_is_copied_into_the_image(dockerfile: Path) -> None:
    copied = _copied_sources(dockerfile)
    missing = sorted(
        path
        for path in _required_paths()
        if not any(path == src or path.startswith(src + "/") for src in copied)
    )
    assert not missing, f"{dockerfile.name} does not copy {missing}"
