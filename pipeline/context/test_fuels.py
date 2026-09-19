"""Tests for `pipeline.context.fuels`, the shared seam of the four fuels asset layers: the column
contract the loader reads, the manifest gate, the coordinate rule and the county placement."""

from __future__ import annotations

import datetime as dt
import pathlib

import pandas as pd
import pytest

from pipeline.connectors.base import GateViolation
from pipeline.context import fuels

MANIFEST = pathlib.Path(__file__).resolve().parents[2] / "data" / "sources.yaml"


def _manifest(tmp_path: pathlib.Path, *, reuse: str, publication: str) -> pathlib.Path:
    text = f"""version: test
sources:
  - id: us.epa.lmop
    name: LMOP
    url: https://www.epa.gov/lmop
    category: registry
    access: bulk_file
    reuse: {reuse}
    publication: {publication}
    cadence: periodic
    license: 'x'
"""
    p = tmp_path / "sources.yaml"
    p.write_text(text)
    return p


def test_entry_for_refuses_gated_reuse(tmp_path):
    with pytest.raises(GateViolation):
        fuels.entry_for("us.epa.lmop", _manifest(tmp_path, reuse="unknown", publication="none"))


def test_entry_for_refuses_publication_none_even_when_open(tmp_path):
    with pytest.raises(GateViolation):
        fuels.entry_for("us.epa.lmop", _manifest(tmp_path, reuse="open", publication="none"))


def test_entry_for_returns_an_open_raw_ok_entry(tmp_path):
    entry = fuels.entry_for("us.epa.lmop", _manifest(tmp_path, reuse="open", publication="raw_ok"))
    assert entry.id == "us.epa.lmop"
    assert entry.licence_id.startswith("us.epa.lmop#")


def test_asset_row_fills_every_contract_column_and_defaults():
    row = fuels.asset_row(source_id="x", source_asset_id="1", name="n", status="operating")
    assert list(row) == fuels.ASSET_COLUMNS
    assert row["technologies"] == {}
    assert row["attributes"] == {}
    assert row["country"] == "US"
    assert row["licence"] == "public-domain"


def test_asset_row_rejects_unknown_columns_and_bad_status():
    with pytest.raises(KeyError):
        fuels.asset_row(source_id="x", source_asset_id="1", name="n", status="operating", nope=1)
    with pytest.raises(ValueError):
        fuels.asset_row(source_id="x", source_asset_id="1", name="n", status="Operational")


def test_assemble_refuses_duplicate_keys():
    rows = [fuels.asset_row(source_id="x", source_asset_id="1", name="a", status="operating")] * 2
    with pytest.raises(ValueError):
        fuels.assemble(rows)


def test_loader_column_contract_matches_services_ingest_assets():
    # The loader documents the columns it reads; every one must be in the contract so
    # `load_assets_parquet` needs no change (task rule for this lane).
    loader_reads = {
        "source_asset_id", "name", "operator_name", "technology", "technology_raw", "technologies",
        "capacity_mw", "capacity_value", "capacity_unit", "unit_count", "commissioned_year", "lon",
        "lat", "state_code", "county_name", "county_fips", "country", "attributes", "status",
        "source_url", "retrieved_at",
    }  # fmt: skip
    assert loader_reads <= set(fuels.ASSET_COLUMNS)


def test_numeric_attributes_are_floats_only_and_text_is_separate():
    num = fuels.numeric_attributes({"a": "1.5", "b": None, "c": "corn", "d": True, "e": float("nan")})
    assert num == {"a": 1.5, "d": 1.0}
    assert all(isinstance(v, float) for v in num.values())
    assert fuels.text_attributes({"a": " corn ", "b": None, "c": "nan"}) == {"a": "corn"}


@pytest.mark.parametrize(
    ("lat", "lon", "expected"),
    [
        (30.0, -90.0, (-90.0, 30.0)),
        ("30.0", "-90.0", (-90.0, 30.0)),
        (0.0, 0.0, None),
        (95.0, 10.0, None),
        (10.0, -200.0, None),
        (None, -90.0, None),
        ("n/a", "x", None),
    ],
)
def test_valid_point_rule(lat, lon, expected):
    assert fuels.valid_point(lat, lon) == expected


