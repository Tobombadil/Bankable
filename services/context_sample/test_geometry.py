"""The corridor lattice: inputs it accepts, the limits it enforces, and that its shares estimate areas."""

from __future__ import annotations

import math

import numpy as np
import pytest

from services.context_sample.geometry import (
    METRES_PER_MILE,
    LineError,
    corridor_lattice,
    parse_lines,
)
from services.context_sample.vectors import points_in_rings


def test_parse_lines_accepts_geojson_feature_and_bare_lists() -> None:
    line = [[-100.0, 30.0], [-99.5, 30.0]]
    assert len(parse_lines({"type": "LineString", "coordinates": line})) == 1
    assert len(parse_lines({"type": "MultiLineString", "coordinates": [line, line]})) == 2
    assert len(parse_lines({"type": "Feature", "geometry": {"type": "LineString", "coordinates": line}})) == 1
    assert len(parse_lines(line)) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"type": "Point", "coordinates": [0, 0]},
        {"type": "LineString", "coordinates": [[0, 0]]},
        {"type": "LineString", "coordinates": [[0, 0], [200, 0]]},
        {"type": "LineString", "coordinates": [["a", 0], [1, 1]]},
        42,
    ],
)
def test_parse_lines_refuses_bad_input(bad: object) -> None:
    with pytest.raises(LineError):
        parse_lines(bad)


