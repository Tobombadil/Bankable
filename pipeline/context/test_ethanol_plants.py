"""Tests for `pipeline.context.ethanol_plants` against `fixtures/eia_atlas_ethanol_plants_sample.zip`:
EIA's own shapefile copy of the Atlas layer, `https://www.eia.gov/maps/map_data/
Ethanol_Plants_US_EIA.zip` (37,644 bytes, 197 point records, data "As of January 1, 2021"),
fetched 2026-09-19 and trimmed to 12 real records (the first ten in file order plus one Iowa and
one Nebraska plant) with the `.prj` kept and the `.shp.xml` metadata dropped from the fixture (its
use-limitation and credit text is quoted in the module docstring).

The feature-service route (`fetch_service`) answered "Token Required" on 2026-09-19 and could not
be recorded; its GeoJSON shape is exercised with an in-test FeatureCollection built from the
service's documented field names, labelled synthetic here, and the token error is exercised with
the exact error body the service returned.
"""

from __future__ import annotations

import io
import json
import pathlib
import zipfile

import pandas as pd
import pytest

from pipeline.connectors.base import ParseError
from pipeline.connectors.http import HttpBlocked
from pipeline.context import fuels
from pipeline.context.ethanol_plants import (
    LAYER_URL,
    SOURCE_ID,
    ZIP_URL,
    build_assets,
    fetch_layer,
    parse,
    query_url,
)

FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "eia_atlas_ethanol_plants_sample.zip"

#: The service's documented field spelling (Company, Site, State postal, PADD, Cap_Mmgal, Source,
#: Period, Longitude, Latitude). Synthetic values -- never presented as real data.
SERVICE_GEOJSON = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "properties": {
                "OBJECTID": 1,
                "Company": "Example Ethanol LLC",
                "Site": "Testville",
                "State": "IA",
                "PADD": 2,
                "Cap_Mmgal": 120.5,
                "Source": "Form EIA-819",
                "Period": "As of January 1, 2026",
                "Longitude": -93.5,
                "Latitude": 42.0,
            },
            "geometry": {"type": "Point", "coordinates": [-93.5, 42.0]},
        },
        {
            "type": "Feature",
            "properties": {
                "OBJECTID": 2,
                "Company": "Example Ethanol LLC",
                "Site": "Testville",
                "State": "IA",
                "PADD": 2,
                "Cap_Mmgal": 10.0,
                "Source": "Form EIA-819",
                "Period": "As of January 1, 2026",
                "Longitude": 0.0,
                "Latitude": 0.0,
            },
            "geometry": {"type": "Point", "coordinates": [0.0, 0.0]},
        },
    ],
}
TOKEN_ERROR = (
    b'{"error":{"code":499,"message":"Token Required","messageCode":"GWM_0003","details":["Token Required"]}}'
)


class _Resp:
    def __init__(self, content: bytes, status: int = 200) -> None:
        self.content = content
        self.status_code = status


class _Session:
    """Stub of `PoliteSession.get`: answers by URL, records what was asked."""

    def __init__(self, answers: dict[str, bytes]) -> None:
        self.answers = answers
        self.calls: list[str] = []
        self.requests_made = 0

    def get(self, url: str, *, honour_robots: bool = True) -> _Resp:
        self.calls.append(url)
        self.requests_made += 1
        for prefix, body in self.answers.items():
            if url.startswith(prefix):
                return _Resp(body)
        raise AssertionError(f"unexpected URL {url}")


@pytest.fixture(scope="module")
def features():
    return parse(FIXTURE.read_bytes())


@pytest.fixture(scope="module")
def assets(features):
    return build_assets(
        features,
        retrieved_at="2026-09-19T15:52:00Z",
        source_url=ZIP_URL,
        licence_id="us.eia.atlas.ethanol_plants#test",
    )


def test_shapefile_zip_parses_to_point_features(features):
    assert len(features) == 12
    f = features[0]
    assert f["geometry"]["type"] == "Point"
    assert f["properties"]["Company"] == "Pinal Energy LLC"
    assert f["properties"]["Site_Name"] == "Maricopa"
    assert f["properties"]["State"] == "Arizona"
    assert f["properties"]["Capacity"] == 55


