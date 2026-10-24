"""`GET /v1/global/electricity` on figures loaded from the recorded fixtures (synthetic values in Ember's
published shapes; `pipeline/connectors/global_ember_electricity/testing.py`), offline.

Held in place: the response says what the numbers are (operating, never a pipeline) in its fields and notes;
the latest year, its year-on-year change, the clean share, the series, the coverage of the latest year and
the wind and solar ranking; `area=US` alone links to the US pipeline figure by URL, the active pipeline
only; attribution in every response, before the first load too; a hidden source serves nothing; bad
parameters are refused; every response validates against `api/openapi.yaml`; World's estimated year is
flagged on every block and row; a yearly download dated `YYYY-01-01` serves yearly figures.
"""

from __future__ import annotations

import json
import pathlib
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sqlalchemy.orm import Session, sessionmaker

from pipeline.connectors.global_ember_electricity import SOURCE_ID
from pipeline.connectors.global_ember_electricity.testing import csv_bytes, dated_wide_csv, wide_rows
from services.api.org_pipeline import ACTIVE_LIFECYCLE_STATES
from services.db.models import Source
from services.global_figures.testing import seed

SPEC = yaml.safe_load((pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml").read_text())
REGISTRY = Registry().with_resource(
    "urn:spec", Resource.from_contents(SPEC, default_specification=DRAFT202012)
)
PATH = "/v1/global/electricity"


def _valid(body: dict[str, Any]) -> None:
    schema = {"$ref": "urn:spec#/components/schemas/GlobalElectricityResponse"}
    validator = jsonschema.Draft202012Validator(schema, registry=REGISTRY)
    errors = sorted(validator.iter_errors(body), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


@pytest.fixture()
def seeded(db_sessionmaker: sessionmaker[Session], tmp_path: pathlib.Path) -> dict[str, Any]:
    with db_sessionmaker() as session:
        out = seed(session, tmp_path)
        session.commit()
    return out


def _get(client: TestClient, **params: str) -> dict[str, Any]:
    resp = client.get(PATH, params=params)
    assert resp.status_code == 200, resp.text
    body: dict[str, Any] = resp.json()
    _valid(body)
    return body


def test_before_the_first_load_nothing_is_served_but_ember_is_credited(client: TestClient) -> None:
    body = _get(client)
    assert body["data"]["loaded"] is False and body["data"]["measure"] == "operating"
    credits = body["notes"]["credits"]
    assert {c["text"] for c in credits} == {
        "Yearly electricity data, Ember",
        "Monthly electricity data, Ember",
        "Monthly wind and solar capacity data, Ember",
    }
    assert all(c["licence_url"] == "https://creativecommons.org/licenses/by/4.0/" for c in credits)
    assert "Ember" in body["notes"]["credit"]


def test_the_world_figures_say_what_they_are(client: TestClient, seeded: dict[str, Any]) -> None:
    body = _get(client)
    data = body["data"]
    assert data["loaded"] is True and data["measure"] == "operating"
    assert "not a pipeline" in data["measure_note"]
    assert data["area"] == {"id": "world", "name": "World", "kind": "world", "iso3": None}
    assert data["us_pipeline"] is None
    # Operating capacity is named as such everywhere; no bare `capacity_gw` field reaches a reader.
    assert '"capacity_gw"' not in json.dumps(body)
    gen = data["generation"]
    assert gen["year"] == 2025 and gen["previous_year"] == 2024 and gen["unit"] == "TWh"
    assert [r["source"] for r in gen["by_source"]] == [
        "solar",
        "wind",
        "hydro",
        "nuclear",
        "gas",
        "coal",
        "other_renewables",
        "bioenergy",
        "other_fossil",
    ]
    solar = gen["by_source"][0]
    assert solar["generation_twh"] == 2700.0 and solar["change_pct"] == 30.0
    assert (
        gen["provenance"]["route"] == "api"
        and gen["provenance"]["citation"] == "Yearly electricity data, Ember"
    )
    cap = data["operating_capacity"]
    assert cap["year"] == 2025 and cap["unit"] == "GW" and cap["provenance"]["route"] == "file"
    assert cap["by_source"][0] == {
        "source": "solar",
        "label": "Solar",
        "operating_capacity_gw": 2200.0,
        "previous_gw": 1692.31,
        "change_gw": 507.69,
        "change_pct": 30.0,
    }
    assert data["clean_share"]["year"] == 2025 and 30 < data["clean_share"]["pct"] < 50
    assert data["clean_share"]["change_pp"] > 0
    # The latest year covers four of the five countries of 2024: World 2025 is Ember's estimate.
    assert data["latest_year"] == 2025 and data["latest_complete_year"] == 2024
    assert data["latest_year_estimated"] is True
    assert data["coverage"]["countries_latest_year"] == 4
    assert [r["year"] for r in data["series"]] == [2023, 2024, 2025]
    assert data["series"][-1]["operating_capacity_gw"]["solar"] == 2200.0
    top = data["top_wind_solar_additions"]
    assert top["basis"] == "yearly" and top["year"] == 2024 and top["countries"][0]["area"]["id"] == "CN"
    month = data["latest_month"]["generation"]
    assert month["month"] == "2026-08" and month["by_source"][0]["same_month_last_year_twh"] is not None
    sources = body["licence_summary"]["sources"]
    assert [s["source_id"] for s in sources] == [SOURCE_ID] and sources[0]["requires_link_back"] is True
    assert sorted(c["dataset"] for c in body["notes"]["credits"]) == ["monthly", "monthly_capacity", "yearly"]


def test_us_links_to_the_us_pipeline_as_a_different_measure(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    data = _get(client, area="US")["data"]
    assert data["area"]["id"] == "US" and data["area"]["iso3"] == "USA"
    assert data["latest_year_estimated"] is False
    link = data["us_pipeline"]
    assert link["measure"] == "pipeline"
    # The active pipeline only: built, withdrawn, cancelled and unknown records, and records no register
    # lists any more, are not a pipeline (review of 2026-10-23).
    assert link["url"] == (
        "https://api.inframation.io/v1/stats/capacity?country=US"
        "&lifecycle_state=announced,filed,studied,permitted,contracted,under_construction&listed=true"
    )
    assert link["list_query"] == {
        "path": "/v1/stats/capacity",
        "query": {"country": "US", "lifecycle_state": ",".join(ACTIVE_LIFECYCLE_STATES), "listed": "true"},
    }
    assert "built" not in link["url"] and "withdrawn" not in link["url"]
    assert "different measure" in link["note"] and "active" in link["note"]
    wind_solar = data["latest_month"]["wind_solar_capacity"]
    assert wind_solar["month"] == "2026-08" and wind_solar["by_source"][0]["source"] == "solar"


def test_a_country_reports_its_own_latest_year(client: TestClient, seeded: dict[str, Any]) -> None:
    data = _get(client, area="br")["data"]
    assert data["area"]["name"] == "Brazil"
    assert data["generation"]["year"] == 2024 and data["operating_capacity"]["year"] == 2024
    assert data["latest_complete_year"] == 2024


def test_regions_by_name_or_slug(client: TestClient, seeded: dict[str, Any]) -> None:
    assert _get(client, area="Europe")["data"]["area"]["kind"] == "region"
    assert _get(client, area="europe")["data"]["area"]["id"] == "europe"


def test_the_years_window_narrows_the_series_only(client: TestClient, seeded: dict[str, Any]) -> None:
    data = _get(client, years="2024")["data"]
    assert [r["year"] for r in data["series"]] == [2024] and data["generation"]["year"] == 2025
    assert [r["year"] for r in _get(client, years="2024-")["data"]["series"]] == [2024, 2025]
    assert [r["year"] for r in _get(client, years="-2023")["data"]["series"]] == [2023]
    assert _get(client, years="2023-2024")["data"]["series_window"] == {"first_year": 2023, "last_year": 2024}


@pytest.mark.parametrize(
    "params", [{"years": "2025-2020"}, {"years": "25"}, {"years": "1800"}, {"region": "x"}]
)
def test_bad_parameters_are_refused(client: TestClient, params: dict[str, str]) -> None:
    assert client.get(PATH, params=params).status_code == 400


def test_an_unknown_area_is_not_found(client: TestClient, seeded: dict[str, Any]) -> None:
    resp = client.get(PATH, params={"area": "Atlantis"})
    assert resp.status_code == 404 and "World" in resp.json()["detail"]


def test_a_hidden_source_serves_nothing(
    client: TestClient, seeded: dict[str, Any], db_sessionmaker: sessionmaker[Session]
) -> None:
    with db_sessionmaker() as session:
        source = session.get(Source, SOURCE_ID)
        assert source is not None
        source.publish_state = "ingest_only"
        session.commit()
    data = _get(client)["data"]
    assert data["loaded"] is False and "generation" not in data


def test_worlds_estimated_year_is_flagged_on_every_block_and_row(
    client: TestClient, seeded: dict[str, Any]
) -> None:
    data = _get(client)["data"]
    assert data["latest_complete_year"] == 2024
    for block in ("generation", "clean_share", "operating_capacity"):
        assert data[block]["year"] == 2025 and data[block]["estimated"] is True, block
    assert [(r["year"], r["estimated"]) for r in data["series"]] == [
        (2023, False),
        (2024, False),
        (2025, True),
    ]
    us = _get(client, area="US")["data"]
    assert us["generation"]["estimated"] is False and not any(r["estimated"] for r in us["series"])


def test_a_yearly_download_with_full_dates_serves_yearly_figures(
    client: TestClient, db_sessionmaker: sessionmaker[Session], tmp_path: pathlib.Path
) -> None:
    with db_sessionmaker() as session:
        out = seed(session, tmp_path, api=False, body=dated_wide_csv())
        session.commit()
    assert out["run"]["outputs"]["datasets"] == ["yearly/file"]
    data = _get(client)["data"]
    assert data["latest_month"] is None  # nothing monthly was loaded
    gen = data["generation"]
    assert gen["year"] == 2025 and gen["provenance"]["route"] == "file"
    assert gen["by_source"][0]["generation_twh"] == 2700.0
    assert data["operating_capacity"]["year"] == 2025 and data["operating_capacity_withheld_reason"] is None
    assert data["coverage"]["countries_latest_year"] == 4
    assert data["top_wind_solar_additions"]["basis"] == "yearly"


def test_a_download_without_a_country_code_column_never_turns_countries_into_regions(
    client: TestClient, db_sessionmaker: sessionmaker[Session], tmp_path: pathlib.Path
) -> None:
    # Review of 2026-10-23: the wide file without `ISO 3 code` and `Area type` (what a header such as
    # 'ISO code' or 'Code' did before those were read) loaded every country as a region.
    fields, rows = wide_rows()
    body = csv_bytes([f for f in fields if f not in ("ISO 3 code", "Area type")], rows)
    with db_sessionmaker() as session:
        out = seed(session, tmp_path, body=body)
        session.commit()
    item = out["run"]["inbox"][0]
    assert item["state"] == "failed" and "country code" in item["error"]
    germany = _get(client, area="DE")["data"]["area"]
    assert germany == {"id": "DE", "name": "Germany", "kind": "country", "iso3": "DEU"}
    assert _get(client, area="US")["data"]["area"]["id"] == "US"
    # A country is found by its code; no region named after it exists.
    assert client.get(PATH, params={"area": "germany"}).status_code == 404
    data = _get(client)["data"]
    assert data["coverage"]["countries_latest_complete_year"] == 5
    assert data["top_wind_solar_additions"]["basis"] == "monthly_december"  # no yearly capacity loaded
