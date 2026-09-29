"""Parser tests for us.epa.echo.icis_air (EPA ICIS-Air data-centre selection, placed through FRS).

Fixture `tests/fixtures/epa_icis_air_data_centers.json` is the connector's own snapshot of 2026-09-29
(ICIS-AIR_FACILITIES.csv pre-filtered to 632 candidates, ECHO Exporter rows for their FRS ids)
trimmed to 35 facilities and their 34 FRS rows: every selection basis, every lifecycle value, every
placement outcome, the rows each exclusion exists for, and seven Virginia facilities that are also
in the Virginia DEQ fixture. No value is edited. No network.
"""

from __future__ import annotations

import io
import json
import zipfile
import zlib

import pandas as pd
import pytest

from conftest import connector_for, snapshot
from pipeline import resolve
from pipeline.connectors.base import ParseError
from pipeline.connectors.us_epa_echo_icis_air.connector import (
    DFR_URL,
    ICIS_URL,
    central_directory,
    inflate_member,
    is_candidate,
    placement_for,
    select_basis,
)
from pipeline.connectors.us_va_deq_data_center_air_sites.connector import LAYER_URL as VA_LAYER_URL

SOURCE_ID = "us.epa.echo.icis_air"
FIXTURE = "epa_icis_air_data_centers.json"


def _run() -> tuple[object, list[dict[str, object]], pd.DataFrame]:
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, ICIS_URL, "application/json")
    rows = c.parse(raw)
    return c, rows, c.normalize(rows, raw)


def test_selector_keeps_data_centres_and_rejects_each_exclusion():
    _, rows, df = _run()
    assert len(rows) == 25
    bases = pd.Series([r["select_basis"] for r in rows]).value_counts().to_dict()
    assert bases == {"naics_518210": 15, "name": 8, "naics_541513_operator": 2}
    names = set(df["name_canonical"])
    for rejected in (
        "SUNGARD AVAILABILITY SERVICES, LP",  # Permanently Closed
        "NYDIG DFM - OXBOW PAD",  # 518210 co-coded 211130 (well-site crypto mining)
        "GRMR OIL AND GAS - DEAL GULCH PRODUCTION",  # 518210, name says oil and gas
        "FDR HEADQUARTERS",  # 518210, name says headquarters
        "PHILCADE LLC / IBM TULSA OK BCS BPO",  # 518210, BPO office
        "LUFKIN PAPER MILL",  # 518210 on a paper mill
        "UBS AMERICAS INC.",  # 541513 without a data-centre operator
    ):
        assert rejected not in names


def test_select_basis_rule():
    row = {"AIR_OPERATING_STATUS_DESC": "Operating", "NAICS_CODES": "999999"}
    assert select_basis({**row, "FACILITY_NAME": "Travelers Data Center"}) == "name"
    assert select_basis({**row, "FACILITY_NAME": "Compass Datacenters PHX II"}) == "name"
    assert select_basis({**row, "FACILITY_NAME": "Fidelity Investments Data Ctr"}) == "name"
    assert select_basis({**row, "FACILITY_NAME": "CENTRA HEALTH ADMINISTRATION - DATA CENT"}) == "name"
    assert select_basis({**row, "FACILITY_NAME": "Data Central Office"}) is None
    assert select_basis({**row, "FACILITY_NAME": "Office Park"}) is None
    naics = {**row, "NAICS_CODES": "518210"}
    assert select_basis({**naics, "FACILITY_NAME": "Sharka"}) == "naics_518210"
    assert select_basis({**naics, "NAICS_CODES": "211130 518210", "FACILITY_NAME": "X"}) is None
    assert select_basis({**naics, "FACILITY_NAME": "NYDIG DFM - Veneta Pad"}) is None
    op = {**row, "NAICS_CODES": "541513"}
    assert select_basis({**op, "FACILITY_NAME": "Equinix LLC - DC 13"}) == "naics_541513_operator"
    assert select_basis({**op, "FACILITY_NAME": "L. L.Bean, Inc."}) is None
    closed = {**row, "AIR_OPERATING_STATUS_DESC": "Permanently Closed", "FACILITY_NAME": "Old Data Center"}
    assert select_basis(closed) is None
    # the fetch-side pre-filter is looser: it keeps closed and excluded rows for parse to judge
    assert is_candidate(closed) and is_candidate({**naics, "FACILITY_NAME": "LUFKIN PAPER MILL"})
    assert not is_candidate({**row, "FACILITY_NAME": "Funeral Home"})


