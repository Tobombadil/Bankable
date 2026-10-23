"""Land parcels per candidate segment (`services/site_finder/parcels.py`) and the in-place re-measure
(`compute.refresh_parcels`), on the site finder's fixture world with the TxGIO builder's synthetic county
beside line A (`services/site_finder/testing.py::write_parcels`): what a segment stores at each width, the
assemblies, the band merge, coverage, appraisal-record links, and that no owner value is ever stored."""

from __future__ import annotations

import json
import pathlib
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session, sessionmaker

from services.db.models import SiteCandidate, SiteFinderRun
from services.db.session import get_engine, get_sessionmaker, init_db
from services.site_finder import parcels as pm
from services.site_finder.cad_links import cad_link, directory_slug
from services.site_finder.compute import refresh, refresh_parcels
from services.site_finder.geometry import LocalPlane
from services.site_finder.testing import FakeSampler, World, seed

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "pipeline" / "context" / "fixtures" / "txgio_parcels"


@pytest.fixture()
def factory(tmp_path: pathlib.Path) -> Iterator[sessionmaker[Session]]:
    engine = get_engine(f"sqlite+pysqlite:///{tmp_path / 'sf-parcels.db'}")
    init_db(engine)
    yield get_sessionmaker(engine)
    engine.dispose()


@pytest.fixture()
def world(factory: sessionmaker[Session], tmp_path: pathlib.Path) -> World:
    with factory() as db:
        w = seed(db, tmp_path / "data")
    refresh(factory, w.data_root, sampler=FakeSampler())
    return w


def _contexts(factory: sessionmaker[Session], line_id: Any) -> list[dict[str, Any] | None]:
    with factory() as db:
        rows = db.execute(
            sa.select(SiteCandidate.context)
            .where(SiteCandidate.line_asset_id == line_id)
            .order_by(SiteCandidate.segment_index)
        ).all()
    return [(r.context or {}).get("parcels") for r in rows]


def _owner_values() -> set[str]:
    doc = json.loads((FIXTURE / "synthetic_county.geojson").read_text(encoding="utf-8"))
    out: set[str] = set()
    for f in doc["features"]:
        for name in ("OWNER_NAME", "NAME_CARE", "SITUS_ADDR", "MAIL_ADDR", "LEGAL_DESC"):
            v = f["properties"].get(name)
            if v and str(v).strip() and "CONFIDENTIAL" not in str(v).upper():
                out.add(str(v).strip().upper())
    return out | {"OWNER ALPHA", "OWNER DELTA", "SYNTH SITUS", "SYNTH MAIL"}


# ------------------------------------------------------------------------------------- assemblies
def test_assemble_joins_the_largest_neighbours_up_to_each_cap() -> None:
    nodes = {"a": 300.0, "b": 200.0, "c": 150.0, "d": 100.0, "e": 50.0, "lone": 400.0}
    chain = {"a": {"b"}, "b": {"a", "c"}, "c": {"b", "d"}, "d": {"c", "e"}, "e": {"d"}}
    best = pm.assemble(nodes, chain, caps=(1, 3, 5, 10))
    # One owner: the 400-acre holding that touches nobody; three: a+b+c; five or more: the whole chain.
    assert best == [400.0, 650.0, 800.0, 800.0]
    assert pm.assemble({}, {}) == [0.0, 0.0, 0.0, 0.0]


