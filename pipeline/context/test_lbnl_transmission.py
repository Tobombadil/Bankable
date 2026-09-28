"""Tests for `pipeline.context.lbnl_transmission` against a recorded fixture (no network).

``fixtures/lbnl_ferc_hifld_transmission_lines_sample.csv`` is the header plus 32 rows copied
byte-for-byte from the real file fetched on 2026-09-28
(`data/snapshots/us.lbnl.ferc_hifld_transmission_lines/20260928T135517Z.csv`, sha256 ec066aa3...):
three Scriba lines in New York (a node named on several lines), and for TX, CA, NY and NV a row with no
SUB_2, a row with no HIFLD owner and five named lines, plus a ``tap`` endpoint, an ``unknown``
endpoint and a 765 kV line."""

from __future__ import annotations

import json
import pathlib

import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.connectors.store import Store
from pipeline.context import lbnl_transmission as lt
from pipeline.context.eia_atlas import ASSET_COLUMNS, Provenance
from pipeline.context.geo import StateIndex

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "lbnl_ferc_hifld_transmission_lines_sample.csv"
PROV = Provenance(lt.SOURCE_ID, lt.DOWNLOAD_URL, "2026-09-28T13:55:17Z", "CC BY 4.0")


@pytest.fixture(scope="module")
def frame() -> pd.DataFrame:
    states = StateIndex.from_file(lt.STATES_GEOJSON) if lt.STATES_GEOJSON.exists() else None
    return lt.normalise(lt.parse(FIXTURE.read_bytes()), PROV, states=states)


def test_parse_reads_every_row_as_text_and_refuses_other_files():
    raw = lt.parse(FIXTURE.read_bytes())
    assert len(raw) == 32
    assert raw["GlobalID"].iloc[0] == "100024" and isinstance(raw["operating_voltage"].iloc[0], str)
    with pytest.raises(ParseError):
        lt.parse(b"a,b\n1,2\n")


def test_one_row_per_line_with_provenance_and_line_geometry(frame):
    assert list(frame.columns) == ASSET_COLUMNS
    assert len(frame) == 32 and frame["source_asset_id"].is_unique
    assert set(frame["source_id"]) == {lt.SOURCE_ID}
    assert set(frame["source_url"]) == {lt.DOWNLOAD_URL}
    assert set(frame["retrieved_at"]) == {"2026-09-28T13:55:17Z"}
    assert set(frame["licence"]) == {"CC BY 4.0"}
    assert frame["geom_line_wkt"].str.startswith("MULTILINESTRING((").all()
    assert set(frame["status"]) == {"unknown"}
    assert frame["lon"].notna().all() and frame["lat"].notna().all()


def test_hifld_side_fields_only(frame):
    row = frame.set_index("source_asset_id").loc["100024"]
    assert row["name"] == "Scriba - Fitzpatrick 345 kV"
    assert row["owner_name"] is None  # shown, not linked (module docstring)
    assert row["state_code"] == "US-NY"
    a = row["attributes"]
    assert (a["voltage_kv"], a["voltage_class"], a["sub_1"], a["sub_2"]) == (
        345.0,
        "345 kV",
        "Scriba",
        "Fitzpatrick",
    )
    assert a["owner"] == "Niagara Mohawk Power" and a["owner_raw"] == "niagara mohawk power"
    assert a["hifld_line_id"] == "100024"
    assert abs(a["miles"] - a["length_miles_source"]) < 0.05  # geodesic length agrees with the source's
    # Nothing from the FERC side of LBNL's linkage is published.
    assert not {k for k in a if "cost" in k or "ferc" in k or "conductor" in k or "expense" in k}


def test_placeholders_and_missing_names(frame):
    by_id = frame.set_index("source_asset_id")
    tap = by_id.loc["100042"]  # SUB_1 "tap"
    assert tap["attributes"]["sub_1"] is None and tap["attributes"]["sub_1_raw"] == "tap"
    assert tap["name"] == "Society Hill 230 kV line"
    unknown = by_id.loc["100100"]  # SUB_2 "unknown"
    assert unknown["attributes"]["sub_2"] is None
    nameless = by_id.loc["300009"]  # "tap" and no SUB_2
    assert nameless["name"] == "138 kV line 300009"
    no_owner = by_id.loc["100517"]
    assert no_owner["attributes"]["owner"] is None and no_owner["attributes"]["owner_raw"] is None
    kv_suffix = by_id.loc["100170"]  # LBNL left "kv" on "cloverdale kv"
    assert kv_suffix["attributes"]["sub_1"] == "Cloverdale"
    assert kv_suffix["attributes"]["voltage_class"] == "735 kV and above"


def test_owner_display_keeps_acronyms():
    assert lt.owner_display("nevada power dba nv energy") == "Nevada Power dba NV Energy"
    assert lt.owner_display("aep texas central") == "AEP Texas Central"
    assert lt.owner_display(None) is None


def test_endpoint_nodes_place_only_names_shared_by_lines(frame):
    nodes = lt.endpoint_nodes(frame).set_index("node_id")
    scriba = nodes.loc["NY:scriba"]
    assert scriba["location_method"] == "shared_endpoint"
    assert sorted(scriba["line_ids"]) == ["100024", "104749", "108769"]
    assert scriba["voltages_kv"] == [345.0]
    single = nodes.loc["NY:fitzpatrick"]
    assert single["location_method"] == "unresolved" and pd.isna(single["lon"])
    assert "NY:tap" not in nodes.index and "NY:unknown" not in nodes.index


def test_run_from_snapshot_writes_parquet(tmp_path):
    store = Store(root=tmp_path)
    out = tmp_path / "out.parquet"
    summary = lt.run(snapshot=FIXTURE, out=out, with_states=False, store=store)
    assert summary["rows"] == 32 and summary["dropped_no_geometry"] == 0
    back = pd.read_parquet(out)
    assert len(back) == 32
    assert json.loads(back["attributes"].iloc[0])["hifld_line_id"] == "100024"
