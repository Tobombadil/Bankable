"""Loader for registry-sourced infrastructure assets (docs/21 §3.22, ADR 0008) -> `asset`.

Generalises `services/ingest/plants.py`'s original `built_plant` loader (still the module for
`power_plant`, docs/21 §3.20) to every asset type in `services.db.models.ASSET_TYPES`. Same
provenance-quartet and licence-gate discipline as `services/ingest/loader.py`; `asset` carries no
lifecycle, no `proposal_source`/`event` rows and no entity resolution -- one row per `(source_id,
source_asset_id)`, upserted in place on every run (docs/21 §3.22 "Unique: (source_id,
source_asset_id)").

Idempotency choice (same as the original plants loader, ADR 0008 does not change it): a re-run
always overwrites every mapped field on an existing row, even when nothing changed, and reports it
as `updated`, not skipped -- one straightforward pass rather than a field-by-field diff that
`asset` has no event log to record the outcome of anyway. `public_id`/`slug` are assigned once, on
insert, and never recomputed on an update (docs/21 §3.22's identity is stable once minted, matching
every other public-id'd entity in this codebase).

One registry source per asset type at this sprint (ADR 0008 consequence: "New point layers are one
parser each" -- EIA-860M's "Operating" sheet remains the first, for `power_plant`).
`ASSET_TYPE_SOURCE_IDS` is intentionally small; loading any other `asset_type` raises
`UnsupportedAssetTypeError` until that type's connector and manifest entry exist, rather than
silently guessing a source.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import time
from dataclasses import asdict, dataclass
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from services.db.models import ASSET_TYPES, Asset, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id as make_public_id
from services.ids import slugify
from services.ingest.loader import set_source_vintage, upsert_licence_and_source
from services.ingest.vintage import NOT_STATED_VINTAGE, Vintage, from_attributes

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")

#: `asset_type` -> the `data/sources.yaml` ids its loader may read from, for the sources wired up
#: so far. Extending this to a new asset type is "one parser each" (ADR 0008 consequences) -- add
#: the connector, the manifest row and one entry here; never adds a case to the loader body itself.
#: A type may have several registries (fuels lane, 2026-09-19: RNG from EPA LMOP *and* AgSTAR,
#: ethanol from the EIA Atlas points *and* the EIA capacity table); `(source_id, source_asset_id)`
#: keeps their rows apart. The frame's own `source_id` column picks the source (`resolve_source_id`),
#: the first id listed is the default for a frame that has none.
ASSET_TYPE_SOURCE_IDS: dict[str, tuple[str, ...]] = {
    "power_plant": ("us.eia.860m",),
    # EIA Atlas natural gas layers, `pipeline/context/eia_atlas.py` (midstream lane, 2026-09-19).
    "gas_pipeline": ("us.eia.atlas.gas_pipelines",),
    "gas_processing_plant": ("us.eia.atlas.gas_processing_plants",),
    "gas_storage": ("us.eia.atlas.gas_storage",),
    "lng_terminal": ("us.eia.atlas.lng_terminals",),
    # Fuels lane, 2026-09-19 (`pipeline/context/lmop.py`, `agstar.py`, `ethanol_*.py`).
    "rng_project": ("us.epa.lmop", "us.epa.agstar"),
    "ethanol_plant": ("us.eia.atlas.ethanol_plants", "us.eia.ethanol_capacity"),
}


class UnsupportedAssetTypeError(ValueError):
    """Raised for an `asset_type` with no wired source yet, or a `source_id` the type does not
    allow (see `ASSET_TYPE_SOURCE_IDS`)."""


def resolve_source_id(df: pd.DataFrame, asset_type: str, source_id: str | None = None) -> str:
    """The manifest id this frame loads under: the explicit `source_id` argument, else the frame's
    single `source_id` column value, else the type's first wired source. Anything outside
    `ASSET_TYPE_SOURCE_IDS[asset_type]` -- or a frame mixing two sources -- is refused rather than
    loaded under the wrong provenance."""
    if asset_type not in ASSET_TYPES:
        raise UnsupportedAssetTypeError(f"{asset_type!r} is not one of {ASSET_TYPES!r}")
    allowed = ASSET_TYPE_SOURCE_IDS.get(asset_type)
    if not allowed:
        raise UnsupportedAssetTypeError(
            f"no source wired for asset_type={asset_type!r} yet (ASSET_TYPE_SOURCE_IDS)"
        )
    if source_id is None and "source_id" in df.columns and len(df):
        found = sorted({str(v) for v in df["source_id"].dropna().unique()})
        if len(found) > 1:
            raise UnsupportedAssetTypeError(f"frame mixes source ids {found}; load one source at a time")
        if found:
            source_id = found[0]
    if source_id is None:
        return allowed[0]
    if source_id not in allowed:
        raise UnsupportedAssetTypeError(
            f"source_id={source_id!r} is not wired for asset_type={asset_type!r}; allowed: {allowed!r}"
        )
    return source_id


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class AssetsLoadResult:
    assets_seen: int = 0
    inserted: int = 0
    updated: int = 0
    placed: int = 0
    unplaced: int = 0


def _none_if_missing(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _to_str(value: object) -> str | None:
    value = _none_if_missing(value)
    return None if value is None else str(value)


def _to_float(value: object) -> float | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _to_int(value: object) -> int | None:
    f = _to_float(value)
    return None if f is None else int(f)


def _to_datetime(value: object) -> dt.datetime | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    ts = pd.Timestamp(value)
    if pd.isna(ts):
        return None
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    result: dt.datetime = ts.to_pydatetime()
    return result


def _to_json_dict(value: object) -> dict[str, float]:
    value = _none_if_missing(value)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else {}
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    # Defensive against a value missing for one raw label in one row of a batch written by a
    # parquet engine that infers a shared struct schema across rows with different key sets
    # (services/ingest/plants.py carries the same guard, for the same reason).
    return {str(k): float(v) for k, v in value.items() if v is not None and not pd.isna(v)}


def _to_attributes(value: object) -> dict[str, Any]:
    """`attributes` is the type's objective feature set (docs/21 §3.22) and holds strings, lists
    and nulls as well as numbers (`states_crossed`, `status_raw`, `field_type`, ...), so unlike
    `technologies` it is *not* coerced to floats -- only parsed from the JSON string
    `to_parquet_safe` wrote, with a non-dict payload becoming `{}`."""
    value = _none_if_missing(value)
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value) if value else {}
        except json.JSONDecodeError:
            return {}
    if not isinstance(value, dict):
        return {}
    return {str(k): (None if isinstance(v, float) and pd.isna(v) else v) for k, v in value.items()}


def _to_geom_line(value: object) -> str | None:
    """`geom_line_wkt` column -> the WKT string `services.db.types.GeographyLine` binds on both
    dialects (a `MULTILINESTRING(...)`; a bare `LINESTRING` is promoted by the type)."""
    text = _to_str(value)
    if text is None:
        return None
    text = text.strip()
    if not text or text.upper().endswith(" EMPTY"):
        return None
    return text


def _slug_for(name: str, state_code: str | None, used_slugs: set[str]) -> str:
    base = slugify(f"{name} {state_code}" if state_code else name)
    slug = base
    suffix = 2
    while slug in used_slugs:
        slug = f"{base}-{suffix}"
        suffix += 1
    used_slugs.add(slug)
    return slug


def _frame_vintage(df: pd.DataFrame) -> Vintage:
    """The release stated by an asset frame's `attributes.source_vintage`, or `not_stated`.

    Scans rather than taking row zero: a layer joined from two files can carry the token on only
    some rows, and the first row is not guaranteed to be one of them. Bounded to the first 50
    rows because the token is a property of the fetched artefact, not of the row."""
    if "attributes" not in df.columns:
        return NOT_STATED_VINTAGE
    for value in df["attributes"].head(50):
        vintage = from_attributes(_to_attributes(value))
        if vintage.stated:
            return vintage
    return NOT_STATED_VINTAGE


def load_assets(
    session: Session,
    df: pd.DataFrame,
    asset_type: str,
    *,
    source_id: str | None = None,
    manifest_version: str = "",
) -> AssetsLoadResult:
    """Upsert one asset-shaped frame by `(source_id, source_asset_id)`. Bulk-friendly: the
    registry/licence/source rows and every existing `asset` key for this `(source, asset_type)`
    are loaded once, up front -- no per-row `SELECT`.

    `df` columns mirror `pipeline.context.eia_plants.aggregate_plants`'s output for
    `asset_type="power_plant"` (`source_asset_id` in place of the plants-only
    `source_plant_id`): `source_asset_id`, `name`, `operator_name`, `technology`,
    `technology_raw`, `technologies`, `capacity_mw`, `capacity_value`, `capacity_unit`,
    `unit_count`, `commissioned_year`, `lon`/`lat`, `state_code`, `county_name`, `county_fips`,
    `country`, `attributes`, `status`, `source_url`, `retrieved_at`, plus `geom_line_wkt` (a
    `MULTILINESTRING` WKT string for line assets, `pipeline.context.eia_atlas`) -> `geom_line`.
    Every column is optional except `source_asset_id` and `name`; a missing column is treated as
    absent for every row. `operator_name`/`owner_name` are stored as spelled and turned into
    `asset_owner` edges by `services/ingest/midstream.py`, not here.
    """
    registry = Registry()
    entry = registry.get(resolve_source_id(df, asset_type, source_id))
    source = upsert_licence_and_source(session, entry, manifest_version or registry.version)
    # The release the source states for this layer. The Energy Atlas puts it in the shapefile
    # member names, which `pipeline/context/eia_atlas.py` already extracts onto every row's
    # `attributes.source_vintage`; a layer whose rows carry no token resolves to `not_stated`
    # rather than borrowing `retrieved_at` (`services/ingest/vintage.py`).
    set_source_vintage(source, _frame_vintage(df))

    result = AssetsLoadResult(assets_seen=len(df))
    if not len(df):
        session.commit()
        return result

    existing: dict[str, Asset] = {
        a.source_asset_id: a
        for a in session.scalars(
            select(Asset).where(Asset.source_id == source.id, Asset.asset_type == asset_type)
        )
    }
    used_slugs: set[str] = set(session.scalars(select(Asset.slug)))

    now = utcnow()
    for row in df.to_dict("records"):
        source_asset_id = str(row.get("source_asset_id"))
        lon = _to_float(row.get("lon"))
        lat = _to_float(row.get("lat"))
        geom = (lon, lat) if lon is not None and lat is not None else None
        retrieved_at = _to_datetime(row.get("retrieved_at")) or now
        name = _to_str(row.get("name")) or f"{asset_type} {source_asset_id}"
        state_code = _to_str(row.get("state_code"))

        fields: dict[str, object] = {
            "name": name,
            "operator_name": _to_str(row.get("operator_name")),
            "status": _to_str(row.get("status")) or "operating",
            "technology": _to_str(row.get("technology")),
            "technology_raw": _to_str(row.get("technology_raw")),
            "technologies": _to_json_dict(row.get("technologies")),
            "capacity_mw": _to_float(row.get("capacity_mw")),
            "capacity_value": _to_float(row.get("capacity_value")),
            "capacity_unit": _to_str(row.get("capacity_unit")),
            "commissioned_year": _to_int(row.get("commissioned_year")),
            "unit_count": _to_int(row.get("unit_count")),
            "geom": geom,
            "geom_line": _to_geom_line(row.get("geom_line_wkt")),
            "attributes": _to_attributes(row.get("attributes")),
            "state_code": state_code,
            "county_name": _to_str(row.get("county_name")),
            "county_fips": _to_str(row.get("county_fips")),
            "country": _to_str(row.get("country")) or "US",
            "source_url": _to_str(row.get("source_url")) or source.url,
            "retrieved_at": retrieved_at,
            "licence_id": source.licence_id,
            "last_changed": now,
        }

        asset = existing.get(source_asset_id)
        if asset is None:
            new_id = new_uuid()
            slug = _slug_for(name, state_code, used_slugs)
            asset = Asset(
                id=new_id,
                public_id=make_public_id("asset", new_id),
                slug=slug,
                asset_type=asset_type,
                source_id=source.id,
                source_asset_id=source_asset_id,
                first_seen=now,
                **fields,
            )
            session.add(asset)
            existing[source_asset_id] = asset
            result.inserted += 1
        else:
            for key, value in fields.items():
                setattr(asset, key, value)
            result.updated += 1

        if geom is not None:
            result.placed += 1
        else:
            result.unplaced += 1

    session.commit()
    return result


def load_assets_parquet(
    session: Session, path: pathlib.Path, asset_type: str, *, source_id: str | None = None
) -> AssetsLoadResult:
    """`source_id` defaults to the parquet's own `source_id` column (`resolve_source_id`)."""
    df = pd.read_parquet(path)
    return load_assets(session, df, asset_type, source_id=source_id)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=pathlib.Path, required=True)
    parser.add_argument("--asset-type", required=True, choices=sorted(ASSET_TYPES))
    parser.add_argument(
        "--source-id", help="Override the parquet's source_id column (must be wired for the type)"
    )
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args(argv)

    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        args.db.parent.mkdir(parents=True, exist_ok=True)
        db_url = f"sqlite+pysqlite:///{args.db}"

    t0 = time.monotonic()
    engine = get_engine(db_url)
    init_db(engine)
    session_factory = get_sessionmaker(engine)
    session = session_factory()
    try:
        result = load_assets_parquet(session, args.parquet, args.asset_type, source_id=args.source_id)
    finally:
        session.close()
    elapsed = round(time.monotonic() - t0, 2)
    print(json.dumps({**asdict(result), "elapsed_s": elapsed}))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
