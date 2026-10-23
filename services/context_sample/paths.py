"""Where the prospect layer files live, for the build and the sampler alike.

`PROSPECT_DATA_DIR` when set (a container volume, or a test's tmp dir), else
`<data root>/normalized/prospect`,
the data root being the connectors' (`INFRAQUE_DATA_DIR`, else this checkout's `data/`; `data/normalized` is
gitignored, so a local build never lands in a commit). The build writes
`layers.json` and one file per layer there; production fills it from R2 with
`python -m services.context_sample.sync` (docs/25 "Prospecting layers").
"""

from __future__ import annotations

import os
import pathlib

MANIFEST_NAME = "layers.json"


def prospect_dir() -> pathlib.Path:
    env = os.environ.get("PROSPECT_DATA_DIR")
    if env:
        return pathlib.Path(env)
    from pipeline.connectors.store import DATA_DIR

    return DATA_DIR / "normalized" / "prospect"


def manifest_path(directory: pathlib.Path | None = None) -> pathlib.Path:
    return (directory or prospect_dir()) / MANIFEST_NAME