def test_assemble_counts_land_two_holdings_share_once() -> None:
    """Two owners on one 261.6-acre outline (undivided interests) and a 100-acre neighbour: any number of
    owners assembles 361.6 acres, not 623.2; whichever of the two is the seed, the other adds nothing."""
    nodes = {"a": 261.6, "b": 261.6, "c": 100.0}
    graph = {"a": {"b", "c"}, "b": {"a", "c"}, "c": {"a", "b"}}
    shared = pm.Shared({"a": {"b": 261.6}, "b": {"a": 261.6}}, {})
    assert pm.assemble(nodes, graph, caps=(1, 3, 10), shared=shared) == [261.6, 361.6, 361.6]
    # Without the shared land listed, the same holdings would read 623.2 acres "contiguous".
    assert pm.assemble(nodes, graph, caps=(1, 3, 10))[1] == pytest.approx(623.2)
    # Past its listing cap a holding's shared land is one figure, subtracted whoever joins (its partner
    # still lists it): still a lower bound, never a double count, whichever is the seed.
    capped = pm.Shared({"a": {"b": 261.6}}, {"b": 261.6})
    assert pm.assemble(nodes, graph, caps=(1, 3), shared=capped) == [261.6, 361.6]


def test_stacked_outlines_beside_a_line_are_never_counted_twice(tmp_path: pathlib.Path) -> None:
    """The stacked fixture (`pipeline/context/fixtures/txgio_parcels/stacked_outlines.geojson`) beside a
    line 110 m south of its first two squares: one owner's two accounts on one outline, two owners on the
    next outline, another owner's lot inside the first. At a quarter mile the assemblage is the two
    squares (about 526 acres), not the 789 acres the outlines' sum would claim. The right-of-way strip and
    the exempt tract are counted as holdings touching the band but left out of every acreage figure."""
    import numpy as np

    from pipeline.context import txgio_parcels as tp
    from services.context_sample.parcels import ParcelLayer, parcels_dir
    from services.site_finder.testing import write_parcels

    write_parcels(tmp_path, "stacked_outlines.geojson")
    layer = ParcelLayer(parcels_dir(tmp_path))
    square = tp.ellipsoid_band_area_m2(30.50, 30.51, 0.01) / tp.ACRE_M2
    ctx = pm.ParcelContext(layer, lambda lons, lats: [("48999", "Fixture County")] * len(lons))
    plane = LocalPlane(-97.49, 30.499)
    (seg,) = ctx.measure(plane, [plane.xy(np.array([[-97.50, 30.499], [-97.48, 30.499]]))])
    assert seg is not None and seg["cov"] == "ok"
    quarter = seg["b"][0]
    assert quarter[0] == pytest.approx(square, abs=1.0)  # one owner: one square
    assert quarter[-1] == pytest.approx(2 * square, abs=1.0)  # any owners: two squares, once each
    # At a mile the half-overlapping pair to the north joins as its union (1.5 squares), and the lot
    # inside the first square adds nothing.
    assert seg["b"][2][-1] == pytest.approx(3.5 * square, abs=2.0)
    # Left out: the strip from a mile, the exempt tract at two; counted in `n`, absent from `h`.
    assert seg["x"] == [0, 0, 1, 2]
    listed = {pm.split_key(h)[1] for h, *_ in seg["h"]}
    kinds = dict(
        zip(
            *(
                layer.read_bbox((-97.6, 30.4, -97.3, 30.6)).column(c).to_pylist()
                for c in ("holding_id", "holding_kind")
            ),
            strict=True,
        )
    )
    assert not {h for h in listed if kinds[h] != "land"}
    assert seg["n"][3] == len([h for h, *_ in seg["h"] if h]) + 2


def test_holdings_of_two_counties_with_one_id_stay_apart() -> None:
    """Holding ids are drawn per county build, so a band crossing a county line keys them by county too."""
    one = _ctx(
        [["48351:h1", 300.0, 2, 0]], [[300.0] * 4] * 4, [1] * 4, d={"48351:h1": ["rural", 900, "48351", "7"]}
    )
    two = _ctx(
        [["48241:h1", 200.0, 1, 0]], [[200.0] * 4] * 4, [1] * 4, d={"48241:h1": ["rural", 800, "48241", "9"]}
    )
    merged = pm.merge_band([one, two], 0.25)
    assert [(h.fips, h.holding_id, h.acres) for h in merged["top"]] == [
        ("48351", "h1", 300.0),
        ("48241", "h1", 200.0),
    ]
    assert merged["holdings_20_plus"] == 2 and merged["top"][1].link_prop_id == "9"


