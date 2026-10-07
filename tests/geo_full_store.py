"""A deterministic, full-scale proposal store for the map's latency budget (docs/04 D-13), built
from committed data only so it runs in CI, where the git-ignored `data/normalized` is absent.

It reproduces the shape of the full dev store (`web/dev_up.build_store` over every
`PROPOSAL_SOURCE_IDS` source, measured 2026-10-07), not its content:

- 10,654 public proposals over eight sources in the dev store's per-source counts and placement
  grades (queue-like sources at county or state centroids, an inventory and two permit registers
  at exact points, a GB register with most rows unplaced, a CCS register), plus 532 merged-away
  unpublished rows: 10,636 public on the dev store.
- Two of the eight licences withhold raw publication (the CAISO/NYISO role), so ~4,000 rows take
  the `precision_reason: licence` path, and 20 exact rows under such a licence take the
  restricted-precision downgrade (`services/api/geo.py::effective_placement`).
- About 4% of public proposals are merges with two to four links (dev store: 4.2%), whose
  `field_provenance` names the members, `capacity_mw` through `source_ids`.
- The dev store's lifecycle mix (30% withdrawn, 17% built ...) and row widths
  (`field_provenance` ~1.5 KB a row, a link's `raw` ~0.9 KB), because the SQL cost is mostly
  reading those rows.

Points are the vendored county gazetteer's centroids (`services/ingest/data/
us_county_centroids.tsv`), jittered for exact rows, so clustering and region grouping see real
geography. Inserted with Core `executemany` (a few seconds), then `ANALYZE`, as `build_store`
ends.
"""

from __future__ import annotations

import csv
import datetime as dt
import random
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from services.db.models import Licence, Location, Proposal, ProposalSource, Source
from services.ids import public_id

UTC = dt.UTC
SEED = 20261007
GAZETTEER = Path(__file__).resolve().parents[1] / "services" / "ingest" / "data" / "us_county_centroids.tsv"
PUBLISHED_AT = dt.datetime(2026, 9, 1, tzinfo=UTC)
RETRIEVED_AT = dt.datetime(2026, 9, 13, 20, 25, 21, tzinfo=UTC)

#: Dev-store lifecycle mix among public proposals (2026-10-07), as weights.
LIFECYCLE_WEIGHTS = {
    "withdrawn": 3211,
    "filed": 1876,
    "built": 1819,
    "studied": 1605,
    "under_construction": 942,
    "announced": 585,
    "permitted": 383,
    "contracted": 190,
    "unknown": 25,
}
TECHNOLOGIES = {
    "generation": ("solar", "wind", "gas_cc", "gas_ct", "nuclear", "hydro", "geothermal"),
    "storage": ("bess_li_ion", "pumped_hydro"),
    "load": ("data_center", "industrial_load"),
    "ccs": ("ccs",),
    "transmission": ("transmission",),
    "other": ("other",),
}


@dataclass(frozen=True)
class SourceShape:
    id: str
    licence: str  # "open" | "attribution" | "derived_only"
    country: str
    states: tuple[str, ...]  # gazetteer states rows are drawn from; empty = anywhere
    placements: dict[str, int]  # precision -> rows
    kinds: dict[str, int]  # kind -> weight


#: Per-source counts and placement grades of the dev store's public proposals (2026-10-07).
SOURCES: tuple[SourceShape, ...] = (
    SourceShape("syn.queue.open", "open", "US", ("TX",), {"county_centroid": 1556},
                {"generation": 922, "storage": 852, "other": 4}),
    SourceShape("syn.queue.derived_a", "derived_only", "US", ("CA",),
                {"county_centroid": 2177, "state_centroid": 35, "unknown": 18, "exact": 20},
                {"generation": 1756, "storage": 511, "other": 11}),
    SourceShape("syn.queue.derived_b", "derived_only", "US", ("NY",),
                {"county_centroid": 1414, "state_centroid": 342, "unknown": 6},
                {"generation": 862, "storage": 534, "other": 215, "transmission": 203}),
    SourceShape("syn.inventory", "open", "US", (), {"exact": 2236},
                {"generation": 1854, "storage": 446, "other": 11}),
    SourceShape("syn.gb_register", "attribution", "GB", (), {"county_centroid": 739, "unknown": 1459},
                {"generation": 1331, "storage": 824, "other": 43}),
    SourceShape("syn.air_permits", "open", "US", (),
                {"exact": 374, "county_centroid": 92, "state_centroid": 6}, {"load": 1}),
    SourceShape("syn.datacentres", "open", "US", ("VA",), {"exact": 110}, {"load": 1}),
    SourceShape("syn.ccs", "open", "US", ("TX", "LA", "ND", "WY", "IL", "CA"),
                {"county_centroid": 61, "state_centroid": 5, "unknown": 2}, {"ccs": 1}),
)  # fmt: skip
#: Survivors that absorbed another register's rows (dev store: 404 of 10,636 with 2+ links).
MERGED_SURVIVORS = 404
#: Merged-away rows: unpublished, `merged_into_id` set, links moved to the survivor.
MERGED_AWAY = 532
#: Distinct GB connection sites the GB rows sit on (dev store: 223).
GB_SITES = 223
PROVENANCE_FIELDS = (
    "kind",
    "name_canonical",
    "technology",
    "technology_raw",
    "capacity_mw",
    "jurisdiction",
    "iso",
    "lifecycle_state",
    "status_raw",
    "identifiers",
    "proposed_online_date",
)


