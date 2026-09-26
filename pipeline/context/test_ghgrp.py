"""Tests for `pipeline/context/ghgrp.py`: the two-path matcher on synthetic frames, the summary-zip
parser on the recorded two-sheet fixture, the owner-row builder and the crosswalk loader."""

from __future__ import annotations

import datetime as dt
import pathlib

import pandas as pd
import pytest

from pipeline.context import ghgrp

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURES = ROOT / "tests" / "fixtures"


def _assets() -> pd.DataFrame:
    return pd.DataFrame(
        [
            # EIA plant 54537 (PSE Ferndale) — reached through the crosswalk
            {
                "asset_id": "a-ferndale",
                "asset_type": "power_plant",
                "source_id": "us.eia.860m",
                "source_asset_id": "54537",
                "name": "Ferndale Generating Station",
                "state_code": "US-WA",
                "technology": "gas_cc",
                "lon": -122.68,
                "lat": 48.83,
            },
            # a gas plant 120 m from the facility with a matching name
            {
                "asset_id": "a-douglas",
                "asset_type": "gas_processing_plant",
                "source_id": "us.eia.atlas.gas_processing_plants",
                "source_asset_id": "h1",
                "name": "Douglas Plant",
                "state_code": "US-WY",
                "technology": None,
                "lon": -105.4000,
                "lat": 42.7500,
            },
            # a solar farm 100 m from a landfill emitter, sharing the town name — never a candidate
            {
                "asset_id": "a-solar",
                "asset_type": "power_plant",
                "source_id": "us.eia.860m",
                "source_asset_id": "61732",
                "name": "Randolph",
                "state_code": "US-MA",
                "technology": "solar",
                "lon": -71.0400,
                "lat": 42.1600,
            },
            # a gas peaker 850 m from that landfill, incompatible NAICS, town name only
            {
                "asset_id": "a-peaker",
                "asset_type": "power_plant",
                "source_id": "us.eia.860m",
                "source_asset_id": "6060",
                "name": "Randolph",
                "state_code": "US-MA",
                "technology": "gas_ct",
                "lon": -71.0300,
                "lat": 42.1620,
            },
            # same coordinates as Douglas but the wrong state — blocked
            {
                "asset_id": "a-otherstate",
                "asset_type": "gas_processing_plant",
                "source_id": "us.eia.atlas.gas_processing_plants",
                "source_asset_id": "h2",
                "name": "Douglas Plant",
                "state_code": "US-CO",
                "technology": None,
                "lon": -105.4000,
                "lat": 42.7500,
            },
        ]
    )


def _facilities() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "ghgrp_facility_id": "1000001",
                "name": "PSE Ferndale Generating Station",
                "state_code": "US-WA",
                "lon": -122.685533,
                "lat": 48.828707,
                "naics_code": "221112",
                "reporting_year": 2023,
                "share_flag": "ok",
                "source_url": "u1",
                "retrieved_at": "2026-09-25T17:54:00Z",
                "licence": "public-domain",
                "parents": [{"name": "PUGET HOLDINGS LLC", "share_pct": 100.0}],
            },
            {
                "ghgrp_facility_id": "1002377",
                "name": "Douglas Gas Plant",
                "state_code": "US-WY",
                "lon": -105.4010,
                "lat": 42.7508,
                "naics_code": "211130",
                "reporting_year": 2023,
                "share_flag": "ok",
                "source_url": "u2",
                "retrieved_at": "2026-09-25T17:54:00Z",
                "licence": "public-domain",
                "parents": [{"name": "TALLGRASS DEVELOPMENT LP", "share_pct": 100.0}],
            },
            {
                "ghgrp_facility_id": "1007947",
                "name": "RANDOLPH LANDFILL",
                "state_code": "US-MA",
                "lon": -71.0410,
                "lat": 42.1605,
                "naics_code": "562212",
                "reporting_year": 2023,
                "share_flag": "not_100",
                "source_url": "u3",
                "retrieved_at": "2026-09-25T17:54:00Z",
                "licence": "public-domain",
                "parents": [{"name": "TOWN OF RANDOLPH", "share_pct": 75.7}],
            },
            {
                "ghgrp_facility_id": "1999999",
                "name": "Nowhere Compressor Station",
                "state_code": "US-NE",
                "lon": -99.0,
                "lat": 41.0,
                "naics_code": "486210",
                "reporting_year": 2023,
                "share_flag": "ok",
                "source_url": "u4",
                "retrieved_at": "2026-09-25T17:54:00Z",
                "licence": "public-domain",
                "parents": [
                    {"name": "TALLGRASS DEVELOPMENT LP", "share_pct": 75.0},
                    {"name": "PHILLIPS 66", "share_pct": 25.0},
                ],
            },
        ]
    )


