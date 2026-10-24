"""pipeline/resolve.py rule L: point evidence and the location veto between data-centre load records
(docs/22 §28; coordinator call of 2026-10-24, docs/00-PLAN.md).

Each frame copies names, counties, registry ids and exact points from the records of the 2026-10-09
ICIS-Air and Virginia DEQ frames, the 2026-10-24 NPDES construction frame and the 2026-10-24 FAA
Part 77 frame, and goes through `run()` itself, so blocking, scoring, the vetoes and the cluster
checks are all exercised.
"""

from __future__ import annotations

import json
import pathlib

import pandas as pd
import pytest

from pipeline import resolve
from pipeline.normalize import norm_county, norm_name

ICIS = "us.epa.echo.icis_air"
DEQ = "us.va.deq.data_center_air_sites"
NPDES = "us.epa.echo.icis_npdes_construction"
FAA = "us.faa.oeaaa.part77_cases"
EIA = "us.eia.860m"
CAISO = "us.iso.caiso.gen_queue"


def load(
    source: str,
    rid: str,
    name: str,
    point: tuple[float, float] | None,
    *,
    county: str,
    state: str,
    refs: str = "",
) -> dict[str, object]:
    """A load (data-centre) record as the connectors write it: `raw` carries `Latitude`/`Longitude`
    only for an exact point."""
    raw = {"Latitude": point[0], "Longitude": point[1]} if point else {}
    return {
        "record_id": f"{source}:{rid}",
        "source_id": source,
        "source_record_id": rid,
        "kind": "load",
        "name_canonical": name,
        "name_norm": norm_name(name),
        "sponsor_name": None,
        "sponsor_norm": None,
        "technology": "load",
        "capacity_mw": None,
        "iso": None,
        "state": state,
        "county": county,
        "county_norm": norm_county(county),
        "lifecycle_state": "built",
        "status_raw": "x",
        "proposed_cod": pd.NaT,
        "queue_id": None,
        "eia_plant_id": None,
        "eia_generator_id": None,
        "cross_refs": refs,
        "raw": json.dumps(raw),
    }


def run(tmp_path: pathlib.Path, rows: list[dict[str, object]]) -> tuple[pd.DataFrame, pd.DataFrame]:
    frame = pd.DataFrame(rows)
    frame["capacity_mw"] = frame["capacity_mw"].astype("Float64")
    frame["proposed_cod"] = pd.to_datetime(frame["proposed_cod"])
    path = tmp_path / "normalized.parquet"
    frame.to_parquet(path, index=False)
    return resolve.run(75.0, path)


def together(clusters: pd.DataFrame, a: str, b: str) -> bool:
    cid = clusters.drop_duplicates("record_id").set_index("record_id")["cluster_id"]
    return a in cid.index and b in cid.index and cid[a] == cid[b]


def pair(matches: pd.DataFrame, a: str, b: str) -> pd.Series:
    hit = matches[
        ((matches["left_id"] == a) & (matches["right_id"] == b))
        | ((matches["left_id"] == b) & (matches["right_id"] == a))
    ]
    assert len(hit) == 1, (a, b, len(hit))
    return hit.iloc[0]


# Vantage in Fulton County, GA: two ICIS-Air campuses 11.6 km apart and an FAA crane 339 m from GA2.
GA1 = load(
    ICIS,
    "GA0000001312100981",
    "VANTAGE DATA CENTERS GA1, LLC",
    (33.60557, -84.53346),
    county="Fulton",
    state="GA",
    refs="icis_air:GA0000001312100981|frs:110071883592",
)
GA2 = load(
    ICIS,
    "GA0000001312100982",
    "VANTAGE DATA CENTERS GA2, LLC",
    (33.70729, -84.5822),
    county="Fulton",
    state="GA",
    refs="icis_air:GA0000001312100982|frs:110071958049",
)
CRANE = load(
    FAA,
    "2025-ASO-24741-OE",
    "Vantage Data Center Crane",
    (33.710119, -84.583553),
    county="Fulton",
    state="GA",
    refs="faa_asn:2025-ASO-24741-OE",
)


