"""`GET /v1/site-finder` and `GET /v1/site-finder/geo` on the site finder's fixture world
(`services/site_finder/testing.py`): the criteria and their defaults, bands merged from adjacent
passing segments, sorting, keyset pages, what a band carries and where each value came from, the tier
gates, the validation, and a latency budget on a synthetic Texas-sized run."""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import math
import pathlib
import re
import uuid as _uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api import site_finder as api
from services.api.app import app
from services.api.deps import get_db
from services.db.models import Asset, Proposal, SiteCandidate, SiteFinderRun, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.site_finder.compute import refresh
from services.site_finder.testing import MILE_LAT, FakeSampler, World, queue_request, seed
from tests.latency import median_seconds


@pytest.fixture()
def factory(tmp_path: pathlib.Path) -> Iterator[sessionmaker[Session]]:
    engine = get_engine(f"sqlite+pysqlite:///{tmp_path / 'sf-api.db'}")
    init_db(engine)
    yield get_sessionmaker(engine)
    engine.dispose()


@pytest.fixture()
def world(factory: sessionmaker[Session], tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> World:
    with factory() as db:
        w = seed(db, tmp_path / "data")
    refresh(factory, w.data_root, sampler=FakeSampler())
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(w.data_root))
    api.clear_caches()
    return w


