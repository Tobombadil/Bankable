"""Tests for `pipeline.context.regions` against tiny synthetic shapefiles/GeoJSON built in the
test itself -- never a live download (`fetch`/`main` are not exercised here, same boundary
`pipeline/connectors/*/test_connector.py` draws around `fetch`)."""

from __future__ import annotations

import io

import pytest
import shapefile

from pipeline.context.regions import (
    country_features,
    country_region_id,
    county_features,
    state_features,
    state_postal_map,
    to_multipolygon,
)

# A square with one hole, and a two-part (island) shape -- exercises both the plain-Polygon and
# the MultiPolygon path pyshp's own __geo_interface__ already distinguishes.
_SQUARE_WITH_HOLE = [
    [[0.0, 0.0], [0.0, 10.0], [10.0, 10.0], [10.0, 0.0], [0.0, 0.0]],  # outer, clockwise
    [[2.0, 2.0], [4.0, 2.0], [4.0, 4.0], [2.0, 4.0], [2.0, 2.0]],  # hole, counter-clockwise
]
_TWO_ISLANDS = [
    [[20.0, 20.0], [20.0, 21.0], [21.0, 21.0], [21.0, 20.0], [20.0, 20.0]],
    [[30.0, 30.0], [30.0, 31.0], [31.0, 31.0], [31.0, 30.0], [30.0, 30.0]],
]


def _shapefile_reader(fields: list[str], records: list[tuple[list[list[list[float]]], list]]):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    w = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    for name in fields:
        w.field(name, "C")
    for polygon_parts, attrs in records:
        w.poly(polygon_parts)
        w.record(*attrs)
    w.close()
    shp.seek(0)
    shx.seek(0)
    dbf.seek(0)
    return shapefile.Reader(shp=shp, shx=shx, dbf=dbf)


@pytest.fixture
def counties():
    return _shapefile_reader(
        ["STATEFP", "COUNTYFP", "GEOID", "NAME"],
        [
            (_SQUARE_WITH_HOLE, ["48", "201", "48201", "Harris"]),
            (_TWO_ISLANDS, ["06", "001", "06001", "Alameda"]),
        ],
    )


@pytest.fixture
def states():
    return _shapefile_reader(
        ["STATEFP", "STUSPS", "NAME"],
        [
            (_SQUARE_WITH_HOLE, ["48", "TX", "Texas"]),
            (_TWO_ISLANDS, ["06", "CA", "California"]),
        ],
    )


def test_a_single_ring_polygon_is_wrapped_as_multipolygon():
    geo = {"type": "Polygon", "coordinates": _SQUARE_WITH_HOLE}
    out = to_multipolygon(geo, ndigits=4)
    assert out["type"] == "MultiPolygon"
    assert out["coordinates"] == [_SQUARE_WITH_HOLE]


def test_a_multipolygon_is_passed_through():
    geo = {"type": "MultiPolygon", "coordinates": [[_TWO_ISLANDS[0]], [_TWO_ISLANDS[1]]]}
    out = to_multipolygon(geo, ndigits=4)
    assert out["type"] == "MultiPolygon"
    assert out["coordinates"] == [[_TWO_ISLANDS[0]], [_TWO_ISLANDS[1]]]


def test_coordinates_are_rounded():
    geo = {"type": "Polygon", "coordinates": [[[1.123456, 2.987654]]]}
    out = to_multipolygon(geo, ndigits=4)
    assert out["coordinates"] == [[[[1.1235, 2.9877]]]]


def test_an_unexpected_geometry_type_raises():
    with pytest.raises(ValueError, match="unexpected geometry type"):
        to_multipolygon({"type": "LineString", "coordinates": []}, ndigits=4)


def test_state_postal_map_reads_statefp_to_stusps(states):
    assert state_postal_map(states) == {"48": "TX", "06": "CA"}


def test_state_features_shape(states):
    feats = state_features(states)
    ids = {f["properties"]["region_id"] for f in feats}
    assert ids == {"US-TX", "US-CA"}
    assert all(f["properties"]["level"] == "state" for f in feats)
    assert all(f["geometry"]["type"] == "MultiPolygon" for f in feats)


def test_county_features_join_state_code_from_the_postal_map(counties, states):
    fips_to_postal = state_postal_map(states)
    feats = county_features(counties, fips_to_postal)
    by_id = {f["properties"]["region_id"]: f["properties"] for f in feats}
    assert by_id["48201"] == {
        "region_id": "48201",
        "name": "Harris",
        "state_code": "US-TX",
        "level": "county",
    }
    assert by_id["06001"]["state_code"] == "US-CA"


def test_county_features_geometry_is_multipolygon(counties, states):
    fips_to_postal = state_postal_map(states)
    feats = county_features(counties, fips_to_postal)
    assert all(f["geometry"]["type"] == "MultiPolygon" for f in feats)


def test_county_with_unmapped_state_fips_gets_no_state_code(counties):
    feats = county_features(counties, fips_to_postal={})
    assert all(f["properties"]["state_code"] is None for f in feats)


def _ne_country(name: str, iso_a2: str, iso_a2_eh: str) -> dict:
    return {
        "type": "Feature",
        "properties": {"NAME": name, "ISO_A2": iso_a2, "ISO_A2_EH": iso_a2_eh},
        "geometry": {"type": "Polygon", "coordinates": _SQUARE_WITH_HOLE},
    }


def test_country_region_id_uses_iso_a2_when_present():
    assert country_region_id({"ISO_A2": "US", "ISO_A2_EH": "US"}) == "US"


def test_country_region_id_falls_back_to_iso_a2_eh_when_iso_a2_is_placeholder():
    # e.g. Norway/France/Kosovo in the real Natural Earth file, verified 2026-09-18.
    assert country_region_id({"ISO_A2": "-99", "ISO_A2_EH": "NO"}) == "NO"


def test_country_region_id_stays_the_placeholder_when_both_fields_are_it():
    # N. Cyprus and Somaliland have no code in either field in the real file -- known gap.
    assert country_region_id({"ISO_A2": "-99", "ISO_A2_EH": "-99"}) == "-99"


def test_country_features_shape():
    ne = {
        "type": "FeatureCollection",
        "features": [
            _ne_country("United States of America", "US", "US"),
            _ne_country("Norway", "-99", "NO"),
        ],
    }
    feats = country_features(ne)
    by_id = {f["properties"]["region_id"]: f["properties"] for f in feats}
    assert by_id["US"]["name"] == "United States of America"
    assert by_id["US"]["level"] == "country"
    assert by_id["NO"]["name"] == "Norway"
