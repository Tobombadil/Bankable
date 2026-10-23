"""The hubs and zones each price connector serves: ids, placement grades and evidence, label
points inside their regions, link-outs (docs/29 §1, §5; slice 1)."""

from __future__ import annotations

import pytest

from pipeline.connectors.prices import PRICE_CONNECTORS
from pipeline.connectors.prices.locations import (
    BY_ID,
    GRADES,
    KINDS,
    LINK_OUTS,
    LOCATIONS,
    _region_index,
    for_source,
    label_point,
    location_id,
    reference_of,
)
from pipeline.connectors.registry import Registry


def _inside(lon: float, lat: float, ring: list[list[float]]) -> bool:
    inside = False
    for (x0, y0), (x1, y1) in zip(ring, ring[1:] + ring[:1], strict=True):
        if (y0 > lat) != (y1 > lat) and lon < (x1 - x0) * (lat - y0) / (y1 - y0) + x0:
            inside = not inside
    return inside


def test_counts_per_market() -> None:
    counts: dict[str, int] = {}
    for loc in LOCATIONS:
        counts[loc.market] = counts.get(loc.market, 0) + 1
    assert counts == {"ERCOT": 15, "SPP": 3, "CAISO": 7, "NYISO": 11, "GB": 1}
    assert len(BY_ID) == len(LOCATIONS) == 37


def test_ids_are_stable_slugs() -> None:
    assert location_id("ERCOT", "HB_NORTH") == "ercot.hb_north"
    assert location_id("NYISO", "N.Y.C.") == "nyiso.n_y_c"
    assert location_id("CAISO", "TH_SP15_GEN-APND") == "caiso.th_sp15_gen_apnd"
    assert all(loc.id == location_id(loc.market, loc.name) for loc in LOCATIONS)


def test_every_location_has_a_kind_and_a_placement_with_its_evidence() -> None:
    for loc in LOCATIONS:
        assert loc.kind in KINDS and loc.placement.grade in GRADES, loc.id
        assert loc.placement.method and loc.placement.basis and loc.placement.region_id, loc.id
        if loc.placement.grade == "point":
            assert loc.placement.lat is not None and loc.placement.lon is not None
            assert loc.placement.evidence_url and loc.placement.retrieved_at, loc.id
        else:
            assert loc.placement.lat is None and loc.placement.lon is None, loc.id


def test_only_spp_hubs_are_points_and_each_lies_in_its_recorded_state() -> None:
    points = [loc for loc in LOCATIONS if loc.placement.grade == "point"]
    assert {loc.market for loc in points} == {"SPP"} and len(points) == 3
    index = _region_index()
    for loc in points:
        polygons = index[loc.placement.region_id]
        assert any(_inside(loc.placement.lon, loc.placement.lat, poly[0]) for poly in polygons), loc.id


@pytest.mark.parametrize("region_id", sorted({loc.placement.region_id for loc in LOCATIONS}))
def test_label_points_fall_inside_their_region(region_id: str) -> None:
    point = label_point(region_id)
    assert point is not None, region_id
    assert "label only" in point["method"]
    polygons = _region_index()[region_id]
    assert any(_inside(point["lon"], point["lat"], poly[0]) for poly in polygons), (region_id, point)


def test_basis_references_are_a_hub_in_the_same_market() -> None:
    refs = {loc.market: loc.reference for loc in LOCATIONS}
    assert refs == {
        "ERCOT": "HB_NORTH",
        "SPP": "SPPNORTH_HUB",
        "CAISO": "TH_SP15_GEN-APND",
        "NYISO": None,
        "GB": None,
    }
    for loc in LOCATIONS:
        ref = reference_of(loc)
        if ref is not None:
            assert ref.kind == "hub" and ref.market == loc.market
    assert reference_of(BY_ID["ercot.hb_north"]) is None  # no basis of a hub to itself


def test_each_connector_serves_its_own_locations() -> None:
    for source_id in PRICE_CONNECTORS:
        if source_id == "us.eia.retail_sales":
            continue
        assert for_source(source_id), source_id
    assert sum(len(for_source(s)) for s in PRICE_CONNECTORS) == len(LOCATIONS)


def test_link_outs_name_a_registered_row_that_is_not_fetched() -> None:
    registry = Registry()
    assert [lo.market for lo in LINK_OUTS] == ["PJM", "MISO", "ISO-NE"]
    for lo in LINK_OUTS:
        entry = registry.get(lo.source_id)
        assert entry.raw.get("status") == "pending" and entry.publication == "none"
        assert lo.url.startswith("https://") and lo.reason and lo.reference.startswith("docs/29")
        assert lo.source_id not in PRICE_CONNECTORS
