"""sample() and sample_line() over every layer built from the recorded fixtures: values, provenance, the
screening flag and sentence, the publication gate, and the hard constraint."""

from __future__ import annotations

import json
import pathlib
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np
import pytest

from services.context_sample import CATALOG, SITE_FINDER_LAYERS
from services.context_sample import sampler as sampler_mod
from services.context_sample.sampler import Sampler

PECOS = (-103.5, 31.4)
BIG_BEND = (-103.25, 29.25)
TITUS = (-94.97, 33.2)
AUSTIN = (-97.74, 30.27)
AMARILLO = (-101.85, 35.22)


def _layer(result: dict[str, Any], layer_id: str) -> dict[str, Any]:
    return next(item for item in result["layers"] if item["layer"] == layer_id)


def test_every_layer_is_built_and_listed(fixture_prospect_dir: pathlib.Path) -> None:
    s = Sampler(fixture_prospect_dir)
    assert sorted(s.available()) == sorted(CATALOG)
    manifest = json.loads((fixture_prospect_dir / "layers.json").read_text())
    for lid, entry in manifest["layers"].items():
        assert entry["sha256"] and entry["bytes"] > 0, lid
        assert set(entry["provenance"]) >= {"source_id", "source_url", "retrieved_at", "licence", "vintage"}


def test_point_values_carry_screening_flag_sentence_and_provenance(
    fixture_prospect_dir: pathlib.Path,
) -> None:
    r = Sampler(fixture_prospect_dir).sample(*PECOS)
    assert r["screening"] == "screening"
    ghi = _layer(r, "solar_ghi")
    assert ghi["status"] == "ok" and ghi["value"] == 5.81 and ghi["unit"] == "kWh/m²/day"
    assert ghi["screening"] == "screening"
    assert "not a bankable resource assessment" in ghi["not"]
    prov = ghi["provenance"]
    assert prov["source_id"] == "us.nrel.nsrdb.psm3_annual"
    assert "entire notice appears in all copies" in prov["notice"]
    assert prov["vintage"].startswith("NSRDB PSM v3, 1998-2016")
    assert "DOE)/NLR/ALLIANCE" in prov["attribution"]
    for item in r["layers"]:
        assert item["screening"] == "screening" and item["not"].endswith("."), item["layer"]
    plays = _layer(r, "oil_gas_plays")
    assert plays["inside"] and plays["features"][0]["name"] == "Wolfcamp"
    assert _layer(r, "oil_gas_basins")["features"][0]["name"] == "PERMIAN"
    reserves = _layer(r, "oil_gas_reserves_state")["features"][0]
    assert reserves["state"] == "Texas" and reserves["crude_oil_lease_condensate_mmbbl"] == 19644.0
    assert _layer(r, "seismic_pga_2pct50")["value"] == pytest.approx(0.066, abs=0.002)
    assert _layer(r, "water_stress")["features"][0]["label"] == "Extremely High (>80%)"


def test_energy_community_values_say_they_are_not_tax_advice(fixture_prospect_dir: pathlib.Path) -> None:
    s = Sampler(fixture_prospect_dir)
    coal = _layer(s.sample(*TITUS), "energy_community_coal_closure")
    assert coal["inside"] and coal["features"][0]["dataset_as_of"] == "2024-06-07"
    assert "not tax advice" in coal["not"] and "taxpayers may not rely" in coal["not"]
    assert "may not be relied upon by taxpayers" in coal["provenance"]["taxpayer_line"]
    ffe = _layer(s.sample(*AUSTIN), "energy_community_ffe")["features"][0]
    assert ffe["statistical_area"] == "Austin-Round Rock, TX"
    assert ffe["meets_ffe_threshold"] is True and ffe["is_energy_community"] is False