def test_merge_band_counts_the_holdings_touching_exactly_or_at_least() -> None:
    b = [[50.0] * 4] * 4
    single = pm.merge_band([_ctx([["48999:h1", 50.0, 1, 0]], b, [7, 7, 7, 7], x=[1, 1, 1, 1])], 0.25)
    assert single["holdings_touching"] == 7 and single["holdings_touching_exact"] is True
    assert single["left_out_at_least"] == 1 and single["smaller_holdings_at_least"] == 5
    # Two segments: the large holdings are known by id, the small ones only as counts: at least.
    one = _ctx([["48999:h1", 50.0, 1, 0]], b, [4, 4, 4, 4])
    two = _ctx([["48999:h2", 40.0, 1, 0]], b, [6, 6, 6, 6], x=[1, 1, 1, 1])
    both = pm.merge_band([one, two], 0.25)
    assert both["holdings_touching"] == 2 + 4 + 1 and both["holdings_touching_exact"] is False
    # Every holding large and listed: the union of the ids is exact.
    big = pm.merge_band(
        [_ctx([["48999:h1", 50.0, 1, 0]], b, [1] * 4), _ctx([["48999:h2", 40.0, 1, 0]], b, [1] * 4)], 0.25
    )
    assert big["holdings_touching"] == 2 and big["holdings_touching_exact"] is True


def test_each_measurement_draws_a_new_measure_id(factory: sessionmaker[Session], world: World) -> None:
    """The API keys its parcel caches on the run's measure id, so a re-measure in place that keeps the
    layer's version (a wider `PARCELS_COUNTIES`, a `--counties` run) is never served from old figures."""

    def stamp() -> tuple[str, str]:
        with factory() as db:
            run = db.scalars(sa.select(SiteFinderRun).where(SiteFinderRun.status == "ok")).one()
        meta = run.inputs["parcels"]
        return meta["version"], meta["measure_id"]

    version, first = stamp()
    refresh_parcels(factory, world.data_root, counties=["48999"])
    again_version, second = stamp()
    assert again_version == version and second != first and len(second) == 32


def test_assemble_is_greedy_from_the_largest_holdings_so_it_is_a_lower_bound() -> None:
    # From "big" the greedy join takes "mid" (the larger neighbour), missing big+small+huge-behind-small.
    nodes = {"big": 100.0, "mid": 60.0, "small": 10.0, "behind": 500.0}
    graph = {"big": {"mid", "small"}, "mid": {"big"}, "small": {"big", "behind"}, "behind": {"small"}}
    best = pm.assemble(nodes, graph, caps=(1, 3))
    # Seeded from "behind" too, so the best three-holding join is found there: behind+small+big.
    assert best == [500.0, 610.0]


# ------------------------------------------------------------------------------- per segment, world
def test_line_a_segments_store_the_holdings_beside_them(factory: sessionmaker[Session], world: World) -> None:
    contexts = _contexts(factory, world.line_a.id)
    assert len(contexts) == 3 and all(c and c["cov"] == "ok" for c in contexts)
    for ctx in contexts:
        assert ctx is not None
        # Holdings touching the band grow with its width; the largest (Delta, 632 acres) touches each one.
        assert ctx["n"] == sorted(ctx["n"]) and ctx["n"][-1] == 12
        assert ctx["b"][0][0] == pytest.approx(632.0, abs=1.0)
        for widths in ctx["b"]:
            assert widths == sorted(widths)  # more owners never assemble less land
        listed = {h: (acres, parcels, w) for h, acres, parcels, w in ctx["h"]}
        # Keyed by county and holding id together: an id is drawn per county build.
        assert all(pm.split_key(h)[0] == "48999" and pm.split_key(h)[1].startswith("h") for h in listed)
        top = max(listed.values())
        assert top[0] == pytest.approx(632.0, abs=1.0) and top[1] == 1
        # Alpha's three parcels (118.5 acres) are one holding, listed once with its parcel count.
        assert any(p == 3 and a == pytest.approx(118.5, abs=0.5) for a, p, _ in listed.values())
        for hid, detail in ctx["d"].items():
            assert hid in listed
            land_use, _vpa, fips, prop = detail
            assert fips == "48999" and prop and land_use


