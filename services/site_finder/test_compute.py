"""The candidate computation (`services/site_finder/compute.py`) on the fixture world
(`services/site_finder/testing.py`): segments, distances in miles, flood shares and coverage, county
and state, context, the prospect values, the voltage floor, the unknown-voltage flag, and runs."""

from __future__ import annotations

import datetime as dt
import math
import pathlib
import uuid as _uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from services.db.models import Proposal, SiteCandidate, SiteFinderRun
from services.db.session import get_engine, get_sessionmaker, init_db
from services.site_finder import compute
from services.site_finder.flood import FloodData
from services.site_finder.geometry import M_PER_DEG, METRES_PER_MILE, join_segments, length_mi
from services.site_finder.testing import MILE_LAT, FakeSampler, World, queue_request, seed


@pytest.fixture()
def factory(tmp_path: pathlib.Path) -> Iterator[sessionmaker[Session]]:
    engine = get_engine(f"sqlite+pysqlite:///{tmp_path / 'sf.db'}")
    init_db(engine)
    yield get_sessionmaker(engine)
    engine.dispose()


@pytest.fixture()
def world(factory: sessionmaker[Session], tmp_path: pathlib.Path) -> World:
    with factory() as db:
        return seed(db, tmp_path / "data")


@pytest.fixture()
def report(factory: sessionmaker[Session], world: World) -> dict[str, Any]:
    return compute.refresh(factory, world.data_root, sampler=FakeSampler())


def _rows(factory: sessionmaker[Session], line_id: Any) -> list[SiteCandidate]:
    with factory() as db:
        rows = list(
            db.scalars(
                sa.select(SiteCandidate)
                .where(SiteCandidate.line_asset_id == line_id)
                .order_by(SiteCandidate.segment_index)
            )
        )
        db.expunge_all()
        return rows


def test_lines_are_cut_into_half_mile_segments_and_the_34_kv_line_is_not_a_candidate(
    factory: sessionmaker[Session], world: World, report: dict[str, Any]
) -> None:
    a = _rows(factory, world.line_a.id)
    assert [r.segment_index for r in a] == [0, 1, 2]
    assert all(r.length_mi == pytest.approx(0.494, abs=0.001) for r in a)
    assert sum(r.length_mi for r in a) == pytest.approx(
        0.025 * M_PER_DEG * math.cos(math.radians(31)) / METRES_PER_MILE, rel=1e-4
    )
    assert len(_rows(factory, world.line_b.id)) == 1
    assert _rows(factory, world.line_d.id) == []
    assert report["lines"] == 3 and report["segments"] == 5


def test_substation_distances_are_miles_from_the_segment(
    factory: sessionmaker[Session], world: World, report: dict[str, Any]
) -> None:
    a = _rows(factory, world.line_a.id)
    # S1 is exactly one mile north of A's western end, the start of segment 0.
    assert a[0].substation_asset_id == world.s1.id
    assert a[0].substation_mi == pytest.approx(1.0, abs=0.002)
    # Segment 1 starts 0.494 miles east of that end: the hypotenuse.
    assert a[1].substation_mi == pytest.approx(math.hypot(1.0, a[0].length_mi), abs=0.002)
    assert a[2].substation_mi == pytest.approx(math.hypot(1.0, a[0].length_mi * 2), abs=0.002)
    (b,) = _rows(factory, world.line_b.id)
    assert b.substation_asset_id == world.s2.id and b.substation_mi == pytest.approx(3.0, abs=0.002)


def test_flood_share_is_the_floodplain_part_of_each_band(
    factory: sessionmaker[Session], world: World, report: dict[str, Any]
) -> None:
    """The SFHA fixture is everything north of A's latitude, so each of A's bands is half floodplain
    at every half-width; B's band has none; C lies outside the build's area of interest."""
    for row in _rows(factory, world.line_a.id):
        for key in ("025", "050", "100", "200"):
            assert getattr(row, f"flood_cover_{key}") == pytest.approx(1.0)
            assert getattr(row, f"flood_share_{key}") == pytest.approx(0.5, abs=0.002), key
        assert row.flood_reason is None
        assert row.context["firm_panels"] == ["48453C0100K"]
    (b,) = _rows(factory, world.line_b.id)
    assert b.flood_share_100 == 0.0 and b.flood_cover_100 == pytest.approx(1.0)
    assert b.context["flood_reported"] == ["shaded_x"]  # reported beside the answer, never excluding
    (c,) = _rows(factory, world.line_c.id)
    assert c.flood_cover_100 == 0.0 and c.flood_share_100 is None
    assert c.flood_reason == "not_computed"
    assert report["flood_status_1mi"] == {"outside": 1, "partly_in": 3, "unavailable": 1}


