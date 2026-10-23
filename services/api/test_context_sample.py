"""`GET /v1/context/sample`: the prospecting layers at a point, inside the attribution envelope, valid against
`api/openapi.yaml`, with the screening flag, the sentence of what each value is not, and provenance on every
value (docs/25 "Prospecting layers"; docs/00-PLAN.md 2026-10-22)."""

from __future__ import annotations

import pathlib
from collections.abc import Iterator
from typing import Any

import jsonschema
import pytest
import yaml
from fastapi.testclient import TestClient

from services.api.app import app
from services.context_sample import sampler as sampler_mod
from services.context_sample.fixture_build import build_fixture_layers

ROOT = pathlib.Path(__file__).resolve().parents[2]
PECOS = {"lat": "31.4", "lon": "-103.5"}


@pytest.fixture(scope="module")
def spec() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load((ROOT / "api" / "openapi.yaml").read_text(encoding="utf-8"))
    return doc


@pytest.fixture(scope="module")
def built(tmp_path_factory: pytest.TempPathFactory) -> pathlib.Path:
    out = tmp_path_factory.mktemp("prospect-api")
    report = build_fixture_layers(out)
    assert not report["failures"], report["failures"]
    return out


@pytest.fixture()
def client(built: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PROSPECT_DATA_DIR", str(built))
    sampler_mod.reset_default_sampler()
    yield TestClient(app)
    sampler_mod.reset_default_sampler()


def _valid(spec: dict[str, Any], body: object) -> None:
    schema = spec["components"]["schemas"]["ContextSampleResponse"]
    resolver = jsonschema.validators.RefResolver.from_schema(spec)
    validator = jsonschema.validators.validator_for(schema)(schema, resolver=resolver)
    errors = sorted(validator.iter_errors(body), key=str)
    assert not errors, "\n".join(f"{e.message} at {list(e.absolute_path)}" for e in errors)


def _layer(body: dict[str, Any], layer_id: str) -> dict[str, Any]:
    return next(item for item in body["data"]["layers"] if item["layer"] == layer_id)


def test_point_answer_matches_the_contract(client: TestClient, spec: dict[str, Any]) -> None:
    r = client.get("/v1/context/sample", params=PECOS)
    assert r.status_code == 200, r.text
    body = r.json()
    _valid(spec, body)
    data = body["data"]
    assert data["screening"] == "screening" and data["point"] == {"lon": -103.5, "lat": 31.4}
    assert sorted(data["layers_built"]) == sorted(item["layer"] for item in data["layers"])
    for item in data["layers"]:
        assert item["screening"] == "screening" and item["not"], item["layer"]
        if item["status"] in ("ok", "no_data"):
            prov = item["provenance"]
            assert {"source_id", "source_url", "retrieved_at", "licence", "vintage"} <= set(prov), item[
                "layer"
            ]
            assert prov["licence_id"], item["layer"]
    ec = data["energy_community"]
    assert ec["status"] == "ok" and ec["qualifies"] is False and "not tax advice" in ec["not"]
    ghi = _layer(body, "solar_ghi")
    assert ghi["value"] == 5.81 and "not a bankable resource assessment" in ghi["not"]
    assert "entire notice appears in all copies" in ghi["provenance"]["notice"]
    assert ghi["provenance"]["licence"].startswith("NLR data notice")


def test_outside_a_layers_coverage_the_api_says_no_data(client: TestClient, spec: dict[str, Any]) -> None:
    """Hobbs, New Mexico, is inside the Texas build's box but outside Texas, the only state PAD-US and NETL
    were fetched for: those layers answer no_data with their coverage, and the energy-community block,
    which names its source and the dataset's dates, has nothing to say."""
    r = client.get("/v1/context/sample", params={"lat": "32.70", "lon": "-103.14"})
    assert r.status_code == 200, r.text
    body = r.json()
    _valid(spec, body)
    pl = _layer(body, "protected_lands")
    assert pl["status"] == "no_data" and pl["coverage"]["states"] == ["Texas"] and "inside" not in pl
    assert body["data"]["energy_community"]["status"] == "unavailable"
    austin = client.get("/v1/context/sample", params={"lat": "30.27", "lon": "-97.74"}).json()
    _valid(spec, austin)
    ec = austin["data"]["energy_community"]
    assert ec["source_id"] == "us.doe.netl.energy_communities"
    assert ec["dataset_as_of"]["energy_community_ffe"] == "2024-05-31"


def test_every_source_that_supplied_a_value_is_credited(client: TestClient) -> None:
    body = client.get("/v1/context/sample", params=PECOS).json()
    sources = {s["source_id"]: s for s in body["licence_summary"]["sources"]}
    assert "us.nrel.nsrdb.psm3_annual" in sources and "us.eia.atlas.shale_plays" in sources
    assert "DOE)/NLR/ALLIANCE" in sources["us.nrel.nsrdb.psm3_annual"]["attribution_text"]
    served = {
        item["provenance"]["source_id"]
        for item in body["data"]["layers"]
        if item["status"] in ("ok", "no_data")
    }
    assert served == set(sources)
    assert body["licence_summary"]["attribution_line"].startswith("Sources: ")


def test_layers_parameter_selects_and_rejects_unknown_ids(client: TestClient) -> None:
    r = client.get("/v1/context/sample", params={**PECOS, "layers": "energy_community_ffe,protected_lands"})
    assert r.status_code == 200
    assert [i["layer"] for i in r.json()["data"]["layers"]] == ["energy_community_ffe", "protected_lands"]
    ec = _layer(r.json(), "energy_community_ffe")
    assert "not tax advice" in ec["not"]
    bad = client.get("/v1/context/sample", params={**PECOS, "layers": "solar_ghi,gold_reserves"})
    assert bad.status_code == 400 and "gold_reserves" in bad.text


@pytest.mark.parametrize(
    "params",
    [
        {"lon": "-103.5"},
        {"lat": "31.4"},
        {"lat": "north", "lon": "-103.5"},
        {"lat": "91", "lon": "-103.5"},
        {"lat": "31.4", "lon": "-181"},
        {"lat": "nan", "lon": "-103.5"},
        {**PECOS, "radius": "5"},
    ],
)
def test_bad_parameters_are_400(client: TestClient, params: dict[str, str]) -> None:
    assert client.get("/v1/context/sample", params=params).status_code == 400


def test_a_withheld_source_carries_no_value_and_no_credit(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sampler_mod._source_gate
    monkeypatch.setattr(
        sampler_mod,
        "_source_gate",
        lambda sid: (
            "is publication: none in data/sources.yaml" if sid == "us.nrel.nsrdb.psm3_annual" else real(sid)
        ),
    )
    body = client.get("/v1/context/sample", params=PECOS).json()
    for layer_id in ("solar_ghi", "solar_dni"):
        item = _layer(body, layer_id)
        assert item["status"] == "withheld" and "value" not in item and item["provenance"] is None
    assert "us.nrel.nsrdb.psm3_annual" not in {s["source_id"] for s in body["licence_summary"]["sources"]}


def test_without_a_build_every_layer_is_unavailable(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch, spec: dict[str, Any]
) -> None:
    monkeypatch.setenv("PROSPECT_DATA_DIR", str(tmp_path / "nothing-built"))
    sampler_mod.reset_default_sampler()
    try:
        r = TestClient(app).get("/v1/context/sample", params=PECOS)
    finally:
        sampler_mod.reset_default_sampler()
    assert r.status_code == 200
    _valid(spec, r.json())
    assert {i["status"] for i in r.json()["data"]["layers"]} == {"unavailable"}
    assert r.json()["data"]["layers_built"] == []