@pytest.fixture()
def client(factory: sessionmaker[Session], world: World) -> Iterator[TestClient]:
    def _override() -> Iterator[Session]:
        s = factory()
        try:
            yield s
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _get(client: TestClient, **params: Any) -> dict[str, Any]:
    resp = client.get("/v1/site-finder", params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    return body


def _ids(body: dict[str, Any]) -> list[str]:
    return [
        b["line"]["name"] + ":" + f"{b['segments']['first']}-{b['segments']['last']}" for b in body["data"]
    ]


def test_the_defaults_find_the_two_lines_near_a_substation_with_land_outside_the_floodplain(
    client: TestClient,
) -> None:
    body = _get(client)
    # Bravo (345 kV) before Alpha (138 kV); Charlie states no voltage; Delta is under 69 kV.
    assert _ids(body) == ["Bravo 345 kV:0-0", "Alpha 138 kV:0-2"]
    summary = body["meta"]["site_finder"]["summary"]
    assert summary["bands"] == 2 and summary["miles"] == pytest.approx(0.6 + 1.5, abs=0.1)
    criteria = body["meta"]["site_finder"]["criteria"]
    assert (criteria["min_kv"], criteria["adjacent_mi"], criteria["max_substation_mi"]) == (69.0, 1.0, 5.0)
    assert criteria["exclude_flood"] is True and criteria["include_unknown_voltage"] is False
    assert criteria["sort"] == "-voltage_kv"
    assert body["meta"]["site_finder"]["run"]["computed"] is True


def test_a_band_is_the_adjacent_passing_segments_of_one_line(client: TestClient) -> None:
    # Alpha's segments are 1.000, 1.115 and 1.406 miles from S1: a 1.2-mile limit keeps the first two.
    body = _get(client, max_substation_mi=1.2)
    assert _ids(body) == ["Alpha 138 kV:0-1"]
    (band,) = body["data"]
    assert band["segments"]["count"] == 2 and band["length_mi"] == pytest.approx(0.988, abs=0.002)
    assert band["substation"]["name"] == "Alpha North" and band["substation"]["distance_mi"] == pytest.approx(
        1.0, abs=0.002
    )


def test_voltage_floor_unknown_voltage_flood_and_protected_land_filters(client: TestClient) -> None:
    assert _ids(_get(client, min_kv=200)) == ["Bravo 345 kV:0-0"]
    # Charlie (voltage not stated) has no flood data: never "outside", so it waits for exclude_flood=false.
    flagged = _get(client, include_unknown_voltage="true")
    assert _ids(flagged) == ["Bravo 345 kV:0-0", "Alpha 138 kV:0-2"]
    assert flagged["meta"]["site_finder"]["summary"]["flood_unavailable_mi"] == pytest.approx(0.6, abs=0.05)
    every = _get(client, include_unknown_voltage="true", exclude_flood="false")
    assert "Charlie (voltage not stated):0-0" in _ids(every)
    charlie = next(b for b in every["data"] if b["line"]["voltage_unknown"])
    assert charlie["line"]["voltage_class"] == "unknown"
    assert charlie["flood"]["status"] == "unavailable" and charlie["flood"]["reason"] == "not_computed"
    assert charlie["flood"]["share"] is None
    # Charlie's band is 60% GAP 1-2 protected land: "mostly on protected land".
    kept = _get(client, include_unknown_voltage="true", exclude_flood="false", exclude_protected="true")
    assert "Charlie (voltage not stated):0-0" not in _ids(kept)


def test_state_and_county_filters_and_the_pickers_facets(client: TestClient) -> None:
    for state in ("TX", "us-tx", "US-TX"):
        assert len(_get(client, state=state)["data"]) == 2
    assert _get(client, state="OK")["data"] == []
    body = _get(client, county="48999")
    assert len(body["data"]) == 2
    facets = _get(client)["meta"]["site_finder"]["summary"]["facets"]
    assert facets["states"] == [{"state_code": "US-TX", "miles": pytest.approx(2.1, abs=0.1)}]
    assert [c["county_fips"] for c in facets["counties"]] == ["48999"]


@pytest.mark.parametrize(
    ("sort", "expected"),
    [
        ("-voltage_kv", ["Bravo 345 kV:0-0", "Alpha 138 kV:0-2"]),
        ("voltage_kv", ["Alpha 138 kV:0-2", "Bravo 345 kV:0-0"]),
        ("substation_mi", ["Alpha 138 kV:0-2", "Bravo 345 kV:0-0"]),
        ("-substation_mi", ["Bravo 345 kV:0-0", "Alpha 138 kV:0-2"]),
        ("-length_mi", ["Alpha 138 kV:0-2", "Bravo 345 kV:0-0"]),
        ("-retiring_mw", ["Alpha 138 kV:0-2", "Bravo 345 kV:0-0"]),
    ],
)
def test_sorting(client: TestClient, sort: str, expected: list[str]) -> None:
    assert _ids(_get(client, sort=sort)) == expected


def test_keyset_pages_walk_every_band_once(client: TestClient) -> None:
    first = _get(client, limit=1)
    assert first["page"]["has_more"] is True and len(first["data"]) == 1
    second = _get(client, limit=1, cursor=first["page"]["next_cursor"])
    assert second["page"]["has_more"] is False
    assert _ids(first) + _ids(second) == _ids(_get(client))


def test_a_list_can_leave_the_outlines_out(client: TestClient) -> None:
    lean = _get(client, geometry="false")
    full = _get(client)
    assert _ids(lean) == _ids(full)
    assert all(b["geometry"] is None and "outline left out" in b["geometry_note"] for b in lean["data"])
    assert all(b["geometry"] is not None for b in full["data"])
    # Everything else is the same band.
    for a, b in zip(lean["data"], full["data"], strict=True):
        assert {k: v for k, v in a.items() if k not in ("geometry", "geometry_note")} == {
            k: v for k, v in b.items() if k not in ("geometry", "geometry_note")
        }
    assert client.get("/v1/site-finder", params={"geometry": "maybe"}).status_code == 400


def test_one_band_by_its_id(client: TestClient) -> None:
    every = _get(client)
    alpha = next(b for b in every["data"] if b["line"]["name"] == "Alpha 138 kV")
    assert alpha["id"].endswith(".0-2")
    (one,) = _get(client, band=alpha["id"])["data"]
    assert one["id"] == alpha["id"] and one["length_mi"] == alpha["length_mi"]
    # Under tighter criteria the same id answers with what of the band still passes.
    (part,) = _get(client, band=alpha["id"], max_substation_mi=1.2)["data"]
    assert part["segments"] == {"first": 0, "last": 1, "count": 2}
    # Another line's range, an unknown line, a range past the band: nothing.
    line = alpha["id"].rsplit(".", 1)[0]
    assert _get(client, band=f"{line}.5-9")["data"] == []
    assert _get(client, band="asset_0000000000000000000000000.0-2")["data"] == []
    for bad in ("Alpha", f"{line}.2-1", f"{line}.a-b"):
        resp = client.get("/v1/site-finder", params={"band": bad})
        assert resp.status_code == 400, bad
        assert any(e.get("field") == "band" for e in resp.json().get("errors") or [])


def test_a_band_carries_each_fact_with_its_source(client: TestClient, world: World) -> None:
    body = _get(client)
    alpha = next(b for b in body["data"] if b["line"]["name"] == "Alpha 138 kV")
    assert alpha["line"]["voltage_kv"] == 138.0 and alpha["line"]["voltage_label"] == "100-161 kV"
    assert alpha["line"]["osm_url"] == "https://www.openstreetmap.org/way/1"
    assert alpha["line"]["provenance"]["source_id"] == "us.osm.power"
    assert alpha["substation"]["osm_url"] == "https://www.openstreetmap.org/node/11"
    # Half of each band is floodplain; the served polygon has it cut out (nothing north of 31.0).
    assert alpha["flood"]["status"] == "partly_in" and alpha["flood"]["share"] == pytest.approx(
        0.5, abs=0.005
    )
    assert alpha["flood"]["provenance"]["source_id"] == "us.fema.nfhl"
    assert "not an official flood determination" in alpha["flood"]["not"]
    assert alpha["geometry"]["type"] in ("Polygon", "MultiPolygon")
    lats = [pt[1] for ring in _rings(alpha["geometry"]) for pt in ring]
    assert max(lats) <= 31.0 + 1e-4 and min(lats) < 30.99
    assert "floodplain cut out" in alpha["geometry_note"]
    assert alpha["place"] == {
        "state_code": "US-TX",
        "county_fips": "48999",
        "county_name": "Fixture County",
        "counties": ["Fixture County"],
        "note": "Census statistical boundaries, not legal land descriptions.",
        "provenance": alpha["place"]["provenance"],
    }
    assert alpha["place"]["provenance"]["source_id"] == "us.census.tiger_counties"
    (plant,) = alpha["context"]["retiring_plants"]
    assert (plant["name"], plant["capacity_mw"], plant["status"]) == ("Old Mill Station", 500.0, "retired")
    assert plant["distance_mi"] == pytest.approx(2.0, abs=0.01)
    assert plant["provenance"]["source_id"] == "us.eia.860m"
    assert alpha["context"]["fiber"]["status"] == "unavailable"
    centre = alpha["centre"]
    assert centre["lat"] == pytest.approx(31.0) and centre["lon"] == pytest.approx(-97.9875, abs=1e-4)
    assert (
        alpha["links"]["google_maps"]
        == f"https://www.google.com/maps/search/?api=1&query={centre['lat']}%2C{centre['lon']}"
    )
    assert alpha["links"]["fema_flood_map"].startswith(
        "https://msc.fema.gov/portal/search?AddressQuery=31.0%2C"
    )
    assert alpha["links"]["county"] == "https://data.census.gov/profile?g=050XX00US48999"
    by_layer = {entry["layer"]: entry for entry in alpha["prospect"]["layers"]}
    assert by_layer["solar_ghi"]["summary"]["mean"] == pytest.approx(5.2025, abs=0.01)
    assert by_layer["solar_ghi"]["screening"] == "screening" and by_layer["solar_ghi"]["not"]
    assert by_layer["wind_speed_100m"]["status"] == "unavailable"
    bravo = next(b for b in body["data"] if b["line"]["name"] == "Bravo 345 kV")
    assert bravo["flood"]["status"] == "outside" and bravo["flood"]["reported"] == ["shaded_x"]
    (centre_load,) = bravo["context"]["data_centres"]
    assert centre_load["name"] == "Hyperscale Campus" and centre_load["distance_mi"] == pytest.approx(
        3.0, abs=0.01
    )
    grid = bravo["context"]["grid_connection"]
    assert grid["name"] == "Bravo 345kV" and grid["queued_mw"] == 200.0 and grid["queued_count"] == 1
    credited = {s["source_id"] for s in body["licence_summary"]["sources"]}
    assert {
        "us.osm.power",
        "us.eia.860m",
        "us.iso.ercot.gen_queue",
        "us.fema.nfhl",
        "us.census.tiger_counties",
    } <= credited
    for band in body["data"]:
        assert band["provenance"] and all(p.get("source_id") for p in band["provenance"])


def _rings(geometry: dict[str, Any]) -> list[list[list[float]]]:
    if geometry["type"] == "Polygon":
        return list(geometry["coordinates"])
    return [ring for poly in geometry["coordinates"] for ring in poly]


def test_without_the_flood_files_the_band_is_not_cut_and_says_so(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path / "elsewhere"))
    api.clear_caches()
    alpha = next(b for b in _get(client)["data"] if b["line"]["name"] == "Alpha 138 kV")
    assert "not cut out" in alpha["geometry_note"]
    assert max(pt[1] for ring in _rings(alpha["geometry"]) for pt in ring) > 31.01


