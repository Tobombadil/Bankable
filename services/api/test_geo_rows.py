"""`GET /v1/proposals/geo` read through plain columns (docs/CHANGELOG.md, 2026-10-07): the
column-level rows draw exactly what the ORM entities drew, a drawn row whose printed fields came
from a hidden source is drawn from its served view in every feature kind, the licence summary
merges its chunks exactly, and individual markers cost a fixed number of queries.
"""

from __future__ import annotations

import datetime as dt
import gc
import json
from collections import Counter
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker
from starlette.requests import Request

from services.api import gc_tuning, geo, geo_cache, records
from services.api.conftest import (
    make_attribution_licence,
    make_location,
    make_open_licence,
    make_public_source,
    make_visible_proposal,
)
from services.api.geo import GeoRow, build_geo_feature_collection
from services.db.models import Location, Proposal, ProposalSource, Source

UTC = dt.UTC
WORLD = "-179,-85,179,85"


def _request(query: str) -> Request:
    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/v1/proposals/geo",
            "query_string": query.encode(),
            "headers": [],
            "scheme": "http",
            "server": ("test", 80),
            "root_path": "",
        }
    )


def _seed_mixed(db: Session) -> None:
    """Every placement path the map rules distinguish, under three licences."""
    open_lic = make_open_licence(db)
    derived = make_attribution_licence(db, id_="derived-lic")
    derived.allows_raw_publication = False
    src = make_public_source(db, open_lic)
    derived_src = make_public_source(db, derived, id_="us.test.derived_only")
    n = 0

    def add(source: Source, lic: Any, **loc: Any) -> None:
        nonlocal n
        n += 1
        location = make_location(db, source, lic, **loc)
        make_visible_proposal(
            db,
            source,
            public_id_suffix=str(n),
            location=location,
            lifecycle_state=("filed", "built", "withdrawn")[n % 3],
            technology=("solar", "bess_li_ion", "wind")[n % 3],
        )

    for i in range(12):  # exact points, two grid cells apart
        add(src, open_lic, geom=(-97.0 - (i % 2) * 20, 30.0 + i * 0.01), precision="exact")
    for _ in range(4):  # exact under a derived-only licence: downgraded to Travis's centroid
        add(derived_src, derived, geom=(-97.7, 30.3), precision="exact", county_name="Travis")
    add(derived_src, derived, geom=(-97.7, 30.3), precision="exact", county_name=None, state_code=None)
    for _ in range(3):  # county grade, with and without a FIPS code (state fallback)
        add(src, open_lic, geom=(-97.75, 30.33), precision="county_centroid")
    add(src, open_lic, geom=(-97.75, 30.33), precision="county_centroid", county_fips=None)
    add(derived_src, derived, geom=(-99.0, 31.0), precision="state_centroid", county_name=None)
    add(src, open_lic, geom=(-1.6, 52.4), precision="country_centroid", country="GB", state_code=None)
    add(src, open_lic, geom=None, precision="unknown", county_name=None, state_code=None)
    add(src, open_lic)  # county grade with no geometry: counted as unplaced, credited, not drawn
    n += 1
    make_visible_proposal(db, src, public_id_suffix=str(n), location=None)
    db.commit()


def _orm_twin(db: Session, rows: list[Any]) -> list[Proposal]:
    """The same rows as fully loaded `Proposal` entities with their `location`, the shape the map
    was built from before it read plain columns."""
    ids = [r.id for r in rows]
    by_id = {p.id: p for p in db.scalars(sa.select(Proposal).where(Proposal.id.in_(ids)))}
    return [by_id[i] for i in ids]


@pytest.mark.parametrize("split", [500, 3])
@pytest.mark.parametrize("query", [f"bbox={WORLD}&zoom=4", "bbox=-100,25,-90,35&zoom=9"])
def test_column_rows_draw_what_the_orm_entities_draw(
    db: Session, monkeypatch: pytest.MonkeyPatch, split: int, query: str
) -> None:
    """Individual markers (`split` 500) and clusters (`split` 3), with region features beside them
    in both: the `GeoRow`s give the same feature collection, value for value and type for type,
    as the ORM entities of the same rows."""
    _seed_mixed(db)
    monkeypatch.setattr(geo, "SPLIT_THRESHOLD", split)
    request = _request(query)
    features = records._proposal_geo_features(db, request)
    rows = features.plottable
    assert rows and all(isinstance(r, GeoRow) for r in rows)
    bbox = records._parse_bbox(request.query_params["bbox"], "/")
    zoom = int(request.query_params["zoom"])
    kwargs: dict[str, Any] = {
        "bbox": bbox,
        "zoom": zoom,
        "records_total": features.records_total,
        "lifecycle_state_counts": features.lifecycle_state_counts,
        "technology_counts": features.technology_counts,
    }
    from_rows = build_geo_feature_collection(
        rows, load_records=lambda ids: records._load_geo_records(db, ids), **kwargs
    )
    from_orm = build_geo_feature_collection(_orm_twin(db, rows), **kwargs)
    assert json.dumps(from_rows) == json.dumps(from_orm)
    kinds = {f["properties"]["feature_kind"] for f in from_rows["features"]}
    assert "region" in kinds
    assert kinds & {"cluster" if split == 3 else "proposal"}