def _gazetteer() -> list[tuple[str, str, float, float, str]]:
    with GAZETTEER.open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh, delimiter="\t"))
    return [
        (
            r["state"],
            r["county_name"].removesuffix(" County"),
            float(r["lon"]),
            float(r["lat"]),
            r["county_fips"],
        )
        for r in rows
    ]


def _licence_row(source: SourceShape) -> dict[str, Any]:
    derived_only = source.licence == "derived_only"
    return {
        "id": f"{source.id}#lic",
        "name": f"{source.id} terms",
        "url": f"https://example.org/{source.id}/terms",
        "reuse_class": "open" if source.licence == "open" else "attribution",
        "attribution_required": source.licence != "open",
        "attribution_text": None if source.licence == "open" else f"Source: {source.id}",
        "requires_link_back": source.licence != "open",
        "allows_derived_publication": True,
        "allows_raw_publication": not derived_only,
        "allows_api_redistribution": True,
        "allows_bulk_export": True,
        "allows_commercial_use": True,
        "gate_flag": False,
        "evidence_url": "https://example.org/terms",
        "evidence_retrieved_at": dt.datetime(2026, 9, 1, tzinfo=UTC),
        "classified_by": "legal-compliance",
    }


def _source_row(source: SourceShape) -> dict[str, Any]:
    return {
        "id": source.id,
        "name": f"Synthetic {source.id}",
        "category": "generation_queue",
        "jurisdiction": source.country,
        "operator": f"Operator of {source.id}",
        "url": f"https://example.org/{source.id}",
        "access": "bulk_file",
        "cadence": "weekly",
        "licence_id": f"{source.id}#lic",
        "publish_state": "public",
        "manifest_version": "2026-09-12",
        "manifest_hash": "0" * 64,
    }


def _ids(rng: random.Random) -> Iterator[uuid.UUID]:
    while True:
        yield uuid.UUID(int=rng.getrandbits(128), version=4)


def _pick(rng: random.Random, weights: dict[str, int]) -> str:
    return rng.choices(list(weights), weights=list(weights.values()))[0]