def test_two_load_points_more_than_2_km_apart_never_merge(tmp_path: pathlib.Path) -> None:
    """The FAA lane's far merges: one operator's name in one county matched the crane to both
    campuses (12.5 km and 339 m), so the two held campuses became one record."""
    matches, clusters = run(tmp_path, [GA1, GA2, CRANE])
    far = pair(matches, GA1["record_id"], CRANE["record_id"])
    assert "veto_location" in far["veto"] and not far["accepted"]
    assert far["apart_km"] == pytest.approx(12.52, abs=0.01)
    assert together(clusters, GA2["record_id"], CRANE["record_id"])
    assert not together(clusters, GA1["record_id"], CRANE["record_id"])


# CyrusOne in Loudoun: DEQ's NVA14 (exact point) and ICIS-Air's NVA14 (no exact point) cite one ICIS
# id; FAA's NVA11 site is 8.1 km from DEQ's point.
NVA14_DEQ = load(
    DEQ,
    "74290",
    "CyrusOne – NVA14",
    (39.019577, -77.450456),
    county="Loudoun",
    state="VA",
    refs="icis_air:VA0000005110774290",
)
NVA14_ICIS = load(
    ICIS,
    "VA0000005110774290",
    "CYRUSONE – NVA14",
    None,
    county="Loudoun",
    state="VA",
    refs="icis_air:VA0000005110774290",
)
NVA11_FAA = load(
    FAA,
    "2025-AEA-5649-OE",
    "CyrusOne - NVA11",
    (38.958806, -77.502806),
    county="Loudoun",
    state="VA",
    refs="faa_asn:2025-AEA-5649-OE",
)


def test_a_record_without_a_point_cannot_bridge_two_far_ones(tmp_path: pathlib.Path) -> None:
    matches, clusters = run(tmp_path, [NVA14_DEQ, NVA14_ICIS, NVA11_FAA])
    assert together(clusters, NVA14_DEQ["record_id"], NVA14_ICIS["record_id"])  # D3, one ICIS id
    assert not together(clusters, NVA11_FAA["record_id"], NVA14_DEQ["record_id"])
    bridge = pair(matches, NVA14_ICIS["record_id"], NVA11_FAA["record_id"])
    assert bridge["score"] >= 75 and not bridge["accepted"]
    assert "veto_location_chain" in bridge["rationale"]


def test_a_shared_registry_id_is_not_vetoed_and_wins_a_tied_score(tmp_path: pathlib.Path) -> None:
    """Visa in Loudoun: ICIS-Air's address match and DEQ's registration point are 3.46 km apart, and
    both cite one ICIS id. The pair scores 100 both as a deterministic and as a fuzzy match; the row
    kept is the deterministic one, which no veto refuses."""
    icis = load(
        ICIS,
        "VA0000005110700886",
        "VISA USA INCORPORATED",
        (39.049674, -77.442742),
        county="Loudoun",
        state="VA",
        refs="icis_air:VA0000005110700886|frs:110031217706",
    )
    deq = load(
        DEQ,
        "73643",
        "Visa USA Incorporated",
        (39.0219, -77.4608),
        county="Loudoun",
        state="VA",
        refs="icis_air:VA0000005110700886",
    )
    # 160 more facilities that both registers list under one ICIS id and one name (each pair scores 100
    # twice), and 160 pairs of different facilities whose scores vary. Which row of a tied pair was
    # kept used to depend on an unstable sort: on the code before rule L, 95 of the 160 kept the fuzzy
    # row (numpy 2.4.6, pandas 2.3.3), the row a location veto may refuse.
    suffixes = [" LLC", " Campus", " Data Hall", " North", " Building 2", " Expansion", " West Annex"]
    rows = [icis, deq]
    for i in range(160):
        rows += [
            load(
                ICIS,
                f"VA{i:016d}",
                f"Site{i} Data Center",
                None,
                county="Albemarle",
                state="VA",
                refs=f"icis_air:VA{i:016d}",
            ),
            load(
                DEQ,
                f"{80000 + i}",
                f"Site{i} Data Center",
                None,
                county="Albemarle",
                state="VA",
                refs=f"icis_air:VA{i:016d}",
            ),
            load(
                ICIS,
                f"VB{i:016d}",
                f"Other{i} Data Center",
                None,
                county="Albemarle",
                state="VA",
                refs=f"icis_air:VB{i:016d}",
            ),
            load(
                DEQ,
                f"{90000 + i}",
                f"Other{i} Data Center{suffixes[i % len(suffixes)]}",
                None,
                county="Albemarle",
                state="VA",
                refs=f"icis_air:VC{i:016d}",
            ),
        ]
    matches, clusters = run(tmp_path, rows)
    row = pair(matches, icis["record_id"], deq["record_id"])
    assert str(row["pass"]).startswith("D3") and row["accepted"]
    assert together(clusters, icis["record_id"], deq["record_id"])
    kept = [str(pair(matches, f"{ICIS}:VA{i:016d}", f"{DEQ}:{80000 + i}")["pass"]) for i in range(160)]
    assert all(p.startswith("D3") for p in kept), sum(not p.startswith("D3") for p in kept)


