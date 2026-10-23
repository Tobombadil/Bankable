"""Segmenting and distances (`services/site_finder/geometry.py`), against closed-form answers."""

from __future__ import annotations

import itertools
import math

import numpy as np
import pytest

from services.site_finder.geometry import (
    M_PER_DEG,
    METRES_PER_MILE,
    LocalPlane,
    PointIndex,
    haversine_m,
    join_segments,
    length_mi,
    points_to_polyline_m,
    split_line,
    sub_line,
)

MILE_LAT = METRES_PER_MILE / M_PER_DEG


def test_a_degree_of_latitude_is_the_mean_sphere_s() -> None:
    assert haversine_m(0.0, 30.0, 0.0, 31.0) == pytest.approx(M_PER_DEG, rel=1e-9)
    assert haversine_m(-98.0, 31.0, -98.0, 31.0 + MILE_LAT) / METRES_PER_MILE == pytest.approx(1.0, rel=1e-9)


def test_split_line_makes_equal_pieces_of_about_half_a_mile_covering_the_line() -> None:
    line = [(-98.0, 31.0), (-97.99, 31.0), (-97.975, 31.0)]
    total = length_mi(np.asarray(line))
    segments = split_line(line, 0.5)
    assert len(segments) == round(total / 0.5) == 3
    assert [s.index for s in segments] == [0, 1, 2]
    assert sum(s.length_mi for s in segments) == pytest.approx(total, rel=1e-9)
    assert all(s.length_mi == pytest.approx(total / 3, rel=1e-9) for s in segments)
    assert segments[1].start_mi == pytest.approx(total / 3, rel=1e-9)
    # Each piece ends where the next starts, and the vertex at -97.99 falls inside the second piece.
    for a, b in itertools.pairwise(segments):
        assert np.allclose(a.coords[-1], b.coords[0])
    assert any(np.allclose(v, (-97.99, 31.0)) for v in segments[1].coords)
    assert segments[1].midpoint[0] == pytest.approx(-97.9875, abs=1e-9)


def test_a_short_line_is_one_segment_and_a_degenerate_one_none() -> None:
    assert len(split_line([(-98.0, 31.0), (-97.999, 31.0)], 0.5)) == 1
    assert split_line([(-98.0, 31.0), (-98.0, 31.0)], 0.5) == []
    assert split_line([(-98.0, 31.0)], 0.5) == []


def test_sub_line_rebuilds_a_run_of_segments() -> None:
    coords = np.asarray([(-98.0, 31.0), (-97.99, 31.0), (-97.975, 31.0)])
    segments = split_line(coords, 0.5)
    run = sub_line(coords, segments[1].start_mi, segments[2].start_mi + segments[2].length_mi)
    assert np.allclose(run[0], segments[1].coords[0]) and np.allclose(run[-1], coords[-1])
    assert length_mi(run) == pytest.approx(segments[1].length_mi + segments[2].length_mi, rel=1e-9)


def test_point_to_polyline_distance_in_the_local_plane() -> None:
    line = np.asarray([(-98.0, 31.0), (-97.975, 31.0)])
    plane = LocalPlane.around(line)
    xy = plane.xy(line)
    # One mile due north of the western end, and one mile north of the middle.
    px, py = plane.forward(
        np.asarray([-98.0, -97.9875, -98.0 - 0.02]), np.asarray([31.0 + MILE_LAT, 31.0 + MILE_LAT, 31.0])
    )
    d = points_to_polyline_m(np.asarray(px), np.asarray(py), xy) / METRES_PER_MILE
    assert d[0] == pytest.approx(1.0, rel=1e-4)
    assert d[1] == pytest.approx(1.0, rel=1e-4)
    # Due west of the end: the distance is to the end point, 0.02 degrees of longitude at 31 N.
    assert d[2] == pytest.approx(0.02 * M_PER_DEG * math.cos(math.radians(31.0)) / METRES_PER_MILE, rel=2e-4)


def test_plane_round_trips() -> None:
    plane = LocalPlane(-98.0, 31.0)
    x, y = plane.forward(np.asarray([-97.5]), np.asarray([31.4]))
    lon, lat = plane.inverse(x, y)
    assert float(lon[0]) == pytest.approx(-97.5) and float(lat[0]) == pytest.approx(31.4)


def test_point_index_finds_points_within_a_radius_of_a_box() -> None:
    idx = PointIndex([-98.0, -97.0, -99.0], [31.0 + MILE_LAT, 30.0, 31.0])
    near = idx.near((-98.0, 31.0, -97.975, 31.0), 1.5 * METRES_PER_MILE)
    assert near.tolist() == [0]
    assert idx.near((-98.0, 31.0, -97.975, 31.0), 0.5 * METRES_PER_MILE).tolist() == []


def test_a_band_s_centreline_is_its_segments_stored_vertices_joined() -> None:
    """The API draws a band from the vertices its run stored (`SiteCandidate.coords`), joined at the
    shared cut points, so the line's later geometry never moves it."""
    coords = np.asarray([(-98.0, 31.0), (-97.99, 31.0), (-97.975, 31.0)])
    segments = split_line(coords, 0.5)
    joined = join_segments([s.coords.tolist() for s in segments[1:]])
    assert np.allclose(joined[0], segments[1].coords[0]) and np.allclose(joined[-1], coords[-1])
    # The cut point between segments 1 and 2 appears once.
    assert len(joined) == len(segments[1].coords) + len(segments[2].coords) - 1
    assert length_mi(joined) == pytest.approx(segments[1].length_mi + segments[2].length_mi, rel=1e-9)
    assert join_segments([]).shape == (0, 2) and join_segments([[], segments[0].coords]).shape[0] == 2
