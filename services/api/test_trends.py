"""`GET /v1/trends`: the catalogue and one series through the real app, gated by the source's own
visibility, inside the attribution envelope, and valid against `api/openapi.yaml`'s `TrendResponse`."""

from __future__ import annotations

import json
import pathlib
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012
from sqlalchemy.orm import Session

from pipeline.connectors.history.documents import DOCUMENT_KIND, SCHEMA_VERSION, MonthlyStore
from services.api.conftest import make_open_licence, make_public_source
from services.trends import series as trends
from services.trends.store import TrendStore

SPEC = pathlib.Path(__file__).resolve().parents[2] / "api" / "openapi.yaml"
DAILY_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[1] / "trends" / "fixtures" / "daily-v1-2026-10-23.json"
)
EIA = trends.EIA


@pytest.fixture(autouse=True)
def data_root(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> pathlib.Path:
    monkeypatch.setenv("INFRAQUE_DATA_DIR", str(tmp_path))
    trends.clear_cache()
    store = MonthlyStore(tmp_path)
    for period, mw in (("2025-09", 90.0), ("2026-08", 100.0), ("2026-09", 120.0)):
        store.write(_doc(period, mw))
    return tmp_path


def _doc(period: str, mw: float) -> dict[str, Any]:
    tally = {"count": 3, "mw": mw, "mw_stated": 3}
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": DOCUMENT_KIND,
        "source_id": EIA,
        "period": period,
        "source_url": f"https://www.eia.gov/electricity/data/eia860m/xls/{period}.xlsx",
        "retrieved_at": "2026-10-23T22:00:00Z",
        "blocks": {
            "planned": {
                "basis": "eia860m/planned/net-summer-mw/50-states-dc",
                "total": tally,
                "by_technology": {"solar": tally},
                "by_state": {"US-TX": tally},
                "excluded": {},
            }
        },
        "dq": {"status": "pass", "checks": []},
    }


def _eia_source(db: Session, publish_state: str = "public") -> None:
    licence = make_open_licence(db, "us.eia.860m.history#0123456789")
    source = make_public_source(db, licence, id_=EIA)
    source.publish_state = publish_state
    source.attribution_text = "Source: U.S. Energy Information Administration"
    db.commit()