def test_unknown_voltage_is_flagged_and_counties_name_their_source(
    factory: sessionmaker[Session], world: World, report: dict[str, Any]
) -> None:
    (c,) = _rows(factory, world.line_c.id)
    assert c.voltage_unknown is True and c.voltage_kv is None
    assert c.substation_mi == pytest.approx(0.5, abs=0.002)
    # Outside the TIGER-shaped fixture file: the vendored 1:20m counties name it, and say so.
    assert c.state_code == "US-TX" and c.county_fips and c.county_fips.startswith("48")
    assert c.context["county_source"] == "us.census.cartographic_boundaries"
    a0 = _rows(factory, world.line_a.id)[0]
    assert a0.voltage_unknown is False and a0.voltage_kv == 138.0
    assert (a0.county_fips, a0.county_name, a0.state_code) == ("48999", "Fixture County", "US-TX")
    assert a0.context["county_source"] == "us.census.tiger_counties"


def test_context_within_five_miles(
    factory: sessionmaker[Session], world: World, report: dict[str, Any]
) -> None:
    a = _rows(factory, world.line_a.id)
    # P1 (retired, 500 MW) is 2 miles south of A's middle; P2 is 7 miles away; the operating plant is not
    # counted.
    assert [p[0] for p in a[1].context["plants"]] == [str(world.p1.id)]
    assert a[1].context["plants"][0][1] == pytest.approx(2.0, abs=0.002)
    assert a[1].retiring_mw == 500.0 and a[1].retiring_count == 1
    (b,) = _rows(factory, world.line_b.id)
    assert b.retiring_mw is None and b.retiring_count == 0
    assert [x[0] for x in b.context["loads"]] == [str(world.load.id)]
    assert b.context["loads"][0][1] == pytest.approx(3.0, abs=0.002)
    # One entry per request naming a connection point: (request, point, miles), never a per-point figure.
    ((request, point, miles),) = b.context["requests"]
    assert (request, point) == (str(world.poi_proposal.id), str(world.point.id))
    assert miles == pytest.approx(1.0, abs=0.01)
    assert "points" not in b.context
    assert b.context["fiber"] is None  # no FCC file on this data root


def test_prospect_values_and_protected_shares_are_stored(
    factory: sessionmaker[Session], world: World, report: dict[str, Any]
) -> None:
    (c,) = _rows(factory, world.line_c.id)
    assert c.hard_protected_share == 0.6 and c.protected_share == 0.6
    assert c.prospect["protected_lands"]["f"] == [["Fixture Preserve", 0.6, "1"]]
    assert c.prospect["wind_speed_100m"] == {"st": "unavailable"}
    a0 = _rows(factory, world.line_a.id)[0]
    assert a0.hard_protected_share == 0.0
    assert a0.prospect["solar_ghi"]["mean"] == pytest.approx(5.2, abs=0.001)
    assert a0.source_id == "us.osm.power" and a0.source_url == "https://www.openstreetmap.org/way/1"


def test_a_run_replaces_the_previous_one_and_is_what_is_served(
    factory: sessionmaker[Session], world: World
) -> None:
    first = compute.refresh(factory, world.data_root, sampler=FakeSampler())
    second = compute.refresh(factory, world.data_root, sampler=FakeSampler())
    with factory() as db:
        current = compute.current_run(db)
        assert current is not None and str(current.id) == second["run_id"]
        runs = {str(r.id): r.status for r in db.scalars(sa.select(SiteFinderRun))}
        assert runs == {first["run_id"]: "ok", second["run_id"]: "ok"}
        ids = set(db.scalars(sa.select(SiteCandidate.run_id)))
        assert ids == {current.id}
        assert current.inputs["flood"]["version"] == "20261023T0000Z"
        assert current.layers["protected_lands"]["provenance"]["source_id"] == "us.usgs.padus"
        assert current.stats["segments"] == 5


