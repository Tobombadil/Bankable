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
from dataclasses import asdict, dataclass, field
from typing import Any, cast

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.context.ethanol_match import DEFAULT_THRESHOLD as ETHANOL_DEFAULT_THRESHOLD
from pipeline.context.ethanol_match import match_ethanol
from services.db.models import ASSET_TYPES, Asset, AssetOwner, AssetSource, Source, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id as make_public_id
from services.ids import slugify
from services.ingest.loader import set_source_vintage, upsert_licence_and_source
from services.ingest.ownership import _build_norm_org_index, _resolve_organization
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


# ============================================================== ethanol_plant resolution (docs/24)
#: The two `ASSET_TYPE_SOURCE_IDS["ethanol_plant"]` entries, named rather than indexed so a change
#: to that tuple's order cannot silently swap which frame this loader treats as primary.
ETHANOL_ATLAS_SOURCE_ID, ETHANOL_CAPACITY_SOURCE_ID = ASSET_TYPE_SOURCE_IDS["ethanol_plant"]


@dataclass
class EthanolLoadResult:
    atlas_rows: int = 0
    capacity_rows: int = 0
    matched_pairs: int = 0
    atlas_only: int = 0
    capacity_only: int = 0
    assets_total: int = 0
    assets_inserted: int = 0
    assets_updated: int = 0
    threshold: float = ETHANOL_DEFAULT_THRESHOLD
    #: One `operator` edge written per matched pair, from whichever registry actually supplied the
    #: name used (coordinator correction, 2026-09-26: a merged asset's operator must be current,
    #: not stale-by-construction because it happens to be the primary source -- docs/24 §7.1).
    operator_edges_written: int = 0
    operator_edges_from_capacity: int = 0
    operator_edges_from_atlas_fallback: int = 0
    #: The matched capacity-side `source_asset_id`s (docs/24 §7 follow-up): `web/dev_up.py` filters
    #: these out of the capacity frame before calling the generic `services.ingest.midstream`
    #: operator-edge loader on it, so that loader is not asked to look up 184 ids that no longer
    #: name an asset of their own (measured harmless today -- `resolve_source_id`'s lookup already
    #: filters by `Asset.source_id`, so it reports them `unmatched_asset_ids` rather than
    #: mis-attaching anything -- but relying on that filter by accident is not the design).
    merged_capacity_source_asset_ids: list[str] = field(default_factory=list)