def _crosswalk() -> pd.DataFrame:
    return pd.DataFrame([{"ghgrp_facility_id": "1000001", "oris_code": "54537"}])


def test_crosswalk_path_is_deterministic_and_first():
    m = ghgrp.match_facilities(_facilities(), _assets(), crosswalk=_crosswalk())
    hit = m[m["ghgrp_facility_id"] == "1000001"].iloc[0]
    assert hit["method"] == "oris_crosswalk" and hit["accepted"] and hit["score"] == 1.0
    assert hit["asset_id"] == "a-ferndale"


def test_geo_name_accepts_a_close_named_compatible_pair():
    m = ghgrp.match_facilities(_facilities(), _assets(), crosswalk=_crosswalk())
    hit = m[m["ghgrp_facility_id"] == "1002377"].iloc[0]
    assert hit["method"] == "geo_name" and hit["asset_id"] == "a-douglas"
    assert hit["distance_km"] < 0.25 and hit["name_score"] == 1.0 and hit["naics_compatible"]
    assert hit["score"] == 1.0 and hit["accepted"]


def test_non_emitting_technologies_are_never_candidates_and_town_name_alone_is_held():
    m = ghgrp.match_facilities(_facilities(), _assets(), crosswalk=_crosswalk())
    land = m[m["ghgrp_facility_id"] == "1007947"]
    assert len(land) == 1
    assert land.iloc[0]["asset_id"] == "a-peaker"  # the solar farm 100 m away was excluded
    assert land.iloc[0]["score"] == pytest.approx(0.53)  # 0.45*0.4 + 0.35*1.0 + 0
    assert not land.iloc[0]["accepted"]  # below DEFAULT_THRESHOLD = 0.55


def test_wrong_state_is_blocked_and_unmatched_facility_has_no_row():
    m = ghgrp.match_facilities(_facilities(), _assets(), crosswalk=_crosswalk())
    assert "a-otherstate" not in set(m["asset_id"])
    assert "1999999" not in set(m["ghgrp_facility_id"])
    assert list(m.columns) == ghgrp.MATCH_COLUMNS


def test_each_asset_is_consumed_once_globally_greedy():
    fac = pd.concat(
        [_facilities(), _facilities().assign(ghgrp_facility_id=lambda d: d["ghgrp_facility_id"] + "9")]
    )
    m = ghgrp.match_facilities(fac, _assets(), crosswalk=None, use_crosswalk=False)
    geo = m[m["method"] == "geo_name"]
    assert geo["asset_id"].is_unique


def test_name_tokens_and_score():
    assert ghgrp.name_tokens("Separator (Etiwanda) BESS") == {"SEPARATOR", "ETIWANDA", "BESS"}
    assert ghgrp.name_tokens("Etiwanda Generating Station") == {"ETIWANDA"}
    assert ghgrp.name_score("Springdale Units I & II", "Springdale 1 & 2") == 1.0
    assert ghgrp.name_score("Plant Barry", "Barry") == 1.0
    assert ghgrp.name_score("Alpha LLC", "Beta Inc") == 0.0
    assert ghgrp.score_pair(2.0, 1.0, True) == 0.0


def test_naics_compatibility_is_a_feature_by_prefix():
    assert ghgrp.naics_compatible("221112", "power_plant")
    assert ghgrp.naics_compatible("211130", "gas_processing_plant")
    assert ghgrp.naics_compatible("562212", "rng_project")
    assert not ghgrp.naics_compatible("322120", "power_plant")
    assert not ghgrp.naics_compatible(None, "power_plant")


