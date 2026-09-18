"""Loader for EIA-860 Schedule 4 ownership shares (docs/21 §3.23, ADR 0008) -> `asset_owner`.

Reads a parquet of generator-level ownership rows (`source_plant_id`, `generator_id`,
`owner_name`, `ownership_pct` 0-100, `as_of`, `source_url`, `retrieved_at`, `licence`) and writes
one `asset_owner` row per `(plant, owner)`, `role="owner"`, against the `asset` row loaded by
`services/ingest/plants.py` (`source_id="us.eia.860m"`, `source_asset_id=source_plant_id`) --
never against a `source_plant_id` that has no matching asset yet (counted as unmatched, not
guessed at). The edge's own provenance quartet is `us.eia.860` (EIA-860 Schedule 4 Owner, the
registry this loader reads), upserted the same way `services/ingest/loader.py`'s
`upsert_licence_and_source` does for every other connector.

**Aggregation rule (generator-level shares -> one plant-level share per owner).** Schedule 4 gives
one row per `(plant, generator, owner)`; this loader groups by `(source_plant_id, owner_name)` and
reduces the group's `ownership_pct` values to a single `share_pct`:

- **Nameplate-weighted mean** when a per-generator nameplate weight is available. This parquet
  schema (per this task's brief) carries none -- Schedule 4's `4___Owner` sheet has no generator
  capacity column of its own, and this loader does not join out to another sheet for one. An
  optional `generator_capacity_mw` column is honoured if present (`_row_weight`), so the weighted
  path activates automatically the day a caller adds one, with no signature change; until then
  every row's weight is `1.0`.
- **Unweighted mean** otherwise -- which, given the above, is what this sprint's loader actually
  computes: `share_pct = mean(ownership_pct over the group's generator rows)`. `as_of` is the
  group's latest reporting date; `owner_name_raw` keeps one representative raw spelling (the
  group's alphabetically-first, for determinism) for audit.

`owner_name` resolves to an `organization` through the existing alias table
(`organization_alias`), matched on `pipeline.normalize.norm_org` -- the deterministic,
corp-suffix-and-punctuation-stripping key `services/resolve/merge.py` already uses to decide two
filings name the same organisation, not a raw case-fold (coordinator correction, 2026-09-18: a
case-fold-only key was measured to over-split organisations 8.2% versus `norm_org` on the eval
corpus). No match creates a new organisation plus a `filing_spelling` alias row
(`created_by="pipeline"`, `confidence=0.9` -- see `_resolve_organization` for why this is lower
than `services/ingest/loader.py`'s exact/punctuation-match `1.0`). `_build_norm_org_index` builds
the whole lookup once per call rather than a per-row query, since this loader's input (one
ownership run a year) does not need `services/ingest/loader.py::_get_or_create_organization`'s
heavier proposal-loader bulk cache.

Known limitation: the group key that feeds the aggregation rule above is still the raw owner
spelling (case-folded), not `norm_org` -- two differently-spelled rows for the *same plant* that
both resolve to one organisation via `norm_org` are aggregated as two separate groups and the
second group's `asset_owner` write overwrites the first's rather than combining both groups'
generator rows into one mean. Not hit by any fixture in this codebase's ownership data so far
(`services/ingest/test_ownership.py` covers the cross-plant case only); recorded here rather than
silently assumed correct.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import pathlib
import time
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.normalize import norm_org
from services.db.models import Asset, AssetOwner, Organization, OrganizationAlias, Source, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id as make_public_id
from services.ids import slugify
from services.ingest.loader import upsert_licence_and_source

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")
OWNERSHIP_SOURCE_ID = "us.eia.860"
PLANT_SOURCE_ID = "us.eia.860m"


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class OwnershipLoadResult:
    rows_seen: int = 0
    assets_matched: int = 0
    unmatched_plant_ids: list[str] = field(default_factory=list)
    organizations_created: int = 0
    edges_written: int = 0

    def as_report(self) -> dict[str, object]:
        return {
            "rows_seen": self.rows_seen,
            "assets_matched": self.assets_matched,
            "unmatched_plant_ids": sorted(set(self.unmatched_plant_ids)),
            "organizations_created": self.organizations_created,
            "edges_written": self.edges_written,
        }


def _none_if_missing(value: object) -> object | None:
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    if value is pd.NA or value is pd.NaT:
        return None
    return value


def _to_float(value: object) -> float | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    try:
        f = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def _to_date(value: object) -> dt.date | None:
    value = _none_if_missing(value)
    if value is None:
        return None
    ts = pd.Timestamp(value)
    return None if pd.isna(ts) else ts.date()


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


def _row_weight(row: dict[str, object]) -> float:
    """`1.0` unless the row carries an optional `generator_capacity_mw` (module docstring's
    forward-compatible nameplate-weighting hook)."""
    w = _to_float(row.get("generator_capacity_mw"))
    return w if w is not None and w > 0 else 1.0


def _aggregate_group(rows: list[dict[str, object]]) -> tuple[float | None, dt.date | None, str]:
    weights_and_pcts: list[tuple[float, float]] = []
    for r in rows:
        pct = _to_float(r.get("ownership_pct"))
        if pct is not None:
            weights_and_pcts.append((_row_weight(r), pct))
    if weights_and_pcts:
        total_weight = sum(w for w, _ in weights_and_pcts)
        share_pct = sum(w * pct for w, pct in weights_and_pcts) / total_weight
    else:
        share_pct = None

    as_of_values = [d for r in rows if (d := _to_date(r.get("as_of"))) is not None]
    as_of = max(as_of_values) if as_of_values else None

    owner_name_raw = sorted(str(r["owner_name"]).strip() for r in rows)[0]
    return share_pct, as_of, owner_name_raw


def _build_norm_org_index(session: Session) -> dict[str, Organization]:
    """`pipeline.normalize.norm_org(name)` -> the first `Organization` seen with that key, scanned
    once per `load_owner_shares` call (coordinator correction, 2026-09-18): a casefold-only key
    over-splits organisations whose spellings differ by corporate suffix or punctuation alone
    (measured 8.2% over-split on the eval corpus vs. `norm_org`) -- the same deterministic,
    corp-suffix-stripping key `services/resolve/merge.py` already uses to decide two filings name
    the same organisation. Built from both `organization.name_canonical` and every
    `organization_alias.alias` so a raw spelling seen only as an alias still resolves."""
    index: dict[str, Organization] = {}
    for org in session.scalars(select(Organization)):
        key = norm_org(org.name_canonical)
        if key:
            index.setdefault(key, org)
    for alias, org_id in session.execute(
        select(OrganizationAlias.alias, OrganizationAlias.organization_id)
    ).all():
        key = norm_org(alias)
        if key and key not in index:
            alias_org = session.get(Organization, org_id)
            if alias_org is not None:
                index[key] = alias_org
    return index


def _resolve_organization(
    session: Session,
    norm_index: dict[str, Organization],
    raw_name: str,
    *,
    source: Source,
    now: dt.datetime,
    created_counter: list[int],
) -> Organization:
    """Resolves `raw_name` to an organisation via `norm_index` (module docstring: `norm_org`, not
    a raw casefold), creating a new organisation plus a `filing_spelling` alias only when no
    existing organisation or alias normalises to the same key. `norm_index` is both the lookup and
    the write-through cache for this call: a newly created organisation is added to it immediately,
    so two owner names in the same run that share a `norm_org` key (e.g. "NextEra Energy
    Resources, LLC" and "NEXTERA ENERGY RESOURCES LLC") resolve to the same row rather than
    creating it twice.
    """
    key = norm_org(raw_name) or raw_name.strip().upper()
    exact_key = raw_name.strip().lower()

    org = norm_index.get(key)
    if org is None:
        org_id = new_uuid()
        slug = slugify(raw_name.strip())
        existing_slugs = set(session.scalars(select(Organization.slug)))
        if slug in existing_slugs:
            slug = f"{slug}-{make_public_id('org', org_id)[-6:].lower()}"
        org = Organization(
            id=org_id,
            public_id=make_public_id("org", org_id),
            slug=slug,
            name_canonical=raw_name.strip(),
            name_normalised=exact_key,
            type="other",
            country="US",
        )
        session.add(org)
        session.flush()
        created_counter[0] += 1
        norm_index[key] = org

    exists_alias = session.scalar(
        select(OrganizationAlias).where(
            OrganizationAlias.organization_id == org.id, OrganizationAlias.alias_normalised == exact_key
        )
    )
    if exists_alias is None:
        session.add(
            OrganizationAlias(
                organization_id=org.id,
                alias=raw_name.strip(),
                alias_normalised=exact_key,
                kind="filing_spelling",
                source_id=source.id,
                source_url=source.url,
                retrieved_at=now,
                licence_id=source.licence_id,
                # 0.9, not the loader's exact/punctuation-match 1.0
                # (`services/ingest/loader.py::_add_organization_alias_if_new`): this match is
                # made on the looser `norm_org` corp-suffix-stripped key, not an exact or
                # punctuation-only spelling match, so it is recorded as slightly less certain
                # (coordinator correction, 2026-09-18).
                confidence=0.9,
                created_by="pipeline",
            )
        )
    return org


def load_owner_shares(
    session: Session, df: pd.DataFrame, *, manifest_version: str = ""
) -> OwnershipLoadResult:
    registry = Registry()
    entry = registry.get(OWNERSHIP_SOURCE_ID)
    source = upsert_licence_and_source(session, entry, manifest_version or registry.version)

    result = OwnershipLoadResult(rows_seen=len(df))
    if not len(df):
        session.commit()
        return result

    now = utcnow()
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in df.to_dict("records"):
        plant_id = str(row.get("source_plant_id"))
        owner_name_key = str(row.get("owner_name") or "").strip().lower()
        if not owner_name_key:
            continue  # a share with no owner name cannot be attributed to anyone
        groups[(plant_id, owner_name_key)].append(row)

    plant_ids = sorted({plant_id for plant_id, _ in groups})
    assets: dict[str, Asset] = {
        a.source_asset_id: a
        for a in session.scalars(
            select(Asset).where(Asset.source_id == PLANT_SOURCE_ID, Asset.source_asset_id.in_(plant_ids))
        )
    }
    result.assets_matched = len({pid for pid in plant_ids if pid in assets})
    result.unmatched_plant_ids = [pid for pid in plant_ids if pid not in assets]

    existing_edges: dict[tuple[str, str, str], AssetOwner] = {
        (str(e.asset_id), str(e.organization_id), e.role): e
        for e in session.scalars(
            select(AssetOwner).where(
                AssetOwner.source_id == source.id, AssetOwner.asset_id.in_([a.id for a in assets.values()])
            )
        )
    }

    norm_index = _build_norm_org_index(session)
    created_counter = [0]

    for (plant_id, _owner_key), rows in groups.items():
        asset = assets.get(plant_id)
        if asset is None:
            continue

        share_pct, as_of, owner_name_raw = _aggregate_group(rows)
        org = _resolve_organization(
            session, norm_index, owner_name_raw, source=source, now=now, created_counter=created_counter
        )
        source_url = next(
            (str(r["source_url"]) for r in rows if _none_if_missing(r.get("source_url"))), source.url
        )
        retrieved_at = max(
            (d for r in rows if (d := _to_datetime(r.get("retrieved_at"))) is not None), default=now
        )

        edge_key = (str(asset.id), str(org.id), "owner")
        edge = existing_edges.get(edge_key)
        if edge is None:
            edge = AssetOwner(
                asset_id=asset.id,
                organization_id=org.id,
                role="owner",
                source_id=source.id,
                licence_id=source.licence_id,
            )
            session.add(edge)
            existing_edges[edge_key] = edge
        edge.share_pct = share_pct
        edge.as_of = as_of
        edge.owner_name_raw = owner_name_raw
        edge.source_url = source_url
        edge.retrieved_at = retrieved_at
        result.edges_written += 1

    result.organizations_created = created_counter[0]
    session.commit()
    return result


def load_owner_shares_parquet(session: Session, path: pathlib.Path) -> OwnershipLoadResult:
    df = pd.read_parquet(path)
    return load_owner_shares(session, df)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--parquet", type=pathlib.Path, required=True)
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
        result = load_owner_shares_parquet(session, args.parquet)
    finally:
        session.close()
    elapsed = round(time.monotonic() - t0, 2)
    print(json.dumps({**result.as_report(), "elapsed_s": elapsed}))  # noqa: T201 — CLI summary line


if __name__ == "__main__":
    main()