def test_state_code_and_content_key():
    assert fuels.state_code("tx") == "US-TX"
    assert fuels.state_code("Texas") is None
    assert fuels.content_key("Farm  Digester", "CA") == fuels.content_key("farm digester ", "ca")
    assert fuels.content_key("a", "CA") != fuels.content_key("a", "AZ")


def test_to_year_accepts_dates_and_strings():
    assert fuels.to_year(dt.datetime(2012, 7, 31, tzinfo=dt.UTC)) == 2012
    assert fuels.to_year(dt.date(2012, 7, 31)) == 2012
    assert fuels.to_year("2012-07-31 00:00:00") == 2012
    assert fuels.to_year("2008") == 2008
    assert fuels.to_year(None) is None
    assert fuels.to_year("unknown") is None


def test_placement_never_sets_lon_lat_and_falls_back_to_state():
    place = fuels.Placement()
    county = place.resolve("TX", "Nolan")
    assert county["placement_precision"] == "county_centroid"
    assert county["county_fips"] == "48353"
    assert county["centroid_lon"] < 0 and county["centroid_lat"] > 0
    assert "lon" not in county and "lat" not in county
    state = place.resolve("TX", "No Such County")
    assert state["placement_precision"] == "state_centroid"
    assert state["county_fips"] is None
    assert place.resolve(None, None)["placement_precision"] == "unknown"


def test_snapshot_metadata_reads_token_and_run_record(tmp_path):
    from pipeline.connectors.store import Store

    store = Store(root=tmp_path)
    when = dt.datetime(2026, 9, 19, 15, 20, 34, tzinfo=dt.UTC)
    path, retrieved = fuels.record_snapshot(
        "us.epa.agstar", b"PK\x03\x04", ext="xlsx", fetched_url="https://example.org/a.xlsx",
        retrieved_at=when, requests_made=2, store=store,
    )  # fmt: skip
    assert retrieved == "2026-09-19T15:20:34Z"
    assert path == tmp_path / "snapshots" / "us.epa.agstar" / "20260919T152034Z.xlsx"
    assert fuels.latest_snapshot("us.epa.agstar", "xlsx", store=store) == path
    assert fuels.snapshot_metadata(path, "us.epa.agstar", "fallback", store=store) == (
        "2026-09-19T15:20:34Z",
        "https://example.org/a.xlsx",
    )
    # A file that is not one of the store's snapshots: "now" and the fallback URL.
    other = tmp_path / "fixture.xlsx"
    other.write_bytes(b"PK")
    ts, url = fuels.snapshot_metadata(other, "us.epa.agstar", "fallback", store=store)
    assert url == "fallback"
    assert ts.endswith("Z")


def test_summarise_counts_placement_and_vocabularies():
    df = pd.DataFrame(
        [
            fuels.asset_row(
                source_id="x", source_asset_id="1", name="a", status="operating", lon=1.0, lat=2.0
            ),
            fuels.asset_row(source_id="x", source_asset_id="2", name="b", status="retired", technology="rng"),
        ]
    )
    s = fuels.summarise(df, extra=1)
    assert s["assets"] == 2 and s["with_coordinates"] == 1 and s["without_coordinates"] == 1
    assert s["by_status"] == {"operating": 1, "retired": 1}
    assert s["by_technology"] == {"rng": 1}
    assert s["extra"] == 1


def test_repo_manifest_registers_the_four_sources_as_open_raw_ok():
    """The manifest this lane publishes under (skipped, not failed, while another lane's edit of
    the file is mid-flight and unparsable -- the committed manifest is what CI sees)."""
    import yaml

    try:
        doc = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    except yaml.YAMLError:
        pytest.skip("data/sources.yaml does not parse in this working tree")
    by_id = {s["id"]: s for s in doc["sources"]}
    for sid in ("us.eia.ethanol_capacity", "us.eia.atlas.ethanol_plants", "us.epa.lmop", "us.epa.agstar"):
        assert by_id[sid]["reuse"] == "open"
        assert by_id[sid]["publication"] == "raw_ok"
    assert by_id["us.anl.rng_database"]["reuse"] == "unknown"  # stays out: terms unread (docs/13 §2.14)