def test_neighbouring_holdings_are_those_within_30_m(world: World) -> None:
    """Alpha's holding has Bravo's (20 m west) and Charlie's first parcel (20 m east) for neighbours;
    Delta's ranch, 200 m south of Alpha, has none, so assembling more owners never adds to it."""
    import numpy as np
    import shapely

    from services.context_sample.parcels import ParcelLayer, parcels_dir

    layer = ParcelLayer(parcels_dir(world.data_root))
    table = layer.read_bbox((-98.1, 30.9, -97.9, 31.1))
    plane = LocalPlane(-97.99, 31.0)
    geoms = plane.to_xy(shapely.from_wkb(np.asarray(table.column("geometry").to_pylist(), dtype=object)))
    hids = table.column("holding_id").to_pylist()
    acres = table.column("holding_acres").to_pylist()
    graph = pm.ParcelContext._neighbours(geoms, hids, acres)
    by_acres = {round(a): h for h, a in zip(hids, acres, strict=True)}
    alpha, bravo, delta = by_acres[118], by_acres[75], by_acres[632]
    assert bravo in graph[alpha] and delta not in graph
    nodes = dict(zip(hids, acres, strict=True))
    best = pm.assemble(nodes, graph, caps=(1, 3))
    assert best[0] == pytest.approx(632.0, abs=1.0)
    alone = {h: a for h, a in nodes.items() if h != delta}
    # Without Delta: Alpha (118.5) + Bravo (75) + Echo (79) or Charlie (39.5), whichever touches.
    three = pm.assemble(alone, graph, caps=(1, 3))[1]
    assert three > 118.5 + 75.0


def test_a_band_far_from_any_parcel_stores_zero_and_one_outside_the_counties_stores_nothing(
    factory: sessionmaker[Session], world: World
) -> None:
    (bravo,) = _contexts(factory, world.line_b.id)
    assert bravo == {
        "cov": "ok",
        "miss": [],
        "n": [0, 0, 0, 0],
        "x": [0, 0, 0, 0],
        "h": [],
        "d": {},
        "b": [[0.0, 0.0, 0.0, 0.0]] * 4,
    }
    (charlie,) = _contexts(factory, world.line_c.id)
    assert charlie is None  # no loaded county anywhere near: the API names the band's own county


def test_no_owner_value_is_stored_in_any_segment(factory: sessionmaker[Session], world: World) -> None:
    with factory() as db:
        contexts = [r.context for r in db.execute(sa.select(SiteCandidate.context))]
        blob = json.dumps(contexts).upper()
        blob += json.dumps([r.inputs for r in db.execute(sa.select(SiteFinderRun.inputs))]).upper()
    # The segments do carry parcels (so the check below is not vacuous): holdings, by id, with acres.
    measured = [c["parcels"] for c in contexts if c and (c.get("parcels") or {}).get("h")]
    assert len(measured) >= 3 and all(len(m["h"]) >= 5 for m in measured)
    assert sum(1 for m in measured for h in m["h"] if "48999:h" in h[0]) >= 15
    assert not sorted(v for v in _owner_values() if v in blob)


def test_the_run_records_the_parcel_layer_it_read(factory: sessionmaker[Session], world: World) -> None:
    with factory() as db:
        run = db.scalars(sa.select(SiteFinderRun).where(SiteFinderRun.status == "ok")).one()
    parcels = run.inputs["parcels"]
    assert parcels["loaded"] is True and parcels["version"] == "v1/20261001-20261023T0900Z"
    assert parcels["counties"]["48999"]["parcels"] == 16