def test_gap_1_2_land_is_a_hard_constraint(fixture_prospect_dir: pathlib.Path) -> None:
    s = Sampler(fixture_prospect_dir)
    pl = _layer(s.sample(*BIG_BEND), "protected_lands")
    assert pl["hard_constraint"] is True and pl["features"][0]["unit_name"] == "Big Bend National Park"
    assert _layer(s.sample(*AUSTIN), "protected_lands")["hard_constraint"] is False
    line = s.sample_line({"type": "LineString", "coordinates": [[-103.35, 29.3], [-103.1, 29.2]]}, 0.5)
    pl_line = _layer(line, "protected_lands")
    assert pl_line["hard_constraint_share"] == pytest.approx(1.0)
    assert pl_line["features"][0]["share"] == pytest.approx(1.0)


def test_line_summary_states_its_method(fixture_prospect_dir: pathlib.Path) -> None:
    s = Sampler(fixture_prospect_dir)
    r = s.sample_line([[-103.6, 31.4], [-103.4, 31.4]], 1.0, list(SITE_FINDER_LAYERS))
    assert [item["layer"] for item in r["layers"]] == list(SITE_FINDER_LAYERS)
    assert r["sample_points"] > 500 and "lattice" in r["method"]
    ghi = _layer(r, "solar_ghi")["summary"]
    assert 5.7 < ghi["mean"] < 5.9 and ghi["coverage_share"] == 1.0
    assert _layer(r, "oil_gas_plays")["share"] > 0.9
    wind = _layer(r, "wind_speed_100m")
    assert wind["status"] == "no_data", "the synthetic wind fixture covers Amarillo only"