def test_totals_count_every_matching_row_and_the_placement_never_narrows_them(db: Session) -> None:
    _seed_mixed(db)
    every = records._proposal_geo_features(db, _request(f"bbox={WORLD}&zoom=4&placement=exact,region,none"))
    exact = records._proposal_geo_features(db, _request(f"bbox={WORLD}&zoom=4&placement=exact"))
    assert every.records_total == exact.records_total == 26
    assert every.lifecycle_state_counts == exact.lifecycle_state_counts
    # No geometry (`unknown`, a county row without a point) and no location at all.
    assert every.unplaced_count == exact.unplaced_count == 3
    # Downgraded exact rows are region grade: `placement=exact` neither draws nor credits them.
    assert len(exact.plottable) == len(exact.credited_ids) == 12
    assert len(every.credited_ids) == 25  # the row with no location matches no grade


# --------------------------------------------------------------------------- the field-level gate
QUEUE = "us.test.queue"
INVENTORY = "us.test.inventory"
INVENTORY_SAYS = {"name_canonical": "Inventory Name LLC", "capacity_mw": 777.0, "lifecycle_state": "built"}
QUEUE_SAYS = {"name_canonical": "Queue Name Solar", "capacity_mw": 141.5, "lifecycle_state": "filed"}


def _seed_gated(db: Session) -> Proposal:
    """One merged record drawn from the queue's own point whose printed name, capacity and
    lifecycle came from the inventory, beside two queue-only records in the same grid cell."""
    lic = make_open_licence(db)
    queue = make_public_source(db, lic, id_=QUEUE)
    make_public_source(db, lic, id_=INVENTORY)
    merged = make_visible_proposal(
        db,
        queue,
        public_id_suffix="1",
        location=make_location(db, queue, lic, geom=(-97.0, 30.0), precision="exact"),
    )
    for name, value in INVENTORY_SAYS.items():
        setattr(merged, name, value)
    merged.technology = "solar"
    merged.field_provenance = {
        name: {"source_id": INVENTORY, "licence_id": lic.id, "retrieved_at": "2026-09-27T15:15:28+00:00"}
        for name in INVENTORY_SAYS
    }
    merged.source_count = 2
    next(link for link in merged.sources if link.source_id == QUEUE).normalised = {
        **QUEUE_SAYS,
        "technology": "solar",
    }
    now = dt.datetime.now(UTC)
    db.add(
        ProposalSource(
            proposal_id=merged.id,
            source_id=INVENTORY,
            source_record_id="P1",
            source_url="https://example.org/inventory#1",
            retrieved_at=now,
            licence_id=lic.id,
            raw={},
            normalised={**INVENTORY_SAYS, "technology": "solar"},
            first_seen=now,
            last_seen=now,
        )
    )
    for i in (2, 3):
        make_visible_proposal(
            db,
            queue,
            public_id_suffix=str(i),
            location=make_location(db, queue, lic, geom=(-97.0 + i / 100, 30.0), precision="exact"),
            lifecycle_state="filed",
            technology="solar",
        )
    db.commit()
    return merged


def _hide_inventory(db: Session) -> None:
    db.get(Source, INVENTORY).publish_state = "ingest_only"  # type: ignore[union-attr]
    db.commit()


def test_a_drawn_row_prints_its_served_values_as_a_marker(client: TestClient, db: Session) -> None:
    merged = _seed_gated(db)
    shown = client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=6").json()
    marker = next(f for f in shown["data"]["features"] if f["id"] == merged.public_id)
    assert marker["properties"]["name"] == INVENTORY_SAYS["name_canonical"]  # control: stored, all public

    _hide_inventory(db)
    served = client.get(f"/v1/proposals/{merged.public_id}").json()["data"]
    assert served["name_canonical"] == QUEUE_SAYS["name_canonical"]
    body = client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=6")
    marker = next(f for f in body.json()["data"]["features"] if f["id"] == merged.public_id)
    assert marker["properties"]["name"] == served["name_canonical"]
    assert marker["properties"]["capacity_mw"] == served["capacity_mw"]
    assert marker["properties"]["lifecycle_state"] == served["lifecycle_state"]
    assert [p["source_id"] for p in marker["properties"]["provenance"]] == [QUEUE]
    assert marker["properties"]["capacity_mw"] != INVENTORY_SAYS["capacity_mw"]
    counts = body.json()["data"]["totals"]["lifecycle_state_counts"]
    assert counts == dict(Counter(["filed", "filed", served["lifecycle_state"]]))
    for hidden in (INVENTORY_SAYS["name_canonical"], INVENTORY):
        assert hidden not in body.text