def test_coverage_names_the_counties_whose_parcels_are_not_loaded(world: World) -> None:
    from services.context_sample.parcels import ParcelLayer, parcels_dir

    layer = ParcelLayer(parcels_dir(world.data_root))

    def lookup(lons: Any, lats: Any) -> list[tuple[str, str] | None]:
        # West of -97.99 is the fixture county; east of it a county with no parcels.
        return [("48999", "Fixture County") if x < -97.99 else ("48001", "Anderson County") for x in lons]

    ctx = pm.ParcelContext(layer, lookup)
    plane = LocalPlane(-97.99, 31.0)
    coords = plane.xy(__import__("numpy").array([[-98.0, 31.0], [-97.995, 31.0]]))
    (seg,) = ctx.measure(plane, [coords])
    assert seg is not None and seg["cov"] == "partial" and seg["miss"] == [["48001", "Anderson County"]]
    assert seg["n"][-1] > 0


def test_refresh_parcels_re_measures_the_served_run_in_place(
    factory: sessionmaker[Session], world: World
) -> None:
    before = _contexts(factory, world.line_a.id)
    with factory() as db:
        for row in db.scalars(sa.select(SiteCandidate)):
            ctx = dict(row.context or {})
            ctx.pop("parcels", None)
            row.context = ctx
        db.commit()
    assert _contexts(factory, world.line_a.id) == [None, None, None]
    report = refresh_parcels(factory, world.data_root, counties=["48999"])
    assert report["segments_measured"] == 4 and report["segments_written"] == 4  # A's three, B's one
    after = _contexts(factory, world.line_a.id)
    # Holding ids are the layer's (the same files), so the figures match the full run's exactly.
    assert after == before


# ------------------------------------------------------------------------------------ band merge
def _ctx(h: list[list[Any]], b: list[list[float]], n: list[int], **extra: Any) -> dict[str, Any]:
    return {"cov": "ok", "miss": [], "h": h, "b": b, "n": n, "d": {}, **extra}


def test_merge_band_unions_holdings_and_takes_the_widths_figures() -> None:
    b = [[50.0, 60.0, 60.0, 60.0], [120.0, 200.0, 210.0, 210.0], [120.0, 200.0, 210.0, 210.0], [700.0] * 4]
    one = _ctx(
        [["h1", 120.0, 3, 1], ["h2", 50.0, 1, 0], ["h9", 700.0, 1, 3]],
        b,
        [2, 5, 6, 9],
        d={"h1": ["open_space", 3000, "48351", "11"]},
    )
    two = _ctx([["h1", 120.0, 3, 0], ["h3", 25.0, 1, 1]], b, [1, 4, 4, 6])
    merged = pm.merge_band([one, two], 0.5)
    assert merged["status"] == "ok"
    assert [h.holding_id for h in merged["top"]] == ["h1", "h2", "h3"]  # h9 touches only the 2-mile band
    assert merged["top"][0].land_use == "open_space" and merged["top"][0].link_prop_id == "11"
    assert merged["largest_acres"] == 120.0 and merged["holdings_20_plus"] == 3
    assert merged["at_threshold"]["100"] == 1 and merged["at_threshold"]["20"] == 3
    # At least: the larger of the segments' smaller-holding counts beyond those listed.
    assert merged["smaller_holdings_at_least"] == 3
    assert merged["assembly_acres"] == 210.0 and merged["owners_cap"] == 10
    assert pm.merge_band([one, two], 0.5, max_owners=1)["assembly_acres"] == 120.0
    assert pm.merge_band([one, two], 0.5, max_owners=3, min_acres=200)["meets_min_acres"] is True
    assert pm.merge_band([one, two], 2.0)["largest_acres"] == 700.0


