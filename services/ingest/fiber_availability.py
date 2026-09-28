"""Loader seam for county fiber availability (`pipeline/context/fcc_bdc.py`, docs/25 §4).

The metric rows have no table yet: the coordinator adds it (or region attributes) in the
migration after 0026. Until then this module does everything a load needs short of that write,
so the migration step is one insert loop over :attr:`CountyFiberBatch.rows`:

- :func:`read_county_fiber` reads the parquet and refuses it unless every row carries the
  provenance quartet (`source_id`, `source_url`, `retrieved_at`, `licence`), the manifest entry is
  publishable (the same gate `services/ingest/loader.py` applies: `reuse` not gated, `publication`
  not `none`), the county key is a unique five-digit FIPS, every share lies in [0, 1], and the file
  names exactly one BDC filing. The filing becomes a :class:`~services.ingest.vintage.Vintage`
  with basis `artefact_filename` -- the FCC's file name states it -- never the fetch date.
- :func:`load_county_fiber` mirrors the manifest entry into `licence` + `source` (existing
  tables), records the vintage on `source`, and returns the batch.

Nothing here writes per-county rows, so running it before the table exists is harmless.
"""

from __future__ import annotations

import datetime as dt
import pathlib
import re
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from pipeline.connectors.registry import GATED_REUSE, Registry
from services.ingest.loader import GateRefused, set_source_vintage, upsert_licence_and_source
from services.ingest.vintage import ARTEFACT_FILENAME, Vintage, label_for

SOURCE_ID = "us.fcc.bdc.fixed_summary"

PROVENANCE = ("source_id", "source_url", "retrieved_at", "licence")
SHARE_COLUMNS = (
    "fiber_share",
    "fiber_share_business",
    "fiber_gigabit_share",
    "served_100_20_share",
    "rural_fiber_share",
)
#: What a stored/served row carries (docs/25 §4 serving proposal). `raw` stays in the parquet.
ROW_FIELDS = (
    "county_fips",
    "county_name",
    "state_code",
    "bsl_units",
    *SHARE_COLUMNS,
    "rural_bsl_units",
    "vintage",
    "as_of_date",
    "file_name",
    "fcc_area_url",
    *PROVENANCE,
    "licence_id",
)


class FiberBatchInvalid(ValueError):
    """The parquet does not satisfy the load contract (module docstring)."""


@dataclass
class CountyFiberBatch:
    source_id: str
    vintage: Vintage
    rows: list[dict[str, Any]] = field(default_factory=list)

    @property
    def by_fips(self) -> dict[str, dict[str, Any]]:
        return {r["county_fips"]: r for r in self.rows}


def _clean(value: Any) -> Any:
    if value is None:
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if hasattr(value, "item"):  # numpy scalar -> python
        return value.item()
    return value


def read_county_fiber(path: pathlib.Path, registry: Registry | None = None) -> CountyFiberBatch:
    df = pd.read_parquet(path)
    entry = (registry or Registry()).get(SOURCE_ID)
    if entry.reuse in GATED_REUSE:
        raise GateRefused(f"{SOURCE_ID}: reuse={entry.reuse!r} is gated; nothing loads")
    if entry.publication == "none":
        raise GateRefused(f"{SOURCE_ID}: publication=none publishes nothing (docs/21 §8)")

    missing = [c for c in (*ROW_FIELDS, "vintage") if c not in df.columns]
    if missing:
        raise FiberBatchInvalid(f"{path}: missing columns {missing}")
    for col in PROVENANCE:
        blank = df[col].isna() | (df[col].astype("string").str.strip() == "")
        if blank.any():
            raise FiberBatchInvalid(f"{path}: {int(blank.sum())} rows without {col}")
    ids = set(df["source_id"])
    if ids != {SOURCE_ID}:
        raise FiberBatchInvalid(f"{path}: source_id {sorted(ids)} is not {SOURCE_ID}")
    fips = df["county_fips"].astype("string")
    if not fips.str.fullmatch(r"\d{5}").all():
        raise FiberBatchInvalid(f"{path}: county_fips not five digits")
    if fips.duplicated().any():
        raise FiberBatchInvalid(f"{path}: duplicate county_fips {sorted(set(fips[fips.duplicated()]))[:5]}")
    for col in SHARE_COLUMNS:
        values = pd.to_numeric(df[col], errors="coerce").dropna()
        if ((values < 0) | (values > 1)).any():
            raise FiberBatchInvalid(f"{path}: {col} outside [0, 1]")
    vintages = set(df["vintage"].dropna())
    if len(vintages) != 1:
        raise FiberBatchInvalid(f"{path}: expected one BDC filing, found {sorted(vintages)}")
    value = str(vintages.pop())
    if not re.fullmatch(r"\d{4}-(06|12)", value):
        raise FiberBatchInvalid(f"{path}: vintage {value!r} is not a BDC filing (June or December)")
    for ts in df["retrieved_at"].unique():
        dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))  # raises on a malformed stamp

    rows = [{k: _clean(r[k]) for k in ROW_FIELDS} for r in df.to_dict("records")]
    return CountyFiberBatch(
        source_id=SOURCE_ID,
        vintage=Vintage(value=value, basis=ARTEFACT_FILENAME, label=label_for(value)),
        rows=rows,
    )


def load_county_fiber(
    session: Session, path: pathlib.Path, *, registry: Registry | None = None, manifest_version: str = ""
) -> CountyFiberBatch:
    """Register the source and its filing; return the validated rows for the table that the
    migration after 0026 adds (the per-county insert is the coordinator's one-line addition)."""
    reg = registry or Registry()
    batch = read_county_fiber(path, reg)
    source = upsert_licence_and_source(session, reg.get(SOURCE_ID), manifest_version or reg.version)
    set_source_vintage(source, batch.vintage)
    session.flush()
    return batch