# Prince William: the construction permit names the campus code, the air permit the operator.
MNZ03_DEQ = load(
    DEQ,
    "74236",
    "Microsoft - Gainesville Data Center - MNZ03",
    (38.780539, -77.56973),
    county="Prince William",
    state="VA",
    refs="icis_air:VA0000005115374236",
)
MNZ03_NPDES = load(
    NPDES,
    "VAR10T763",
    "MNZ03 DATA CENTER",
    (38.7822, -77.5663),
    county="Prince William",
    state="VA",
    refs="npdes:VAR10T763|frs:110071670945",
)


def test_near_points_pair_names_the_first_word_block_never_compares(tmp_path: pathlib.Path) -> None:
    matches, clusters = run(tmp_path, [MNZ03_DEQ, MNZ03_NPDES])
    row = pair(matches, MNZ03_DEQ["record_id"], MNZ03_NPDES["record_id"])
    assert "B4_point_near" in row["rationale"] and row["point_score"] == 100.0
    assert together(clusters, MNZ03_DEQ["record_id"], MNZ03_NPDES["record_id"])


def test_nearness_never_makes_a_match_without_the_name(tmp_path: pathlib.Path) -> None:
    """Apple's Mesa construction permit sits at the very point of the air permit filed under its
    developer's name: one site, but nothing in the two names says so, and 56 of 205 Virginia DEQ air
    sites have another DEQ site within 300 m. Rule N still refuses the pair."""
    icis = load(
        ICIS,
        "AZMCA000000F003392",
        "PLATYPUS DEVELOPMENT",
        (33.34684, -111.60093),
        county="Maricopa",
        state="AZ",
        refs="icis_air:AZMCA000000F003392|frs:110062853416",
    )
    npdes = load(
        NPDES,
        "AZC116834",
        "APPLE DATA CENTER",
        (33.34684, -111.60093),
        county="Maricopa",
        state="AZ",
        refs="npdes:AZC116834|frs:110072222421",
    )
    matches, clusters = run(tmp_path, [icis, npdes])
    row = pair(matches, icis["record_id"], npdes["record_id"])
    assert row["point_score"] == 100.0 and "veto_name_floor" in row["veto"]
    assert not together(clusters, icis["record_id"], npdes["record_id"])


