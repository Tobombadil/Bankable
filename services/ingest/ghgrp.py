"""Loader for EPA GHGRP facilities (docs/02 §12; docs/24 constraint) -> `asset_owner` edges and
`asset.attributes["ghgrp"]` on **existing** assets. Never inserts an `asset` row.

Reads the facility parquet `pipeline/context/ghgrp.py` writes (`FACILITY_COLUMNS`), pulls every
asset with a point out of the session, runs `pipeline.context.ghgrp.match_facilities` (EPA's ORIS
crosswalk first, then state + ≤1 km + name + NAICS, accepted at `threshold`), and for each accepted
match:

- writes one `asset_owner` edge per parent named in the register — `role="owner"`, `share_pct`
  exactly as stated (NULL where the register states none), **`as_of` = 31 December of the
  reporting year** (the first source in this schema that states both shares and a date, which
  docs/24 §7.1 says must be populated before anything ranks owner edges), `owner_name_raw` the
  register's spelling, provenance quartet of `us.epa.ghgrp`, `source_url` the facility-year's own
  REST address. Organisations resolve through the same `org_key` index and alias table
  `services/ingest/ownership.py` uses (its helpers are reused, not copied). Two parents of one
  facility that resolve to the same organisation are summed and the facility is flagged. Shares
  that do not sum to 100 are written as stated and counted (`facilities_shares_not_100`), never
  rescaled.
- sets `asset.attributes["ghgrp"]` to the facility's `ghgrp_facility_id`, `frs_id`,
  `reporting_year`, `naics_code`, `co2_captured`, `rr_mrv_plan_url`, subpart membership
  (`subparts`, `subpart_rr/uu/pp`), the RR/UU quantities when the summary zip was joined, the
  match method and score, and the provenance quartet **inline** — `asset` has no per-field
  provenance (docs/24 §4), so the attribute carries its own citation rather than borrowing the
  row's. Other attribute keys are left untouched.

Unmatched facilities are counted (`held_no_candidate`, `held_below_threshold`) and stay in the
parquet only. A facility whose match is not accepted writes nothing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import time
import uuid as _uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.context.ghgrp import (
    DEFAULT_CROSSWALK,
    DEFAULT_THRESHOLD,
    build_owner_rows,
    load_crosswalk,
    match_facilities,
)
from services.db.models import Asset, AssetOwner
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import upsert_licence_and_source
from services.ingest.ownership import (
    PLACEHOLDER_OWNER_NAMES,
    _build_norm_org_index,
    _resolve_organization,
    _to_datetime,
    org_key,
)

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")
SOURCE_ID = "us.epa.ghgrp"
ATTRIBUTE_KEY = "ghgrp"
ATTRIBUTE_FIELDS: tuple[str, ...] = (
    "ghgrp_facility_id",
    "frs_id",
    "reporting_year",
    "naics_code",
    "co2_captured",
    "rr_mrv_plan_url",
    "subparts",
    "subpart_rr",
    "subpart_uu",
    "subpart_pp",
    "rr_co2_sequestered_t",
    "rr_co2_sequestered_confidential",
    "uu_co2_received_t",
    "uu_co2_received_confidential",
    "share_flag",
)

log = logging.getLogger(__name__)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class GhgrpLoadResult:
    facilities_seen: int = 0
    facilities_with_point: int = 0
    matched_crosswalk: int = 0
    matched_geo_name: int = 0
    held_below_threshold: int = 0
    held_no_candidate: int = 0
    matched_by_asset_type: dict[str, int] = field(default_factory=dict)
    assets_updated: int = 0
    edges_written: int = 0
    edges_without_share: int = 0
    facilities_shares_not_100: int = 0
    facilities_parents_merged: int = 0
    rows_skipped_placeholder_owner: int = 0
    organizations_created: int = 0
    organizations_matched: int = 0
    threshold: float = DEFAULT_THRESHOLD

    def as_report(self) -> dict[str, object]:
        return {
            "facilities_seen": self.facilities_seen,
            "facilities_with_point": self.facilities_with_point,
            "matched_crosswalk": self.matched_crosswalk,
            "matched_geo_name": self.matched_geo_name,
            "held_below_threshold": self.held_below_threshold,
            "held_no_candidate": self.held_no_candidate,
            "matched_by_asset_type": dict(sorted(self.matched_by_asset_type.items())),
            "assets_updated": self.assets_updated,
            "edges_written": self.edges_written,
            "edges_without_share": self.edges_without_share,
            "facilities_shares_not_100": self.facilities_shares_not_100,
            "facilities_parents_merged": self.facilities_parents_merged,
            "rows_skipped_placeholder_owner": self.rows_skipped_placeholder_owner,
            "organizations_created": self.organizations_created,
            "organizations_matched": self.organizations_matched,
            "threshold": self.threshold,
        }


def assets_frame(session: Session) -> pd.DataFrame:
    """Every asset with a point, in the shape `match_facilities` reads."""
    rows: list[dict[str, Any]] = []
    for a in session.scalars(select(Asset)):
        geom = a.geom
        lon, lat = (geom[0], geom[1]) if isinstance(geom, (tuple, list)) and len(geom) == 2 else (None, None)
        rows.append(
            {
                "asset_id": str(a.id),
                "asset_type": a.asset_type,
                "source_id": a.source_id,
                "source_asset_id": a.source_asset_id,
                "name": a.name,
                "state_code": a.state_code,
                "technology": a.technology,
                "lon": lon,
                "lat": lat,
            }
        )
    return pd.DataFrame(
        rows,
        columns=[
            "asset_id",
            "asset_type",
            "source_id",
            "source_asset_id",
            "name",
            "state_code",
            "technology",
            "lon",
            "lat",
        ],
    )


def _json_value(v: Any) -> Any:
    if v is None or v is pd.NA or v is pd.NaT:
        return None
    if isinstance(v, float) and pd.isna(v):
        return None
    if isinstance(v, str):
        stripped = v.strip()
        if stripped[:1] in "[{":
            try:
                return json.loads(stripped)
            except json.JSONDecodeError:
                return v
        return v
    if hasattr(v, "item"):
        return v.item()
    if isinstance(v, (list, dict, bool, int, float)):
        return v
    return str(v)


def _facility_attributes(f: dict[str, Any], m: dict[str, Any], *, licence_id: str) -> dict[str, Any]:
    out: dict[str, Any] = {k: _json_value(f.get(k)) for k in ATTRIBUTE_FIELDS if k in f}
    out["match_method"] = m["method"]
    out["match_score"] = float(m["score"])
    out["source_id"] = SOURCE_ID
    out["source_url"] = f.get("source_url")
    out["retrieved_at"] = f.get("retrieved_at")
    out["licence_id"] = licence_id
    return out


def load_ghgrp(
    session: Session,
    facilities: pd.DataFrame,
    *,
    crosswalk: pd.DataFrame | None = None,
    threshold: float = DEFAULT_THRESHOLD,
    manifest_version: str = "",
) -> tuple[GhgrpLoadResult, pd.DataFrame]:
    """Match, then write edges and attributes. Returns the result and the full match frame
    (accepted and not), so a caller can write it next to the parquet as the run's evidence."""
    registry = Registry()
    entry = registry.get(SOURCE_ID)
    source = upsert_licence_and_source(session, entry, manifest_version or registry.version)

    result = GhgrpLoadResult(facilities_seen=len(facilities), threshold=threshold)
    if not len(facilities):
        session.commit()
        return result, pd.DataFrame()
    result.facilities_with_point = int(facilities["lon"].notna().sum())

    assets = assets_frame(session)
    matches = match_facilities(facilities, assets, crosswalk=crosswalk, threshold=threshold)
    accepted = matches[matches["accepted"].astype(bool)]
    result.matched_crosswalk = int((accepted["method"] == "oris_crosswalk").sum())
    result.matched_geo_name = int((accepted["method"] == "geo_name").sum())
    result.held_below_threshold = int((~matches["accepted"].astype(bool)).sum())
    matched_ids = set(matches["ghgrp_facility_id"].astype(str))
    result.held_no_candidate = int((~facilities["ghgrp_facility_id"].astype(str).isin(matched_ids)).sum())
    result.matched_by_asset_type = {k: int(v) for k, v in accepted["asset_type"].value_counts().items()}

    now = utcnow()
    fac_by_id = {str(r["ghgrp_facility_id"]): r for r in facilities.to_dict("records")}

    # ---- attributes on the existing rows
    for m in accepted.to_dict("records"):
        asset = session.get(Asset, _uuid.UUID(str(m["asset_id"])))
        if asset is None:
            continue
        f = fac_by_id[str(m["ghgrp_facility_id"])]
        attrs = dict(asset.attributes or {})
        attrs[ATTRIBUTE_KEY] = _facility_attributes(f, m, licence_id=source.licence_id)
        asset.attributes = attrs
        asset.last_changed = now
        result.assets_updated += 1
    session.flush()

    # ---- owner edges, one per (asset, organisation)
    owners = build_owner_rows(facilities, accepted)
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in owners.to_dict("records"):
        raw_owner = str(row.get("owner_name") or "").strip()
        if not raw_owner:
            continue
        if raw_owner.lower() in PLACEHOLDER_OWNER_NAMES:
            result.rows_skipped_placeholder_owner += 1
            continue
        groups[(str(row["asset_id"]), org_key(raw_owner))].append(row)
    flagged = {
        str(r["ghgrp_facility_id"]) for r in owners.to_dict("records") if r.get("share_flag") == "not_100"
    }
    result.facilities_shares_not_100 = len(flagged)

    asset_ids = sorted({aid for aid, _ in groups})
    existing: dict[tuple[str, str], AssetOwner] = {
        (str(e.asset_id), str(e.organization_id)): e
        for e in session.scalars(
            select(AssetOwner).where(
                AssetOwner.source_id == source.id,
                AssetOwner.role == "owner",
                AssetOwner.asset_id.in_([_uuid.UUID(a) for a in asset_ids]),
            )
        )
    }
    norm_index = _build_norm_org_index(session)
    created_counter = [0]
    seen_org_ids: set[object] = set()
    merged_facilities: set[str] = set()

    for (asset_id, _key), rows in groups.items():
        if len(rows) > 1:
            merged_facilities.update(str(r["ghgrp_facility_id"]) for r in rows)
        shares = [
            float(r["share_pct"])
            for r in rows
            if r.get("share_pct") is not None and not pd.isna(r["share_pct"])
        ]
        share_pct = round(sum(shares), 3) if shares else None
        owner_name_raw = sorted(str(r["owner_name"]).strip() for r in rows)[0]
        as_of_values = [r["as_of"] for r in rows if r.get("as_of") is not None and not pd.isna(r["as_of"])]
        as_of = max(as_of_values) if as_of_values else None
        created_before = created_counter[0]
        org = _resolve_organization(
            session, norm_index, owner_name_raw, source=source, now=now, created_counter=created_counter
        )
        if created_counter[0] == created_before and org.id not in seen_org_ids:
            result.organizations_matched += 1
        seen_org_ids.add(org.id)
        retrieved_at = max(
            (d for r in rows if (d := _to_datetime(r.get("retrieved_at"))) is not None), default=now
        )
        edge = existing.get((asset_id, str(org.id)))
        if edge is None:
            edge = AssetOwner(
                asset_id=_uuid.UUID(asset_id),
                organization_id=org.id,
                role="owner",
                source_id=source.id,
                licence_id=source.licence_id,
            )
            session.add(edge)
            existing[(asset_id, str(org.id))] = edge
        edge.share_pct = share_pct
        edge.as_of = as_of
        edge.owner_name_raw = owner_name_raw
        edge.source_url = str(rows[0].get("source_url") or source.url)
        edge.retrieved_at = retrieved_at
        result.edges_written += 1
        if share_pct is None:
            result.edges_without_share += 1

    result.facilities_parents_merged = len(merged_facilities)
    result.organizations_created = created_counter[0]
    session.commit()
    return result, matches