def test_withheld_when_the_manifest_does_not_allow_publication(
    fixture_prospect_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = sampler_mod._source_gate
    monkeypatch.setattr(
        sampler_mod,
        "_source_gate",
        lambda sid: "is publication: none" if sid == "us.nrel.nsrdb.psm3_annual" else real(sid),
    )
    r = Sampler(fixture_prospect_dir).sample(*PECOS)
    ghi = _layer(r, "solar_ghi")
    assert ghi["status"] == "withheld" and "value" not in ghi and ghi["provenance"] is None
    assert _layer(r, "oil_gas_plays")["status"] == "ok"


def test_the_real_gate_reads_data_sources_yaml() -> None:
    sampler_mod.reset_default_sampler()
    assert sampler_mod._source_gate("us.nrel.nsrdb.psm3_annual") is None
    # us.usfws.nwi was the gated example until its terms were read on 2026-10-24 (docs/07 §1.6);
    # the EIA field outlines stay unknown (not found).
    assert "reuse: unknown" in str(sampler_mod._source_gate("us.eia.oil_gas_field_boundaries"))
    assert "no row" in str(sampler_mod._source_gate("not.a.source"))


def test_unavailable_when_a_layer_is_not_built(
    fixture_prospect_dir: pathlib.Path, tmp_path: pathlib.Path
) -> None:
    d = tmp_path / "p"
    shutil.copytree(fixture_prospect_dir, d)
    manifest = json.loads((d / "layers.json").read_text())
    del manifest["layers"]["wind_speed_100m"]
    (d / "layers.json").write_text(json.dumps(manifest))
    r = Sampler(d).sample(*AMARILLO)
    assert _layer(r, "wind_speed_100m")["status"] == "unavailable"
    assert Sampler(tmp_path / "empty").sample(*AMARILLO)["layers"][0]["status"] == "unavailable"


def test_wind_from_the_manual_snapshot(fixture_prospect_dir: pathlib.Path) -> None:
    wind = _layer(Sampler(fixture_prospect_dir).sample(*AMARILLO), "wind_speed_100m")
    assert wind["value"] == pytest.approx(6.4) and wind["unit"] == "m/s"
    assert wind["provenance"]["retrieved_at"] == "2026-10-22T00:00:00Z"
    assert "Global Wind Atlas version 4.0" in wind["provenance"]["attribution"]


def test_catalogue_sources_all_have_quoted_terms() -> None:
    from pipeline.connectors.registry import Registry

    reg = Registry()
    for info in CATALOG.values():
        entry = reg.get(info.source_id)
        assert entry.license and "READ 2026-10-22" in entry.license, info.source_id
        assert entry.publication in ("raw_ok", "derived_only"), info.source_id
        # One plain sentence of what the value is not.
        assert info.not_statement.endswith(".") and ". " not in info.not_statement, info.layer_id


MCALLEN = (-98.23, 26.2)


def test_the_combined_energy_community_answer(fixture_prospect_dir: pathlib.Path) -> None:
    """One answer across the two NETL layers: a coal-closure tract, or a statistical area that is an
    energy community this year. An FFE-only area (Travis County, 2024) meets the employment threshold but
    does not qualify."""
    s = Sampler(fixture_prospect_dir)
    titus, mcallen, austin = s.sample(*TITUS), s.sample(*MCALLEN), s.sample(*AUSTIN)
    assert titus["energy_community"]["qualifies"] is True
    assert mcallen["energy_community"]["qualifies"] is True
    assert _layer(mcallen, "energy_community_ffe")["qualifies"] is True
    assert austin["energy_community"]["qualifies"] is False
    assert (
        _layer(austin, "energy_community_ffe")["inside"]
        and not _layer(austin, "energy_community_ffe")["qualifies"]
    )
    ec = austin["energy_community"]
    assert ec["status"] == "ok" and ec["screening"] == "screening" and "not tax advice" in ec["not"]
    assert ec["layers"] == ["energy_community_coal_closure", "energy_community_ffe"]
    assert "energy_community" not in s.sample(*AUSTIN, ["solar_ghi"])


def test_the_combined_answer_names_its_source_and_the_list_year(fixture_prospect_dir: pathlib.Path) -> None:
    """The combined block is what a reader sees first, so it carries the source and the dataset's own
    dates: Treasury revises the statistical-area list each year, and the 2024 layers are what NETL serves."""
    s = Sampler(fixture_prospect_dir)
    for ec in (
        s.sample(*AUSTIN)["energy_community"],
        s.sample_line([[-97.75, 30.3], [-97.6, 30.3]], 0.5)["energy_community"],
    ):
        assert ec["source_id"] == "us.doe.netl.energy_communities"
        assert ec["dataset_as_of"] == {
            "energy_community_coal_closure": "2024-06-07",
            "energy_community_ffe": "2024-05-31",
        }
        assert set(ec["vintage"]) == set(ec["layers"])
        assert all("2024" in str(v) for v in ec["vintage"].values())


def test_an_ffe_only_area_names_the_service_it_came_from(fixture_prospect_dir: pathlib.Path) -> None:
    """The statistical-area layer joins NETL's energy-community and FFE-only services; Travis County comes
    from the second, so its feature says so, and the layer's provenance names both."""
    s = Sampler(fixture_prospect_dir)
    ffe = _layer(s.sample(*AUSTIN), "energy_community_ffe")
    assert ffe["features"][0]["source_url"].endswith(
        "/2024_MSAs_NonMSAs_that_only_meet_the_FFE_Threshold/FeatureServer/0"
    )
    assert len(ffe["provenance"]["source_urls"]) == 2
    hidalgo = _layer(s.sample(*MCALLEN), "energy_community_ffe")["features"][0]
    assert "source_url" not in hidalgo, "from the layer's own source_url"


HOBBS_NM = (-103.14, 32.70)  # inside the Texas bounding box, outside Texas


def test_outside_a_state_filtered_layer_is_no_data_not_outside(fixture_prospect_dir: pathlib.Path) -> None:
    """The Texas build fetches NETL and PAD-US for Texas only: in New Mexico they have no answer, which is
    "no data", not "not protected" or "not an energy community". Layers clipped to the region box (EIA,
    Aqueduct) still answer there."""
    s = Sampler(fixture_prospect_dir)
    r = s.sample(*HOBBS_NM)
    for lid in ("protected_lands", "energy_community_coal_closure", "energy_community_ffe"):
        item = _layer(r, lid)
        assert item["status"] == "no_data" and "inside" not in item, lid
        assert item["coverage"]["states"] == ["Texas"] and "Texas" in item["reason"]
    assert _layer(r, "oil_gas_basins")["status"] == "ok"
    assert r["energy_community"]["status"] == "unavailable" and "qualifies" not in r["energy_community"]


def test_a_corridor_running_out_of_coverage_is_partial(fixture_prospect_dir: pathlib.Path) -> None:
    """A corridor from Texas into New Mexico: the NM part has no PAD-US or NETL data in this build, so
    those layers are `partial`, with `coverage_share` and shares that are lower bounds."""
    s = Sampler(fixture_prospect_dir)
    line = s.sample_line([[-103.3, 31.95], [-103.3, 32.1]], 0.5)  # crosses the TX-NM line at 32.0 N
    pl = _layer(line, "protected_lands")
    assert pl["status"] == "partial" and 0.2 < pl["coverage_share"] < 0.6
    assert "lower bounds" in pl["reason"]
    assert line["energy_community"]["status"] == "partial"
    assert "only part of the band" in line["energy_community"]["reason"]
    inside = s.sample_line([[-103.6, 31.4], [-103.4, 31.4]], 1.0)
    assert _layer(inside, "protected_lands")["status"] == "ok"
    assert _layer(inside, "protected_lands")["coverage_share"] == 1.0
    outside = s.sample_line([[-103.3, 32.4], [-103.2, 32.4]], 0.5)
    assert _layer(outside, "protected_lands")["status"] == "no_data"

    # Travis County to Austin's east: inside an FFE area along its whole length, qualifying nowhere.
    line = s.sample_line([[-97.75, 30.3], [-97.6, 30.3]], 0.5)
    ffe = _layer(line, "energy_community_ffe")
    assert ffe["share"] == pytest.approx(1.0) and ffe["qualifying_share"] == 0.0
    assert line["energy_community"]["share"] == 0.0
    # Across Titus County's coal tracts: the union counts each lattice point once.
    coal_line = s.sample_line([[-95.1, 33.2], [-94.85, 33.2]], 1.0)
    assert coal_line["energy_community"]["share"] == pytest.approx(
        _layer(coal_line, "energy_community_coal_closure")["share"]
    )
    assert coal_line["energy_community"]["share"] > 0.9


def test_the_energy_community_answer_says_when_it_is_partial(fixture_prospect_dir: pathlib.Path) -> None:
    """With one of the two layers missing, the answer is a lower bound and says so; with neither, it has
    no value at all."""
    d = Sampler(fixture_prospect_dir)
    del d.manifest["layers"]["energy_community_ffe"]
    ec = d.sample(*MCALLEN)["energy_community"]
    assert ec["status"] == "partial" and ec["qualifies"] is False and "energy_community_ffe" in ec["reason"]
    del d.manifest["layers"]["energy_community_coal_closure"]
    ec = d.sample(*MCALLEN)["energy_community"]
    assert ec["status"] == "unavailable" and "qualifies" not in ec


def _comparable(answer: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in answer.items() if k != "elapsed_ms"}


def test_one_sampler_shared_by_threads_gives_the_serial_answers(fixture_prospect_dir: pathlib.Path) -> None:
    """FastAPI runs GET /v1/context/sample in a threadpool over one process-wide Sampler, so cold
    requests open and read the same files at once. 16 threads over 400 random Texas points must give
    exactly the answers one thread gives (the reviewer's reproduction, 2026-10-23)."""
    rng = np.random.default_rng(23)
    pts = np.column_stack([rng.uniform(-106.5, -93.6, 400), rng.uniform(25.9, 36.4, 400)])
    serial = Sampler(fixture_prospect_dir)
    want = [_comparable(serial.sample(float(x), float(y))) for x, y in pts]
    threads = 16
    for _trial in range(2):
        shared = Sampler(fixture_prospect_dir)
        gate = threading.Barrier(threads)

        def work(k: int, s: Sampler = shared, b: threading.Barrier = gate) -> list[dict[str, Any]]:
            b.wait()
            return [_comparable(s.sample(float(x), float(y))) for x, y in pts[k::threads]]

        with ThreadPoolExecutor(threads) as pool:
            parts = list(pool.map(work, range(threads)))
        got: list[dict[str, Any]] = [{} for _ in pts]
        for k, part in enumerate(parts):
            for n, answer in enumerate(part):
                got[k + n * threads] = answer
        assert got == want
