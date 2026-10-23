"""Load the OpenStreetMap power layers (`pipeline/context/osm_power.py`) into `asset`: transmission
lines (`transmission_line`) and substations (`substation`), source `us.osm.power`, ODbL 1.0
(docs/25 §2.7; docs/13 §6).

Each row goes through `services.ingest.assets.load_assets`, so it carries the provenance quartet
every asset carries (`source_id`, `source_url` = the element's openstreetmap.org page,
`retrieved_at` = the extract's replication timestamp, `licence_id`) and is upserted by
`(source_id, source_asset_id)`: `way/<id>`, `node/<id>`, `relation/<id>`. A re-run of the same files
inserts nothing and changes no identity.

**Withdrawal.** An element a new extract no longer has (deleted in OpenStreetMap, re-tagged below
69 kV, merged into another way) is withdrawn: its asset row is deleted, with its owner and source
links, and an interconnection point that named it as its substation is unlinked. Assets carry no
lifecycle and no events (ADR 0008), so the count and a sample of ids are in the load report and the
log, not in `event`. The rule for what a load may withdraw is the one that decided what it holds
(`pipeline/context/osm_power.py`, "Regions"): a held row is in a load's scope when its
`attributes.scope_regions` meet the regions the load covers. Two guards keep a bad build from
emptying a state:

* only rows in the covered regions' scope are touched (`covered_regions`: the manifest's list, else
  the rows' `coverage_regions`), so a build of Texas alone withdraws nothing wholly in Oklahoma;
* a region whose withdrawals would exceed `MAX_WITHDRAW_SHARE` of the rows it held before this load,
  and number at least `MIN_WITHDRAW_ROWS`, is held: its missing rows stay (and so does any missing
  row in its scope) until a person looks, and the region is logged (the churn gate the connectors
  apply, `pipeline/connectors/dq.py`, here for a context layer). "Held before this load" is read
  before the upsert, so this load's own inserts never dilute the share.

**Regions no longer loaded.** When the manifest says the load was narrowed to named regions
(`regions_loaded`, the server's `GRID_LAYERS_REGIONS`) and the store holds `us.osm.power` rows in none
of their scopes (the setting was narrowed, say from ``US-TX,US-OK`` to ``US-TX``), those rows are
removed (`remove_unloaded`): nothing would refresh them again, and while they stayed, their
`coverage_regions` would keep telling the map's source rule that OpenStreetMap covers a state whose
LBNL lines it then hides (`services/api/assets.py`, `_mark_superseded`). It is a configuration change,
not a bad build, so the churn guard does not apply; the count per region is in the report and the log.
Nothing is removed without a manifest, nor when the load was not narrowed (`regions_loaded` null, as
a ``normalise`` or an ``all`` pull writes it), nor for a region that was asked for but that the build
does not hold (the pull reports it in `regions_missing`): the first bullet above still applies to a
build that lacks a state.

**Files written together.** `write_asset_frames` stamps both frames and the manifest copy with one
`frame_set` token and renames them into place, the manifest last. A load whose files carry different
tokens (a crash part-way through a write) loads nothing and withdraws nothing (`frames_consistent`),
so new frames are never read against the previous load's covered regions; the store keeps what it
holds until the next pull writes a whole set.

**Share-alike.** The licence row is marked `share_alike` (ODbL §4.4): the loader writes it because
`upsert_licence_and_source` has no manifest field for it, and every licence it creates starts false.

The operator stays text (`attributes.operator`), as the pipeline module explains; no edge loader runs.
"""

from __future__ import annotations

import json
import logging
import pathlib
from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

import pandas as pd
import pyarrow.parquet as pq  # type: ignore[import-untyped,unused-ignore]
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Asset, InterconnectionPoint, Licence
from services.ingest.assets import AssetsLoadResult, load_assets

log = logging.getLogger(__name__)