def build(session: Session) -> dict[str, int]:
    """Insert the store into `session`'s (empty, schema-created) database and commit. Returns the
    counts a test asserts the shape on."""
    rng = random.Random(SEED)  # noqa: S311 - deterministic fixture, not security
    ids = _ids(rng)
    gaz = _gazetteer()
    by_state: dict[str, list[tuple[str, str, float, float, str]]] = {}
    for row in gaz:
        by_state.setdefault(row[0], []).append(row)
    state_centroids = {
        st: (sum(r[2] for r in rows) / len(rows), sum(r[3] for r in rows) / len(rows))
        for st, rows in by_state.items()
    }
    gb_sites = [
        (round(rng.uniform(-5.4, 1.6), 3), round(rng.uniform(50.3, 57.6), 3)) for _ in range(GB_SITES)
    ]

    session.execute(sa.insert(Licence), [_licence_row(s) for s in SOURCES])
    session.execute(sa.insert(Source), [_source_row(s) for s in SOURCES])

    locations: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    links: list[dict[str, Any]] = []
    raw_pad = {f"Column {k:02d}": f"value {k:02d} of a register row" for k in range(22)}
    counter = iter(range(1, 1_000_000))

    def add(source: SourceShape, precision: str) -> dict[str, Any]:
        """One proposal with its location and its link; returns the proposal row."""
        n = next(counter)
        licence_id = f"{source.id}#lic"
        pool = [r for st in source.states for r in by_state[st]] if source.states else gaz
        state, county, lon, lat, fips = rng.choice(pool)
        geom: tuple[float, float] | None
        if source.country == "GB":
            state = county = fips = ""
            geom = rng.choice(gb_sites) if precision != "unknown" else None
        elif precision == "exact":
            geom = (round(lon + rng.uniform(-0.3, 0.3), 5), round(lat + rng.uniform(-0.3, 0.3), 5))
        elif precision == "county_centroid":
            geom = (lon, lat)
        elif precision == "state_centroid":
            geom = state_centroids[state]
        else:
            geom = None
        loc_id = next(ids)
        gb_site_name = f"GB site {n % GB_SITES}" if source.country == "GB" else None
        locations.append(
            {
                "id": loc_id,
                "kind": "point" if precision == "exact" else "county",
                "geom": geom,
                "precision": precision,
                "precision_reason": "licence" if source.licence == "derived_only" else None,
                "county_fips": fips if precision == "county_centroid" and fips else None,
                "county_name": county or gb_site_name,
                "state_code": f"{source.country}-{state}" if state else None,
                "country": source.country,
                "source_id": source.id,
                "source_url": f"https://example.org/{source.id}",
                "retrieved_at": RETRIEVED_AT,
                "licence_id": licence_id,
            }
        )
        kind = _pick(rng, source.kinds)
        name = f"Synthetic {kind} project {n}"
        technology = rng.choice(TECHNOLOGIES[kind])
        lifecycle = _pick(rng, LIFECYCLE_WEIGHTS)
        pid = next(ids)
        capacity = round(rng.uniform(1, 1200), 1)
        provenance = {
            f: {
                "source_id": source.id,
                "licence_id": licence_id,
                "retrieved_at": "2026-09-13T20:25:21+00:00",
                "rule": "single_source",
            }
            for f in PROVENANCE_FIELDS
        }
        proposals.append(
            {
                "id": pid,
                "public_id": public_id("prop", pid),
                "slug": f"synthetic-{n}",
                "kind": kind,
                "name_canonical": name,
                "technology": technology,
                "technology_raw": technology.replace("_", " ").title(),
                "capacity_mw": capacity,
                "jurisdiction": f"{source.country}-{state}" if state else source.country,
                "iso": source.id.rsplit(".", 1)[-1].upper(),
                "location_id": loc_id,
                "lifecycle_state": lifecycle,
                "status_raw": f"Status {lifecycle}",
                "identifiers": {"queue_id": f"Q{n:05d}"},
                "first_seen": RETRIEVED_AT - dt.timedelta(days=n % 900),
                "last_changed": RETRIEVED_AT - dt.timedelta(days=n % 90),
                "publish_state": "public",
                "published_at": PUBLISHED_AT,
                "public_at": PUBLISHED_AT,
                "min_reuse_class": "open" if source.licence == "open" else "attribution",
                "field_provenance": provenance,
                "overrides": {},
                "source_count": 1,
            }
        )
        links.append(
            {
                "id": next(ids),
                "proposal_id": pid,
                "source_id": source.id,
                "source_record_id": f"{source.id}:{n}",
                "source_url": f"https://example.org/{source.id}#{n}",
                "retrieved_at": RETRIEVED_AT,
                "licence_id": licence_id,
                "raw": {**raw_pad, "Capacity (MW)": capacity, "Status": lifecycle},
                "normalised": {"name_canonical": name, "capacity_mw": capacity},
                "first_seen": RETRIEVED_AT - dt.timedelta(days=30),
                "last_seen": RETRIEVED_AT,
            }
        )
        return proposals[-1]

    for source in SOURCES:
        for precision, count in source.placements.items():
            for _ in range(count):
                add(source, precision)

    # Merges: every survivor (an inventory row, exact point) absorbs one to three queue rows, which
    # are then unpublished with `merged_into_id` set and their links moved, as the resolver does.
    queues = [s for s in SOURCES if s.id.startswith("syn.queue.")]
    absorbed = [
        add(rng.choices(queues, weights=[sum(q.placements.values()) for q in queues])[0], "county_centroid")
        for _ in range(MERGED_AWAY)
    ]
    inventory = [p for p, lk in zip(proposals, links, strict=True) if lk["source_id"] == "syn.inventory"]
    survivors = rng.sample(inventory, MERGED_SURVIVORS)
    link_of = {lk["proposal_id"]: lk for lk in links}
    it = iter(absorbed)
    for i, survivor in enumerate(survivors):
        # 404 survivors x (1 + every 4th + every 15th) = exactly the 532 absorbed rows.
        members = [next(it) for _ in range(1 + (i % 4 == 0) + (i % 15 == 0))]
        member_sources = []
        for m in members:
            m["publish_state"] = "unpublished"
            m["merged_into_id"] = survivor["id"]
            link = link_of[m["id"]]
            link["proposal_id"] = survivor["id"]
            member_sources.append(link["source_id"])
        survivor["source_count"] = 1 + len(members)
        survivor["field_provenance"]["capacity_mw"] = {
            "source_id": member_sources[0],
            "source_ids": sorted({"syn.inventory", *member_sources}),
            "licence_id": "syn.inventory#lic",
            "retrieved_at": "2026-09-13T20:25:21+00:00",
            "rule": "interconnection_request",
        }
        # The strictest member licence (docs/22 §23): attribution once a derived-only queue joins.
        if any(sid != "syn.queue.open" for sid in member_sources):
            survivor["min_reuse_class"] = "attribution"
    assert next(it, None) is None, "every absorbed row has a survivor"
    for p in proposals:
        p.setdefault("merged_into_id", None)

    # Proposals reference their survivor, so survivors go in first.
    session.execute(sa.insert(Location), locations)
    session.execute(sa.insert(Proposal), [p for p in proposals if p["merged_into_id"] is None])
    session.execute(sa.insert(Proposal), [p for p in proposals if p["merged_into_id"] is not None])
    session.execute(sa.insert(ProposalSource), links)
    session.commit()
    session.execute(sa.text("ANALYZE"))
    session.commit()
    public = sum(1 for p in proposals if p["publish_state"] == "public")
    return {
        "public": public,
        "merged_away": len(absorbed),
        "merged_survivors": sum(1 for p in proposals if p["source_count"] > 1),
        "sources": len(SOURCES),
        "unplaced": sum(1 for loc in locations if loc["geom"] is None),
    }