def test_records_are_load_proposals_with_provenance():
    c, _, df = _run()
    assert list(df.columns) == c.columns
    assert set(df["kind"]) == {"load"} and set(df["technology"]) == {"load"}
    assert df["capacity_mw"].isna().all() and df["sponsor_name"].isna().all()
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert df["record_id"].is_unique
    assert set(df["licence_id"]) == {c.source.licence_id}


def test_identity_url_and_cross_refs():
    _, _, df = _run()
    row = df[df["source_record_id"] == "VA0000005115374349"].iloc[0]
    assert row["record_id"] == f"{SOURCE_ID}:VA0000005115374349"
    assert row["name_canonical"] == "AMAZON DATA SERVICES INC IAD-264"
    raw = json.loads(row["raw"])
    assert row["source_url"] == DFR_URL.format(registry_id=raw["REGISTRY_ID"])
    assert row["cross_refs"] == f"icis_air:VA0000005115374349|frs:{raw['REGISTRY_ID']}"
    no_frs = df[df["name_canonical"] == "DOTIER, LLC"].iloc[0]
    assert no_frs["source_url"] == ICIS_URL and no_frs["cross_refs"] == "icis_air:AL0000000110100108"


def test_status_map_covers_every_value_in_the_fixture():
    _, _, df = _run()
    by_raw = dict(zip(df["status_raw"].fillna(""), df["lifecycle_state"], strict=False))
    assert by_raw["Planned Facility"] == "filed"
    assert by_raw["Under Construction"] == "under_construction"
    assert by_raw["Operating"] == "built"
    assert by_raw["Temporarily Closed"] == "built"
    assert by_raw[""] == "unknown"
    assert not df["status_rule"].str.endswith(".unmapped").any()
    assert df["lifecycle_state"].value_counts().to_dict() == {
        "built": 17,
        "filed": 3,
        "under_construction": 3,
        "unknown": 2,
    }


def test_only_frs_site_points_become_coordinates():
    _, rows, df = _run()
    by_name = {r["FACILITY_NAME"]: r for r in rows}
    placements = pd.Series([r["placement"] for r in rows]).value_counts().to_dict()
    assert placements == {
        "exact": 14,
        "method_not_site_specific": 8,
        "point_outside_state": 1,
        "accuracy_not_stated_or_coarse": 1,
        "no_frs_record": 1,
    }
    for r in rows:
        has_point = "Latitude" in r and "Longitude" in r
        assert has_point == (r["placement"] == "exact"), r["FACILITY_NAME"]
        assert "FAC_LAT" not in r  # a non-site coordinate never reaches `raw`
    walmart = by_name["WAL-MART NORTH DATA CENTER, FACILITY #8678"]
    assert walmart["placement"] == "point_outside_state"
    aligned = by_name["ALIGNED DATA CENTER (EGV) PROPCO LLC"]
    assert aligned["placement"] == "accuracy_not_stated_or_coarse" and aligned["frs_accuracy_m"] > 200
    counties = dict(zip(df["name_canonical"], df["county"], strict=False))
    # exact point: county from the point (ICIS says "Richland"; Mount Pleasant is in Racine County)
    assert counties["MICROSOFT CORPORATION - MKE 3B DATA CENTER"] == "Racine"
    assert by_name["MICROSOFT CORPORATION - MKE 3B DATA CENTER"]["COUNTY_NAME"] == "Richland"
    # not exact and ICIS county "Undetermined": no county, the loader places it at the state
    assert counties["CMH050"] is None
    assert placement_for(None, "VA") == ("no_frs_record", None, None)
    zip_centroid = {"FAC_LAT": "38.9", "FAC_LONG": "-77.4", "FAC_COLLECTION_METHOD": "Zip Code Centroid"}
    assert placement_for({**zip_centroid, "FAC_ACCURACY_METERS": "10"}, "VA")[0] == "method_not_site_specific"