def test_owner_rows_carry_shares_as_stated_and_as_of_year_end():
    fac, m = _facilities(), ghgrp.match_facilities(_facilities(), _assets(), crosswalk=_crosswalk())
    owners = ghgrp.build_owner_rows(fac, m)
    assert list(owners.columns) == ghgrp.OWNER_COLUMNS
    assert set(owners["ghgrp_facility_id"]) == {"1000001", "1002377"}  # only accepted matches
    assert (owners["as_of"] == dt.date(2023, 12, 31)).all()
    puget = owners[owners["owner_name"] == "PUGET HOLDINGS LLC"].iloc[0]
    assert puget["share_pct"] == 100.0 and puget["match_method"] == "oris_crosswalk"


def test_owner_rows_keep_not_100_flag_when_accepted():
    fac = _facilities()
    m = ghgrp.match_facilities(fac, _assets(), crosswalk=_crosswalk(), threshold=0.5)
    owners = ghgrp.build_owner_rows(fac, m)
    randolph = owners[owners["ghgrp_facility_id"] == "1007947"].iloc[0]
    assert randolph["share_pct"] == 75.7 and randolph["share_flag"] == "not_100"


def test_evaluate_against_crosswalk_reports_precision_and_recall():
    ev = ghgrp.evaluate_against_crosswalk(_facilities(), _assets(), _crosswalk(), thresholds=[0.5, 0.99])
    assert list(ev["threshold"]) == [0.5, 0.99]
    assert ev.iloc[0]["gold_pairs"] == 1 and ev.iloc[0]["correct"] == 1 and ev.iloc[0]["precision"] == 1.0


def test_vendored_crosswalk_loads():
    cw = ghgrp.load_crosswalk()
    assert list(cw.columns) == ["ghgrp_facility_id", "oris_code"]
    assert len(cw) == 2166 and cw["ghgrp_facility_id"].nunique() == 2131
    assert ("1000001", "54537") in set(zip(cw["ghgrp_facility_id"], cw["oris_code"], strict=True))


def test_summary_zip_fixture_parses_rr_and_uu():
    content = (FIXTURES / "epa_ghgrp_2023_summary_sample.zip").read_bytes()
    q = ghgrp.parse_summary_zip(content)
    assert list(q.columns) == ghgrp.SUMMARY_COLUMNS
    assert (q["summary_year"] == 2023).all()
    # one RR mass is blank in EPA's sheet; every one of the twelve UU quantities kept reads `confidential`
    assert q["rr_co2_sequestered_t"].notna().sum() == 19
    assert q["rr_co2_sequestered_confidential"].sum() == 0
    assert q["uu_co2_received_t"].notna().sum() == 0
    assert q["uu_co2_received_confidential"].sum() == 12
    hobbs = q[q["ghgrp_facility_id"] == "1012121"].iloc[0]
    assert hobbs["rr_co2_sequestered_t"] == pytest.approx(5198001.5)
    shute = q[q["ghgrp_facility_id"] == "1002150"].iloc[0]
    assert shute["rr_co2_sequestered_t"] == pytest.approx(435080.9)


def test_enrich_joins_on_facility_id():
    fac = _facilities()
    q = pd.DataFrame(
        [
            {
                "ghgrp_facility_id": "1002377",
                "summary_year": 2023,
                "rr_co2_sequestered_t": None,
                "rr_co2_sequestered_confidential": False,
                "uu_co2_received_t": 12.5,
                "uu_co2_received_confidential": False,
            }
        ]
    )
    out = ghgrp.enrich_with_summary(fac, q)
    assert len(out) == len(fac)
    assert out.loc[out["ghgrp_facility_id"] == "1002377", "uu_co2_received_t"].iloc[0] == 12.5
    assert out.loc[out["ghgrp_facility_id"] == "1000001", "uu_co2_received_t"].isna().all()


def test_summary_zip_rejects_wrong_payload():
    with pytest.raises(ValueError, match="neither"):
        ghgrp.parse_summary_zip(b"<html>")