def test_merge_band_says_when_parcels_are_partial_or_not_loaded() -> None:
    measured = _ctx([], [[0.0] * 4] * 4, [0] * 4)
    partial = pm.merge_band([measured, None], 1.0)
    assert partial["status"] == "partial"
    none = pm.merge_band([{"cov": "none", "miss": [["22019", "Calcasieu Parish"]]}, None], 1.0)
    assert none["status"] == "not_loaded" and none["largest_acres"] is None
    assert none["counties_missing"] == [{"fips": "22019", "name": "Calcasieu Parish"}]
    capped = pm.merge_band([_ctx([["h1", 30.0, 1, 0]], [[30.0] * 4] * 4, [1] * 4, more=True)], 1.0)
    assert capped["holdings_20_plus_exact"] is False


# ---------------------------------------------------------------------------------- record links
def test_appraisal_links_use_the_verified_record_route_or_say_where_to_search() -> None:
    newton = cad_link("48351", "123456", "Newton County")
    assert newton == {
        "kind": "record",
        "url": "https://esearch.newtoncad.org/Property/View/123456",
        "district": "Newton Central Appraisal District",
        "property_id": "123456",
        "verified": "2026-10-23",
    }
    assert cad_link("48245", "R 1/2", None)["url"] == "https://esearch.jcad.org/Property/View/R%201%2F2"  # type: ignore[index]
    pecos = cad_link("48371", "77", "Pecos County")
    assert pecos is not None and pecos["kind"] == "search" and pecos["property_id"] == "77"
    orange = cad_link("48361", "5", "Orange County")
    assert (
        orange is not None
        and orange["kind"] == "search"
        and orange["url"] == "https://esearch.orangecad.net/"
    )
    fort_bend = cad_link("48157", "9", "Fort Bend County")
    assert fort_bend is not None and fort_bend["kind"] == "directory"
    assert (
        fort_bend["url"] == "https://comptroller.texas.gov/taxes/property-tax/county-directory/fortbend.php"
    )
    assert directory_slug("De Witt County") == "dewitt"
    assert cad_link(None, "1", "X") is None and cad_link("48999", "1", None) is None


def test_a_crowded_outline_never_assembles_more_than_its_area(tmp_path: pathlib.Path) -> None:
    """Forty owners on one outline (a condominium's or heirs' accounts, each drawn as the whole tract)
    with the listing cap at 32: whichever owners join, in whatever order, the assemblage stays one
    tract, because each pair is listed or counted in `rest` on both sides."""
    import itertools

    import pyarrow.parquet as pq

    from pipeline.context import txgio_parcels as tp

    square = {
        "type": "Polygon",
        "coordinates": [[[-97.5, 30.5], [-97.49, 30.5], [-97.49, 30.51], [-97.5, 30.51], [-97.5, 30.5]]],
    }
    feats = [
        {
            "type": "Feature",
            "geometry": square,
            "properties": {
                "OBJECTID": i,
                "PROP_ID": str(i),
                "OWNER_NAME": f"SYNTH HEIR {i:02d}",
                "STAT_LAND_USE": "D1",
            },
        }
        for i in range(40)
    ]
    tp.build_county(tp.frame_from_features(feats, "48999", "Fixture"), tmp_path)
    rows = pq.read_table(tmp_path / "parcels" / "48999.parquet").to_pylist()
    area = rows[0]["holding_acres"]
    nodes = {r["holding_id"]: r["holding_acres"] for r in rows}
    shared = pm.Shared(
        {r["holding_id"]: {e["holding_id"]: e["acres"] for e in r["holding_overlaps"] or []} for r in rows},
        {r["holding_id"]: r["holding_overlap_rest_acres"] for r in rows},
    )
    assert max(len(r["holding_overlaps"] or []) for r in rows) <= tp.MAX_OVERLAP_PARTNERS
    for a, b in itertools.combinations(nodes, 2):
        # Each side either lists the other or counts the pair in its rest.
        for one, other in ((a, b), (b, a)):
            assert other in shared.pairs[one] or shared.rest[one] >= area - 0.01
    everyone = {h: set(nodes) - {h} for h in nodes}
    best = pm.assemble(nodes, everyone, caps=(1, 3, 10), shared=shared)
    assert best == pytest.approx([area] * 3, abs=0.05)