def load_ghgrp_parquet(
    session: Session,
    path: pathlib.Path,
    *,
    crosswalk_path: pathlib.Path | None = DEFAULT_CROSSWALK,
    threshold: float = DEFAULT_THRESHOLD,
) -> tuple[GhgrpLoadResult, pd.DataFrame]:
    df = pd.read_parquet(path)
    crosswalk = load_crosswalk(crosswalk_path) if crosswalk_path and crosswalk_path.exists() else None
    return load_ghgrp(session, df, crosswalk=crosswalk, threshold=threshold)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=pathlib.Path, required=True)
    parser.add_argument("--db", type=pathlib.Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--crosswalk", type=pathlib.Path, default=DEFAULT_CROSSWALK)
    parser.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD)
    parser.add_argument("--matches-out", type=pathlib.Path, default=None, help="Write the match frame here")
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
        result, matches = load_ghgrp_parquet(
            session, args.parquet, crosswalk_path=args.crosswalk, threshold=args.threshold
        )
    finally:
        session.close()
    if args.matches_out is not None and len(matches):
        args.matches_out.parent.mkdir(parents=True, exist_ok=True)
        matches.to_parquet(args.matches_out, index=False)
    elapsed = round(time.monotonic() - t0, 2)
    print(json.dumps({**result.as_report(), "elapsed_s": elapsed}))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