def test_shapefile_field_spelling_is_normalised(assets):
    row = assets[assets["source_asset_id"] == "AZ-pinal-energy-llc-maricopa"].iloc[0]
    assert row["name"] == "Pinal Energy LLC (Maricopa, AZ)"
    assert row["state_code"] == "US-AZ"
    assert row["capacity_value"] == pytest.approx(55.0) and row["capacity_unit"] == "MMgal/yr"
    assert row["attributes"] == {"nameplate_capacity_mmgal_yr": 55.0}
    assert row["attributes_text"]["state_name"] == "Arizona"
    assert row["attributes_text"]["data_period"] == "As of January 1, 2021"
    assert row["attributes_text"]["padd"] == "5"
    assert row["owner_raw"] == "Pinal Energy LLC" == row["operator_name"]
    assert row["feedstock"] is None


def test_exact_point_from_eia_geometry(assets):
    row = assets[assets["source_asset_id"] == "AZ-pinal-energy-llc-maricopa"].iloc[0]
    assert row["lon"] == pytest.approx(-112.0033, abs=1e-4)
    assert row["lat"] == pytest.approx(33.0228, abs=1e-4)
    assert row["placement_precision"] == "exact"
    assert assets["lon"].notna().all()
    assert assets["source_asset_id"].is_unique


def test_contract_columns_and_provenance(assets):
    assert list(assets.columns) == fuels.ASSET_COLUMNS
    row = assets.iloc[0]
    assert row["source_id"] == SOURCE_ID
    assert row["source_url"] == ZIP_URL
    assert row["licence"] == "public-domain" and row["licence_id"].startswith(SOURCE_ID)
    assert row["status"] == "operating" and row["technology"] == "ethanol"
    assert row["raw"]["properties"]["OBJECTID"] == 1


def test_service_geojson_parses_with_its_own_field_spelling_and_rejects_00():
    content = json.dumps(SERVICE_GEOJSON).encode()
    feats = parse(content)
    df = build_assets(feats, retrieved_at="t", source_url="u", licence_id="l")
    assert len(df) == 2
    first, second = df.iloc[0], df.iloc[1]
    assert first["source_asset_id"] == "IA-example-ethanol-llc-testville"
    assert second["source_asset_id"] == "IA-example-ethanol-llc-testville-2"  # same key, suffixed
    assert first["capacity_value"] == pytest.approx(120.5)
    assert first["placement_precision"] == "exact"
    # (0, 0) is a placeholder, not a point: unplaced, state centroid carried separately.
    # (None becomes NaN in a float column once the frame is assembled -- test_eia_plants' note.)
    assert pd.isna(second["lon"]) and pd.isna(second["lat"])
    assert second["placement_precision"] == "state_centroid"


def test_token_required_reply_is_a_block_not_a_parse():
    with pytest.raises(HttpBlocked):
        parse(TOKEN_ERROR)


def test_bad_payloads_raise_parse_error():
    with pytest.raises(ParseError):
        parse(b"not json at all")
    with pytest.raises(ParseError):
        parse(json.dumps({"type": "FeatureCollection", "features": []}).encode())
    with pytest.raises(ParseError):
        parse(json.dumps({"features": [{"properties": {"Nope": 1}}]}).encode())
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("only.dbf", b"")
    with pytest.raises(ParseError):
        parse(buf.getvalue())


def test_fetch_layer_falls_back_to_the_eia_zip_only_on_the_token_block():
    session = _Session({LAYER_URL: TOKEN_ERROR, ZIP_URL: FIXTURE.read_bytes()})
    content, url, ext = fetch_layer(session)  # type: ignore[arg-type]
    assert ext == "zip" and url == ZIP_URL
    assert content.startswith(b"PK")
    assert session.calls[0].startswith(query_url(LAYER_URL, 0).split("?")[0])
    assert session.calls[1] == ZIP_URL


def test_fetch_layer_uses_the_service_when_it_answers():
    page = dict(SERVICE_GEOJSON)
    session = _Session({LAYER_URL: json.dumps(page).encode()})
    content, url, ext = fetch_layer(session)  # type: ignore[arg-type]
    assert ext == "geojson" and url == query_url(LAYER_URL, 0)
    assert len(json.loads(content)["features"]) == 2
    assert len(session.calls) == 1  # no exceededTransferLimit -> one page, no zip fetch


def test_query_url_asks_for_wgs84_geojson_pages():
    url = query_url(LAYER_URL, 1000)
    assert "outSR=4326" in url and "f=geojson" in url and "resultOffset=1000" in url


def test_assets_survive_the_parquet_round_trip(assets, tmp_path):
    out = fuels.write_context_parquet(assets, tmp_path / "x.parquet")
    back = pd.read_parquet(out)
    assert len(back) == 12 and list(back.columns) == fuels.ASSET_COLUMNS