def test_near_points_outweigh_an_frs_only_conflict_and_far_ones_do_not(tmp_path: pathlib.Path) -> None:
    """Compark, Douglas County CO: the stormwater notice and the air permit of one campus cite
    different FRS ids 47 m apart. Moved 3 km off, the same pair keeps its conflict."""
    icis = load(
        ICIS,
        "CO0000000803500754",
        "FLEXENTIAL CORP - COMPARK DATA CENTER",
        (39.55, -104.85),
        county="Douglas",
        state="CO",
        refs="icis_air:CO0000000803500754|frs:110069566945",
    )
    near = load(
        NPDES,
        "COR03K655",
        "COMPARK DATA CENTER",
        (39.5503, -104.8504),
        county="Douglas",
        state="CO",
        refs="npdes:COR03K655|frs:110000000001",
    )
    matches, clusters = run(tmp_path, [icis, near])
    assert not bool(pair(matches, icis["record_id"], near["record_id"])["id_conflict"])
    assert together(clusters, icis["record_id"], near["record_id"])
    far = {**near, "raw": json.dumps({"Latitude": 39.577, "Longitude": -104.85})}
    matches, clusters = run(tmp_path, [icis, far])
    assert not together(clusters, icis["record_id"], far["record_id"])
    # an ICIS-Air id conflict is never outweighed: two air registrations are two facilities
    assert resolve.id_conflict("icis_air:A|frs:1", "icis_air:B|frs:1", 0.01)
    assert not resolve.id_conflict("frs:1", "frs:2", 0.01) and resolve.id_conflict("frs:1", "frs:2", 0.6)
    assert resolve.id_conflict("frs:1", "frs:2", None) and resolve.id_conflict("frs:1", "frs:2", float("nan"))


def test_generation_records_carry_no_point_evidence(tmp_path: pathlib.Path) -> None:
    """EIA-860M's raw payload carries Latitude/Longitude too, but a plant spans kilometres and its
    queue requests state no point: rule L is for loads only."""
    eia = {
        **load(EIA, "70001-PV1", "Sunflower Solar", (36.0, -119.0), county="Kern", state="CA"),
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": 100.0,
        "eia_plant_id": "70001",
        "eia_generator_id": "PV1",
    }
    caiso = {
        **load(CAISO, "1999", "SUNFLOWER SOLAR", (36.3, -119.0), county="Kern", state="CA"),
        "kind": "generation",
        "technology": "solar",
        "capacity_mw": 100.0,
        "queue_id": "1999",
        "iso": "CAISO",
    }
    points = resolve.point_columns(pd.DataFrame([eia, caiso]))
    assert points["point_lat"].isna().all()
    matches, clusters = run(tmp_path, [eia, caiso])
    row = pair(matches, eia["record_id"], caiso["record_id"])
    assert pd.isna(row["point_score"]) and row["accepted"]
    assert together(clusters, eia["record_id"], caiso["record_id"])


def test_clusters_do_not_depend_on_row_order(tmp_path: pathlib.Path) -> None:
    rows = [GA1, GA2, CRANE, NVA14_DEQ, NVA14_ICIS, NVA11_FAA, MNZ03_DEQ, MNZ03_NPDES]

    def groups(frame_rows: list[dict[str, object]]) -> set[frozenset[str]]:
        _, clusters = run(tmp_path, frame_rows)
        return {frozenset(g["record_id"]) for _, g in clusters.groupby("cluster_id")}

    assert groups(rows) == groups(list(reversed(rows)))


def test_point_helpers() -> None:
    assert resolve.point_score(0.0) == 100.0 and resolve.point_score(0.5) == 100.0
    assert resolve.point_score(1.25) == pytest.approx(50.0) and resolve.point_score(2.0) == 0.0
    assert resolve.point_score(5.0) == 0.0
    assert resolve.raw_point('{"Latitude": 38.78, "Longitude": -77.57}') == (38.78, -77.57)
    assert resolve.raw_point({"Latitude": "38.78", "Longitude": "-77.57"}) == (38.78, -77.57)
    for bad in (
        '{"Latitude": 0, "Longitude": 0}',
        '{"Latitude": 91, "Longitude": -77}',
        '{"Latitude": null, "Longitude": -77}',
        '{"Latitude": "NaN", "Longitude": -77}',
        '{"Longitude": -77}',
        "not json",
        None,
        float("nan"),
    ):
        assert resolve.raw_point(bad) is None, bad
    assert resolve.km_between((38.780539, -77.56973), (38.7822, -77.5663)) == pytest.approx(0.35, abs=0.01)
    frame = pd.DataFrame([MNZ03_DEQ, {**MNZ03_NPDES, "kind": "other"}])
    cols = resolve.point_columns(frame)
    assert cols.loc[0, "point_lat"] == 38.780539 and pd.isna(cols.loc[1, "point_lat"])
    assert resolve.point_columns(frame.drop(columns=["raw"]))["point_lat"].isna().all()