SOURCE_ID = "us.osm.power"
LINES_FILE = f"{SOURCE_ID}.lines.parquet"
SUBSTATIONS_FILE = f"{SOURCE_ID}.substations.parquet"
MANIFEST_FILE = f"{SOURCE_ID}.manifest.json"
#: The token `pipeline.context.osm_power.write_asset_frames` writes into the manifest copy and into
#: each frame's parquet key-value metadata (`pipeline.context.osm_power.FRAME_SET_KEY`).
FRAME_SET_KEY = "frame_set"
#: `normalized/context/<file>` -> `asset_type`, in load order.
OSM_POWER_FILES: tuple[tuple[str, str], ...] = (
    (LINES_FILE, "transmission_line"),
    (SUBSTATIONS_FILE, "substation"),
)
#: Most of a region's rows one reload may withdraw. Above it the region's withdrawals are held...
MAX_WITHDRAW_SHARE = 0.10
#: ...when they number at least this many: a handful of ordinary edits in a small state is over 10%.
#: The same floor as the build's promotion gate (`pipeline.context.osm_power.MIN_REGION_DROP_ROWS`).
MIN_WITHDRAW_ROWS = 50
#: `IN (...)` chunk size for deleting withdrawn rows (SQLite's bound-parameter limit).
DELETE_CHUNK = 500
#: Ids listed per region in the report (the rest are counted).
WITHDRAWN_SAMPLE = 20


@dataclass
class OsmPowerLoadResult:
    asset_type: str
    loaded: dict[str, int] = field(default_factory=dict)
    covered_regions: list[str] = field(default_factory=list)
    withdrawn: int = 0
    withdrawn_sample: list[str] = field(default_factory=list)
    #: region -> `{"would_withdraw": n, "held": m}` for a region the churn guard stopped.
    held_regions: dict[str, dict[str, int]] = field(default_factory=dict)
    interconnection_points_unlinked: int = 0
    #: Rows of the other OpenStreetMap type whose element now arrives as this one (a closed way
    #: re-tagged from `power=substation` to `power=line`), replaced rather than left to collide on
    #: `(source_id, source_asset_id)`.
    retyped: int = 0
    #: Rows removed because no covered region's scope holds them any more (`remove_unloaded`), and
    #: how many per region of their scope.
    unloaded_removed: int = 0
    unloaded_regions: dict[str, int] = field(default_factory=dict)

    def as_report(self) -> dict[str, Any]:
        return asdict(self)


def _attributes(value: object) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def scope_of_attributes(attributes: object) -> list[str]:
    """A held row's `scope_regions` (`pipeline/context/osm_power.py`, "Regions"); a row without one
    falls back to its `region`."""
    attrs = _attributes(attributes)
    scope = attrs.get("scope_regions")
    if isinstance(scope, list) and scope:
        return sorted(str(r) for r in scope if r)
    region = attrs.get("region")
    return [str(region)] if region else []


def covered_regions(df: pd.DataFrame, manifest: dict[str, Any] | None) -> list[str]:
    """The regions a load covers: the manifest's `regions[].region` when the manifest is there
    (`write_asset_frames` narrows it to the regions loaded), else every `attributes.coverage_regions`
    the rows carry."""
    if manifest and manifest.get("regions"):
        return sorted({str(r["region"]) for r in manifest["regions"] if r.get("region")})
    found: set[str] = set()
    for a in df.get("attributes", pd.Series(dtype=object)):
        coverage = _attributes(a).get("coverage_regions")
        found.update(str(r) for r in coverage or () if r)
    return sorted(found)


def _held_scopes(session: Session, asset_type: str) -> dict[str, list[str]]:
    """`source_asset_id -> scope` for every `us.osm.power` row of `asset_type` the store holds. Only
    `scope_regions` and `region` are read (JSON path access), never a substation's outline."""
    stmt = select(Asset.source_asset_id, Asset.attributes["scope_regions"], Asset.attributes["region"]).where(
        Asset.source_id == SOURCE_ID, Asset.asset_type == asset_type
    )
    return {
        str(source_asset_id): scope_of_attributes({"scope_regions": scope, "region": region})
        for source_asset_id, scope, region in session.execute(stmt).all()
    }


def held_in_scope(session: Session, asset_type: str, regions: list[str]) -> dict[str, list[str]]:
    """`source_asset_id -> the covered regions in its scope` for every `us.osm.power` row of
    `asset_type` the store holds in `regions`' scope, read before a load changes anything."""
    region_set = set(regions)
    if not region_set:
        return {}
    held: dict[str, list[str]] = {}
    for source_asset_id, scope in _held_scopes(session, asset_type).items():
        in_scope = sorted(set(scope) & region_set)
        if in_scope:
            held[source_asset_id] = in_scope
    return held


