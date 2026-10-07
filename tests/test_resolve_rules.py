"""pipeline/resolve.py: the fuzzy vetoes and the EIA rollup of docs/22 §22 (lane H5).

Each test builds a minimal normalised frame that reproduces one wrong merge the dev store made on
2026-09-29 (names, MW, counties and technologies copied from the real records) and, next to it,
the legitimate pattern the same rule must keep merging. The frames go through `run()` itself, so
blocking, scoring, the vetoes and union-find are all exercised.
"""

from __future__ import annotations

import pathlib

import pandas as pd

from pipeline import resolve
from pipeline.normalize import norm_county, norm_name, norm_org

EIA = "us.eia.860m"
CAISO = "us.iso.caiso.gen_queue"
ERCOT = "us.iso.ercot.gen_queue"
NYISO = "us.iso.nyiso.gen_queue"
ICIS = "us.epa.echo.icis_air"


def rec(
    source: str,
    rid: str,
    name: str,
    technology: str | None,
    mw: float | None,
    *,
    county: str,
    state: str,
    sponsor: str | None = None,
    plant: str | None = None,
    lifecycle: str = "filed",
) -> dict[str, object]:
    return {
        "record_id": f"{source}:{rid}",
        "source_id": source,
        "source_record_id": rid,
        "source_url": f"https://example.org/{rid}",
        "retrieved_at": "2026-09-29T00:00:00Z",
        "licence": "test",
        "kind": "generation",
        "name_canonical": name,
        "name_norm": norm_name(name),
        "sponsor_name": sponsor,
        "sponsor_norm": norm_org(sponsor) if sponsor else None,
        "technology": technology,
        "capacity_mw": mw,
        "iso": None if source == EIA else source.split(".")[2].upper(),
        "state": state,
        "county": county,
        "county_norm": norm_county(county),
        "lifecycle_state": lifecycle,
        "status_raw": "x",
        "proposed_cod": pd.NaT,
        "queue_id": None if source == EIA else rid,
        "eia_plant_id": plant,
        "eia_generator_id": rid.split("-", 1)[1] if plant else None,
        "cross_refs": "",
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
    assert len(hit), f"{a} / {b} was never a candidate"
    return hit.sort_values("score", ascending=False).iloc[0]


# ------------------------------------------------------------------ rule T: technology class
def test_tech_compatible_table() -> None:
    assert not resolve.tech_compatible("load", "solar")
    assert not resolve.tech_compatible("transmission", "storage")
    assert not resolve.tech_compatible("solar", "wind")
    assert not resolve.tech_compatible("storage", "gas_steam")
    assert not resolve.tech_compatible("biomass", "storage")
    assert resolve.tech_compatible("load", "load")
    assert resolve.tech_compatible("solar", "storage")
    assert resolve.tech_compatible("solar_storage", "storage")
    assert resolve.tech_compatible("wind", "storage")
    assert resolve.tech_compatible("gas_ct", "gas_cc")
    assert resolve.tech_compatible("wind_offshore", "wind")
    assert resolve.tech_compatible("unknown", "load")
    assert resolve.tech_compatible(None, "solar")


def test_data_centre_never_merges_with_a_solar_plant(tmp_path: pathlib.Path) -> None:
    rows = [
        rec(
            ICIS,
            "IL000031096ARO",
            "FRANKLIN PARK (CHI22) DATA CENTER",
            "load",
            None,
            county="Cook",
            state="IL",
        ),
        rec(
            EIA,
            "68135-CH802",
            "Franklin Park 2-SLCHI802",
            "solar",
            1.2,
            county="Cook",
            state="IL",
            plant="68135",
        ),
    ]
    matches, clusters = run(tmp_path, rows)
    hit = pair(matches, f"{ICIS}:IL000031096ARO", f"{EIA}:68135-CH802")
    assert "veto_tech_class" in hit["veto"]
    assert not hit["accepted"]
    assert not together(clusters, f"{ICIS}:IL000031096ARO", f"{EIA}:68135-CH802")


def test_hybrid_requests_still_merge_with_their_plant(tmp_path: pathlib.Path) -> None:
    # ERCOT files a hybrid's solar and storage as two requests; EIA lists two generators.
    sponsor = "Harryoung Solar LLC"
    rows = [
        rec(
            ERCOT, "25INR0270", "Harryoung Solar", "solar", 190.01, county="Hale", state="TX", sponsor=sponsor
        ),
        rec(
            ERCOT, "25INR0552", "HarryoungBESS", "storage", 190.01, county="Hale", state="TX", sponsor=sponsor
        ),
        rec(
            EIA,
            "67777-GRS4P",
            "Harryoung PV and BESS",
            "solar",
            190.0,
            county="Hale",
            state="TX",
            sponsor=sponsor,
            plant="67777",
        ),
        rec(
            EIA,
            "67777-GRS4B",
            "Harryoung PV and BESS",
            "storage",
            190.0,
            county="Hale",
            state="TX",
            sponsor=sponsor,
            plant="67777",
        ),
    ]
    _, clusters = run(tmp_path, rows)
    for rid in ("25INR0270", "25INR0552"):
        assert together(clusters, f"{ERCOT}:{rid}", f"{EIA}:67777-GRS4P")
    assert together(clusters, f"{EIA}:67777-GRS4P", f"{EIA}:67777-GRS4B")


# ------------------------------------------------------------------ rule N: name floor without sponsor
def test_county_and_capacity_alone_do_not_merge_two_named_projects(tmp_path: pathlib.Path) -> None:
    rows = [
        rec(CAISO, "1761", "GRACE ENERGY CENTER", "solar_storage", 500.0, county="KERN", state="CA"),
        rec(
            CAISO,
            "294",
            "DRACKER SOLAR",
            "solar_storage",
            485.0,
            county="KERN",
            state="CA",
            lifecycle="built",
        ),
        rec(
            EIA,
            "69820-GRA1",
            "Grace Energy Center",
            "solar",
            500.0,
            county="Kern",
            state="CA",
            sponsor="Grace Solar LLC",
            plant="69820",
        ),
        rec(
            EIA,
            "69820-GRA2",
            "Grace Energy Center",
            "storage",
            500.0,
            county="Kern",
            state="CA",
            sponsor="Grace Solar LLC",
            plant="69820",
        ),
    ]
    matches, clusters = run(tmp_path, rows)
    hit = pair(matches, f"{CAISO}:294", f"{EIA}:69820-GRA1")
    assert hit["score"] >= 75  # the pair the dev store merged at 76.7
    assert "veto_name_floor" in hit["veto"]
    assert not together(clusters, f"{CAISO}:294", f"{EIA}:69820-GRA1")
    assert together(clusters, f"{CAISO}:1761", f"{EIA}:69820-GRA1")


# ------------------------------------------------------------------ rule C: capacity factor
def test_capacity_veto_compares_a_request_with_the_whole_plant() -> None:
    plant = {"source_id": EIA, "eia_plant_id": "70242", "capacity_mw": 4.9, "plant_mw": 8.9}
    assert resolve.capacity_veto({"source_id": NYISO, "capacity_mw": 75.0}, plant)  # Riverhead Storage
    assert not resolve.capacity_veto({"source_id": NYISO, "capacity_mw": 20.0}, plant)  # 2.2x: left to rule K
    # Darden: one 1,150 MW CAISO request over a 646 MW EIA plant (one of four) is a complex, not a veto.
    darden = {"source_id": EIA, "eia_plant_id": "69661", "capacity_mw": 342.7, "plant_mw": 646.4}
    assert not resolve.capacity_veto({"source_id": CAISO, "capacity_mw": 1150.0}, darden)
    # A request much smaller than the plant is a phase or an add-on, not a veto.
    assert not resolve.capacity_veto(
        {"source_id": NYISO, "capacity_mw": 5.0}, {**darden, "capacity_mw": 119.0}
    )


def test_request_far_larger_than_the_plant_is_refused(tmp_path: pathlib.Path) -> None:
    rows = [
        rec(
            NYISO,
            "0762",
            "Riverhead Storage Energy Center",
            "storage",
            75.0,
            county="Suffolk",
            state="NY",
            sponsor="Invenergy Storage Development LLC",
        ),
        rec(
            EIA,
            "70242-215",
            "Riverhead - CVE",
            "solar",
            4.9,
            county="Suffolk",
            state="NY",
            sponsor="CVE US NY Riverhead 215 LLC",
            plant="70242",
        ),
        rec(
            EIA,
            "70242-216",
            "Riverhead - CVE",
            "storage",
            4.0,
            county="Suffolk",
            state="NY",
            sponsor="CVE US NY Riverhead 215 LLC",
            plant="70242",
        ),
    ]
    matches, clusters = run(tmp_path, rows)
    hit = pair(matches, f"{NYISO}:0762", f"{EIA}:70242-216")
    assert "veto_capacity" in hit["veto"]
    assert not together(clusters, f"{NYISO}:0762", f"{EIA}:70242-216")


def test_one_request_over_many_generators_still_merges(tmp_path: pathlib.Path) -> None:
    # Rock Island: one 121.8 MW ERCOT request, six 20.3 MW EIA units of one plant.
    sponsor = "Rock Island Generation LLC"
    rows = [
        rec(
            ERCOT,
            "27INR0321",
            "Rock Island Generating (TEF -Due Diligence)",
            "gas_ice",
            121.8,
            county="Colorado",
            state="TX",
            sponsor=sponsor,
        ),
    ] + [
        rec(
            EIA,
            f"69760-RIG{i}",
            "Rock Island Generation Project",
            "gas_ice",
            20.3,
            county="Colorado",
            state="TX",
            sponsor=sponsor,
            plant="69760",
        )
        for i in range(1, 7)
    ]
    _, clusters = run(tmp_path, rows)
    for i in range(1, 7):
        assert together(clusters, f"{ERCOT}:27INR0321", f"{EIA}:69760-RIG{i}")


# ------------------------------------------------------------------ rule Q: phase surplus
def test_second_phase_does_not_join_a_plant_its_first_phase_covers(tmp_path: pathlib.Path) -> None:
    # Bonanza: two 300 MW CAISO requests, one 300 MW EIA plant (plus its 195 MW storage).
    rows = [
        rec(CAISO, "1649", "BONANZA SOLAR", "solar_storage", 300.0, county="CLARK", state="NV"),
        rec(
            CAISO,
            "1797",
            "BONANZA SOLAR 2",
            "solar_storage",
            300.0,
            county="CLARK",
            state="NV",
            lifecycle="withdrawn",
        ),
        rec(
            EIA,
            "66908-BZPV",
            "Bonanza Solar and Storage Project",
            "solar",
            300.0,
            county="Clark",
            state="NV",
            sponsor="EDF Renewables Development, Inc.",
            plant="66908",
        ),
        rec(
            EIA,
            "66908-BZES",
            "Bonanza Solar and Storage Project",
            "storage",
            195.0,
            county="Clark",
            state="NV",
            sponsor="EDF Renewables Development, Inc.",
            plant="66908",
        ),
    ]
    matches, clusters = run(tmp_path, rows)
    assert pair(matches, f"{CAISO}:1797", f"{EIA}:66908-BZPV")["veto"] == "veto_phase_surplus"
    assert together(clusters, f"{CAISO}:1649", f"{EIA}:66908-BZPV")
    assert not together(clusters, f"{CAISO}:1797", f"{EIA}:66908-BZPV")


def test_unnumbered_request_does_not_join_a_numbered_plant_its_twin_covers(tmp_path: pathlib.Path) -> None:
    # Bellefield: 'BELLEFIELD SOLAR FARM' beside 'BELLEFIELD 2 SOLAR FARM', EIA plant 'Bellefield 2'.
    eia = {"county": "Kern", "state": "CA", "sponsor": "26SB 8me LLC", "plant": "64209"}
    rows = [
        rec(CAISO, "1510", "BELLEFIELD SOLAR FARM", "solar_storage", 500.0, county="KERN", state="CA"),
        rec(CAISO, "1631", "BELLEFIELD 2 SOLAR FARM", "solar_storage", 500.0, county="KERN", state="CA"),
        rec(EIA, "64209-26SBA", "Bellefield 2 Solar & Energy Storage Farm", "solar", 500.0, **eia),
        rec(EIA, "64209-26SBB", "Bellefield 2 Solar & Energy Storage Farm", "storage", 500.0, **eia),
    ]
    _, clusters = run(tmp_path, rows)
    assert together(clusters, f"{CAISO}:1631", f"{EIA}:64209-26SBA")
    assert not together(clusters, f"{CAISO}:1510", f"{EIA}:64209-26SBA")


def test_phases_that_add_up_to_the_plant_still_merge(tmp_path: pathlib.Path) -> None:
    sponsor = "Enel Green Power Roseland Solar, LLC"
    rows = [
        rec(
            ERCOT, "20INR0205", "Roseland Solar", "solar", 254.0, county="Falls", state="TX", sponsor=sponsor
        ),
        rec(
            ERCOT,
            "22INR0506",
            "Roseland Solar II",
            "solar",
            254.0,
            county="Falls",
            state="TX",
            sponsor=sponsor,
        ),
        rec(
            EIA,
            "65028-ROSES",
            "Roseland Solar Project, LLC",
            "solar",
            500.0,
            county="Falls",
            state="TX",
            sponsor=sponsor,
            plant="65028",
        ),
        # Vast Sands Power I + II against the plant's two 440 MW turbines
        rec(
            ERCOT,
            "28INR0105",
            "Vast Sands Power I (TEF -Due Diligence)",
            "gas_ct",
            440.0,
            county="Ward",
            state="TX",
            sponsor="Permian Power I LLC",
        ),
        rec(
            ERCOT,
            "28INR0109",
            "Vast Sands Power II (TEF-Due Diligence)",
            "gas_ct",
            440.0,
            county="Ward",
            state="TX",
            sponsor="Permian Power I LLC",
        ),
        rec(
            EIA,
            "69689-CT11",
            "Vast Sands Power",
            "gas_ct",
            440.0,
            county="Ward",
            state="TX",
            sponsor="Permian Power I LLC",
            plant="69689",
        ),
        rec(
            EIA,
            "69689-CT12",
            "Vast Sands Power",
            "gas_ct",
            440.0,
            county="Ward",
            state="TX",
            sponsor="Permian Power I LLC",
            plant="69689",
        ),
    ]
    _, clusters = run(tmp_path, rows)
    assert together(clusters, f"{ERCOT}:20INR0205", f"{EIA}:65028-ROSES")
    assert together(clusters, f"{ERCOT}:22INR0506", f"{EIA}:65028-ROSES")
    assert together(clusters, f"{ERCOT}:28INR0105", f"{EIA}:69689-CT11")
    assert together(clusters, f"{ERCOT}:28INR0109", f"{EIA}:69689-CT12")


def test_phase_coverage_is_counted_within_the_technology(tmp_path: pathlib.Path) -> None:
    # Indigo: the solar phase-1 request covers the plant's 150 MW solar, not its 180 MW storage, so
    # 'Indigo Storage 2' may join while 'Indigo solar 2' and the 800 MW 'Indigo Solar 3' may not.
    sponsor = "Innovative Solar 245, LLC"
    eia = {"county": "Fisher", "state": "TX", "sponsor": sponsor, "plant": "66891"}
    rows = [
        rec(ERCOT, "21INR0031", "Indigo Solar", "solar", 150.0, county="Fisher", state="TX", sponsor=sponsor),
        rec(
            ERCOT,
            "24INR0496",
            "Indigo Storage",
            "storage",
            60.0,
            county="Fisher",
            state="TX",
            sponsor=sponsor,
        ),
        rec(
            ERCOT,
            "25INR0528",
            "Indigo Storage 2",
            "storage",
            60.0,
            county="Fisher",
            state="TX",
            sponsor=sponsor,
        ),
        rec(
            ERCOT,
            "25INR0547",
            "Indigo solar 2",
            "solar",
            180.0,
            county="Fisher",
            state="TX",
            sponsor="Indigo Solar 2",
        ),
        rec(
            ERCOT,
            "28INR0067",
            "Indigo Solar 3",
            "solar",
            800.0,
            county="Fisher",
            state="TX",
            sponsor="Indigo solar 3",
        ),
        rec(EIA, "66891-IS245", "Indigo Solar & Storage", "solar", 150.0, **eia),
        rec(EIA, "66891-245BS", "Indigo Solar & Storage", "storage", 180.0, **eia),
    ]
    _, clusters = run(tmp_path, rows)
    assert together(clusters, f"{ERCOT}:21INR0031", f"{EIA}:66891-IS245")
    assert together(clusters, f"{ERCOT}:25INR0528", f"{EIA}:66891-245BS")
    assert not together(clusters, f"{ERCOT}:25INR0547", f"{EIA}:66891-245BS")
    assert not together(clusters, f"{ERCOT}:28INR0067", f"{EIA}:66891-IS245")


def test_phase_that_completes_the_plant_is_kept(tmp_path: pathlib.Path) -> None:
    # Sunrise Wind (Q-v2): NYISO lists 880 MW and a 44 MW 'Sunrise Wind II'; EIA lists the 924 MW
    # plant. The unnumbered request covers 95 % of the plant, but 880 + 44 = 924, so II is the rest
    # of the same plant and stays. Agricola Wind 2 does not complete its plant (97 + 79.3 vs 99).
    rows = [
        rec(NYISO, "0766", "Sunrise Wind", "unknown", 880.0, county="Suffolk", state="NY"),
        rec(NYISO, "0987", "Sunrise Wind II", "unknown", 44.0, county="Suffolk", state="NY"),
        rec(
            EIA,
            "67435-SRWND",
            "Sunrise Wind",
            "wind_offshore",
            924.0,
            county="Suffolk",
            state="NY",
            sponsor="Orsted Wind Power North America LLC",
            plant="67435",
        ),
        rec(NYISO, "1148", "Agricola Wind", "wind", 97.0, county="Cayuga", state="NY"),
        rec(
            NYISO,
            "1583",
            "Agricola Wind 2",
            "wind",
            79.3,
            county="Cayuga",
            state="NY",
            sponsor="Liberty Renewables",
        ),
        rec(
            EIA,
            "68228-Q1448",
            "Agricola Wind",
            "wind",
            99.0,
            county="Cayuga",
            state="NY",
            sponsor="Liberty Renewables Incorporated",
            plant="68228",
        ),
    ]
    matches, clusters = run(tmp_path, rows)
    assert pair(matches, f"{NYISO}:0987", f"{EIA}:67435-SRWND")["veto"] == ""
    assert together(clusters, f"{NYISO}:0987", f"{EIA}:67435-SRWND")
    assert together(clusters, f"{NYISO}:0766", f"{EIA}:67435-SRWND")
    assert pair(matches, f"{NYISO}:1583", f"{EIA}:68228-Q1448")["veto"] == "veto_phase_surplus"
    assert not together(clusters, f"{NYISO}:1583", f"{EIA}:68228-Q1448")
    assert together(clusters, f"{NYISO}:1148", f"{EIA}:68228-Q1448")


def test_phase_one_reads_as_unnumbered() -> None:
    assert resolve.phase_key("Moonlight Flats Solar Power 1") == resolve.phase_key("Moonlight Flats Solar")
    assert resolve.phase_key("Vast Sands Power I") == frozenset()
    assert resolve.phase_key("Roseland Solar II") == frozenset({2})


# ------------------------------------------------------------------ rule E: rollup on registry ids
def test_plant_rollup_fires_on_the_registry_source_id() -> None:
    rows = [
        rec(
            EIA,
            f"68461-KEYB{x}",
            "Key Energy Storage",
            "storage",
            mw,
            county="Fresno",
            state="CA",
            plant="68461",
        )
        for x, mw in (("A", 75.0), ("B", 190.0), ("C", 35.0))
    ]
    extra = resolve.eia_plant_rollup(pd.DataFrame(rows))
    assert list(extra["record_id"]) == [f"{EIA}:plant-68461:storage"]
    assert float(extra["capacity_mw"].iloc[0]) == 300.0


def test_request_matching_only_the_plant_total_joins_via_the_rollup(tmp_path: pathlib.Path) -> None:
    rows = [
        rec(CAISO, "1479", "KEY STORAGE 1", "storage", 300.1, county="FRESNO", state="CA"),
    ] + [
        rec(
            EIA,
            f"68461-KEYB{x}",
            "Key Energy Storage",
            "storage",
            mw,
            county="Fresno",
            state="CA",
            plant="68461",
        )
        for x, mw in (("A", 75.0), ("B", 190.0), ("C", 35.0))
    ]
    matches, clusters = run(tmp_path, rows)
    rollup = f"{EIA}:plant-68461:storage"
    assert bool(pair(matches, f"{CAISO}:1479", rollup)["accepted"])
    assert together(clusters, f"{CAISO}:1479", rollup)


def test_deterministic_pairs_are_never_vetoed(tmp_path: pathlib.Path) -> None:
    # Two sources citing one facility-registry id stay a D3 match even when a veto would fire on
    # the fuzzy side (here: no sponsor and a weak name).
    a = rec(
        ICIS, "VA0000005110774181", "AMAZON DATA SERVICES, INC.", "load", None, county="Loudoun", state="VA"
    )
    b = rec(
        "us.va.deq.data_center_air_sites",
        "74181",
        "Ashburn campus",
        "load",
        None,
        county="Loudoun County",
        state="VA",
    )
    a["cross_refs"] = b["cross_refs"] = "icis_air:VA0000005110774181"
    matches, clusters = run(tmp_path, [a, b])
    det = matches[matches["pass"].str.startswith("D")]
    assert len(det) == 1 and bool(det["accepted"].iloc[0])
    assert together(clusters, str(a["record_id"]), str(b["record_id"]))


# ------------------------------------------------------------------ rule T over kinds (2026-10-07)
CLASS_VI = "us.epa.class_vi"


def class_vi(rid: str, project: str, company: str, *, county: str) -> dict[str, object]:
    """A Class VI tracker row as `us.epa.class_vi` normalises it: kind `ccs`, its own technology
    token, no capacity, the permit number as queue id."""
    row = rec(
        CLASS_VI, rid, project, "co2_geologic_sequestration", None, county=county, state="CA", sponsor=company
    )
    row.update(kind="ccs", iso=None, lifecycle_state="studied")
    return row


def caiso(
    rid: str, name: str, technology: str, mw: float, *, county: str, lifecycle: str
) -> dict[str, object]:
    return rec(CAISO, rid, name, technology, mw, county=county, state="CA", lifecycle=lifecycle)


#: The three Class VI projects the resolver merged into CAISO generation records on the 2026-10-07
#: store copy (confidence 0.81-0.91), each with the CAISO request(s) it was merged into. Names,
#: companies, counties, technologies, MW and states as the real rows carry them.
WRONG_CCS_MERGES = [
    (
        class_vi(
            "h2656a779ab3b",
            "Tulare County Carbon Storage Project",
            "Tulare County Carbon Storage Project LLC",
            county="Tulare",
        ),
        [caiso("857", "TULARE SOLAR", "solar", 150.0, county="TULARE", lifecycle="withdrawn")],
    ),
    (
        class_vi(
            "R09-CA-0014",
            "Sutter Decarbonization Project",
            "Calpine California CCUS Holdings",
            county="Sutter",
        ),
        [caiso("379", "SUTTER ENERGY CENTER", "gas_cc", 600.0, county="SUTTER", lifecycle="withdrawn")],
    ),
    (
        class_vi(
            "R09-CA-0013",
            "Montezuma Carbon LLC",
            "Montezuma NorCal Carbon Sequestration Hub",
            county="Solano",
        ),
        [
            caiso(
                "22",
                "MONTEZUMA (HIGH WINDS III)",
                "wind_storage",
                38.0,
                county="SOLANO",
                lifecycle="contracted",
            ),
            caiso("222", "MONTEZUMA II", "wind_storage", 78.0, county="SOLANO", lifecycle="contracted"),
            caiso("489", "MONTEZUMA II EXPANSION", "wind", 98.9, county="SOLANO", lifecycle="withdrawn"),
        ],
    ),
]


def test_kind_compatible_table() -> None:
    assert not resolve.kind_compatible("ccs", "generation")
    assert not resolve.kind_compatible("ccs", "storage")
    assert not resolve.kind_compatible("load", "generation")
    assert not resolve.kind_compatible("pipeline", "lng")
    assert not resolve.kind_compatible("hydrogen", "generation")
    assert resolve.kind_compatible("generation", "storage")  # hybrids file their halves separately
    assert resolve.kind_compatible("nuclear", "generation")
    assert resolve.kind_compatible("ccs", "ccs")
    assert resolve.kind_compatible("other", "ccs")
    assert resolve.kind_compatible(None, "ccs")
    # Every kind of the proposal vocabulary has a class, except the wildcard `other`.
    vocabulary = {
        "generation",
        "storage",
        "load",
        "transmission",
        "pipeline",
        "lng",
        "nuclear",
        "ccs",
        "hydrogen",
    }
    assert set(resolve.KIND_CLASSES) == vocabulary


def test_a_ccs_project_never_merges_with_the_power_plant_it_is_named_after(tmp_path: pathlib.Path) -> None:
    rows = [r for well, plants in WRONG_CCS_MERGES for r in (well, *plants)]
    matches, clusters = run(tmp_path, rows)
    for well, plants in WRONG_CCS_MERGES:
        for plant in plants:
            a, b = str(well["record_id"]), str(plant["record_id"])
            hit = pair(matches, a, b)
            assert "veto_tech_class" in str(hit["veto"]), hit["rationale"]
            assert not bool(hit["accepted"])
            assert not together(clusters, a, b)
    # The Montezuma requests were joined to each other only through the Class VI record.
    montezuma = [str(p["record_id"]) for p in WRONG_CCS_MERGES[2][1]]
    assert not together(clusters, montezuma[0], montezuma[1])
    assert not together(clusters, montezuma[1], montezuma[2])


def test_kind_classes_do_not_join_through_a_record_of_kind_other(tmp_path: pathlib.Path) -> None:
    """A record whose kind is `other` is compatible with both sides pairwise, so the pairwise
    check alone would let it chain a CCS project into a power plant."""
    well = class_vi("R09-CA-0014", "Sutter Decarbonization Project", "Calpine", county="Sutter")
    plant = rec(
        EIA,
        "55112-CTG1",
        "Sutter Energy Center",
        "gas_cc",
        600.0,
        county="Sutter",
        state="CA",
        sponsor="Calpine",
        plant="55112",
    )
    # CAISO files 11 requests as kind `other` (technology `other`); one such row is the bridge.
    bridge = rec(
        CAISO, "1999", "SUTTER ENERGY CENTER", "other", None, county="SUTTER", state="CA", sponsor="Calpine"
    )
    bridge.update(kind="other")
    matches, clusters = run(tmp_path, [well, plant, bridge])
    w, p, b = (str(r["record_id"]) for r in (well, plant, bridge))
    assert not together(clusters, w, p)
    assert together(clusters, p, b)  # the stronger edge wins: the bridge is the plant, by name
    refused = matches[matches["rationale"].str.contains("veto_kind_chain")]
    assert len(refused) == 1 and not bool(refused["accepted"].iloc[0])


def test_kind_chain_check_orders_by_score_and_spares_deterministic_pairs() -> None:
    df = pd.DataFrame({"kind": ["ccs", "other", "generation", "load", "load"]})
    matches = pd.DataFrame(
        {
            "li": [0, 1, 3],
            "ri": [1, 2, 2],
            "pass": ["F_fuzzy:B2", "F_fuzzy:B2", "D3_xref"],
            "score": [80.0, 95.0, 100.0],
            "accepted": [True, True, True],
        }
    )
    refused = resolve.kind_chain_veto(df, matches)
    # D3 (load-generation) is taken first and never refused; then 1-2 (95) joins the other record
    # to that cluster; then 0-1 (80) would bring a ccs record into it and is refused.
    assert refused.tolist() == [True, False, False]