def test_a_failed_run_keeps_the_previous_run_serving(
    factory: sessionmaker[Session], world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    good = compute.refresh(factory, world.data_root, sampler=FakeSampler())

    def boom(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        raise RuntimeError("measured nothing")

    monkeypatch.setattr(compute, "line_rows", boom)
    with pytest.raises(RuntimeError):
        compute.refresh(factory, world.data_root, sampler=FakeSampler())
    with factory() as db:
        current = compute.current_run(db)
        assert current is not None and str(current.id) == good["run_id"]
        failed = db.scalars(sa.select(SiteFinderRun).where(SiteFinderRun.status == "failed")).one()
        assert "measured nothing" in (failed.error or "")
        assert (
            db.scalar(
                sa.select(sa.func.count()).select_from(SiteCandidate).where(SiteCandidate.run_id == failed.id)
            )
            == 0
        )
        assert db.scalar(sa.select(sa.func.count()).select_from(SiteCandidate)) == 5


def test_without_a_flood_build_every_band_is_unavailable_not_outside(
    factory: sessionmaker[Session], world: World
) -> None:
    (world.data_root / "normalized" / "context" / "us.fema.nfhl" / "latest.json").unlink()
    compute.refresh(factory, world.data_root, sampler=FakeSampler())
    for line in (world.line_a, world.line_b):
        for row in _rows(factory, line.id):
            assert (
                row.flood_cover_100 == 0.0
                and row.flood_share_100 is None
                and row.flood_reason == "not_computed"
            )


def test_states_narrow_the_lines(factory: sessionmaker[Session], world: World) -> None:
    report = compute.refresh(factory, world.data_root, states=["OK"], sampler=FakeSampler())
    assert report["segments"] == 0


def test_requests_are_kept_per_request_whatever_their_visibility_and_only_within_the_radius(
    factory: sessionmaker[Session], world: World
) -> None:
    """The run serves every tier, so a hidden request is kept, as its own entry, for the API to drop at
    the caller's tier; a request beyond the radius is not kept at all, so it can never be attributed
    to the band's neighbourhood."""
    with factory() as db:
        hidden = queue_request(
            db, world, suffix="hidden", geom=(-96.995, 30.0 - 2 * MILE_LAT), capacity_mw=80.0
        )
        far = queue_request(db, world, suffix="far", geom=(-96.995, 30.0 - 200 * MILE_LAT), capacity_mw=50.0)
        db.execute(sa.update(Proposal).where(Proposal.id == hidden.id).values(publish_state="unpublished"))
        db.commit()
    compute.refresh(factory, world.data_root, sampler=FakeSampler())
    (b,) = _rows(factory, world.line_b.id)
    kept = {r[0]: r for r in b.context["requests"]}
    assert set(kept) == {str(world.poi_proposal.id), str(hidden.id)}
    assert str(far.id) not in kept
    assert kept[str(hidden.id)][1] == str(world.point.id)
    assert kept[str(hidden.id)][2] == pytest.approx(2.0, abs=0.01)
    # Nearest first.
    assert [r[0] for r in b.context["requests"]] == [str(world.poi_proposal.id), str(hidden.id)]


def test_segments_store_their_own_vertices(
    factory: sessionmaker[Session], world: World, report: dict[str, Any]
) -> None:
    """A band is drawn from the run's vertices, so they join back into the line the run measured."""
    a = _rows(factory, world.line_a.id)
    joined = join_segments([r.coords for r in a])
    assert joined[0].tolist() == [-98.0, 31.0] and joined[-1].tolist() == [-97.975, 31.0]
    assert length_mi(joined) == pytest.approx(sum(r.length_mi for r in a), rel=1e-4)
    for r in a:
        assert r.coords[0][1] == 31.0 and len(r.coords) >= 2
    assert report["flood_reads"] == {"states": 1, "layers": 1}


def test_lines_are_measured_grouped_by_the_flood_states_they_need(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A two-state flood build and lines whose bands reach one state or both: measured in
    `measuring_order`, each set of states builds its layer once and each state's files are read
    once. The order the lines were loaded in (home state, then id) alternated between {TX} and
    {LA, TX} at every border line and rebuilt the layer, and re-read Texas, each time."""
    import shapely

    def fake_state(_self: FloodData, state: str) -> dict[str, Any]:
        box = (
            shapely.box(-100.0, 29.0, -94.0, 33.0) if state == "TX" else shapely.box(-94.0, 29.0, -89.0, 33.0)
        )
        empty: tuple[list[dict[str, Any]], list[Any]] = ([], [])
        return {"aoi": box, "tolerance_m": 10.0, "sfha": empty, "zones_other": empty, "coverage": empty}

    monkeypatch.setattr(FloodData, "read_state", fake_state)
    flood = FloodData({"states": {"TX": {}, "LA": {}}}, tmp_path)

    def line(n: int, regions: list[str]) -> compute.LineInput:
        return compute.LineInput(
            id=_uuid.uuid4(),
            public_id=f"asset_{n:03d}",
            attributes={"voltage_kv": 345.0, "scope_regions": regions, "states_crossed": regions},
            parts=[],
            source_id="us.osm.power",
            source_url="",
            retrieved_at=dt.datetime(2026, 10, 3, tzinfo=dt.UTC),
            licence_id="osm-lic",
            state_code="US-TX",
        )

    lines = [line(i, ["US-TX"] if i % 2 else ["US-TX", "US-LA"]) for i in range(10)]
    inputs = compute.Inputs(lines, *([None] * 6), flood, None, {}, {})  # type: ignore[arg-type]
    order = compute.measuring_order(inputs)
    for ln in order:
        compute._flood_layer(inputs, ln)
    assert (flood.layers_built, flood.state_reads) == (2, 2)
    assert [compute.flood_key(inputs, ln) for ln in order] == [("LA", "TX")] * 5 + [("TX",)] * 5