def _validate(body: dict[str, Any]) -> None:
    """Against `components.schemas.TrendResponse` of the committed spec (`test_sites.py`'s way)."""
    spec = yaml.safe_load(SPEC.read_text(encoding="utf-8"))
    base = "https://spec.local/openapi.yaml"
    registry = Registry().with_resource(uri=base, resource=Resource(contents=spec, specification=DRAFT202012))
    validator = Draft202012Validator({"$ref": f"{base}#/components/schemas/TrendResponse"}, registry=registry)
    errors = sorted(validator.iter_errors(body), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def test_the_catalogue_lists_a_series_only_while_its_source_is_public(
    client: TestClient, db: Session
) -> None:
    before = client.get("/v1/trends")
    assert before.status_code == 200
    assert "us_planned_capacity_mw" not in {s["metric"] for s in before.json()["data"]}
    _eia_source(db)
    body = client.get("/v1/trends").json()
    _validate(body)
    entry = next(s for s in body["data"] if s["metric"] == "us_planned_capacity_mw")
    assert entry["first"] == "2025-09" and entry["last"] == "2026-09" and entry["points"] == 3
    # EIA asks for the publication date: September 2026's workbook was published in October 2026
    assert entry["source"]["credit"] == "Source: U.S. Energy Information Administration (Oct 2026)"
    [eia] = [s for s in body["licence_summary"]["sources"] if s["source_id"] == EIA]
    assert eia["record_count"] == 3  # one row per month the catalogue describes
    assert body["notes"]["like_for_like"]


def test_a_source_taken_off_the_surface_takes_its_series(client: TestClient, db: Session) -> None:
    _eia_source(db, publish_state="ingest_only")
    response = client.get("/v1/trends", params={"metric": "us_planned_capacity_mw"})
    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == "metric"


def test_one_series_with_like_for_like_changes(client: TestClient, db: Session) -> None:
    _eia_source(db)
    response = client.get("/v1/trends", params={"metric": "us_planned_capacity_mw", "from": "2026-09"})
    assert response.status_code == 200
    body = response.json()
    _validate(body)
    line = body["data"]["lines"][0]
    assert line["key"] == "total"
    [point] = line["points"]
    assert point["period"] == "2026-09"
    assert point["mom"]["change"] == 20.0 and point["yoy"]["from"] == "2025-09"
    assert point["source_url"].endswith("2026-09.xlsx")
    by_state = client.get("/v1/trends", params={"metric": "us_planned_capacity_mw", "by": "state"}).json()
    _validate(by_state)
    assert [line["key"] for line in by_state["data"]["lines"]] == ["US-TX"]


@pytest.mark.parametrize(
    ("params", "field"),
    [
        ({"metric": "us_planned_capacity_mw", "by": "colour"}, "by"),
        ({"by": "technology"}, "by"),
        ({"metric": "us_planned_capacity_mw", "from": "last year"}, "from"),
    ],
)
def test_bad_requests_name_the_field(
    client: TestClient, db: Session, params: dict[str, str], field: str
) -> None:
    _eia_source(db)
    response = client.get("/v1/trends", params=params)
    assert response.status_code == 400
    assert response.json()["errors"][0]["field"] == field


def test_an_unknown_parameter_is_refused(client: TestClient) -> None:
    response = client.get("/v1/trends", params={"sort": "value"})
    assert response.status_code == 400
    assert response.json()["code"] == "unknown_parameter"


def test_the_daily_record_is_its_own_series_beside_the_archives(
    client: TestClient, db: Session, data_root: pathlib.Path
) -> None:
    """The recorder's documents serve as daily series with no source row needed (they are counts the
    public tier served), each point naming its day's document and the list query it totals; they
    never join a monthly line."""
    document = json.loads(DAILY_FIXTURE.read_text(encoding="utf-8"))
    TrendStore(data_root).write(document)
    _eia_source(db)
    catalogue = {s["metric"]: s for s in client.get("/v1/trends").json()["data"]}
    daily = catalogue["inframation_active_projects"]
    assert daily["frequency"] == "daily" and daily["first"] == daily["last"] == document["as_of"]
    assert daily["source"]["source_id"] == trends.DAILY
    assert catalogue["us_planned_capacity_mw"]["source"]["source_id"] == EIA
    body = client.get("/v1/trends", params={"metric": "inframation_active_projects", "by": "state"}).json()
    _validate(body)
    lines = {line["key"]: line["points"] for line in body["data"]["lines"]}
    tx = lines["US-TX"][0]
    assert tx["document"] == document["as_of"] and tx["query"] == document["queries"]["proposals.active"]
    assert tx["value"] == document["metrics"]["proposals"]["active"]["by_state"]["US-TX"]["records"]
    assert tx["mom"] is None and tx["yoy"] is None  # one day: nothing to compare with


def test_a_daily_series_carries_every_counted_sources_own_credit(
    client: TestClient, db: Session, data_root: pathlib.Path
) -> None:
    """The daily counts include derived-only, credit-required sources (CAISO: "Source: California
    ISO"; NYISO: "Source: NYISO"). Their credits are in the licence summary of every response that
    serves those days, the catalogue's too: from the source's row where the store holds it, as every
    route credits it, else as the day's document recorded it. A monthly series' summary lists only
    its own source."""
    document = json.loads(DAILY_FIXTURE.read_text(encoding="utf-8"))
    recorded = dict(document["sources"][0])
    document["sources"] = [
        {**recorded, "source_id": "us.iso.caiso.gen_queue", "name": "CAISO Public Queue Report",
         "attribution_text": "Source: California ISO", "retrieved_at_max": "2026-10-23T03:10:00Z"},
        {**recorded, "source_id": "us.iso.nyiso.gen_queue", "name": "NYISO Interconnection Queue",
         "attribution_text": "Source: NYISO (as recorded)", "retrieved_at_max": "2026-10-23T03:20:00Z"},
    ]  # fmt: skip
    TrendStore(data_root).write(document)
    nyiso = make_public_source(db, make_open_licence(db, "nyiso-lic"), id_="us.iso.nyiso.gen_queue")
    nyiso.name = "NYISO Interconnection Queue"
    nyiso.attribution_text = "Source: NYISO"
    _eia_source(db)
    body = client.get("/v1/trends", params={"metric": "inframation_active_projects"}).json()
    _validate(body)
    credits = {s["source_id"]: s for s in body["licence_summary"]["sources"]}
    assert set(credits) == {"us.iso.caiso.gen_queue", "us.iso.nyiso.gen_queue"}
    assert credits["us.iso.caiso.gen_queue"]["attribution_text"] == "Source: California ISO"
    assert credits["us.iso.nyiso.gen_queue"]["attribution_text"] == "Source: NYISO"  # today's credit
    assert credits["us.iso.caiso.gen_queue"]["record_count"] == 1
    assert credits["us.iso.caiso.gen_queue"]["retrieved_at_max"].startswith("2026-10-23T03:10:00")
    assert "CAISO Public Queue Report" in body["licence_summary"]["attribution_line"]
    catalogue = client.get("/v1/trends").json()
    assert {"us.iso.caiso.gen_queue", EIA} <= {
        s["source_id"] for s in catalogue["licence_summary"]["sources"]
    }
    monthly = client.get("/v1/trends", params={"metric": "us_planned_capacity_mw", "from": "2026-09"}).json()
    assert [(s["source_id"], s["record_count"]) for s in monthly["licence_summary"]["sources"]] == [(EIA, 1)]