def _ethanol_row_fields(row: dict[str, object], *, source: Source, now: dt.datetime) -> dict[str, object]:
    """The same field set `load_assets` writes per row, extracted standalone (not shared with that
    function) so `load_assets`'s existing, separately-pinned behaviour for every other asset type
    cannot be touched by this one type's resolution path."""
    lon = _to_float(row.get("lon"))
    lat = _to_float(row.get("lat"))
    geom = (lon, lat) if lon is not None and lat is not None else None
    retrieved_at = _to_datetime(row.get("retrieved_at")) or now
    state_code = _to_str(row.get("state_code"))
    return {
        "name": _to_str(row.get("name")) or f"ethanol_plant {row.get('source_asset_id')}",
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
        "geom_line": None,
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


def _capacity_report_as_of(attributes: dict[str, Any]) -> dt.date | None:
    """The capacity report's own stated vintage, as a date: the workbook's title row states
    "as of January 1, <year>" (`pipeline/context/ethanol_capacity.py`'s parser), carried onto every
    row as `attributes.as_of_year`. `source.vintage`/`vintage_basis` do not carry this today --
    `_frame_vintage` only recognises the Atlas shapefile's `source_vintage` token, so this source
    resolves `not_stated` at the source level even though every row states a year -- so this reads
    the row's own attribute directly rather than the (currently blind, for this source) `Vintage`
    machinery. Fixing `_frame_vintage` to also recognise `as_of_year` is a separate change."""
    year = attributes.get("as_of_year")
    if year is None:
        return None
    try:
        return dt.date(int(year), 1, 1)
    except (TypeError, ValueError):
        return None


def load_ethanol_plants(
    session: Session,
    atlas_df: pd.DataFrame,
    capacity_df: pd.DataFrame,
    *,
    threshold: float = ETHANOL_DEFAULT_THRESHOLD,
    manifest_version: str = "",
) -> EthanolLoadResult:
    """Load both `ethanol_plant` registries as **one asset per real plant** (docs/24 §5(a) option
    (a), measured 48.2% cross-source duplication with no resolution -- docs/24 §2.3).

    `pipeline.context.ethanol_match.match_ethanol` scores every same-state candidate pair on
    company name, city and nameplate capacity and assigns globally greedy at `threshold`
    (`data/eval/ethanol_match_labels.csv` is the labelled sample the default threshold is chosen
    against). For each accepted pair, one `asset` row is written keyed on the **Atlas** record's
    `(source_id, source_asset_id)` -- the Atlas layer carries coordinates, so it is always the
    primary source when a plant has one (docs/24 §5(a): "the primary source... the one with
    coordinates when present") -- with **location and identity from the Atlas row, nameplate
    capacity from the capacity report** (docs/24 §7.2: the two registries' nameplate figures
    disagree more than their owner fields do, and the capacity report is the current annual
    figure). `asset.attributes["sources"]` records which registry each merged field came from, so
    a detail page can cite it. Two `asset_source` rows are written per matched pair (one per
    registry), and exactly one for every row either registry did not match -- **nothing is
    dropped**: an unmatched Atlas or capacity row still loads as its own single-source asset,
    keyed on its own identity exactly as `load_assets` would key it.

    **Operator is the current name, not the primary source's name** (coordinator correction,
    2026-09-26, docs/24 §7.1): a merged asset's `operator_name`, and the `asset_owner` `operator`
    edge this function writes for it, take the capacity report's operator string when the matched
    capacity row states one (essentially always -- `respondent` is a required field of that table)
    and fall back to the Atlas row's only when it does not. Using the primary source's name
    unconditionally would have reproduced exactly the staleness docs/24 §7.1 measured (40 of 183
    confirmed pairs disagree on operator, and the capacity report is the current one) on the very
    rows this loader merges. The edge carries `as_of` from the capacity row's own stated vintage
    (`_capacity_report_as_of`) when the capacity name is used, and the provenance quartet of
    whichever registry actually supplied the name. The Atlas string is never discarded even when
    overridden: it is kept at `attributes["sources"]["atlas_operator_name"]`. Organisation
    resolution reuses `services.ingest.ownership._resolve_organization`/`_build_norm_org_index`
    (the same helper `services/ingest/midstream.py` and `services/ingest/ghgrp.py` call), not a
    copy of it. Singles are unaffected -- their operator edges still come from
    `services.ingest.midstream.load_operator_edges`, run by the caller as before.

    Idempotent against an unchanged pair of frames at an unchanged threshold: assets are looked up
    by their existing `(source_id, source_asset_id)` and `asset_source` links by their existing
    `(source_id, source_record_id)`, both upserted in place. It is not reversible across a
    *changed* match decision (a different threshold, or new data that re-scores a pair): a source
    record that is re-pointed from one asset to another leaves its former asset row in place with
    one fewer link rather than deleting or merging it -- the reversible-merge machinery docs/22 §13
    describes for proposals does not exist for assets yet (docs/24 §6.3 declines to build the link
    table and a full resolution pass in one change; this loader is deliberately the first half).
    """
    registry = Registry()
    atlas_source = upsert_licence_and_source(
        session, registry.get(ETHANOL_ATLAS_SOURCE_ID), manifest_version or registry.version
    )
    capacity_source = upsert_licence_and_source(
        session, registry.get(ETHANOL_CAPACITY_SOURCE_ID), manifest_version or registry.version
    )
    set_source_vintage(atlas_source, _frame_vintage(atlas_df))
    set_source_vintage(capacity_source, _frame_vintage(capacity_df))

    result = EthanolLoadResult(atlas_rows=len(atlas_df), capacity_rows=len(capacity_df), threshold=threshold)
    now = utcnow()

    atlas_rows: dict[str, dict[str, object]] = {
        str(r["source_asset_id"]): r for r in atlas_df.to_dict("records")
    }
    capacity_rows: dict[str, dict[str, object]] = {
        str(r["source_asset_id"]): r for r in capacity_df.to_dict("records")
    }

    matches = match_ethanol(atlas_df, capacity_df, threshold=threshold)
    accepted = matches[matches["accepted"].astype(bool)] if len(matches) else matches
    matched_atlas_ids = set(accepted["atlas_source_asset_id"]) if len(accepted) else set()
    matched_capacity_ids = set(accepted["capacity_source_asset_id"]) if len(accepted) else set()
    result.matched_pairs = len(accepted)
    result.atlas_only = len(atlas_rows) - len(matched_atlas_ids)
    result.capacity_only = len(capacity_rows) - len(matched_capacity_ids)
    result.assets_total = result.matched_pairs + result.atlas_only + result.capacity_only

    existing_assets: dict[tuple[str, str], Asset] = {
        (a.source_id, a.source_asset_id): a
        for a in session.scalars(select(Asset).where(Asset.asset_type == "ethanol_plant"))
    }
    existing_links: dict[tuple[str, str], AssetSource] = {
        (link.source_id, link.source_record_id): link
        for link in session.scalars(
            select(AssetSource).where(AssetSource.source_id.in_([atlas_source.id, capacity_source.id]))
        )
    }
    existing_owner_edges: dict[tuple[str, str, str, str], AssetOwner] = {}
    _existing_asset_ids = [a.id for a in existing_assets.values()]
    if _existing_asset_ids:
        existing_owner_edges = {
            (str(e.asset_id), str(e.organization_id), e.role, e.source_id): e
            for e in session.scalars(
                select(AssetOwner).where(
                    AssetOwner.asset_id.in_(_existing_asset_ids), AssetOwner.role == "operator"
                )
            )
        }
    used_slugs: set[str] = set(session.scalars(select(Asset.slug)))
    norm_org_index = _build_norm_org_index(session)
    org_created_counter = [0]

    def upsert_operator_edge(
        *,
        asset: Asset,
        raw_name: str,
        role_source: Source,
        as_of: dt.date | None,
        source_url: str,
        retrieved_at: dt.datetime,
    ) -> None:
        org = _resolve_organization(
            session,
            norm_org_index,
            raw_name,
            source=role_source,
            now=now,
            created_counter=org_created_counter,
        )
        key = (str(asset.id), str(org.id), "operator", role_source.id)
        edge = existing_owner_edges.get(key)
        if edge is None:
            edge = AssetOwner(
                asset_id=asset.id,
                organization_id=org.id,
                role="operator",
                source_id=role_source.id,
                licence_id=role_source.licence_id,
            )
            session.add(edge)
            existing_owner_edges[key] = edge
        edge.share_pct = None
        edge.as_of = as_of
        edge.owner_name_raw = raw_name
        edge.source_url = source_url
        edge.retrieved_at = retrieved_at
        result.operator_edges_written += 1

    def upsert_asset(*, source: Source, source_asset_id: str, fields: dict[str, object]) -> Asset:
        key = (source.id, source_asset_id)
        asset = existing_assets.get(key)
        if asset is None:
            new_id = new_uuid()
            slug = _slug_for(str(fields["name"]), fields.get("state_code"), used_slugs)  # type: ignore[arg-type]
            asset = Asset(
                id=new_id,
                public_id=make_public_id("asset", new_id),
                slug=slug,
                asset_type="ethanol_plant",
                source_id=source.id,
                source_asset_id=source_asset_id,
                first_seen=now,
                **fields,
            )
            session.add(asset)
            existing_assets[key] = asset
            result.assets_inserted += 1
        else:
            for k, v in fields.items():
                setattr(asset, k, v)
            result.assets_updated += 1
        return asset

    def upsert_link(
        *,
        asset: Asset,
        source: Source,
        source_record_id: str,
        is_primary: bool,
        match_method: str,
        match_score: float | None,
        source_url: str,
        retrieved_at: dt.datetime,
    ) -> None:
        key = (source.id, source_record_id)
        link = existing_links.get(key)
        if link is None:
            link = AssetSource(
                asset_id=asset.id,
                source_id=source.id,
                source_record_id=source_record_id,
                licence_id=source.licence_id,
            )
            session.add(link)
            existing_links[key] = link
        else:
            link.asset_id = asset.id
        link.source_url = source_url
        link.retrieved_at = retrieved_at
        link.is_primary = is_primary
        link.match_method = match_method
        link.match_score = match_score

    def sources_map(*, capacity_from: str, rest_from: str) -> dict[str, str]:
        return {
            "capacity_value": capacity_from,
            "location": rest_from,
            "name": rest_from,
            "operator_name": rest_from,
        }

    for m in accepted.to_dict("records") if len(accepted) else []:
        atlas_id, capacity_id = str(m["atlas_source_asset_id"]), str(m["capacity_source_asset_id"])
        atlas_row, capacity_row = atlas_rows[atlas_id], capacity_rows[capacity_id]
        atlas_fields = _ethanol_row_fields(atlas_row, source=atlas_source, now=now)
        capacity_fields = _ethanol_row_fields(capacity_row, source=capacity_source, now=now)
        fields = dict(atlas_fields)  # identity and location: Atlas (docs/24 §5(a))
        # capacity: the annual report (docs/24 §7.2 -- the two registries disagree on nameplate
        # more than they disagree on owner; the capacity report is the current, dated figure).
        if capacity_fields["capacity_value"] is not None:
            fields["capacity_value"] = capacity_fields["capacity_value"]
        # operator: the current name, not the primary source's name (coordinator correction,
        # 2026-09-26, docs/24 §7.1) -- the capacity report's respondent string when it states one
        # (essentially always), Atlas's only as a fallback.
        atlas_operator = cast("str | None", atlas_fields["operator_name"])
        capacity_operator = cast("str | None", capacity_fields["operator_name"])
        operator_from_capacity = capacity_operator is not None
        preferred_operator = capacity_operator if operator_from_capacity else atlas_operator
        fields["operator_name"] = preferred_operator
        atlas_attrs = cast("dict[str, Any]", atlas_fields["attributes"])
        capacity_attrs = cast("dict[str, Any]", capacity_fields["attributes"])
        merged_attrs: dict[str, Any] = {**atlas_attrs, **capacity_attrs}
        operator_source = capacity_source if operator_from_capacity else atlas_source
        merged_attrs["sources"] = sources_map(capacity_from=capacity_source.id, rest_from=atlas_source.id)
        merged_attrs["sources"]["operator_name"] = operator_source.id
        # Never discarded, even though overridden: the Atlas string is docs/24 §7.1's own evidence
        # of staleness, and a reader should still be able to see what Atlas said.
        if atlas_operator is not None:
            merged_attrs["atlas_operator_name"] = atlas_operator
        fields["attributes"] = merged_attrs
        asset = upsert_asset(source=atlas_source, source_asset_id=atlas_id, fields=fields)
        upsert_link(
            asset=asset,
            source=atlas_source,
            source_record_id=atlas_id,
            is_primary=True,
            match_method="deterministic_key",
            match_score=None,
            source_url=str(atlas_fields["source_url"]),
            retrieved_at=atlas_fields["retrieved_at"],  # type: ignore[arg-type]
        )
        upsert_link(
            asset=asset,
            source=capacity_source,
            source_record_id=capacity_id,
            is_primary=False,
            match_method="rule",
            match_score=float(m["score"]),
            source_url=str(capacity_fields["source_url"]),
            retrieved_at=capacity_fields["retrieved_at"],  # type: ignore[arg-type]
        )
        if preferred_operator is not None:
            as_of = _capacity_report_as_of(capacity_attrs) if operator_from_capacity else None
            operator_fields = capacity_fields if operator_from_capacity else atlas_fields
            upsert_operator_edge(
                asset=asset,
                raw_name=preferred_operator,
                role_source=operator_source,
                as_of=as_of,
                source_url=str(operator_fields["source_url"]),
                retrieved_at=operator_fields["retrieved_at"],  # type: ignore[arg-type]
            )
            if operator_from_capacity:
                result.operator_edges_from_capacity += 1
            else:
                result.operator_edges_from_atlas_fallback += 1

    for source_asset_id, row in atlas_rows.items():
        if source_asset_id in matched_atlas_ids:
            continue
        fields = _ethanol_row_fields(row, source=atlas_source, now=now)
        fields["attributes"] = {
            **cast("dict[str, Any]", fields["attributes"]),
            "sources": sources_map(capacity_from=atlas_source.id, rest_from=atlas_source.id),
        }
        asset = upsert_asset(source=atlas_source, source_asset_id=source_asset_id, fields=fields)
        upsert_link(
            asset=asset,
            source=atlas_source,
            source_record_id=source_asset_id,
            is_primary=True,
            match_method="deterministic_key",
            match_score=None,
            source_url=str(fields["source_url"]),
            retrieved_at=fields["retrieved_at"],  # type: ignore[arg-type]
        )

    for source_asset_id, row in capacity_rows.items():
        if source_asset_id in matched_capacity_ids:
            continue
        fields = _ethanol_row_fields(row, source=capacity_source, now=now)
        fields["attributes"] = {
            **cast("dict[str, Any]", fields["attributes"]),
            "sources": sources_map(capacity_from=capacity_source.id, rest_from=capacity_source.id),
        }
        asset = upsert_asset(source=capacity_source, source_asset_id=source_asset_id, fields=fields)
        upsert_link(
            asset=asset,
            source=capacity_source,
            source_record_id=source_asset_id,
            is_primary=True,
            match_method="deterministic_key",
            match_score=None,
            source_url=str(fields["source_url"]),
            retrieved_at=fields["retrieved_at"],  # type: ignore[arg-type]
        )

    result.merged_capacity_source_asset_ids = sorted(matched_capacity_ids)
    session.commit()
    return result


def load_ethanol_plants_parquet(
    session: Session,
    atlas_path: pathlib.Path,
    capacity_path: pathlib.Path,
    *,
    threshold: float = ETHANOL_DEFAULT_THRESHOLD,
) -> EthanolLoadResult:
    return load_ethanol_plants(
        session, pd.read_parquet(atlas_path), pd.read_parquet(capacity_path), threshold=threshold
    )


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