def test_hidden_records_never_reach_a_band(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    with factory() as db:
        db.execute(
            sa.update(Proposal).where(Proposal.id == world.load.id).values(publish_state="unpublished")
        )
        db.commit()
    bravo = next(b for b in _get(client)["data"] if b["line"]["name"] == "Bravo 345 kV")
    assert bravo["context"]["data_centres"] == []
    with factory() as db:
        db.execute(sa.update(Source).where(Source.id == "us.osm.power").values(publish_state="ingest_only"))
        db.commit()
    api.clear_caches()
    body = _get(client)
    assert body["data"] == [] and body["meta"]["site_finder"]["summary"]["bands"] == 0


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"adjacent_mi": "0.75"}, "adjacent_mi"),
        ({"min_kv": "34.5"}, "min_kv"),
        ({"max_substation_mi": "12"}, "max_substation_mi"),
        ({"exclude_flood": "maybe"}, "exclude_flood"),
        ({"state": "Texas"}, "state"),
        ({"county": "Travis"}, "county"),
        ({"sort": "-score"}, "sort"),
        ({"cursor": "not-a-cursor"}, None),
        ({"zip": "78701"}, "zip"),
    ],
)
def test_bad_criteria_are_refused(client: TestClient, params: dict[str, str], field: str | None) -> None:
    resp = client.get("/v1/site-finder", params=params)
    assert resp.status_code == 400, resp.text
    if field:
        assert any(e.get("field") == field for e in resp.json().get("errors") or [])