def remove_unloaded(
    session: Session, asset_type: str, regions: list[str], present: set[str], result: OsmPowerLoadResult
) -> None:
    """Delete the `us.osm.power` rows of `asset_type` that this load does not carry (`present`) and
    whose scope meets none of `regions` (module docstring, "Regions no longer loaded"). Called only
    with a manifest's non-empty `regions_loaded`."""
    region_set = set(regions)
    if not region_set:
        return
    gone = {
        sid: scope
        for sid, scope in _held_scopes(session, asset_type).items()
        if sid not in present and not set(scope) & region_set
    }
    if not gone:
        return
    ids = sorted(gone)
    doomed: list[Asset] = []
    for i in range(0, len(ids), DELETE_CHUNK):
        doomed.extend(
            session.scalars(
                select(Asset).where(
                    Asset.source_id == SOURCE_ID,
                    Asset.asset_type == asset_type,
                    Asset.source_asset_id.in_(ids[i : i + DELETE_CHUNK]),
                )
            )
        )
    result.interconnection_points_unlinked += _delete_assets(session, doomed)
    result.unloaded_removed = len(doomed)
    result.unloaded_regions = dict(
        sorted(Counter(r for scope in gone.values() for r in scope or ["(none)"]).items())
    )
    session.commit()
    log.warning(
        "osm power: %s: removed %d rows of regions this load no longer covers (%s); covered: %s",
        asset_type,
        result.unloaded_removed,
        result.unloaded_regions,
        ", ".join(sorted(region_set)),
    )


def withdraw_missing(
    session: Session,
    held: dict[str, list[str]],
    present: set[str],
    asset_type: str,
    regions: list[str],
    *,
    max_share: float = MAX_WITHDRAW_SHARE,
    min_rows: int = MIN_WITHDRAW_ROWS,
) -> OsmPowerLoadResult:
    """Delete the rows of `held` (`held_in_scope`, read before the load) that `present` (the load's
    `source_asset_id`s) no longer has, unless a region in their scope would lose more than
    `max_share` of the rows it held, and at least `min_rows`: that region is held, and so is every
    missing row in its scope."""
    result = OsmPowerLoadResult(asset_type=asset_type, covered_regions=list(regions))
    missing = {sid: scope for sid, scope in held.items() if sid not in present}
    held_count = Counter(r for scope in held.values() for r in scope)
    missing_count = Counter(r for scope in missing.values() for r in scope)
    for region, n in sorted(missing_count.items()):
        if n >= min_rows and n / held_count[region] > max_share:
            result.held_regions[region] = {"would_withdraw": n, "held": held_count[region]}
            log.warning(
                "osm power: %s %s would withdraw %d of %d rows (over %.0f%% and %d rows); held for review",
                asset_type,
                region,
                n,
                held_count[region],
                max_share * 100,
                min_rows,
            )
    doomed_ids = sorted(
        sid for sid, scope in missing.items() if not any(r in result.held_regions for r in scope)
    )
    if not doomed_ids:
        return result
    doomed: list[Asset] = []
    for i in range(0, len(doomed_ids), DELETE_CHUNK):
        doomed.extend(
            session.scalars(
                select(Asset).where(
                    Asset.source_id == SOURCE_ID,
                    Asset.asset_type == asset_type,
                    Asset.source_asset_id.in_(doomed_ids[i : i + DELETE_CHUNK]),
                )
            )
        )
    result.interconnection_points_unlinked = _delete_assets(session, doomed)
    result.withdrawn = len(doomed)
    result.withdrawn_sample = doomed_ids[:WITHDRAWN_SAMPLE]
    session.commit()
    return result


def _delete_assets(session: Session, assets: list[Asset]) -> int:
    """Delete `assets` (owners and source links cascade through `Asset.owners`/`Asset.sources`)
    after unlinking any interconnection point that names one as its substation. Returns how many
    points were unlinked."""
    ids = [a.id for a in assets]
    unlinked = session.execute(
        sa.update(InterconnectionPoint)
        .where(InterconnectionPoint.substation_asset_id.in_(ids))
        .values(substation_asset_id=None)
    )
    for asset in assets:
        session.delete(asset)
    return int(getattr(unlinked, "rowcount", 0) or 0)


def replace_retyped(session: Session, df: pd.DataFrame, asset_type: str) -> int:
    """Delete the OpenStreetMap rows of another asset type whose element id `df` now carries as
    `asset_type`, so the upsert inserts it afresh instead of failing on the unique source key."""
    incoming = {str(v) for v in df["source_asset_id"]} if len(df) else set()
    clashing = [
        a
        for a in session.scalars(
            select(Asset).where(Asset.source_id == SOURCE_ID, Asset.asset_type != asset_type)
        )
        if a.source_asset_id in incoming
    ]
    if clashing:
        _delete_assets(session, clashing)
        session.commit()
    return len(clashing)


