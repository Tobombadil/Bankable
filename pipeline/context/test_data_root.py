"""Every context builder writes under the connector data root (`INFRAQUE_DATA_DIR`), not under the
checkout: in the worker image /app is read-only and the data root is the `connector_data` volume
(docs/64 §7). Checked in a fresh interpreter, because the paths are module constants read once at
import, as `pipeline.connectors.store.DATA_DIR` is."""

from __future__ import annotations

import json
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

PROBE = """
import json
from pipeline.context import (
    agstar, eia860_plants, eia923, eia_atlas, eia_owners, eia_plants, ethanol_capacity,
    ethanol_plants, fuels, ghgrp, gleif, lbnl_transmission, lmop, phmsa, rfs,
)
from services.ingest import organizations
print(json.dumps({
    "fuels.CONTEXT_DIR": str(fuels.CONTEXT_DIR),
    "eia_atlas.NORMALIZED_DIR": str(eia_atlas.NORMALIZED_DIR),
    "lbnl_transmission.NORMALIZED_DIR": str(lbnl_transmission.NORMALIZED_DIR),
    "gleif.CONTEXT_DIR": str(gleif.CONTEXT_DIR),
    "eia_owners.SNAPSHOT_DIR": str(eia_owners.SNAPSHOT_DIR),
    "eia_owners.RUNS_DIR": str(eia_owners.RUNS_DIR),
    "eia_owners.DEFAULT_OUT": str(eia_owners.DEFAULT_OUT),
    "eia_plants.SNAPSHOT_DIR": str(eia_plants.SNAPSHOT_DIR),
    "eia_plants.DEFAULT_OUT": str(eia_plants.DEFAULT_OUT),
    "eia860_plants.SNAPSHOT_DIR": str(eia860_plants.SNAPSHOT_DIR),
    "eia860_plants.DEFAULT_OUT": str(eia860_plants.DEFAULT_OUT),
    "agstar.DEFAULT_OUT": str(agstar.DEFAULT_OUT),
    "lmop.DEFAULT_OUT": str(lmop.DEFAULT_OUT),
    "ghgrp.DEFAULT_OUT": str(ghgrp.DEFAULT_OUT),
    "ethanol_plants.DEFAULT_OUT": str(ethanol_plants.DEFAULT_OUT),
    "ethanol_capacity.DEFAULT_OUT": str(ethanol_capacity.DEFAULT_OUT),
    "eia923.default_output": str(eia923.default_output()),
    "phmsa.default_output": str(phmsa.default_output()),
    "rfs.default_output": str(rfs.default_output()),
    "organizations.DEFAULT_GLEIF_PARQUET": str(organizations.DEFAULT_GLEIF_PARQUET),
    "eia_atlas.STATES_GEOJSON": str(eia_atlas.STATES_GEOJSON),
}))
"""


def test_builders_write_under_infraque_data_dir(tmp_path: pathlib.Path) -> None:
    env = {"INFRAQUE_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"}
    result = subprocess.run(  # noqa: S603 -- fixed argv, this interpreter
        [sys.executable, "-c", PROBE], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    )
    assert result.returncode == 0, result.stderr[-2000:]
    paths = json.loads(result.stdout.strip().splitlines()[-1])
    vendored = paths.pop("eia_atlas.STATES_GEOJSON")
    outside = {name: path for name, path in paths.items() if not path.startswith(str(tmp_path))}
    assert not outside, outside
    # Vendored inputs ship with the code (the image copies data/vendored), not with the data root.
    assert vendored == str(ROOT / "data" / "vendored" / "regions" / "us_states.geojson")