def test_a_drawn_row_is_summed_as_served_in_a_cluster(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(geo, "SPLIT_THRESHOLD", 1)
    merged = _seed_gated(db)
    _hide_inventory(db)
    served = client.get(f"/v1/proposals/{merged.public_id}").json()["data"]
    assert served["capacity_mw"] != INVENTORY_SAYS["capacity_mw"]
    body = client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=2")
    (cluster,) = body.json()["data"]["features"]
    props = cluster["properties"]
    assert props["feature_kind"] == "cluster" and props["count"] == 3
    assert props["lifecycle_state_counts"] == dict(Counter(["filed", "filed", served["lifecycle_state"]]))
    # The two queue-only rows carry 102 and 103 MW (`make_visible_proposal`).
    assert props["capacity_mw_sum"] == pytest.approx((served["capacity_mw"] or 0) + 102.0 + 103.0)
    assert INVENTORY_SAYS["name_canonical"] not in body.text


# --------------------------------------------------------------------------- licence summary
def test_the_licence_summary_merges_its_chunks_exactly(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    licences = [make_open_licence(db, "lic-a"), make_attribution_licence(db, "lic-b")]
    sources = [make_public_source(db, licences[i % 2], id_=f"us.test.src_{c}") for i, c in enumerate("cab")]
    for i in range(9):
        src = sources[i % 3]
        loc = make_location(db, src, src.licence, geom=(-97.0 + i, 30.0), precision="exact")
        prop = make_visible_proposal(db, src, public_id_suffix=str(i + 1), location=loc)
        prop.sources[0].retrieved_at = dt.datetime(2026, 9, 1 + i, tzinfo=UTC)
    db.commit()
    whole = client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=4").json()["licence_summary"]
    monkeypatch.setattr(records, "_AGGREGATE_ID_CHUNK", 2)
    geo_cache.reset()  # nothing was written, so the map cache would otherwise answer from memory
    chunked = client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=4").json()["licence_summary"]
    assert chunked == whole
    assert [s["source_id"] for s in whole["sources"]] == ["us.test.src_a", "us.test.src_b", "us.test.src_c"]
    assert [s["record_count"] for s in whole["sources"]] == [3, 3, 3]
    assert whole["sources"][2]["retrieved_at_max"].startswith("2026-09-07")  # src_c: rows 1, 4, 7


# --------------------------------------------------------------------------- query count
def test_individual_markers_cost_a_fixed_number_of_queries(
    client: TestClient, db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    """Before, each marker read its `.sources` lazily: one query per marker (up to 500)."""
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    statements: list[str] = []
    engine = db_sessionmaker.kw["bind"]
    sa.event.listen(engine, "before_cursor_execute", lambda *a: statements.append(a[2]))
    counts = []
    for batch in range(2):
        for i in range(20 * (batch + 1)):
            loc = make_location(db, src, lic, geom=(-97.0 + i / 10, 30.0 + batch), precision="exact")
            make_visible_proposal(db, src, public_id_suffix=f"{batch}{i:03d}", location=loc)
        db.commit()
        statements.clear()
        body = client.get(f"/v1/proposals/geo?bbox={WORLD}&zoom=8").json()
        assert all(f["properties"]["feature_kind"] == "proposal" for f in body["data"]["features"])
        counts.append(len([s for s in statements if s.lstrip().upper().startswith("SELECT")]))
    assert len(body["data"]["features"]) == 60
    assert counts[0] == counts[1], counts


# --------------------------------------------------------------------------- startup heap
def test_the_startup_heap_is_frozen_once_per_process(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    monkeypatch.setattr(gc_tuning, "_frozen", False)

    def collect(*_args: Any) -> int:
        calls.append("collect")
        return 0

    def freeze() -> None:
        calls.append("freeze")

    monkeypatch.setattr(gc, "collect", collect)
    monkeypatch.setattr(gc, "freeze", freeze)
    assert gc_tuning.freeze_startup_heap() is True
    assert gc_tuning.freeze_startup_heap() is False
    assert calls == ["collect", "freeze"]


def test_the_app_lifespan_freezes_the_heap(monkeypatch: pytest.MonkeyPatch) -> None:
    from services.api.app import app

    seen: list[bool] = []

    def freeze_startup_heap() -> bool:
        seen.append(True)
        return True

    monkeypatch.setattr(gc_tuning, "freeze_startup_heap", freeze_startup_heap)
    with TestClient(app):
        pass
    assert seen == [True]


def test_location_rows_are_never_read_as_entities_on_the_map_path(db: Session) -> None:
    """The map's main query selects columns: no `Location` entity enters the session."""
    _seed_mixed(db)
    db.expunge_all()
    records._proposal_geo_features(db, _request(f"bbox={WORLD}&zoom=4"))
    assert not [obj for obj in db.identity_map.values() if isinstance(obj, Location | Proposal)]