def test_every_lattice_point_is_within_the_buffer() -> None:
    lines = parse_lines([[-100.0, 30.0], [-99.8, 30.1], [-99.6, 30.0]])
    lat = corridor_lattice(lines, 1.0)
    assert 1000 <= lat.count <= 2200
    # Distance check with an independent haversine to the nearest vertex-interpolated point.
    pts = np.column_stack([lat.lons, lat.lats])
    dense = np.vstack([np.linspace(a, b, 400) for a, b in zip(lines[0][:-1], lines[0][1:], strict=True)])
    r = 6_371_008.8
    for lon, lat_ in pts[:: max(1, len(pts) // 200)]:
        d = [
            2
            * r
            * math.asin(
                math.sqrt(
                    math.sin(math.radians(lat_ - y) / 2) ** 2
                    + math.cos(math.radians(lat_))
                    * math.cos(math.radians(y))
                    * math.sin(math.radians(lon - x) / 2) ** 2
                )
            )
            for x, y in dense
        ]
        assert min(d) <= 1.0 * METRES_PER_MILE * 1.01


def test_share_of_a_half_plane_is_about_half() -> None:
    lines = parse_lines([[-100.0, 30.0], [-99.5, 30.0]])
    lat = corridor_lattice(lines, 1.0)
    north = [np.array([[-101.0, 30.0], [-98.0, 30.0], [-98.0, 31.0], [-101.0, 31.0], [-101.0, 30.0]])]
    share = points_in_rings(lat.lons, lat.lats, north).mean()
    assert share == pytest.approx(0.5, abs=0.03)
    assert lat.length_m / METRES_PER_MILE == pytest.approx(29.8, rel=0.02)


def test_limits() -> None:
    lines = parse_lines([[-100.0, 30.0], [-99.5, 30.0]])
    with pytest.raises(LineError):
        corridor_lattice(lines, 0)
    with pytest.raises(LineError):
        corridor_lattice(lines, 11)
    with pytest.raises(LineError):
        corridor_lattice(parse_lines([[-110.0, 30.0], [-100.0, 30.0]]), 1)
    with pytest.raises(LineError):
        parse_lines([[float(i) / 1000, 30.0] for i in range(5001)])


def test_a_caller_chosen_spacing_too_fine_for_the_corridor_is_refused() -> None:
    line = parse_lines([[-103.0, 31.0], [-102.0, 31.0]])  # ~59 miles
    with pytest.raises(LineError, match="too fine for this corridor; use at least"):
        corridor_lattice(line, 2.0, spacing_m=10.0)
    with pytest.raises(LineError, match="positive"):
        corridor_lattice(line, 2.0, spacing_m=-5.0)
    assert corridor_lattice(line, 2.0, spacing_m=500.0).count > 1000


def _distance_to_meridian_m(lons: np.ndarray, lats: np.ndarray, lon0: float) -> np.ndarray:
    """Great-circle distance from each point to the meridian lon0 (the foot falls inside the line)."""
    r = 6_371_008.8
    return np.asarray(r * np.arcsin(np.abs(np.sin(np.radians(lons - lon0)) * np.cos(np.radians(lats)))))


def test_a_long_north_south_line_keeps_its_true_buffer() -> None:
    """A 240-mile north-south line: lattice points are kept by their true distance from the line. The
    equirectangular plane at the mean latitude kept points up to 1.6% beyond the buffer at the southern
    end (cos 28 / cos 29.7) and dropped them inside it at the northern end."""
    lat0, lat1 = 28.0, 28.0 + 240 * METRES_PER_MILE / 111_195.0
    lat = corridor_lattice(parse_lines([[-100.0, lat0], [-100.0, lat1]]), 1.0, spacing_m=100.0)
    inner = (lat.lats > lat0 + 0.05) & (lat.lats < lat1 - 0.05)
    d = _distance_to_meridian_m(lat.lons[inner], lat.lats[inner], -100.0) / METRES_PER_MILE
    south, north = lat.lats[inner] < lat0 + 0.5, lat.lats[inner] > lat1 - 0.5
    assert d.max() <= 1.0005
    # The outermost column sits at the same true distance at both ends: the band is one width throughout.
    assert d[south].max() == pytest.approx(d[north].max(), abs=0.001)
    assert d[south].max() > 1.0 - 100.0 / METRES_PER_MILE, "within one spacing of the buffer's edge"
    assert lat.length_m / METRES_PER_MILE == pytest.approx(240.0, rel=0.002)


def test_a_long_diagonal_segment_is_the_straight_lon_lat_line() -> None:
    """One 212-mile diagonal segment: GeoJSON means the straight line in lon/lat (RFC 7946), so the band
    follows it to within the check's resolution, not a line straight in some projected plane (which bows
    about a kilometre away from it here)."""
    line = [[-99.0, 26.0], [-97.0, 28.5]]
    lat = corridor_lattice(parse_lines(line), 2.0)
    dense = np.linspace(line[0], line[1], 4000)
    r = 6_371_008.8
    nearest = np.empty(lat.count)
    for k in range(0, lat.count, 500):
        p_lon, p_lat = np.radians(lat.lons[k : k + 500])[:, None], np.radians(lat.lats[k : k + 500])[:, None]
        q_lon, q_lat = np.radians(dense[:, 0])[None, :], np.radians(dense[:, 1])[None, :]
        h = (
            np.sin((p_lat - q_lat) / 2) ** 2
            + np.cos(p_lat) * np.cos(q_lat) * np.sin((p_lon - q_lon) / 2) ** 2
        )
        nearest[k : k + 500] = (2 * r * np.arcsin(np.sqrt(h))).min(axis=1)
    assert nearest.max() <= 2.0 * METRES_PER_MILE * 1.002
    band = 2 * lat.buffer_m * lat.length_m + math.pi * lat.buffer_m**2
    assert lat.count * lat.spacing_m**2 == pytest.approx(band, rel=0.01)


def test_far_apart_parts_are_sampled_separately_with_automatic_spacing() -> None:
    """Two 0.6-mile parts 500 miles apart (West Texas and Beaumont) at a 1-mile buffer: the old single
    bounding box refused the request ("spacing_m 97.65 is too fine") though no spacing was given."""
    west = [[-103.0, 31.0], [-102.9899, 31.0]]
    east = [[-94.1, 30.08], [-94.0899, 30.08]]
    lat = corridor_lattice(parse_lines({"type": "MultiLineString", "coordinates": [west, east]}), 1.0)
    assert lat.groups == 2
    assert 1_200 <= lat.count <= 1_800
    assert 0.4 <= float((lat.lons < -98).mean()) <= 0.6
    # Each part's points lie within the buffer of that part (great-circle distance to the densified part).
    r = 6_371_008.8
    for part, side in ((west, lat.lons < -98), (east, lat.lons > -98)):
        dense = np.linspace(part[0], part[1], 400)
        p_lon, p_lat = np.radians(lat.lons[side])[:, None], np.radians(lat.lats[side])[:, None]
        q_lon, q_lat = np.radians(dense[:, 0])[None, :], np.radians(dense[:, 1])[None, :]
        h = (
            np.sin((p_lat - q_lat) / 2) ** 2
            + np.cos(p_lat) * np.cos(q_lat) * np.sin((p_lon - q_lon) / 2) ** 2
        )
        nearest = (2 * r * np.arcsin(np.sqrt(h))).min(axis=1)
        assert float(nearest.max()) <= METRES_PER_MILE * 1.001


def test_a_bent_line_with_a_narrow_buffer_needs_no_spacing() -> None:
    """An L of two 118-mile legs at a 0.1-mile buffer: the old lattice laid out the whole bounding box
    (about 440,000 cells, over the cap) and refused; pieces cost what the band's area does."""
    lines = parse_lines([[-101.0, 31.0], [-99.0, 31.0], [-99.0, 32.7]])
    auto = corridor_lattice(lines, 0.1)
    # At least six columns across the 0.2-mile band, so more than the 1,500-point target.
    assert auto.spacing_m <= 2 * auto.buffer_m / 6 + 1e-9 and auto.count > 1_500
    # Equal-area lattice: points x cell area estimates the band's area (sides plus the end discs).
    fine = corridor_lattice(lines, 0.1, spacing_m=40.0)
    band = 2 * fine.buffer_m * fine.length_m + math.pi * fine.buffer_m**2
    assert fine.count * fine.spacing_m**2 == pytest.approx(band, rel=0.01)
    assert auto.count * auto.spacing_m**2 == pytest.approx(band, rel=0.05)


def test_a_long_narrow_band_is_never_one_cell_wide() -> None:
    """A 100-mile line at a quarter-mile buffer: the 1,500-point target alone gives a spacing of about
    240 m, two cells across the 805 m band; the spacing is capped at a sixth of the band."""
    lat = corridor_lattice(parse_lines([[-102.0, 31.0], [-100.32, 31.0]]), 0.25)
    assert lat.spacing_m == pytest.approx(2 * lat.buffer_m / 6)
    band = 2 * lat.buffer_m * lat.length_m + math.pi * lat.buffer_m**2
    assert lat.count * lat.spacing_m**2 == pytest.approx(band, rel=0.05)


def test_overlapping_pieces_count_each_point_once() -> None:
    """A line that doubles back on itself covers the same band twice; its points are counted once."""
    there = parse_lines([[-100.0, 30.0], [-99.8, 30.0]])
    back = parse_lines([[-100.0, 30.0], [-99.8, 30.0], [-100.0, 30.0], [-99.8, 30.0]])
    a = corridor_lattice(there, 0.5, spacing_m=80.0)
    b = corridor_lattice(back, 0.5, spacing_m=80.0)
    # Same grid; at most a cell or two on the buffer's edge differs (each piece tests in its own plane).
    assert abs(a.count - b.count) <= 2
    pts = np.column_stack([b.lons, b.lats])
    assert len(np.unique(np.round(pts, 9), axis=0)) == b.count