def test_virginia_overlap_resolves_to_one_record_per_facility():
    """DEQ's `icis_air:<PLA_ICIS_ID>` and ICIS-Air's own `icis_air:<PGM_SYS_ID>` pair deterministically
    (D3); neighbouring campuses of one operator with different ids never pair."""
    _, _, icis = _run()
    va_c = connector_for("us.va.deq.data_center_air_sites")
    va_raw = snapshot("va_deq_air_sites_data_centers.json", VA_LAYER_URL, "application/json")
    va = va_c.normalize(va_c.parse(va_raw), va_raw)
    df = pd.concat([va, icis], ignore_index=True)
    det = resolve.deterministic(df)
    pairs = {
        (df.at[int(a), "source_record_id"], df.at[int(b), "source_record_id"])
        for a, b, p in zip(det["li"], det["ri"], det["pass"], strict=True)
        if p == "D3_xref"
    }
    assert pairs == {
        ("11790", "VA0000005119511790"),
        ("21527", "VA0000005111700071"),
        ("30142", "VA0000005111700009"),
        ("73158", "VA0000005110700814"),
        ("73160", "VA0000005110701042"),
        ("74129", "VA0000005168374129"),
        ("74349", "VA0000005115374349"),
    }
    assert resolve.shared_id_conflict("icis_air:A", "icis_air:B|frs:1")
    assert not resolve.shared_id_conflict("icis_air:A", "icis_air:A|frs:1")
    assert not resolve.shared_id_conflict("icis_air:A", "")
    # a source that repeats the id is ambiguous and is left to review, not paired
    dup = pd.concat([df, df[df["source_record_id"] == "74349"]], ignore_index=True)
    dup_pairs = resolve.deterministic(dup)
    assert not dup_pairs["rationale"].str.contains("VA0000005115374349").any()


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_ranged_zip_member_reader():
    first = b'"PGM_SYS_ID","REGISTRY_ID"\n' + b'"A","1"\n' * 500
    blob = _zip_bytes({"ICIS-AIR_FACILITIES.csv": first, "ICIS-AIR_PROGRAMS.csv": b"x" * 1000})
    tail = blob[-200:] if len(blob) > 200 else blob
    members = {m.name: m for m in central_directory(blob, len(blob))}
    assert set(members) == {"ICIS-AIR_FACILITIES.csv", "ICIS-AIR_PROGRAMS.csv"}
    m = members["ICIS-AIR_FACILITIES.csv"]
    assert m.size == len(first) and m.crc == zlib.crc32(first)
    assert inflate_member(blob[m.header_offset :], m) == first
    # the central directory can be read from the tail alone
    assert [x.name for x in central_directory(tail, len(blob))] == list(members)
    with pytest.raises(ParseError):
        inflate_member(blob[m.header_offset : m.header_offset + 40], m)  # truncated range
    with pytest.raises(ParseError):
        central_directory(b"not a zip", 9)


def test_redact_strips_contact_columns_and_leaves_clean_bytes_alone():
    c = connector_for(SOURCE_ID)
    doc = {
        "icis": {"columns": ["PGM_SYS_ID", "CONTACT_NAME"], "rows": [["A", "someone"]]},
        "frs": {"columns": ["REGISTRY_ID"], "rows": [["1"]]},
    }
    cleaned = json.loads(c.redact(json.dumps(doc).encode()))
    assert cleaned["icis"] == {"columns": ["PGM_SYS_ID"], "rows": [["A"]]}
    raw_bytes = snapshot(FIXTURE, ICIS_URL).content
    assert c.redact(raw_bytes) == raw_bytes


def test_layout_change_and_bad_payload_are_parse_errors():
    c = connector_for(SOURCE_ID)
    raw = snapshot(FIXTURE, ICIS_URL, "application/json")
    raw.content = b"<html>not json</html>"
    with pytest.raises(ParseError):
        c.parse(raw)
    raw.content = json.dumps({"icis": {"columns": ["NAME"], "rows": []}, "frs": {"columns": []}}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)
    raw.content = json.dumps({"pages": []}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)
