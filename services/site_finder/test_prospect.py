"""The prospecting layers per segment and per band (`services/site_finder/prospect.py`): on canned
answers, and on the real `Sampler` over the prospect build made from the recorded fixtures, so the
compaction is checked against the answer shape the sampler actually returns."""

from __future__ import annotations

import pathlib
from typing import Any

import pytest

from services.context_sample.catalog import CATALOG
from services.site_finder import prospect

ANSWER = {
    "layers": [
        {
            "layer": "protected_lands",
            "status": "partial",
            "share": 0.4,
            "coverage_share": 0.8,
            "hard_constraint_share": 0.3,
            "features": [
                {"unit_name": "Big Bend", "share": 0.3, "gap_status": "1"},
                {"unit_name": "Ranch easement", "share": 0.1, "gap_status": "4"},
            ],
        },
        {
            "layer": "solar_ghi",
            "status": "ok",
            "summary": {"mean": 5.8, "min": 5.7, "max": 5.9, "coverage_share": 1.0},
        },
        {"layer": "wind_speed_100m", "status": "unavailable"},
        {
            "layer": "energy_community_ffe",
            "status": "ok",
            "share": 1.0,
            "qualifying_share": 0.0,
            "features": [],
        },
    ],
    "energy_community": {"status": "ok", "share": 0.25},
}

META = {
    "protected_lands": {
        "title": "Protected",
        "kind": "polygons",
        "not": "not a title record",
        "provenance": {"source_id": "us.usgs.padus"},
    },
    "solar_ghi": {
        "title": "GHI",
        "kind": "raster",
        "not": "not bankable",
        "provenance": {"source_id": "us.nrel.nsrdb.psm3_annual"},
    },
    "wind_speed_100m": {"title": "Wind", "kind": "raster", "not": "not bankable", "provenance": None},
    "energy_community_coal_closure": {
        "title": "Coal",
        "kind": "polygons",
        "not": "not tax advice",
        "provenance": {"dataset_as_of": "2024-06-07"},
    },
}


def test_compact_keeps_the_numbers_and_names_only() -> None:
    out = prospect.compact(ANSWER)
    assert out["protected_lands"] == {
        "st": "partial",
        "share": 0.4,
        "cov": 0.8,
        "hard": 0.3,
        "f": [["Big Bend", 0.3, "1"], ["Ranch easement", 0.1, "4"]],
    }
    assert out["solar_ghi"] == {"st": "ok", "mean": 5.8, "min": 5.7, "max": 5.9, "cov": 1.0}
    assert out["wind_speed_100m"] == {"st": "unavailable"}
    assert out["energy_community_ffe"]["q"] == 0.0
    assert out["energy_community"] == {"st": "ok", "share": 0.25}


def test_a_band_takes_length_weighted_values_and_the_worst_status() -> None:
    a = prospect.compact(ANSWER)
    b = {
        "protected_lands": {"st": "ok", "share": 0.0, "cov": 1.0, "hard": 0.0},
        "solar_ghi": {"st": "ok", "mean": 5.2, "min": 5.1, "max": 5.3},
        "wind_speed_100m": {"st": "unavailable"},
        "energy_community": {"st": "ok", "share": 0.75},
    }
    band = prospect.band_prospect([(a, 1.0), (b, 3.0)], META)
    by = {entry["layer"]: entry for entry in band["layers"]}
    assert by["protected_lands"]["status"] == "partial"  # a lower bound somewhere in the band
    assert by["protected_lands"]["share"] == 0.1 and by["protected_lands"]["hard_constraint_share"] == 0.075
    assert by["protected_lands"]["features"][0] == {"name": "Big Bend", "share": 0.075, "gap_status": "1"}
    assert by["protected_lands"]["not"] == "not a title record"
    assert by["solar_ghi"]["summary"] == {"mean": 5.35, "min": 5.1, "max": 5.9}
    assert by["wind_speed_100m"]["status"] == "unavailable" and "summary" not in by["wind_speed_100m"]
    assert by["wind_speed_100m"]["provenance"] is None
    assert band["energy_community"]["share"] == 0.625
    assert band["energy_community"]["dataset_as_of"] == {"energy_community_coal_closure": "2024-06-07"}
    assert band["buffer_mi"] == prospect.BUFFER_MI


#: A half-mile segment near Pecos, inside the fixture build's solar, seismic and polygon layers.
PECOS: dict[str, Any] = {"type": "LineString", "coordinates": [[-103.5, 31.2], [-103.4916, 31.2]]}


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    """Every prospect layer built once from the recorded fixtures (offline, about 8 s)."""
    from services.context_sample.fixture_build import build_fixture_layers

    out = tmp_path_factory.mktemp("prospect")
    report = build_fixture_layers(out)
    assert not report["failures"], report["failures"]
    return out


def test_the_real_sampler_s_answer_compacts_and_rebuilds_unchanged(built: pathlib.Path) -> None:
    """One segment's real answer, compacted as a candidate row stores it and rebuilt as the API serves
    a one-segment band: every layer keeps its status, its values, its largest features, what it is not
    and its provenance."""
    from services.context_sample import SITE_FINDER_LAYERS, Sampler

    sampler = Sampler(built)
    answer = sampler.sample_line(
        PECOS, prospect.BUFFER_MI, list(SITE_FINDER_LAYERS), spacing_m=prospect.SPACING_M
    )
    stored = prospect.compact(answer)
    meta = prospect.layer_metadata(sampler)
    band = prospect.band_prospect([(stored, 0.5)], meta)
    served = {entry["layer"]: entry for entry in band["layers"]}
    assert set(served) == set(SITE_FINDER_LAYERS) == {item["layer"] for item in answer["layers"]}
    statuses = {}
    for item in answer["layers"]:
        lid = item["layer"]
        entry = served[lid]
        statuses[lid] = item["status"]
        assert stored[lid]["st"] == item["status"] == entry["status"], lid
        assert entry["not"] == CATALOG[lid].not_statement and entry["screening"] == "screening"
        if item["status"] in ("withheld", "unavailable"):
            assert entry["provenance"] is None and "summary" not in entry and "share" not in entry
            continue
        assert entry["provenance"]["source_id"] == (sampler.entry(lid) or {})["provenance"]["source_id"]
        summary = item.get("summary")
        if isinstance(summary, dict):
            for key in ("mean", "min", "max"):
                if summary.get(key) is not None:
                    assert entry["summary"][key] == pytest.approx(summary[key], abs=1e-3), (lid, key)
            continue
        for key in ("share", "hard_constraint_share", "qualifying_share", "coverage_share"):
            if item.get(key) is not None:
                assert entry[key] == pytest.approx(item[key], abs=1e-4), (lid, key)
        name_field = CATALOG[lid].name_field
        if name_field:
            expected = [f.get(name_field) for f in (item.get("features") or [])[: prospect.MAX_FEATURES]]
            assert [f["name"] for f in entry["features"]] == [str(n) for n in expected if n is not None], lid
    # The build covers the segment: the rasters and a polygon layer answer with values, not gaps.
    assert statuses["solar_ghi"] in ("ok", "partial") and statuses["seismic_pga_2pct50"] in ("ok", "partial")
    assert served["solar_ghi"]["summary"]["mean"] is not None
    assert any(statuses[lid] in ("ok", "partial") for lid in ("oil_gas_basins", "water_stress"))
    ec = answer.get("energy_community") or {}
    assert band["energy_community"]["status"] == (ec.get("status") or "unavailable")
