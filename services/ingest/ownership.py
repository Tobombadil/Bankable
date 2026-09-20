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
one row per `(plant, generator, owner)`; this loader groups by `(source_plant_id, org_key(owner_name))`
and reduces the group to a single `share_pct`, in this order:

- **Share of plant nameplate** when the parquet carries `generator_capacity_mw` and
  `plant_capacity_mw` (written by `pipeline/context/eia_owners.py` from the same zip's Schedule 3
  "Operable" sheet since 2026-09-19): `share_pct = sum(ownership_pct_g * nameplate_g) /
  plant_nameplate` over the owner's generator rows that have both a share and an operable
  nameplate, where `plant_nameplate` is the sum over **all** operable generators of the plant,
  listed on Schedule 4 or not. Schedule 4 lists jointly or third-party owned generators only, so
  the shares of a plant's listed owners sum to less than 100 %: the remainder is the operator's
  wholly-owned capacity, left implicit (no edge is invented for it). An owner whose rows at a
  plant are all retired, cancelled or proposed generators (no operable nameplate) gets an edge
  with a NULL share -- the ownership is a fact, its share of operating capacity is not stated.
- **Unweighted mean** of the group's non-null `ownership_pct` otherwise (a parquet without the
  capacity columns, e.g. one built from the trimmed test fixture).

`OwnershipLoadResult` counts which path each edge took. `as_of` is the group's latest reporting
date; `owner_name_raw` keeps one representative raw spelling (the group's alphabetically-first, for
determinism) for audit.

**Over-allocated generators are flagged, not normalised.** The listed shares of one generator should
sum to at most 100; where they exceed it by more than `OVER_ALLOCATION_TOLERANCE_PCT` the generator is
recorded in `OwnershipLoadResult.generators_over_100` (and logged) and its rows are used as stated --
the registry's arithmetic is the registry's to correct, and a silent rescale would hide it from the
company page. The 2025 release has two such generators (plant 341 CT5 at 150 %, plant 70387 BESS1
at 100.09 %).

`owner_name` resolves to an `organization` through the existing alias table
(`organization_alias`), matched on `pipeline.normalize.org_key` -- the deterministic,
legal-form-and-punctuation-stripping key `services/resolve/merge.py` already uses to decide two
filings name the same organisation, not a raw case-fold (coordinator correction, 2026-09-18: a
case-fold-only key was measured to over-split organisations 8.2% versus that key on the eval
corpus). Since 2026-09-19 the key strips legal forms *only*: it used to strip industry and
geography words too, which merged 258 pairs of distinct legal entities across this file's owner
strings and the dev store -- `MidAmerican Energy Co` with `MidAmerican Solar LLC`, `Shell
Renewables` with `Shell Wind Energy Inc.` -- and put one company's plants on another's page
(the measured census is docs/22 §16). No match creates a new organisation plus a `filing_spelling` alias row
(`created_by="pipeline"`, `confidence=0.9` -- see `_resolve_organization` for why this is lower
than `services/ingest/loader.py`'s exact/punctuation-match `1.0`). `_build_norm_org_index` builds
the whole lookup once per call rather than a per-row query, since this loader's input (one
ownership run a year) does not need `services/ingest/loader.py::_get_or_create_organization`'s
heavier proposal-loader bulk cache.

The group key is `org_key(owner_name)`, the same key the resolver uses, so two spellings of one
owner at the same plant ("NextEra Energy Resources, LLC" and "NEXTERA ENERGY RESOURCES LLC")
aggregate into one edge rather than the second overwriting the first (fixed 2026-09-19; the
2026-09-18 version keyed on the raw casefold and recorded this as a known limitation).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import pathlib
import time
from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.normalize import org_key as _org_key
from services.db.models import Asset, AssetOwner, Organization, OrganizationAlias, Source, new_uuid
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id as make_public_id
from services.ids import slugify
from services.ingest.loader import upsert_licence_and_source