def test_before_any_run_the_finder_says_it_has_not_computed(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    with factory() as db:
        db.execute(sa.delete(SiteCandidate))
        db.execute(sa.delete(SiteFinderRun))
        db.commit()
    body = _get(client)
    assert body["data"] == [] and body["meta"]["site_finder"]["run"] == {"computed": False}


def test_the_map_endpoint_draws_the_bands_in_view(client: TestClient) -> None:
    resp = client.get("/v1/site-finder/geo", params={"bbox": "-98.2,30.8,-97.8,31.2", "zoom": "10"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    (feature,) = body["data"]["features"]
    assert (
        feature["properties"]["voltage_class"] == "115"
        and feature["properties"]["flood_status"] == "partly_in"
    )
    assert feature["geometry"]["type"] == "LineString" and len(feature["geometry"]["coordinates"]) >= 2
    assert body["meta"]["site_finder"]["bands_in_view"] == 1
    assert {s["source_id"] for s in body["licence_summary"]["sources"]} == {"us.osm.power"}
    empty = client.get("/v1/site-finder/geo", params={"bbox": "-90,40,-89,41", "zoom": "10"}).json()
    assert empty["data"]["features"] == []
    assert client.get("/v1/site-finder/geo", params={"bbox": "1,2,3"}).status_code == 400


# ---------------------------------------------------------------------------------------- latency
def _synthetic_run(factory: sessionmaker[Session], world: World, n_lines: int, per_line: int) -> None:
    """`n_lines` x `per_line` segments on the world's line A's quartet: Texas's run is about 108,000."""
    with factory() as db:
        run = db.scalars(sa.select(SiteFinderRun).where(SiteFinderRun.status == "ok")).one()
        template = db.scalars(
            sa.select(SiteCandidate).where(SiteCandidate.line_asset_id == world.line_a.id)
        ).first()
        assert template is not None
        base = {c.name: getattr(template, c.name) for c in SiteCandidate.__table__.columns}
        rows = []
        for i in range(n_lines):
            line_id = world.line_a.id if i == 0 else _uuid.uuid4()
            for k in range(per_line):
                rows.append(
                    {
                        **base,
                        "id": _uuid.uuid4(),
                        "run_id": run.id,
                        "line_asset_id": line_id,
                        "segment_index": k + (10 if i == 0 else 0),
                        "voltage_kv": (69.0, 138.0, 345.0)[i % 3],
                        "substation_mi": (k % 20) / 2.0,
                        "flood_share_100": (k % 5) / 5.0,
                        # Texas has 254 counties; a line keeps to one here.
                        "county_fips": f"48{(i % 254) * 2 + 1:03d}",
                        "computed_at": dt.datetime.now(dt.UTC),
                    }
                )
        db.execute(sa.text("PRAGMA foreign_keys=OFF"))
        for start in range(0, len(rows), 5000):
            db.execute(sa.insert(SiteCandidate), rows[start : start + 5000])
        db.commit()
        db.execute(sa.text("ANALYZE"))
        db.commit()


def test_the_list_answers_within_budget_on_a_texas_sized_run(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    """108,000 segments (Texas's run is about that) answer the default list in well under a second on
    SQLite, page and summary together, and the filter uses the candidate index."""
    _synthetic_run(factory, world, n_lines=900, per_line=120)
    api.clear_caches()

    def call() -> None:
        r = client.get("/v1/site-finder", params={"max_substation_mi": 3, "sort": "-length_mi"})
        assert r.status_code == 200

    def cold() -> None:
        api.clear_caches()
        call()

    assert median_seconds(cold, runs=3) < 2.0
    # Warm: the bands, the summary and the drawn polygons are kept, so a sort, a page or a return to
    # the list is a slice of one list.
    call()
    assert median_seconds(call, runs=3) < 0.5
    with factory() as db:
        run_id = db.scalars(sa.select(SiteFinderRun.id).where(SiteFinderRun.status == "ok")).one()
        crit = api.Criteria(69.0, 1.0, 3.0, True, False, False, None, None).base
        # The store-wide band query reads the run's segments in the window's own order, through the
        # (run, line, segment) unique index: no sort of 108,000 rows before the window runs.
        assert _index_columns(db, api.bands_subquery(crit, api.segment_filters(crit, run_id, "public"))) == [
            "run_id",
            "line_asset_id",
            "segment_index",
        ]
        # A county's bands read only that county's segments, through the place index.
        county = dataclasses.replace(crit, state="US-TX", county="48453")
        assert _index_columns(
            db, api.bands_subquery(county, api.segment_filters(county, run_id, "public"))
        ) == ["run_id", "state_code", "county_fips"]


def _index_columns(db: Session, subquery: Any) -> list[str]:
    """The columns of the index SQLite's plan reads `site_candidate` through for `subquery`."""
    sql = str(
        sa.select(subquery).compile(dialect=db.get_bind().dialect, compile_kwargs={"literal_binds": True})
    )
    plan = [str(r[3]) for r in db.execute(sa.text("EXPLAIN QUERY PLAN " + sql))]
    found = [re.search(r"SEARCH site_candidate USING (?:COVERING )?INDEX (\w+)", line) for line in plan]
    names = [m.group(1) for m in found if m]
    assert len(names) == 1, plan
    return [str(r[2]) for r in db.execute(sa.text(f"PRAGMA index_info({names[0]})"))]


# --------------------------------------------------------------------------------------- contract
def test_responses_match_the_committed_spec(client: TestClient) -> None:
    import yaml

    from tests.test_api_contract import OPENAPI_PATH, assert_valid

    spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    assert_valid(
        spec, "SiteFinderListResponse", _get(client, include_unknown_voltage="true", exclude_flood="false")
    )
    assert_valid(spec, "SiteFinderListResponse", _get(client, limit=1))
    geo = client.get("/v1/site-finder/geo", params={"bbox": "-100,29,-96,33", "zoom": "6"}).json()
    assert_valid(spec, "SiteFinderGeoResponse", geo)


# ------------------------------------------------------------------------- fix round (2026-10-23)
def _bravo(body: dict[str, Any]) -> dict[str, Any]:
    return next(b for b in body["data"] if b["line"]["name"] == "Bravo 345 kV")


def _withheld_licence(db: Session) -> str:
    """A licence that allows derived publication but not raw coordinates (a `precise_geo` source)."""
    from services.db.models import Licence

    lic = Licence(
        id="no-raw-lic",
        name="no raw terms",
        reuse_class="open",
        attribution_required=True,
        allows_derived_publication=True,
        allows_raw_publication=False,
        allows_api_redistribution=True,
        allows_bulk_export=True,
        allows_commercial_use=True,
        share_alike=False,
        gate_flag=False,
        evidence_url="https://example.org/terms",
        evidence_retrieved_at=dt.datetime(2026, 10, 3, tzinfo=dt.UTC),
        classified_by="test",
    )
    db.add(lic)
    db.flush()
    return lic.id


def test_a_hidden_request_never_reaches_the_grid_connection(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    """The reviewer's case: POI stays visible through a request 200 miles away, and the only request
    within 5 miles of Bravo (1 mile south) is unpublished. The band names no connection point, so
    neither the hidden request's distance nor the far request's MW is served."""
    with factory() as db:
        queue_request(db, world, suffix="far", geom=(-96.995, 30.0 - 200 * MILE_LAT), capacity_mw=50.0)
        db.execute(
            sa.update(Proposal)
            .where(Proposal.id == world.poi_proposal.id)
            .values(publish_state="unpublished")
        )
        db.commit()
    point = client.get(f"/v1/interconnection-points/{world.point.public_id}")
    assert point.status_code == 200  # the point itself is public, through the far request
    assert _bravo(_get(client))["context"]["grid_connection"] is None


@pytest.mark.parametrize("hide", ["not_yet_public", "location_withheld"])
def test_a_delayed_request_or_one_whose_point_is_withheld_is_left_out(
    client: TestClient, factory: sessionmaker[Session], world: World, hide: str
) -> None:
    """Inside the free tier's delay (`public_at` ahead), or under a licence that forbids serving its
    exact location (`location_exact_permitted`): no distance from it is served."""
    with factory() as db:
        queue_request(db, world, suffix="far", geom=(-96.995, 30.0 - 200 * MILE_LAT), capacity_mw=50.0)
        prop = db.get(Proposal, world.poi_proposal.id)
        assert prop is not None
        if hide == "not_yet_public":
            prop.public_at = dt.datetime.now(dt.UTC) + dt.timedelta(days=30)
        else:
            assert prop.location is not None
            prop.location.licence_id = _withheld_licence(db)
        db.commit()
    assert _bravo(_get(client))["context"]["grid_connection"] is None


def test_the_queued_mw_counts_only_the_visible_requests_within_the_radius(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    """POI's queue holds the near request (200 MW, 1 mile) and a far one (50 MW, 200 miles): the band
    shows 200 MW in one request, never the point's 250 MW in two. A second near request that is
    unpublished is not counted and its distance is not used."""
    with factory() as db:
        queue_request(db, world, suffix="far", geom=(-96.995, 30.0 - 200 * MILE_LAT), capacity_mw=50.0)
        hidden = queue_request(
            db, world, suffix="hid", geom=(-96.995, 30.0 - 0.5 * MILE_LAT), capacity_mw=90.0
        )
        db.execute(sa.update(Proposal).where(Proposal.id == hidden.id).values(publish_state="unpublished"))
        db.commit()
    refresh(factory, world.data_root, sampler=FakeSampler())
    api.clear_caches()
    grid = _bravo(_get(client))["context"]["grid_connection"]
    assert grid["name"] == "Bravo 345kV"
    assert (grid["queued_mw"], grid["queued_count"], grid["requests_counted"]) == (200.0, 1, 1)
    assert grid["nearest_request_mi"] == pytest.approx(1.0, abs=0.01)  # not the hidden one's 0.5
    assert {p["source_id"] for p in grid["request_provenance"]} == {"us.iso.ercot.gen_queue"}


def test_a_load_whose_exact_location_may_not_be_served_is_left_out(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    assert [c["name"] for c in _bravo(_get(client))["context"]["data_centres"]] == ["Hyperscale Campus"]
    with factory() as db:
        load = db.get(Proposal, world.load.id)
        assert load is not None and load.location is not None
        load.location.licence_id = _withheld_licence(db)
        db.commit()
    assert _bravo(_get(client))["context"]["data_centres"] == []


@pytest.mark.parametrize("zoom", ["inf", "1e999", "-inf", "nan", "x"])
def test_a_zoom_that_is_not_a_finite_number_is_a_400(client: TestClient, zoom: str) -> None:
    resp = client.get("/v1/site-finder/geo", params={"zoom": zoom})
    assert resp.status_code == 400, resp.text
    assert any(e.get("field") == "zoom" for e in resp.json().get("errors") or [])


def _long_lines_with_plants(factory: sessionmaker[Session]) -> None:
    """Echo: 16 miles at 345 kV with a retired 500 MW plant near each end (12 miles apart, so no segment
    is within 5 miles of both). Foxtrot: 2 miles at 345 kV beside one retired 800 MW plant."""
    from services.site_finder.testing import line_asset, plant_asset, substation_asset

    mile_lon = MILE_LAT / math.cos(math.radians(30.5))
    with factory() as db:
        west = -97.6
        line_asset(
            db, key="way/5", name="Echo 345 kV", kv=345.0, coords=[(west, 30.5), (west + 16 * mile_lon, 30.5)]
        )
        substation_asset(
            db, key="node/15", name="Echo Mid", geom=(west + 8 * mile_lon, 30.5 + MILE_LAT), kv=345.0
        )
        for n, at in ((1, 2.0), (2, 14.0)):
            plant_asset(
                db,
                key=f"eia/1{n}",
                name=f"Echo Plant {n}",
                geom=(west + at * mile_lon, 30.5 - MILE_LAT),
                mw=500.0,
            )
        f_west = -97.3
        line_asset(
            db,
            key="way/6",
            name="Foxtrot 345 kV",
            kv=345.0,
            coords=[(f_west, 30.2), (f_west + 2 * mile_lon, 30.2)],
        )
        substation_asset(db, key="node/16", name="Foxtrot Tap", geom=(f_west, 30.2 + MILE_LAT), kv=345.0)
        plant_asset(
            db, key="eia/20", name="Foxtrot Plant", geom=(f_west + mile_lon, 30.2 - MILE_LAT), mw=800.0
        )
        db.commit()


def test_the_retiring_sort_orders_by_the_figure_each_row_shows(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    """A long band beside two 500 MW plants shows 1,000 MW and sorts as 1,000 MW, above a band beside
    one 800 MW plant (the per-segment figure, 500, sorted it below)."""
    _long_lines_with_plants(factory)
    refresh(factory, world.data_root, sampler=FakeSampler())
    api.clear_caches()
    body = _get(client, sort="-retiring_mw", max_substation_mi=10)
    shown = [(b["line"]["name"], b["context"]["retiring_mw"]) for b in body["data"]]
    assert shown[:3] == [("Echo 345 kV", 1000.0), ("Foxtrot 345 kV", 800.0), ("Alpha 138 kV", 500.0)]
    figures = [mw or 0.0 for _, mw in shown]
    assert figures == sorted(figures, reverse=True)
    ascending = [
        b["context"]["retiring_mw"] or 0.0
        for b in _get(client, sort="retiring_mw", max_substation_mi=10)["data"]
    ]
    assert ascending == sorted(ascending)


def test_bands_are_drawn_from_the_vertices_their_run_measured(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    """A context load moves line A two miles north after the run: until the next run, the band, its
    centreline, its outline and the map still show where the run measured it."""
    with factory() as db:
        db.execute(
            sa.update(Asset)
            .where(Asset.id == world.line_a.id)
            .values(geom_line=f"MULTILINESTRING((-98.0 {31.0 + 2 * MILE_LAT}, -97.95 {31.0 + 2 * MILE_LAT}))")
        )
        db.commit()
    api.clear_caches()
    alpha = next(b for b in _get(client)["data"] if b["line"]["name"] == "Alpha 138 kV")
    assert {pt[1] for pt in alpha["centreline"]["coordinates"]} == {31.0}
    assert alpha["centreline"]["coordinates"][0] == [-98.0, 31.0]
    assert alpha["centreline"]["coordinates"][-1] == [-97.975, 31.0]
    lats = [pt[1] for ring in _rings(alpha["geometry"]) for pt in ring]
    assert max(lats) <= 31.0 + 1e-4 and min(lats) == pytest.approx(31.0 - MILE_LAT, abs=1e-3)
    geo = client.get("/v1/site-finder/geo", params={"bbox": "-98.2,30.8,-97.8,31.2", "zoom": "12"}).json()
    (feature,) = geo["data"]["features"]
    assert {pt[1] for pt in feature["geometry"]["coordinates"]} == {31.0}


def test_the_band_outline_never_reaches_into_the_floodplain() -> None:
    """A floodplain edge with 10 m teeth across the band: the outline is the band less the floodplain
    exactly (to the 1e-5 degree rounding), where simplifying after the cut flattened the teeth and
    put up to 15 m of floodplain back inside the outline."""
    import numpy as np
    import shapely

    from services.site_finder.geometry import M_PER_DEG

    class Pieces:
        def __init__(self, geoms: list[Any]) -> None:
            self.geoms = geoms

        def within(self, bbox: tuple[float, float, float, float]) -> list[Any]:
            return [g for g in self.geoms if shapely.intersects(g, shapely.box(*bbox))]

    lat0 = 31.0
    centre = np.asarray([[-98.0, lat0], [-97.97, lat0]])
    # The floodplain: everything north of a sawtooth 300 m north of the line (teeth 10 m high, 30 m apart).
    kx = M_PER_DEG * math.cos(math.radians(lat0))
    xs = np.arange(-98.05, -97.92, 30.0 / kx)
    ys = lat0 + (300.0 + np.where(np.arange(len(xs)) % 2 == 0, 0.0, 10.0)) / M_PER_DEG
    flood = shapely.Polygon(
        [(x, y) for x, y in zip(xs, ys, strict=True)] + [(xs[-1], lat0 + 0.1), (xs[0], lat0 + 0.1)]
    )
    geojson, cut = api.band_polygon(centre, 1.0, [Pieces([flood])], True)  # type: ignore[list-item]
    assert cut
    outline = shapely.geometry.shape(geojson)
    inside = shapely.intersection(outline, flood)
    # The rounding to 1e-5 degrees moves an edge at most about half a metre: over the ~3 km of edge the
    # band shares with the floodplain that is at most ~1,500 m2. The teeth alone hold 3 km x 5 m.
    area_m2 = shapely.area(inside) * M_PER_DEG * kx
    assert area_m2 < 1500.0, area_m2


def test_the_voltage_floor_and_the_unknown_flag_filter_one_cached_band_list(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Five floors, with and without unknown voltages, and a substation distance that rounds to the
    same quarter mile: one store-wide window query, not one per value."""
    calls = 0
    real = api.bands_subquery

    def counting(*a: Any, **k: Any) -> Any:
        nonlocal calls
        calls += 1
        return real(*a, **k)

    monkeypatch.setattr(api, "bands_subquery", counting)
    api.clear_caches()
    seen = {}
    for kv in (69, 115, 138.5, 200, 345):
        for unknown in ("true", "false"):
            for sub in (5, 4.9, 5.1):
                body = _get(client, min_kv=kv, include_unknown_voltage=unknown, max_substation_mi=sub)
                assert body["meta"]["site_finder"]["criteria"]["max_substation_mi"] == 5.0
                seen[(kv, unknown)] = _ids(body)
    assert calls == 1
    assert seen[(345, "false")] == ["Bravo 345 kV:0-0"] and seen[(69, "false")] == [
        "Bravo 345 kV:0-0",
        "Alpha 138 kV:0-2",
    ]


def test_the_substation_distance_is_rounded_to_a_quarter_mile_and_echoed(client: TestClient) -> None:
    for asked, applied in (("1.2", 1.25), ("0.1", 0.25), ("10", 10.0), ("3.37", 3.25)):
        body = _get(client, max_substation_mi=asked)
        assert body["meta"]["site_finder"]["criteria"]["max_substation_mi"] == applied
        assert body["meta"]["site_finder"]["criteria"]["rules"]["substation_step_mi"] == 0.25


def _many_lines(factory: sessionmaker[Session], world: World, n: int) -> None:
    """`n` real one-segment lines on Bravo's segment (same voltage, flood, substation), with seven band
    lengths among them: sort values tie in every column."""
    from services.site_finder.testing import line_asset

    with factory() as db:
        run = db.scalars(sa.select(SiteFinderRun).where(SiteFinderRun.status == "ok")).one()
        template = db.scalars(
            sa.select(SiteCandidate).where(SiteCandidate.line_asset_id == world.line_b.id)
        ).one()
        base = {c.name: getattr(template, c.name) for c in SiteCandidate.__table__.columns}
        rows = []
        for i in range(n):
            lon = -97.5 + i * 0.001
            line = line_asset(
                db,
                key=f"way/{100 + i}",
                name=f"Line {i:03d}",
                kv=345.0,
                coords=[(lon, 30.3), (lon + 0.0005, 30.3)],
            )
            rows.append(
                {
                    **base,
                    "id": _uuid.uuid4(),
                    "line_asset_id": line.id,
                    "run_id": run.id,
                    "length_mi": 0.1 + (i % 7) * 0.3,
                }
            )
        db.execute(sa.insert(SiteCandidate), rows)
        db.commit()


@pytest.mark.parametrize("sort", ["-voltage_kv", "-retiring_mw", "substation_mi", "length_mi"])
def test_keyset_pages_walk_tied_bands_once_each_in_order(
    client: TestClient, factory: sessionmaker[Session], world: World, sort: str
) -> None:
    """60 bands whose sort values tie (retiring MW 0 for all but Alpha; one voltage; one substation
    distance; seven lengths): pages of 7 visit every band once, in the one-page order."""
    _many_lines(factory, world, 60)
    api.clear_caches()
    every = _ids(_get(client, sort=sort, limit=100, geometry="false"))
    assert len(every) == 62
    walked: list[str] = []
    cursor = None
    for _ in range(20):
        params: dict[str, Any] = {"sort": sort, "limit": 7, "geometry": "false"}
        if cursor:
            params["cursor"] = cursor
        body = _get(client, **params)
        walked += _ids(body)
        cursor = body["page"]["next_cursor"]
        if not body["page"]["has_more"]:
            break
    assert walked == every and len(set(walked)) == 62
    if sort == "-voltage_kv":
        # At one voltage, the longest band first (a switchyard stub no longer leads the list).
        bands = _get(client, sort=sort, limit=100, geometry="false")["data"]
        at_345 = [b["length_mi"] for b in bands if b["line"]["voltage_kv"] == 345.0]
        assert at_345 == sorted(at_345, reverse=True)


def test_counties_are_listed_for_one_state_and_asked_for_across_many(
    client: TestClient, factory: sessionmaker[Session], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every county is listed, across states, up to `COUNTY_FACET_CAP` (a Texas run, with its border
    segments, has 284); past it the list waits for a state. A chosen state lists its own, uncapped
    (the previous list stopped at 500, sorted by state, so Texas's came after its neighbours')."""
    every = {"include_unknown_voltage": "true", "exclude_flood": "false"}
    with factory() as db:
        db.execute(
            sa.update(SiteCandidate)
            .where(SiteCandidate.line_asset_id == world.line_c.id)
            .values(state_code="US-OK", county_fips="40109", county_name="Oklahoma County")
        )
        db.commit()
    api.clear_caches()
    facets = _get(client, **every)["meta"]["site_finder"]["summary"]["facets"]
    assert [s["state_code"] for s in facets["states"]] == ["US-OK", "US-TX"]
    assert [c["county_fips"] for c in facets["counties"]] == ["40109", "48999"]
    assert facets["counties_need_state"] is False
    monkeypatch.setattr(api, "COUNTY_FACET_CAP", 1)
    api.clear_caches()
    facets = _get(client, **every)["meta"]["site_finder"]["summary"]["facets"]
    assert facets["counties"] == [] and facets["counties_need_state"] is True
    ok = _get(client, **every, state="OK")["meta"]["site_finder"]["summary"]["facets"]
    assert [c["county_fips"] for c in ok["counties"]] == ["40109"] and ok["counties_need_state"] is False
    tx = _get(client, **every, state="TX")["meta"]["site_finder"]["summary"]["facets"]
    assert {c["state_code"] for c in tx["counties"]} == {"US-TX"}


def test_health_says_whether_the_site_finder_has_a_run_to_show(
    factory: sessionmaker[Session], client: TestClient
) -> None:
    """`GET /v1/health` `site_finder` (the public navigation lists the finder only when `computed`):
    true with a finished run that has segments, false once there is none."""
    finder = client.get("/v1/health").json()["site_finder"]
    assert finder["computed"] is True and finder["segments"] > 0
    with factory() as db:
        db.execute(sa.delete(SiteCandidate))
        db.execute(sa.delete(SiteFinderRun))
        db.commit()
    assert client.get("/v1/health").json()["site_finder"] == {"computed": False, "segments": 0}


# ------------------------------------------------------------------------- land parcels (lane parcels)
_PARCEL_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[2] / "pipeline" / "context" / "fixtures" / "txgio_parcels"
)


def _owner_strings() -> set[str]:
    """The synthetic county's owner, care-of, situs and mailing values (`pipeline/context/fixtures/
    txgio_parcels/synthetic_county.geojson`): none may reach a response."""
    doc = json.loads((_PARCEL_FIXTURE / "synthetic_county.geojson").read_text(encoding="utf-8"))
    out: set[str] = set()
    for f in doc["features"]:
        for name in ("OWNER_NAME", "NAME_CARE", "SITUS_ADDR", "MAIL_ADDR", "LEGAL_DESC"):
            v = f["properties"].get(name)
            if v and str(v).strip() and "CONFIDENTIAL" not in str(v).upper():
                out.add(str(v).strip().upper())
    return out | {"OWNER ALPHA", "OWNER BRAVO", "OWNER DELTA", "SYNTH SITUS", "SYNTH MAIL", "SYNTH CARE"}


def _alpha(body: dict[str, Any]) -> dict[str, Any]:
    return next(b for b in body["data"] if b["line"]["name"] == "Alpha 138 kV")


def test_a_band_carries_its_holdings_with_appraisal_links_and_no_owner(client: TestClient) -> None:
    body = _get(client)
    land = _alpha(body)["parcels"]
    assert land["status"] == "ok" and land["counties_missing"] == []
    assert (
        land["note"] == "Owner names are on the appraisal district's record; Inframation does not store them."
    )
    assert land["largest_holding_acres"] == pytest.approx(632.0, abs=1.0)
    top = land["top_holdings"]
    assert top[0]["acres"] == pytest.approx(632.0, abs=1.0) and top[0]["parcels"] == 1
    assert {h["owner_count"] for h in top} == {1}
    alpha_holding = next(h for h in top if h["parcels"] == 3)
    assert alpha_holding["acres"] == pytest.approx(118.5, abs=0.5)
    assert alpha_holding["land_use"] == "open_space" and alpha_holding["value_per_acre"] > 0
    # 48999 has no verified appraisal-district route: the Comptroller's directory, with the id to copy.
    link = top[0]["appraisal_record"]
    assert link["kind"] == "directory" and link["property_id"] == "50015"
    assert link["url"] == "https://comptroller.texas.gov/taxes/property-tax/county-directory/fixture.php"
    assert land["holdings_20_acres_plus"] >= 10 and land["holdings_at_least"]["500"] == 1
    assert land["provenance"]["source_id"] == "us.tx.txgio.parcels"
    credits = {r["source_id"]: r for r in body["licence_summary"]["sources"]}
    assert "Texas Geographic Information Office" in credits["us.tx.txgio.parcels"]["attribution_text"]
    assert "Neither Inframation nor this information" in credits["us.tx.txgio.parcels"]["attribution_text"]
    blob = json.dumps(body).upper()
    assert not sorted(v for v in _owner_strings() if v in blob)
    # A band whose county has parcels but none near it says so with zeros, not "not loaded".
    bravo = _bravo(body)["parcels"]
    assert bravo["status"] == "ok" and bravo["largest_holding_acres"] == 0.0 and bravo["top_holdings"] == []


def test_a_band_outside_the_loaded_counties_names_its_county(client: TestClient) -> None:
    body = _get(client, include_unknown_voltage="true", exclude_flood="false")
    charlie = next(b for b in body["data"] if b["line"]["voltage_unknown"])
    land = charlie["parcels"]
    assert land["status"] == "not_loaded" and land["provenance"] is None
    assert land["counties_missing"] and land["counties_missing"][0]["fips"] == charlie["place"]["county_fips"]


def test_contiguous_acres_and_owners_filter_segments_and_count_the_miles_without_parcels(
    client: TestClient,
) -> None:
    assert _ids(_get(client, min_acres=500)) == ["Alpha 138 kV:0-2"]
    assert _ids(_get(client, min_acres=1000)) == []
    assert _ids(_get(client, min_acres=500, max_owners=1)) == ["Alpha 138 kV:0-2"]
    assert (
        _get(client, min_acres="none", max_owners="any")["meta"]["site_finder"]["criteria"]["min_acres"]
        is None
    )
    every = _get(client, include_unknown_voltage="true", exclude_flood="false", min_acres=20)
    assert "Charlie (voltage not stated):0-0" not in _ids(every)
    summary = every["meta"]["site_finder"]["summary"]
    assert summary["parcels_unavailable_mi"] == pytest.approx(0.6, abs=0.05)
    criteria = every["meta"]["site_finder"]["criteria"]
    assert criteria["min_acres"] == 20 and criteria["rules"]["min_acres_choices"] == [
        20,
        50,
        100,
        200,
        500,
        1000,
    ]
    land = _alpha(_get(client, max_owners=3))["parcels"]
    assert land["assembly"]["max_owners"] == 3 and land["assembly"]["acres"] >= land["largest_holding_acres"]


def test_bands_sort_by_their_largest_holding(client: TestClient) -> None:
    assert _ids(_get(client, sort="-largest_holding_acres")) == ["Alpha 138 kV:0-2", "Bravo 345 kV:0-0"]
    assert _ids(_get(client, sort="largest_holding_acres")) == ["Bravo 345 kV:0-0", "Alpha 138 kV:0-2"]
    # Charlie has no parcels measured: last whichever way.
    for sort in ("-largest_holding_acres", "largest_holding_acres"):
        every = _get(client, include_unknown_voltage="true", exclude_flood="false", sort=sort)
        assert _ids(every)[-1] == "Charlie (voltage not stated):0-0"


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"min_acres": "30"}, "min_acres"),
        ({"min_acres": "lots"}, "min_acres"),
        ({"max_owners": "2"}, "max_owners"),
        ({"max_owners": "-1"}, "max_owners"),
        ({"sort": "largest_holding"}, "sort"),
    ],
)
def test_parcel_criteria_are_validated(client: TestClient, params: dict[str, str], field: str) -> None:
    resp = client.get("/v1/site-finder", params=params)
    assert resp.status_code == 400, resp.text
    assert field in resp.text


AROUND_A = "-98.01,30.98,-97.96,31.02"


def test_parcel_outlines_are_drawn_from_zoom_12_with_links_and_no_owner(client: TestClient) -> None:
    far = client.get("/v1/site-finder/parcels", params={"bbox": AROUND_A, "zoom": "11"}).json()
    assert far["data"]["features"] == [] and far["meta"]["site_finder"]["parcels"]["status"] == "zoom_in"
    wide = client.get("/v1/site-finder/parcels", params={"bbox": "-99,30,-97,32", "zoom": "13"}).json()
    assert wide["meta"]["site_finder"]["parcels"]["status"] == "zoom_in"
    near = client.get("/v1/site-finder/parcels", params={"bbox": AROUND_A, "zoom": "13"})
    assert near.status_code == 200
    body = near.json()
    meta = body["meta"]["site_finder"]["parcels"]
    assert meta["status"] == "ok" and meta["in_view"] == 16 and meta["drawn"] == 16
    assert meta["counties_loaded_in_view"] == [{"fips": "48999", "name": "Fixture County"}]
    props = [f["properties"] for f in body["data"]["features"]]
    assert props[0]["holding_acres"] == pytest.approx(632.0, abs=1.0)  # largest holdings first
    assert all(
        p["appraisal_record"]["kind"] == "directory" and p["holding_id"].startswith("h") for p in props
    )
    assert body["data"]["features"][0]["geometry"]["type"] in ("Polygon", "MultiPolygon")
    assert [r["source_id"] for r in body["licence_summary"]["sources"]] == ["us.tx.txgio.parcels"]
    blob = json.dumps(body).upper()
    assert not sorted(v for v in _owner_strings() if v in blob)
    assert client.get("/v1/site-finder/parcels", params={"zoom": "13"}).status_code == 400
    assert (
        client.get("/v1/site-finder/parcels", params={"bbox": AROUND_A, "zoom": "13", "x": "1"}).status_code
        == 400
    )


def test_parcel_outlines_say_when_this_server_holds_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    from services.context_sample import parcels as cp

    monkeypatch.setenv("PARCELS_DATA_DIR", str(tmp_path / "nothing"))
    cp.clear_cache()
    body = client.get("/v1/site-finder/parcels", params={"bbox": AROUND_A, "zoom": "13"}).json()
    assert (
        body["meta"]["site_finder"]["parcels"]["status"] == "unavailable" and body["data"]["features"] == []
    )
    cp.clear_cache()


def test_a_gated_parcel_source_is_withheld_everywhere(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    with factory() as db:
        db.add(
            Source(
                id="us.tx.txgio.parcels",
                name="TxGIO parcels",
                jurisdiction="US-TX",
                category="land",
                operator="TxGIO",
                url="https://example.org/txgio",
                access="bulk_file",
                cadence="annual",
                licence_id="osm-lic",
                publish_state="ingest_only",
                manifest_version="2026-10-23",
                manifest_hash="0" * 64,
            )
        )
        db.commit()
    api.clear_caches()
    land = _alpha(_get(client))["parcels"]
    assert land["status"] == "withheld" and "top_holdings" not in land and land["provenance"] is None
    body = client.get("/v1/site-finder/parcels", params={"bbox": AROUND_A, "zoom": "13"}).json()
    assert body["meta"]["site_finder"]["parcels"]["status"] == "withheld" and body["data"]["features"] == []


def test_parcel_responses_match_the_committed_spec(client: TestClient) -> None:
    import yaml

    from tests.test_api_contract import OPENAPI_PATH, assert_valid

    spec = yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))
    listed = _get(client, min_acres=100, max_owners=3, sort="-largest_holding_acres")
    assert_valid(spec, "SiteFinderListResponse", listed)
    every = _get(client, include_unknown_voltage="true", exclude_flood="false")
    assert_valid(spec, "SiteFinderListResponse", every)
    parcels = client.get("/v1/site-finder/parcels", params={"bbox": AROUND_A, "zoom": "13"}).json()
    assert_valid(spec, "SiteFinderParcelsResponse", parcels)


def test_the_largest_holding_sort_follows_a_re_measure_in_place(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    """A server pulls a different set of counties from the same `latest.json` and re-measures in place
    (`--parcels-only`): the layer's version is unchanged, but the order must follow the new figures,
    not the per-run cache of the old ones, within one request."""
    from services.context_sample import parcels as cp
    from services.site_finder.compute import refresh_parcels

    assert _ids(_get(client, sort="-largest_holding_acres")) == ["Alpha 138 kV:0-2", "Bravo 345 kV:0-0"]
    root = world.data_root / "normalized" / "context" / "us.tx.txgio.parcels"
    current = json.loads((root / "current.json").read_text(encoding="utf-8"))
    loaded = dict(current["counties"])
    current["counties"] = {}
    (root / "current.json").write_text(json.dumps(current), encoding="utf-8")
    cp.clear_cache()
    refresh_parcels(factory, world.data_root)
    after = _get(client, sort="-largest_holding_acres")
    assert {b["parcels"]["status"] for b in after["data"]} == {"not_loaded"}
    # Neither band has a figure now: the voltage tie-break orders them, as a fresh process would.
    assert _ids(after) == ["Bravo 345 kV:0-0", "Alpha 138 kV:0-2"]
    current["counties"] = loaded
    (root / "current.json").write_text(json.dumps(current), encoding="utf-8")
    cp.clear_cache()
    refresh_parcels(factory, world.data_root)
    assert _ids(_get(client, sort="-largest_holding_acres")) == ["Alpha 138 kV:0-2", "Bravo 345 kV:0-0"]
    assert _ids(_get(client, min_acres=500)) == ["Alpha 138 kV:0-2"]


def test_a_band_says_how_many_holdings_touch_it_and_which_are_left_out(client: TestClient) -> None:
    land = _alpha(_get(client))["parcels"]
    # Alpha's band at a mile: every holding of the fixture county touches it; Foxtrot's exempt tract is
    # counted but left out of the acreage figures and the list.
    assert land["holdings_touching"] == 12 and land["holdings_touching_exact"] is False
    assert land["left_out_holdings_at_least"] == 1
    assert land["left_out_note"].startswith("Right-of-way strips, exempt land and utility land")
    assert all(h["land_use"] != "exempt" for h in land["top_holdings"])
    assert all(":" not in h["holding_id"] and h["county_fips"] == "48999" for h in land["top_holdings"])


def test_a_band_outside_texas_says_parcels_are_texas_only(
    client: TestClient, factory: sessionmaker[Session], world: World
) -> None:
    with factory() as db:
        for row in db.scalars(sa.select(SiteCandidate).where(SiteCandidate.line_asset_id == world.line_c.id)):
            row.county_fips, row.county_name, row.state_code = "22019", "Calcasieu Parish", "US-LA"
        db.commit()
    api.clear_caches()
    body = _get(client, include_unknown_voltage="true", exclude_flood="false")
    land = next(b for b in body["data"] if b["line"]["voltage_unknown"])["parcels"]
    assert land["status"] == "not_loaded" and land["counties_missing"] == []
    assert land["outside_texas"] == [{"fips": "22019", "name": "Calcasieu Parish"}]


def test_a_view_with_more_parcels_than_the_cap_keeps_the_largest_holdings(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The view's rows are ranked and cut in Arrow (`_parcel_features`): the cap keeps the largest
    holdings, in order, and counts every parcel in view."""
    monkeypatch.setattr(api, "PARCELS_CAP", 5)
    body = client.get("/v1/site-finder/parcels", params={"bbox": AROUND_A, "zoom": "13"}).json()
    meta = body["meta"]["site_finder"]["parcels"]
    assert meta["in_view"] == 16 and meta["drawn"] == 5
    props = [f["properties"] for f in body["data"]["features"]]
    acres = [p["holding_acres"] for p in props]
    assert acres == sorted(acres, reverse=True) and acres[0] == pytest.approx(632.0, abs=1.0)
    # Alpha's three parcels (118.5 acres together) follow Delta's ranch, tied ones by parcel key.
    assert [p["holding_parcels"] for p in props[1:4]] == [3, 3, 3]
    assert [p["parcel_key"] for p in props[1:4]] == sorted(p["parcel_key"] for p in props[1:4])
    assert {p["holding_kind"] for p in props} == {"land"}


def test_over_the_cap_only_the_kept_parcels_outlines_are_read(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dense view is ranked without outlines; the outlines read afterwards are the kept parcels' only,
    so a city view never holds every outline in it (the api's 1 GB limit, docs/25-siting-parcels.md §8.9)."""
    from services.context_sample import parcels as cp

    calls: list[tuple[bool, int | None]] = []
    real = cp.ParcelLayer.read_bbox

    def spy(self: cp.ParcelLayer, *args: Any, **kwargs: Any) -> Any:
        keys = kwargs.get("keys")
        calls.append((kwargs.get("geometry", True), None if keys is None else len(keys)))
        return real(self, *args, **kwargs)

    monkeypatch.setattr(cp.ParcelLayer, "read_bbox", spy)
    monkeypatch.setattr(api, "PARCELS_CAP", 5)
    body = client.get("/v1/site-finder/parcels", params={"bbox": AROUND_A, "zoom": "13"}).json()
    assert body["meta"]["site_finder"]["parcels"]["drawn"] == 5
    assert calls == [(False, None), (True, 5)]
