"""Build every prospect layer from the recorded fixtures (no network), for tests.

`pipeline/context/prospect/fixtures/` is laid out as `pipeline.context.prospect.build.SNAPSHOT_LAYOUT`
expects, so the real build runs over it unchanged; only the inputs are small. The wind input there is
SYNTHETIC (a GDAL-written grid on the Global Wind Atlas lattice): no Global Wind Atlas file was downloaded.
"""

from __future__ import annotations

import pathlib
from typing import Any

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "pipeline" / "context" / "prospect" / "fixtures"


def build_fixture_layers(out_dir: pathlib.Path) -> dict[str, Any]:
    from pipeline.context.prospect.build import SOURCES, run

    return run(region="texas", sources=list(SOURCES), out_dir=out_dir, snapshots=FIXTURES, store=None)