DEFAULT_DB_PATH = pathlib.Path("web/.data/dev.db")
OWNERSHIP_SOURCE_ID = "us.eia.860"
PLANT_SOURCE_ID = "us.eia.860m"
#: Generator share sums above 100 + this many percentage points are flagged (module docstring).
OVER_ALLOCATION_TOLERANCE_PCT = 0.05
#: Owner strings that name nobody. EIA-860 files unnamed minority owners as "Other" with
#: Ownership ID 99999 and no address (35 rows on 11 plants in the 2025 release); an edge to an
#: organisation called "Other" would be noise on every company page, so these rows are counted
#: in `rows_skipped_placeholder_owner` and not attributed.
PLACEHOLDER_OWNER_NAMES = frozenset({"other", "others", "n/a", "na", "none", "unknown", "various"})

log = logging.getLogger(__name__)


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass
class OwnershipLoadResult:
    rows_seen: int = 0
    rows_skipped_placeholder_owner: int = 0
    assets_matched: int = 0
    unmatched_plant_ids: list[str] = field(default_factory=list)
    organizations_created: int = 0
    organizations_matched: int = 0
    edges_written: int = 0
    edges_nameplate_weighted: int = 0
    edges_unweighted_mean: int = 0
    edges_without_share: int = 0
    generators_over_100: dict[str, float] = field(default_factory=dict)

    def as_report(self) -> dict[str, object]:
        return {
            "rows_seen": self.rows_seen,
            "rows_skipped_placeholder_owner": self.rows_skipped_placeholder_owner,
            "assets_matched": self.assets_matched,
            # A sample, not the whole list: a real run leaves ~219 plants unmatched (retired,
            # cancelled or proposed units EIA-860M does not carry), and `web/dev_up.py` logs this
            # report as one line. The full list stays on the dataclass for a caller that wants it.
            "unmatched_plant_ids_sample": sorted(set(self.unmatched_plant_ids))[:10],
            "unmatched_plant_count": len(set(self.unmatched_plant_ids)),
            "organizations_created": self.organizations_created,
            "organizations_matched": self.organizations_matched,
            "edges_written": self.edges_written,
            "edges_nameplate_weighted": self.edges_nameplate_weighted,
            "edges_unweighted_mean": self.edges_unweighted_mean,
            "edges_without_share": self.edges_without_share,
            "generators_over_100": dict(sorted(self.generators_over_100.items())),
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


AGGREGATION_METHODS = ("nameplate_share", "unweighted_mean", "none")


def _aggregate_group(rows: list[dict[str, object]]) -> tuple[float | None, str, dt.date | None, str]:
    """`(share_pct, method, as_of, owner_name_raw)` for one `(plant, owner)` group -- the module
    docstring's aggregation rule. `method` is one of `AGGREGATION_METHODS`."""
    plant_mw = next(
        (mw for r in rows if (mw := _to_float(r.get("plant_capacity_mw"))) is not None and mw > 0), None
    )
    weighted: list[tuple[float, float]] = []
    pcts: list[float] = []
    for r in rows:
        pct = _to_float(r.get("ownership_pct"))
        if pct is None:
            continue
        pcts.append(pct)
        gen_mw = _to_float(r.get("generator_capacity_mw"))
        if gen_mw is not None and gen_mw > 0:
            weighted.append((pct, gen_mw))

    share_pct: float | None
    if plant_mw is not None:
        if weighted:
            share_pct = sum(pct * mw for pct, mw in weighted) / plant_mw
            method = "nameplate_share"
        else:
            share_pct, method = None, "none"
    elif pcts:
        share_pct, method = sum(pcts) / len(pcts), "unweighted_mean"
    else:
        share_pct, method = None, "none"

    as_of_values = [d for r in rows if (d := _to_date(r.get("as_of"))) is not None]
    as_of = max(as_of_values) if as_of_values else None

    owner_name_raw = sorted(str(r["owner_name"]).strip() for r in rows)[0]
    return share_pct, method, as_of, owner_name_raw


def find_over_allocated_generators(
    df: pd.DataFrame, *, tolerance_pct: float = OVER_ALLOCATION_TOLERANCE_PCT
) -> dict[str, float]:
    """`{"<plant>/<generator>": summed_pct}` for every generator whose listed shares sum above
    `100 + tolerance_pct`. Reported, never corrected (module docstring)."""
    if not len(df) or "ownership_pct" not in df.columns:
        return {}
    pct = pd.to_numeric(df["ownership_pct"], errors="coerce")
    sums = (
        df.assign(_pct=pct)
        .groupby([df["source_plant_id"].astype(str), df["generator_id"].astype(str)])["_pct"]
        .sum(min_count=1)
    )
    over = sums[sums > 100.0 + tolerance_pct]
    return {f"{plant}/{gen}": round(float(v), 3) for (plant, gen), v in over.items()}


#: The resolver key for an organisation string. Since 2026-09-19 this lane's helper *is*
#: `pipeline.normalize.org_key` -- one function for the index, the lookup, the aggregation group
#: key and every other consumer (docs/22 §16). Re-exported under the old name so this module's
#: callers and tests keep working.
org_key = _org_key


def _build_norm_org_index(session: Session) -> dict[str, Organization]:
    """`pipeline.normalize.org_key(name)` -> the first `Organization` seen with that key, scanned
    once per `load_owner_shares` call (coordinator correction, 2026-09-18): a casefold-only key
    over-splits organisations whose spellings differ by corporate suffix or punctuation alone
    (measured 8.2% over-split on the eval corpus vs. that key) -- the same deterministic,
    legal-form-stripping key `services/resolve/merge.py` already uses to decide two filings name
    the same organisation. Built from both `organization.name_canonical` and every
    `organization_alias.alias` so a raw spelling seen only as an alias still resolves."""
    index: dict[str, Organization] = {}
    for org in session.scalars(select(Organization)):
        key = org_key(org.name_canonical)
        if key:
            index.setdefault(key, org)
    for alias, org_id in session.execute(
        select(OrganizationAlias.alias, OrganizationAlias.organization_id)
    ).all():
        key = org_key(alias)
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
    """Resolves `raw_name` to an organisation via `norm_index` (module docstring: `org_key`, not
    a raw casefold), creating a new organisation plus a `filing_spelling` alias only when no
    existing organisation or alias normalises to the same key. `norm_index` is both the lookup and
    the write-through cache for this call: a newly created organisation is added to it immediately,
    so two owner names in the same run that share an `org_key` (e.g. "NextEra Energy
    Resources, LLC" and "NEXTERA ENERGY RESOURCES LLC") resolve to the same row rather than
    creating it twice.
    """
    key = org_key(raw_name)
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
                # made on the looser `org_key` legal-form-stripped key, not an exact or
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
    result.generators_over_100 = find_over_allocated_generators(df)
    if result.generators_over_100:
        log.warning(
            "ownership: %d generator(s) with listed shares over 100 %% left as stated: %s",
            len(result.generators_over_100),
            result.generators_over_100,
        )
    groups: dict[tuple[str, str], list[dict[str, object]]] = defaultdict(list)
    for row in df.to_dict("records"):
        plant_id = str(row.get("source_plant_id"))
        raw_owner = str(row.get("owner_name") or "").strip()
        if not raw_owner:
            continue  # a share with no owner name cannot be attributed to anyone
        if raw_owner.lower() in PLACEHOLDER_OWNER_NAMES:
            result.rows_skipped_placeholder_owner += 1
            continue
        groups[(plant_id, org_key(raw_owner))].append(row)

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
    seen_org_ids: set[object] = set()

    for (plant_id, _owner_key), rows in groups.items():
        asset = assets.get(plant_id)
        if asset is None:
            continue

        share_pct, method, as_of, owner_name_raw = _aggregate_group(rows)
        created_before = created_counter[0]
        org = _resolve_organization(
            session, norm_index, owner_name_raw, source=source, now=now, created_counter=created_counter
        )
        if created_counter[0] == created_before and org.id not in seen_org_ids:
            result.organizations_matched += 1
        seen_org_ids.add(org.id)
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
        if method == "nameplate_share":
            result.edges_nameplate_weighted += 1
        elif method == "unweighted_mean":
            result.edges_unweighted_mean += 1
        else:
            result.edges_without_share += 1

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