def _mark_share_alike(session: Session) -> None:
    licence_ids = set(
        session.scalars(select(Asset.licence_id).where(Asset.source_id == SOURCE_ID).distinct())
    )
    for licence in session.scalars(select(Licence).where(Licence.id.in_(licence_ids))):
        if not licence.share_alike:
            licence.share_alike = True
    session.commit()


def load_osm_power_frame(
    session: Session,
    df: pd.DataFrame,
    asset_type: str,
    *,
    manifest: dict[str, Any] | None = None,
    max_share: float = MAX_WITHDRAW_SHARE,
    min_rows: int = MIN_WITHDRAW_ROWS,
) -> OsmPowerLoadResult:
    """Read what the covered regions held, upsert one frame, withdraw what it no longer has, remove
    the rows of regions no longer loaded (with a manifest only), mark the licence."""
    regions = covered_regions(df, manifest)
    held = held_in_scope(session, asset_type, regions)
    retyped = replace_retyped(session, df, asset_type)
    loaded: AssetsLoadResult = load_assets(session, df, asset_type, source_id=SOURCE_ID)
    present = {str(v) for v in df["source_asset_id"]} if len(df) else set()
    result = withdraw_missing(
        session, held, present, asset_type, regions, max_share=max_share, min_rows=min_rows
    )
    narrowed_to = (manifest or {}).get("regions_loaded")
    if isinstance(narrowed_to, list) and narrowed_to:
        remove_unloaded(session, asset_type, sorted(str(r) for r in narrowed_to if r), present, result)
    result.loaded = asdict(loaded)
    result.retyped = retyped
    _mark_share_alike(session)
    return result


def read_manifest(directory: pathlib.Path) -> dict[str, Any] | None:
    path = directory / MANIFEST_FILE
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.info("osm power: manifest %s unreadable (%s); regions come from the rows", path, exc)
        return None
    return manifest if isinstance(manifest, dict) else None


def frame_set_of(path: pathlib.Path) -> str | None:
    """The `FRAME_SET_KEY` token in a frame's parquet metadata (`None` for a frame written before
    the token existed, or by hand)."""
    value = (pq.read_schema(path).metadata or {}).get(FRAME_SET_KEY.encode())
    return value.decode() if value else None


def frames_consistent(directory: pathlib.Path, manifest: dict[str, Any] | None) -> bool:
    """Whether the frames present and the manifest copy come from one write (module docstring,
    "Files written together"): every one that carries a token carries the same one, and when any
    carries one, all of them do. Files that carry none (written before the token, or a test's own)
    are accepted as before."""
    tokens: dict[str, str | None] = {}
    if manifest is not None:
        tokens[MANIFEST_FILE] = str(manifest.get(FRAME_SET_KEY) or "") or None
    for file_name, _ in OSM_POWER_FILES:
        path = directory / file_name
        if path.exists():
            tokens[file_name] = frame_set_of(path)
    if not any(tokens.values()) or len(set(tokens.values())) == 1:
        return True
    log.warning(
        "osm power: %s holds files from different writes (%s); nothing loaded or withdrawn until the "
        "next pull writes a whole set",
        directory,
        ", ".join(f"{name}={token or 'none'}" for name, token in sorted(tokens.items())),
    )
    return False


def load_osm_power(session: Session, directory: pathlib.Path) -> list[OsmPowerLoadResult]:
    """Both files under `directory` (`normalized/context/`), lines first. A file that is not there
    is one log line; nothing is withdrawn for a type whose file is absent. Files from different
    writes load nothing (`frames_consistent`)."""
    manifest = read_manifest(directory)
    results: list[OsmPowerLoadResult] = []
    if not frames_consistent(directory, manifest):
        return results
    for file_name, asset_type in OSM_POWER_FILES:
        path = directory / file_name
        if not path.exists():
            log.info("osm power: %s not found, skipping", path)
            continue
        result = load_osm_power_frame(session, pd.read_parquet(path), asset_type, manifest=manifest)
        log.info("osm power: loaded %s from %s (%s)", asset_type, file_name, result.as_report())
        results.append(result)
    return results
