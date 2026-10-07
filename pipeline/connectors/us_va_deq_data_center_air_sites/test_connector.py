"""Parser tests for us.va.deq.data_center_air_sites (Virginia DEQ Air Sites, data-centre selection).

Fixture `tests/fixtures/va_deq_air_sites_data_centers.json` is the connector's own snapshot of
2026-09-28 trimmed to 22 of its 205 rows (every AIR_OP_STATUS value, both selection bases, an
independent city), plus three real non-data-centre rows from the same layer fetched the same day so
the client-side selector has something to reject. No value is edited. No network.
"""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.us_va_deq_data_center_air_sites.connector import (
    LAYER_URL,
    WHERE,
    county_for_point,
    record_url,
    select_basis,
)

SOURCE_ID = "us.va.deq.data_center_air_sites"
FIXTURE = "va_deq_air_sites_data_centers.json"


def _run() -> tuple[object, list[dict[str, object]], object]:
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, LAYER_URL, "application/json")
    rows = c.parse(raw)
    return c, rows, c.normalize(rows, raw)


def test_selector_keeps_deq_flagged_and_principal_product_rows_only():
    _, rows, df = _run()
    assert len(rows) == 22
    names = set(df["name_canonical"])
    for rejected in (
        "The DR Henderson Funeral Home Inc",
        "Alleghany Asphalt and Construction Inc",
        "Energy Enterprises of Norton Inc",
    ):
        assert rejected not in names
    bases = [r["select_basis"] for r in rows]
    assert bases.count("principal_product") == 4
    assert bases.count("deq_flag") == 18


def test_select_basis_rule():
    assert select_basis({"PLA_DATA_CENTER_YN": "Y", "PLA_PRINCIPAL_PRODUCT": "Office"}) == "deq_flag"
    assert select_basis({"PLA_DATA_CENTER_YN": "N", "PLA_PRINCIPAL_PRODUCT": "U.S Gov't Data Center"}) == (
        "principal_product"
    )
    assert (
        select_basis({"PLA_DATA_CENTER_YN": None, "PLA_PRINCIPAL_PRODUCT": "datacenter"})
        == "principal_product"
    )
    assert select_basis({"PLA_DATA_CENTER_YN": "N", "PLA_PRINCIPAL_PRODUCT": "Data Processing"}) is None
    assert select_basis({"PLA_DATA_CENTER_YN": None, "PLA_PRINCIPAL_PRODUCT": None}) is None
    # the server-side clause names the same two fields
    assert "PLA_DATA_CENTER_YN = 'Y'" in WHERE and "PLA_PRINCIPAL_PRODUCT" in WHERE


def test_records_are_load_proposals_with_provenance():
    c, _, df = _run()
    assert list(df.columns) == c.columns
    assert set(df["kind"]) == {"load"}
    assert set(df["technology"]) == {"load"}
    assert set(df["state"]) == {"VA"}
    assert df["capacity_mw"].isna().all()
    assert df["sponsor_name"].isna().all()
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert df["record_id"].is_unique
    assert set(df["licence_id"]) == {c.source.licence_id}


def test_identity_is_the_deq_registration_number():
    _, _, df = _run()
    row = df[df["source_record_id"] == "74349"].iloc[0]
    assert row["record_id"] == f"{SOURCE_ID}:74349"
    assert row["name_canonical"] == "Amazon Data Services Inc IAD-264"
    assert row["source_url"] == record_url("74349")
    assert "where=PLA_REG_NUM%3D74349" in row["source_url"]
    assert row["cross_refs"] == "icis_air:VA0000005115374349"


def test_status_map_covers_every_value_in_the_fixture():
    _, _, df = _run()
    assert not df["status_rule"].str.endswith(".unmapped").any()
    by_raw = dict(zip(df["status_raw"], df["lifecycle_state"], strict=False))
    assert by_raw["Planned"] == "filed"
    assert by_raw["Under Construction"] == "under_construction"
    assert by_raw["Operating"] == "built"
    assert by_raw["Temporarily Shutdown"] == "built"
    assert (df["lifecycle_state"] == "filed").sum() == 6


def test_points_are_carried_at_source_precision_and_county_is_derived():
    _, _, df = _run()
    raw = [json.loads(r) for r in df["raw"]]
    for r in raw:
        assert isinstance(r["Latitude"], float) and 36.0 < r["Latitude"] < 39.7
        assert isinstance(r["Longitude"], float) and -83.8 < r["Longitude"] < -75.0
        assert round(r["Latitude"], 6) == r["Latitude"]
    counties = dict(zip(df["source_record_id"], df["county"], strict=False))
    assert counties["74349"] == "Prince William"  # Gainesville
    assert counties["74129"] == "Manassas city"  # independent city, a county equivalent
    assert counties["53198"] == "Mecklenburg"  # Chase City
    assert df["county"].notna().all()
    assert county_for_point(None, None) == (None, None)
    assert county_for_point(-0.1, 51.5) == (None, None)  # outside Virginia


def test_county_spelling_is_the_one_icis_air_and_eia_use():
    """One county, one spelling: DEQ wrote "Loudoun County" where ICIS-Air and EIA-860M write
    "Loudoun", so a county column split ten Virginia counties in two (review 2026-10-07)."""
    from pipeline.connectors.us_epa_echo_icis_air.connector import county_for_point as icis_county

    _, _, df = _run()
    assert not df["county"].str.endswith(" County").any()
    for lon, lat, geoid, name in (
        (-77.4875, 39.0438, "51107", "Loudoun"),  # Ashburn
        (-77.4311, 38.8942, "51059", "Fairfax"),  # Chantilly, Fairfax County
        (-77.3064, 38.8462, "51600", "Fairfax city"),  # the independent city
        (-77.4753, 38.7509, "51683", "Manassas city"),
    ):
        assert county_for_point(lon, lat) == (geoid, name)
        assert icis_county(lon, lat) == name


def test_disclaimer_and_staff_columns_never_reach_raw():
    c, rows, _ = _run()
    assert all("Data_Disclaimer" not in r for r in rows)
    payload = {
        "pages": [
            {
                "fields": [{"name": "PLA_NAME"}, {"name": "CHANGED_BY"}],
                "features": [
                    {"attributes": {"PLA_NAME": "X", "INSERTED_BY": "someone", "VERIFIEDBY": "someone"}}
                ],
            }
        ]
    }
    cleaned = json.loads(c.redact(json.dumps(payload).encode()))
    assert cleaned["pages"][0]["fields"] == [{"name": "PLA_NAME"}]
    assert cleaned["pages"][0]["features"][0]["attributes"] == {"PLA_NAME": "X"}
    raw_bytes = snapshot(FIXTURE, LAYER_URL).content
    assert c.redact(raw_bytes) == raw_bytes  # nothing to strip: bytes untouched


def test_error_body_and_layout_change_are_parse_errors():
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, LAYER_URL, "application/json")
    raw.content = b'{"pages": [{"error": {"code": 400, "message": "Invalid query"}}]}'
    with pytest.raises(ParseError):
        c.parse(raw)
    raw.content = b'{"pages": [{"features": [{"attributes": {"NAME": "x"}}]}]}'
    with pytest.raises(ParseError):
        c.parse(raw)
    raw.content = b"<html>not json</html>"
    with pytest.raises(ParseError):
        c.parse(raw)
