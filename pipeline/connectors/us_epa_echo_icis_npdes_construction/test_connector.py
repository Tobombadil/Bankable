"""Parser and fetch tests for us.epa.echo.icis_npdes_construction (EPA ICIS-NPDES construction stormwater
coverage of data centres).

Fixture `fixtures/npdes_construction_data_centers.json` is the connector's own snapshot of 2026-10-24
(run 1c039fec, sha256 018e07e0…1bda: the 439 pre-filter candidates of the ECHO file of 2026-10-18, their
current permit versions, 59 master general permits with every date their versions carry, 388 component
rows, the construction-coverage filings of each state by month and the file's own date) trimmed to 69
facilities, their 69 permits, their 21 masters, 66 component rows and the filing counts of their 14
states: every construction basis, every exclusion (a name the rule rejects, five kinds of
non-construction coverage, three cryptocurrency mines), every permit status, every placement outcome,
each way permits join into a site and the ways they are kept apart, infrastructure-only sites, held
duplicates, and each lifecycle rule (a lapsed coverage, a termination on the expiry date, a state that
stopped reporting, a handover, a termination years after an older coverage). No value is edited. The
snapshot never held the permittee (`PERMIT_NAME`): the tests that prove it is dropped put a placeholder
column in memory. No network: the fetch tests run the connector against `FakeEcho`, a zip built from
the fixture's own rows.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import pathlib
import re
import tempfile
import zipfile
import zlib
from email.utils import formatdate
from typing import Any

import pandas as pd
import pytest

from conftest import connector_for
from pipeline import resolve
from pipeline.connectors.base import ParseError, RawSnapshot
from pipeline.connectors.canonical import harmonise_status, norm_name
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.connectors.us_epa_echo_icis_air import connector as air
from pipeline.connectors.us_epa_echo_icis_npdes_construction import connector as npdes
from pipeline.connectors.us_epa_echo_icis_npdes_construction.connector import (
    COMPONENTS_MEMBER,
    DFR_URL,
    FACILITIES_MEMBER,
    FETCH_VERSION,
    HELD_PATH,
    HELD_REASONS,
    PERMITS_MEMBER,
    ZIP_URL,
    Evidence,
    MemberStream,
    Permit,
    code_families,
    construction_basis,
    county_from_code,
    held_permits,
    is_candidate,
    is_construction_title,
    is_crypto_mine,
    is_infrastructure,
    name_key,
    placement_for,
    select_basis,
    site_decision,
    site_link,
    site_tokens,
)
from services.resolve import merge

SOURCE_ID = "us.epa.echo.icis_npdes_construction"
FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "npdes_construction_data_centers.json"
RETRIEVED = dt.datetime(2026, 10, 24, 1, 31, 2, tzinfo=dt.UTC)
PLACEHOLDER = "[permittee removed from fixture]"
#: Still held after rule L released 12 entries (2026-10-24, docs/22 §28): CAZ478830, ILR10ZC4M, VAR10Q014 and
#: VAR10S340 of the fixture's six now merge with their held records and are emitted.
HELD_IN_FIXTURE = ["VAR10Q048", "VAR10Q910"]


def _snapshot(content: bytes | None = None, retrieved_at: dt.datetime = RETRIEVED) -> RawSnapshot:
    return RawSnapshot(
        content=content if content is not None else FIXTURE.read_bytes(),
        content_type="application/json",
        url=ZIP_URL,
        retrieved_at=retrieved_at,
        http_status=200,
        ext="json",
    )


def _run(
    content: bytes | None = None, retrieved_at: dt.datetime = RETRIEVED
) -> tuple[Any, RawSnapshot, list[dict[str, Any]], pd.DataFrame]:
    c = connector_for(SOURCE_ID)
    raw = _snapshot(content, retrieved_at)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


@pytest.fixture()
def no_holds(monkeypatch: pytest.MonkeyPatch) -> None:
    """The site grouping without the reviewed hold list, so a held campus's grouping can be tested."""
    monkeypatch.setattr(npdes, "held_permits", lambda: {})


def _site(rows: list[dict[str, Any]], npdes_id: str) -> dict[str, Any]:
    """The site row that holds a permit."""
    return next(r for r in rows if npdes_id in {p["NPDES_ID"] for p in r["permits"]})


def _permit_row(rows: list[dict[str, Any]], npdes_id: str) -> dict[str, Any]:
    return next(p for p in _site(rows, npdes_id)["permits"] if p["NPDES_ID"] == npdes_id)


def _doc() -> dict[str, Any]:
    return dict(json.loads(FIXTURE.read_text(encoding="utf-8")))


# ------------------------------------------------------------------ selection
def test_selection_keeps_data_centre_construction_coverage_and_rejects_each_exclusion() -> None:
    _, raw, rows, df = _run()
    assert len(rows) == 31
    assert raw.meta["permits_selected"] == 59
    assert raw.meta["excluded"] == {"crypto_mining": 3, "name_not_selected": 2, "not_construction": 5}
    assert raw.meta["construction_basis"] == {"component": 43, "master_component": 11, "master_title": 5}
    names = {p["FACILITY_NAME"] for r in rows for p in r["permits"]}
    for rejected in (
        "DATA CHEM, INC.",  # the pre-filter's "DATA C", not a data centre
        "TECH DATA CORPORATION",
        "PNJ1 DATA CENTER",  # non-contact cooling water general permit
        "HOCKLEY DATA CENTER",  # an individual discharge permit, no component
        "REDI-MIX COMPASS DATA CENTER",  # a concrete batch plant general permit
        "CORESITE RESTON DATA CENTER",  # petroleum-contaminated sites general permit
        "GC DATA CENTER GRANBURY",  # industrial stormwater (its master carries SWI)
        "CIPHER MINING ODESSA DATA CENTER",  # a bitcoin mine that says so
        "ALBORZ DATA CENTER",  # Cipher's joint-venture mine near Happy, TX
        "KING MOUNTAIN DATA CENTER - NW SITE",  # Hut 8's joint-venture mine, Upton County
    ):
        assert rejected not in names
    assert "SERVERFARM LONGHORN" in names  # an operator name that is a data-centre word
    assert set(df["source_record_id"]) == {r["site_key"] for r in rows}


def test_name_rule() -> None:
    for name in (
        "Travelers Data Center",
        "QTS DATA CENTRE",
        "COMPASS DATACENTER - PHX II CAMPUS",
        "SPECIAL PROJ - SAT14 DATA CENT SHELL - BLDGS 1-3",  # abbreviated
        "25156403 DATA CTR-STAFFORD TECH CAMPUS PH4 SOUTHCA",  # abbreviated
        "ASH1 BRODERICK DC2 ASHBURN 1 TO BRODERICK DATA CEN",  # cut by the field length
        "INTRUST DISASTER RECOVERY DATA C",
        "WA13 VANTAGE DATA CENETER",  # the file's misspellings
        "TEMPLE GREEN DATA CETNER",
        "LPO DC CAMPUS",
        "NORTH TEXAS HYPERSCALE",
        "SERVERFARM ATL2",
        "SERVER FARM",
        "CPT-01 CHIRISA DATA HALL (AKA CHIRISA RICHMOND)",
    ):
        assert select_basis(name) == "name", name
        assert is_candidate({"FACILITY_NAME": name}), name
    for name in (
        "DATA CHEM, INC.",
        "DATA CENTRAL OFFICE",
        "QUADRANGLE TRACT 5B (DATA COURT STUDENT HOUSING)",
        "TECH DATA CORPORATION",
        "GENERAL DATACOMM, INC",
        "DC WATER CAMPUS",
        "SERVER ROOM RENOVATION",
    ):
        assert select_basis(name) is None, name
    # the fetch-side pre-filter is looser: it keeps what parse must judge
    assert is_candidate({"FACILITY_NAME": "DATA CHEM, INC."})
    assert not is_candidate({"FACILITY_NAME": "FUNERAL HOME"})


def test_cryptocurrency_mines_are_not_data_centres() -> None:
    """Six names declare mining; nine more are mines their operators' own filings name (connector
    `KNOWN_CRYPTO_SITES`, cited there). ICIS-Air excludes the same class."""
    for name in (
        "CRYPTOWATT MINING DATA CENTER",
        "CIPHER MINING ODESSA DATA CENTER",
        "CARTERVILLE - BLOCKCHAIN DATA CENTER",
        "RINGO - BLOCKCHAIN DATA CENTER",
        "DIGITAL ASSET DATA CENTER",
        "BITPOWER DATA CENTER",
        "BITCOIN DATA CENTER",
    ):
        assert is_crypto_mine(name, "MT"), name
    for name in (
        "KING MOUNTAIN DATA CENTER - NE SITE",
        "ARGO DATA CENTER",
        "ALBORZ DATA CENTER",
        "BEAR DATA CENTER",
        "CHIEF DATA CENTER",
    ):
        assert is_crypto_mine(name, "TX"), name
        assert not is_crypto_mine(name, "OR"), name  # the known mines are Texas sites, nowhere else
    for name in ("CORE SCIENTIFIC DATA CENTER EXPANSION", "BARBER LAKE DATA CENTER PROJECT", "DATA MINER"):
        assert not is_crypto_mine(name, "TX"), name
    _, raw, rows, _ = _run()
    assert raw.meta["excluded"]["crypto_mining"] == 3
    held = {p["NPDES_ID"] for r in rows for p in r["permits"]}
    assert not {"TXR1556HQ", "TXR1583FL", "TXR1592IJ"} & held


def test_construction_basis_and_master_titles() -> None:
    components = {
        "TXR15X": frozenset({"SWC"}),
        "VAG83X": frozenset({"POT"}),
        "TXR150000": frozenset({"SWC"}),
        "TXR050000": frozenset({"SWI"}),
    }
    titles = {
        "AZG200001": "CONSTRUCTION GENERAL PERMIT",
        "COG070000": "Master General Permit - Construction Dewatering",
    }
    assert construction_basis("TXR15X", "", components, titles) == "component"
    assert construction_basis("VAG83X", "TXR150000", components, titles) is None  # its own components rule
    assert construction_basis("TXR15Y", "TXR150000", components, titles) == "master_component"
    assert construction_basis("TXR05Y", "TXR050000", components, titles) is None
    assert construction_basis("AZC1", "AZG200001", components, titles) == "master_title"
    assert construction_basis("COG07X", "COG070000", components, titles) is None
    assert construction_basis("TX0131938", "", components, titles) is None
    # Real master titles from the 2026-10-18 file: the construction stormwater masters that carry no
    # SWC component, and the construction-word titles that are not construction stormwater.
    for title in (
        "CONSTRUCTION GENERAL PERMIT",
        "Construction Stormwater",
        "CONSTRUCTION MASTER GENERAL PERMIT",
        "Construction Site Stormwater General Permit",
        "Hawaii Master General Permit Storm Water Construction",
        "WISDOT SW CONSTRUCTION SITE PERMIT",
        "STORM WATER CONSTRUCTION SITE PERMIT",
    ):
        assert is_construction_title(title), title
    for title in (
        "Temporary Discharge - Construction Dewatering",
        "MULTI-SECTOR GENERAL PERMIT FOR STORMWATER DISCHARGES ASSOCIATED WITH INDUSTRIAL ACTIVITY (EXCLUDING CONSTRUCTION)",  # noqa: E501
        "GENERAL PERMIT FOR CONSTRUCTION DEWATERING AND HYDROSTATIC TESTING",
        "Vessel Deconstruction GP",
        "CA Non-Construction Stormwater",
        "DISCHARGES FROM NATURAL GAS UTILITY CONSTRUCTION, OPERATIONS AND MAINTENANCE ACTIVITIES",
        "Non-Contact Cooling Water (GP)",
        "",
    ):
        assert not is_construction_title(title), title
    # In the fixture, Arizona's 2020 permit (no component) is read from its title, its 2025 one from SWC.
    _, _, rows, _ = _run()
    mesa = {p["NPDES_ID"]: p["construction_basis"] for p in _site(rows, "AZC103911")["permits"]}
    assert mesa == {"AZC103911": "master_title", "AZC111271": "master_component"}


# ------------------------------------------------------------------ record model
def test_records_are_load_proposals_with_provenance() -> None:
    c, _, _, df = _run()
    assert list(df.columns) == c.columns
    assert set(df["kind"]) == {"load"} and set(df["technology"]) == {"load"}
    assert set(df["technology_raw"]) == {"Data Center"}
    assert df["capacity_mw"].isna().all() and df["sponsor_name"].isna().all()
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["licence_id"]) == {c.source.licence_id}
    assert set(df["retrieved_at"]) == {"2026-10-24T01:31:02Z"}
    assert df["record_id"].is_unique
    assert (df["record_id"] == SOURCE_ID + ":" + df["source_record_id"]).all()


def test_identity_url_and_cross_refs_cite_every_frs_id() -> None:
    _, _, _, df = _run()
    by_id = df.set_index("source_record_id")
    mesa = by_id.loc["AZC103911"]
    assert mesa["name_canonical"] == "NDA MESA DATA CENTER"
    raw = json.loads(mesa["raw"])
    assert mesa["source_url"] == DFR_URL.format(registry_id=raw["FACILITY_UIN"])
    assert mesa["cross_refs"] == "npdes:AZC103911|npdes:AZC111271|frs:110071727164"
    # every permit's FRS id, deduplicated: the site's FRS ids are facility-registry ids to the resolver
    lancaster = by_id.loc["TXR1540QW", "cross_refs"]
    assert lancaster == "npdes:TXR1540QW|npdes:TXR1505UK|frs:110071728553|frs:110071943842"
    assert resolve.shared_ids(lancaster) == {"frs": {"110071728553", "110071943842"}}
    # a different FRS id on an ICIS-Air record refuses the fuzzy pair; the same one is a D3 match
    assert resolve.shared_id_conflict(mesa["cross_refs"], "icis_air:AZ0000000401300001|frs:110000000001")
    assert not resolve.shared_id_conflict(mesa["cross_refs"], "icis_air:AZ0000000401300001|frs:110071727164")
    icis_like = {"kind": "load", "technology": "load"}
    assert resolve.class_compatible(df.iloc[0].to_dict(), icis_like)


def _clusters(frame: pd.DataFrame) -> list[list[str]]:
    with tempfile.TemporaryDirectory() as tmp:
        path = pathlib.Path(tmp) / "normalized.parquet"
        frame.to_parquet(path, index=False)
        _, clusters = resolve.run(merge.MERGE_SCORE_THRESHOLD, path)
    if not len(clusters):
        return []
    col = next(c for c in clusters.columns if "cluster" in c)
    return [sorted(g["record_id"]) for _, g in clusters.groupby(col)]


def test_frs_ids_keep_one_operators_other_campuses_apart() -> None:
    """The reviewer's Texas case (2026-10-24): Vantage TX-B and TX-A (11.2 km apart) fuzzy-matched
    both of ICIS-Air's San Antonio Vantage records, so loading them made two held campuses one
    record. With every permit's FRS id cited, the resolver refuses each pair whose FRS ids differ,
    unless the two exact points lie within 500 m (rule L, docs/22 §28): TX-B and ICIS-Air's TX2,
    316 m apart, are one campus. The ICIS-Air records carry their real placement: TX2 its exact
    point, TX 11 none (its FRS point is not site-specific)."""
    _, _, _, df = _run()
    ours = df[df["source_record_id"].isin(["TXR1520QD", "TXR1544QG"])]
    held = []
    for rid, name, frs, point in (
        ("TX0000004802900726", "VANTAGE TX 11", "110070000726", None),
        ("TX0000004802900727", "VANTAGE DATA CENTER TX2", "110070000727", (29.481991, -98.700221)),
    ):
        r = ours.iloc[0].copy()
        r["source_id"], r["source_record_id"] = "us.epa.echo.icis_air", rid
        r["record_id"] = f"us.epa.echo.icis_air:{rid}"
        r["name_canonical"], r["name_norm"] = name, norm_name(name)
        r["cross_refs"], r["lifecycle_state"] = f"icis_air:{rid}|frs:{frs}", "built"
        r["raw"] = json.dumps({"Latitude": point[0], "Longitude": point[1]} if point else {})
        held.append(r)
    frame = pd.concat([ours, pd.DataFrame(held)], ignore_index=True)
    tx2, tx_b = "us.epa.echo.icis_air:TX0000004802900727", f"{SOURCE_ID}:TXR1520QD"
    assert _clusters(frame) == [[tx2, tx_b]]
    # the same rows citing no FRS id (the previous cross_refs) chained all four into one cluster;
    # the location veto now keeps TX-A, 11 km from TX-B and TX2, out of theirs
    bare = frame.copy()
    bare["cross_refs"] = [
        refs if sid != SOURCE_ID else "|".join(x for x in refs.split("|") if x.startswith("npdes:"))
        for refs, sid in zip(bare["cross_refs"], bare["source_id"], strict=True)
    ]
    tx_a = f"{SOURCE_ID}:TXR1544QG"
    assert all(not ({tx_a, tx_b} <= set(c) or {tx_a, tx2} <= set(c)) for c in _clusters(bare))


def test_the_site_key_is_the_first_coverage_of_the_data_centre_itself() -> None:
    _, _, rows, _ = _run()
    river = _site(rows, "LAR10Q310")
    # Four NOIs for one campus, none with a coordinate: two filed by the developer's contractors, the
    # substation filed by the developer and a station filed by Entergy (a utility's permit is works).
    # The works come after the data centre's own permits, so neither is the key.
    assert river["site_key"] == "LAR10Q622"
    assert [p["NPDES_ID"] for p in river["permits"]][-2:] == ["LAR10Q310", "LAR10Q424"]
    assert all(p["infrastructure"] for p in river["permits"][-2:])
    assert river["site_links"] == ["shared_token_place"]


# ------------------------------------------------------------------ held sites
def test_a_held_records_duplicate_is_not_emitted(no_holds: None) -> None:
    _, _, unheld, _ = _run()
    h5 = _site(unheld, "VAR10S340")  # H5 Ashburn, 11 m from Virginia DEQ's built H5 record
    assert h5["lifecycle_state"] == "under_construction"


def test_held_sites_are_listed_and_never_emitted() -> None:
    _, raw, rows, _ = _run()
    held = raw.meta["held_sites"]
    assert [h["site_key"] for h in held] == HELD_IN_FIXTURE
    assert {h["reason"] for h in held} == {"duplicate"}
    emitted = {p["NPDES_ID"] for r in rows for p in r["permits"]}
    assert not set(HELD_IN_FIXTURE) & emitted
    assert "VAR10V759" not in emitted  # a held site is held whole (Dulles Berry LC4 and LC8)


def test_the_hold_list_is_well_formed_and_reviewed() -> None:
    held_permits.cache_clear()
    listed = held_permits()
    assert len(listed) == 35 and set(listed.values()) == {"duplicate", "bridge"}
    assert listed["VAR10T675"] == "bridge"  # Vantage VA16: chained four Vantage campuses 11.7 km across
    import yaml

    doc = yaml.safe_load(HELD_PATH.read_text(encoding="utf-8"))
    sources = {"us.epa.echo.icis_air", "us.va.deq.data_center_air_sites"}
    for entry in doc["held"]:
        assert entry["reason"] in HELD_REASONS
        assert entry["held_record"].rsplit(":", 1)[0] in sources, entry
        assert isinstance(entry["m"], int) and entry["m"] >= 0
        assert set(entry) <= {"npdes_id", "name", "reason", "held_record", "m", "uncertain", "note"}, entry
    with tempfile.TemporaryDirectory() as tmp:
        bad = pathlib.Path(tmp) / "held.yaml"
        bad.write_text("held:\n  - npdes_id: X1\n    reason: maybe\n    held_record: a:b\n", encoding="utf-8")
        with pytest.raises(ValueError):
            held_permits.__wrapped__(bad)


# ------------------------------------------------------------------ personal data
def _doc_with_permittee() -> dict[str, Any]:
    doc = _doc()
    cols = doc["permits"]["columns"]
    doc["permits"]["columns"] = [*cols, "PERMIT_NAME"]
    doc["permits"]["rows"] = [[*r, PLACEHOLDER] for r in doc["permits"]["rows"]]
    doc["facilities"]["columns"] = [*doc["facilities"]["columns"], "CONTACT_PHONE"]
    doc["facilities"]["rows"] = [[*r, "555-0100"] for r in doc["facilities"]["rows"]]
    return doc


def test_the_permittee_never_reaches_a_record() -> None:
    assert "PERMIT_NAME" not in FIXTURE.read_text(encoding="utf-8")
    payload = json.dumps(_doc_with_permittee()).encode()
    c, raw, rows, df = _run(payload)
    assert len(rows) == 31
    for r in rows:
        text = json.dumps(r)
        assert "PERMIT_NAME" not in text and PLACEHOLDER not in text and "555-0100" not in text
    assert not df["raw"].str.contains(PLACEHOLDER, regex=False).any()
    assert "PERMIT_NAME" not in raw.meta["source_header"]
    # `redact` strips the same columns before a snapshot is stored, and leaves clean bytes alone
    cleaned = json.loads(c.redact(payload))
    assert "PERMIT_NAME" not in cleaned["permits"]["columns"]
    assert "CONTACT_PHONE" not in cleaned["facilities"]["columns"]
    assert PLACEHOLDER not in json.dumps(cleaned)
    assert c.redact(FIXTURE.read_bytes()) == FIXTURE.read_bytes()
    assert c.personal_data_columns == ("PERMIT_NAME",)


def test_a_facility_name_read_as_a_person_is_dropped() -> None:
    doc = _doc()
    cols = doc["facilities"]["columns"]
    row = next(r for r in doc["facilities"]["rows"] if r[0] == "VAR104158")
    row[cols.index("FACILITY_NAME")] = "PAT DOE DATACENTER"  # a made-up name
    _, raw, rows, _ = _run(json.dumps(doc).encode())
    assert raw.meta["excluded"]["personal_name"] == 1
    assert "PAT DOE DATACENTER" not in json.dumps(rows)
    # The one name seen with a possible person's name inside a project name is works only, never a row.
    assert "DILLON ROGERS" not in json.dumps(_run()[2])


# ------------------------------------------------------------------ lifecycle
def test_status_map_and_the_site_state() -> None:
    _, raw, rows, df = _run()
    state = dict(zip(df["source_record_id"], df["lifecycle_state"], strict=True))
    raw_status = dict(zip(df["source_record_id"], df["status_raw"], strict=True))
    assert _permit_row(rows, "TXR1598ZP")["lifecycle_state"] == "filed"  # a pending NOI
    assert state["GAR148151"] == "unknown" and raw_status["GAR148151"] == "EXP"
    assert state["MOR10D339"] == "built" and raw_status["MOR10D339"] == "TRM"  # two terminated phases
    iron = _site(rows, "AZC100769")
    assert iron["status_permit"] == "AZC111426" and iron["PERMIT_STATUS_CODE"] == "EFF"
    assert iron["lifecycle_state"] == "under_construction"
    assert df["lifecycle_state"].value_counts().to_dict() == {
        "under_construction": 18,
        "built": 7,
        "unknown": 6,
    }
    assert not df["status_rule"].str.endswith(".unmapped").any()
    assert set(df.set_index("source_record_id").loc[:, "status_rule"].str.rsplit(".", n=1).str[-1]) == {
        "map",
        "terminated",
        "not_stated",
        "lapsed_coverage",
        "state_not_reporting",
        "terminated_at_expiry",
    }
    assert {r["status_as_of"] for r in rows} == {"2026-10-18"} and raw.meta["as_of"] == "2026-10-18"


def test_the_status_map_and_the_rules_before_it() -> None:
    c = connector_for(SOURCE_ID)
    sm = c.status_map
    key = c.status_key
    assert harmonise_status(key, {"status_raw": "EFF", "terminated": "yes"}, sm) == (
        "built",
        "epa_icis_npdes_construction.terminated",
    )
    expected = {"PND": "filed", "EFF": "under_construction", "ADC": "under_construction", "TRM": "built"}
    for code, want in expected.items():
        assert harmonise_status(key, {"status_raw": code, "terminated": "no"}, sm)[0] == want, code
    assert harmonise_status(key, {"status_raw": "ZZZ", "terminated": "no"}, sm) == (
        "unknown",
        "epa_icis_npdes_construction.unmapped",
    )
    # `unknown` is the fallback no status map names; the statuses that say nothing about
    # construction reach it through the connector's own rule, never as an unmapped value
    assert "unknown" not in set(sm["sources"][key]["map"].values())
    ev = _evidence()
    for code in ("EXP", "NON", "RET"):
        permit = Permit(code, {}, {"PERMIT_STATUS_CODE": code}, "component", "exact", None)
        assert c._permit_state(permit, ev) == ("unknown", "epa_icis_npdes_construction.not_stated")
        ended = Permit(
            code, {}, {"PERMIT_STATUS_CODE": code, "TERMINATION_DATE": "01/02/2026"}, "component", "x", None
        )
        assert c._permit_state(ended, ev)[0] == "built"


def _evidence(reporting: dict[str, int] | None = None, before: dict[str, int] | None = None) -> Evidence:
    return Evidence(
        as_of=pd.Timestamp("2026-10-18"),
        reporting=reporting,
        master_dates={"XXR100000": frozenset({pd.Timestamp("2024-07-01")})},
        reporting_before=before or {},
    )


def _coverage(code: str, **dates: str) -> Permit:
    permit = {"PERMIT_STATUS_CODE": code, "MASTER_EXTERNAL_PERMIT_NMBR": "XXR100000", **dates}
    return Permit("XXR10T001", {"STATE_CODE": "XX"}, permit, "component", "exact", None)


def test_coverage_in_force_long_past_its_expiry_is_not_construction() -> None:
    """A coverage still EFF or ADC more than a year after its own expiration date is a record nobody
    closed: Virginia's 2019-permit coverages (expired 2024-06-30), a Hawaii school's (2018-12-05)."""
    c = connector_for(SOURCE_ID)
    ev = _evidence()
    rule = "epa_icis_npdes_construction.lapsed_coverage"
    assert c._permit_state(_coverage("ADC", EXPIRATION_DATE="06/30/2024"), ev) == ("unknown", rule)
    assert c._permit_state(_coverage("EFF", EXPIRATION_DATE="10/17/2025"), ev) == ("unknown", rule)
    # within the year a state takes to reissue its general permit, ADC is coverage in force
    assert c._permit_state(_coverage("ADC", EXPIRATION_DATE="03/31/2026"), ev)[0] == "under_construction"
    _, _, rows, df = _run()
    by_id = df.set_index("source_record_id")
    assert by_id.loc["ORR10F482", "lifecycle_state"] == "unknown"  # Hillsboro, expired 2020-12-14
    assert by_id.loc["ORR10F482", "status_rule"] == rule
    # NDA Mesa: the stale AZG2020 coverage no longer outranks the AZG2025 coverage's termination
    assert _permit_row(rows, "AZC103911")["status_rule"] == rule
    assert by_id.loc["AZC103911", "lifecycle_state"] == "built"
    assert by_id.loc["AZC103911", "status_raw"] == "TRM"


def test_a_termination_on_the_expiry_date_is_a_lapse_not_a_notice() -> None:
    c = connector_for(SOURCE_ID)
    ev = _evidence()
    rule = "epa_icis_npdes_construction.terminated_at_expiry"
    for ended in ("06/30/2024", "07/01/2024"):
        coverage = _coverage("TRM", EXPIRATION_DATE="06/30/2024", TERMINATION_DATE=ended)
        assert c._permit_state(coverage, ev) == ("unknown", rule)
    early = _coverage("TRM", EXPIRATION_DATE="06/30/2024", TERMINATION_DATE="06/27/2024")
    assert c._permit_state(early, ev)[0] == "built"
    _, _, _, df = _run()
    crossroads = df.set_index("source_record_id").loc["INRA03445"]  # expired and ended 2024-06-02
    assert (crossroads["lifecycle_state"], crossroads["status_rule"]) == ("unknown", rule)


def test_coverage_in_a_state_that_stopped_reporting_is_not_current() -> None:
    c = connector_for(SOURCE_ID)
    rule = "epa_icis_npdes_construction.state_not_reporting"
    stopped = _evidence({"XX": 22}, {"XX": 12_183})  # California, 2026-10-18
    falling = _evidence({"XX": 120}, {"XX": 900})  # a quarter or less of the year before
    reporting = _evidence({"XX": 944}, {"XX": 898})  # Virginia
    old = _coverage("EFF", ORIGINAL_ISSUE_DATE="09/08/2022", EFFECTIVE_DATE="09/08/2022")
    assert c._permit_state(old, stopped) == ("unknown", rule)
    assert c._permit_state(old, falling) == ("unknown", rule)
    assert c._permit_state(old, reporting)[0] == "under_construction"
    assert c._permit_state(old, _evidence(None))[0] == "under_construction"  # a snapshot with no counts
    recent = _coverage("EFF", ORIGINAL_ISSUE_DATE="05/02/2026", EFFECTIVE_DATE="05/02/2026")
    assert c._permit_state(recent, stopped)[0] == "under_construction"  # its own report is recent
    # a date that is the master's own is no evidence of a recent report
    stamped = _coverage("EFF", ORIGINAL_ISSUE_DATE="07/01/2024", EFFECTIVE_DATE="07/01/2024")
    assert c._permit_state(stamped, stopped) == ("unknown", rule)
    _, raw, _, df = _run()
    assert raw.meta["states_not_reporting"] == ["CA", "WY"]
    hayward = df.set_index("source_record_id").loc["CAZ449816"]
    assert (hayward["lifecycle_state"], hayward["status_rule"]) == ("unknown", rule)
    # Georgia and Virginia report: their coverages are judged on their own dates
    ev = connector_for(SOURCE_ID)._evidence(_doc(), _snapshot(), _doc_masters())
    assert ev.reports("GA") and ev.reports("VA") and not ev.reports("CA") and not ev.reports("WY")


def _doc_masters() -> list[dict[str, str]]:
    m = _doc()["masters"]
    return [dict(zip(m["columns"], r, strict=True)) for r in m["rows"]]


def test_the_most_recent_filing_decides_except_a_handover() -> None:
    t = pd.Timestamp
    # a termination after an older coverage still recorded in force: the termination decides
    assert site_decision(["under_construction", "built"], [t("2024-07-09"), t("2026-06-15")], ["A", "B"]) == 1
    # a new NOI on the day of the termination (a handover), or within the year before it: in force
    assert site_decision(["built", "under_construction"], [t("2026-06-23"), t("2026-06-23")], ["A", "B"]) == 1
    assert site_decision(["built", "under_construction"], [t("2025-05-06"), t("2025-04-25")], ["A", "B"]) == 1
    # with no coverage in force, the latest filing decides, whatever its state
    assert site_decision(["built", "unknown"], [t("2018-01-08"), t("2018-04-13")], ["A", "B"]) == 1
    assert site_decision(["filed", "under_construction"], [t("2026-10-02"), t("2025-03-28")], ["A", "B"]) == 1
    _, _, rows, _ = _run()
    westpointe = _site(rows, "NMR1006JH")  # coverage in force since 2024-07, a later NOI terminated 2026-06
    assert westpointe["lifecycle_state"] == "built" and westpointe["status_permit"] == "NMR1006VQ"
    freebird = _site(rows, "TXR1591WC")  # terminated and re-filed on 2026-06-23
    assert freebird["lifecycle_state"] == "under_construction" and freebird["status_permit"] == "TXR1568YQ"
    lancaster = _site(rows, "TXR1540QW")  # LAN01's NOI 11 days before the older coverage ended
    assert lancaster["lifecycle_state"] == "under_construction"


def test_states_are_judged_at_the_files_date_not_the_retrieval_date() -> None:
    _, _, _, now = _run()
    _, _, _, later = _run(retrieved_at=dt.datetime(2029, 1, 1, tzinfo=dt.UTC))
    assert now["lifecycle_state"].tolist() == later["lifecycle_state"].tolist()
    assert now["status_rule"].tolist() == later["status_rule"].tolist()


def test_first_covered_is_the_coverages_own_date_not_the_general_permits() -> None:
    _, _, rows, _ = _run()
    by_key = {r["site_key"]: r for r in rows}
    # Virginia stamps 07/01/2019 or 07/01/2024, the general permit's own dates, on every coverage
    assert by_key["VAR104158"]["first_covered"] is None
    assert _permit_row(rows, "VAR104158")["coverage_date"] is None
    # Georgia stamps its general permits' dates (08/01/2018, 08/01/2023) on the original issue and issue
    # dates and dates the coverage itself by its effective date
    aligned = _permit_row(rows, "GAR18A841")
    assert aligned["ORIGINAL_ISSUE_DATE"] == "08/01/2018" and aligned["coverage_date"] == "2023-11-03"
    assert by_key["TXR1540QW"]["first_covered"] == "2024-05-15"


# ------------------------------------------------------------------ sites
def test_permits_group_into_sites(no_holds: None) -> None:
    _, raw, rows, _ = _run()
    members = {r["site_key"]: sorted(p["NPDES_ID"] for p in r["permits"]) for r in rows}
    links = {r["site_key"]: r["site_links"] for r in rows}
    # Arizona re-covered open sites under its 2025 permit with new ids at the same point
    assert members["AZC100769"] == ["AZC100769", "AZC103735", "AZC111426"] and links["AZC100769"] == ["near"]
    assert members["NJG372781"] == ["NJG372781", "NJG387487"]  # CoreWeave, two applications, one point
    assert members["CAZ478830"] == ["CAZ478830", "CAZ481194"] and links["CAZ478830"] == ["shared_token"]
    assert members["VAR10Q910"] == ["VAR10Q910", "VAR10V759"]  # Dulles Berry LC4 and LC8, 331 m
    assert members["MOR10D339"] == ["MOR10D339", "MORA07437"] and links["MOR10D339"] == ["same_name"]
    # a data-centre campus keeps the substation and duct bank filed beside it
    assert members["ILR10ZDR8"] == ["ILR10ZDR8", "ILR10ZGFX", "ILR10ZHH6"]
    # "CCO-06" and "CCO06" are one campus code (505 m)
    assert members["GAR189816"] == ["GAR189816", "GAR24904F"] and links["GAR189816"] == ["shared_token"]
    # a name with no distinctive word joins the one site around it: Lancaster (150 m), Milam (408 m)
    assert members["TXR1540QW"] == ["TXR1505UK", "TXR1540QW"] and links["TXR1540QW"] == ["generic_near"]
    assert members["TXR1540UC"] == ["TXR1540UC", "TXR1598ZP"] and links["TXR1540UC"] == ["generic_near"]
    # "PROJECT BORDEN COUNTY DATA CENTER" is "BORDEN COUNTY DATA CENTER" (1,050 m)
    assert members["TXR1575YC"] == ["TXR1549YY", "TXR1575YC"] and links["TXR1575YC"] == ["same_name"]
    # Microsoft's CYS13 and CYS14 (176 m) are one campus filed building by building
    assert members["WYR106140"] == ["WYR106140", "WYR106508"] and links["WYR106140"] == ["code_family"]
    # Kept apart: Prime ORD-1 and the ComEd works for Stream share only their city's words...
    assert members["ILR10ZC4M"] == ["ILR10ZC4M"] and "ILR10ZF9O" not in members
    # ...QTS Ashburn 1 and Ashburn 2 share only a place word...
    assert members["VAR10Q014"] == ["VAR10Q014"] and members["VAR10Q048"] == ["VAR10Q048"]
    # ...and Microsoft's PHX 15 and PHX 16 are not PHX 70/71 though all four name Microsoft
    assert members["AZCN88753"] == ["AZCN88753", "AZCN90218"]
    assert members["AZC114064"] == ["AZC114064"] and members["AZC114065"] == ["AZC114065"]
    assert raw.meta["sites_with_several_permits"] == 16  # 14 published, 2 held


def test_tokens_join_hyphenated_codes_and_drop_place_words() -> None:
    assert site_tokens("CCO-06 DATA CENTER") == site_tokens("CCO06 DATA CENTER") == {"CCO06"}
    assert name_key("CCO-06 DATA CENTER") == name_key("CCO06 DATA CENTER") == "CCO06"
    assert site_tokens("PRIME DATA CENTERS ORD-1 ELK GROVE VILLAGE", "ELK GROVE VILLAGE") == {"PRIME", "ORD1"}
    # a county's words are place words, as a city's are
    assert site_tokens("MILAM DATA CENTER BUILDING 1", "BURLINGTON", county_from_code("TX331")) == frozenset()
    assert site_tokens("BORDEN COUNTY DATA CENTER", "GAIL", county_from_code("TX033")) == frozenset()
    assert name_key("PROJECT BORDEN COUNTY DATA CENTER") == name_key("BORDEN COUNTY DATA CENTER")
    assert name_key("PROPOSED DATA CENTER ORD-1") == "ORD1" != name_key("PROPOSED DATA CENTER ORD-2")
    assert code_families(frozenset({"CYS13"})) == {"CYS"} and code_families(frozenset({"CH4"})) == {"CH"}
    # a state code is no family: NTT's VA10 and Vantage's VA11 are different operators
    assert code_families(frozenset({"VA10", "TX2", "B9B10"})) == frozenset()


def test_an_infrastructure_only_site_is_not_a_data_centre_record() -> None:
    _, raw, rows, _ = _run()
    assert raw.meta["infrastructure_only_sites"] == [
        "ILR10ZF9O",
        "ILR10ZGVR",
        "TXR1515UC",
        "TXR1552VW",
        "TXR1596YR",
    ]
    held = {p["NPDES_ID"] for r in rows for p in r["permits"]}
    assert not {"TXR1552VW", "TXR1581ZA", "TXR1515UC", "TXR1596YR", "ILR10ZGVR", "ILR10ZF9O"} & held
    for works in (
        "QTS DFW 2 DATA CENTER SUBSTATION PROJECT",
        "NTT DATA CENTER TO TSS 101",
        "GOODLAND DATA CENTER OFFSITES",
        "DATA CENTER SWITCH STATION GRADING",
        "DATA CENTER PORTABLE BATCH PLANT",
        "DATA CENTER LAYDOWN YARD DILLON ROGERS PHASE 1 & PHASE 2",  # a join of two works
        "COMED KEHOE DATA CENTER DUCT PACKAGE PROJECT",
        "COMED TSS184 ELK GROVE EAST BUILDING & STREAM DATA CENTER TUG LINES",  # a utility's permit
        "EAGLE MOUNTAIN DATA CENTER INFRASTRUCTURE",
        "LOISDALE RD DATA CENTER CONNECT",
        "ENTERGY LOUISIANA, LLC: RIVER BEND DATA CENTER-PROPOSED STATION",
    ):
        assert is_infrastructure(works), works
    for own in (
        "PROJECT TROPICAL PHASE 2 - DATA CENTER AND SUBSTAT",  # one part names the data centre alone
        "ALIGNED DATA CENTERS - ORD04 & ELECTRICAL DUCT BANK EXTENSION",
        "DORAL DATA CENTER PARKING LOT IMPROVEMENTS",
        "QTS PROJECT LANDRY DC3 DATA CENTER & SERVICE YARD",
        "SCE&G IRMO DATA CENTER",  # a utility's own data centre, not a utility's works
        "STACK INFRASTRUCTURE DATA CENTER",
    ):
        assert not is_infrastructure(own), own


def _permit(
    pid: str, name: str, point: tuple[float, float] | None, city: str = "", county: str = ""
) -> Permit:
    fac = {"NPDES_ID": pid, "FACILITY_NAME": name, "STATE_CODE": "VA", "CITY": city, "COUNTY_CODE": county}
    return Permit(
        npdes_id=pid,
        facility=fac,
        permit={},
        construction_basis="component",
        placement="exact" if point else "no_coordinate",
        point=point,
        tokens=site_tokens(name, city),
        key=name_key(name),
    )


def test_site_link_thresholds() -> None:
    a = _permit("A", "ALPHA DATA CENTER", (39.0, -77.5))
    near = _permit("B", "BRAVO DATA CENTER", (39.0008, -77.5))  # ~89 m
    apart = _permit("C", "CHARLIE DATA CENTER", (39.0013, -77.5))  # ~145 m
    assert site_link(a, near) == "near" and site_link(a, apart) is None
    same_op = _permit("D", "ALPHA DATA CENTER BUILDING 2", (39.008, -77.5))  # ~890 m, shares ALPHA
    assert site_link(a, same_op) == "shared_token"
    far_op = _permit("E", "ALPHA DATA CENTER NORTH", (39.02, -77.5))  # ~2.2 km
    assert site_link(a, far_op) is None
    same_name_far = _permit("F", "ALPHA DATA CENTER", (39.03, -77.5))  # ~3.3 km, same name
    assert site_link(a, same_name_far) == "same_name"
    # building codes of one family within 400 m, not beyond
    cys13, cys14 = _permit("G", "CYS13 DATA CENTER", (41.0, -104.8)), _permit("H", "CYS14", (41.0016, -104.8))
    assert site_link(cys13, cys14) == "code_family"  # ~178 m
    assert site_link(cys13, _permit("I", "CYS19 DATA CENTER", (41.0054, -104.8))) is None  # ~600 m
    # a generic name says nothing
    generic = _permit("J", "DATA CENTER", (39.03, -77.5))
    assert site_link(_permit("K", "DATA CENTER", (39.0, -77.5)), generic) is None
    assert name_key("DATA CENTER") == "" and name_key("MICROSOFT PHX 16 DATA CENTER") == "MICROSOFT PHX 16"
    # no point: a shared word and the same county code or city
    x = _permit("X", "HUT 8 RIVER BEND DATA CENTER", None, city="SAINT FRANCISVILLE", county="LA125")
    y = _permit("Y", "CAJUN INDUSTRIES - RIVER BEND DATA CENTER", None, city="SAINT FRANCISVILLE")
    z = _permit("Z", "RIVER BEND DATA CENTER", None, city="UNKNOWN")
    assert (
        site_link(x, y) == "shared_token_place"
        and site_link(z, _permit("W", "RIVER BEND 2", None, "UNKNOWN")) is None
    )
    assert "ASHBURN" not in site_tokens("ASH1 BRODERICK ASHBURN DATA CENTER", "STERLING")
    assert "ELK" not in site_tokens("PRIME DATA CENTERS ORD-1 ELK GROVE VILLAGE", "ELK GROVE VILLAGE")


def test_a_generic_name_joins_one_site_and_never_bridges_two() -> None:
    alpha = _permit("A", "ALPHA DATA CENTER", (39.0, -77.5))
    bravo = _permit("B", "BRAVO DATA CENTER", (39.006, -77.5))  # ~670 m from alpha
    one = _permit("G", "DATA CENTER", (39.002, -77.5))  # ~220 m from alpha, ~450 m from bravo
    sites = {tuple(sorted(p.npdes_id for p in m)) for m, _ in npdes.group_sites([alpha, bravo, one])}
    assert sites == {("A",), ("B",), ("G",)}  # two sites within 500 m: it joins neither
    sites = {tuple(sorted(p.npdes_id for p in m)) for m, _ in npdes.group_sites([alpha, one])}
    assert sites == {("A", "G")}


# ------------------------------------------------------------------ placement
def test_only_site_coordinates_become_points() -> None:
    _, raw, rows, df = _run()
    assert raw.meta["placement"] == {
        "coarse_coordinate": 1,
        "exact": 51,
        "no_coordinate": 5,
        "point_outside_state": 2,
    }
    for r in rows:
        assert ("Latitude" in r) == (r["placement"] == "exact"), r["site_key"]
        for p in r["permits"]:
            assert ("Latitude" in p) == (p["placement"] == "exact")
            assert "GEOCODE_LATITUDE" not in p and "GEOCODE_LONGITUDE" not in p
    county = dict(zip(df["source_record_id"], df["county"], strict=True))
    # Georgia files whole degrees ("34", "-84"): no point, and no county code either
    assert _site(rows, "GAR148151")["placement"] == "coarse_coordinate" and county["GAR148151"] is None
    # a longitude with the wrong sign is outside Mississippi; the county comes from COUNTY_CODE
    assert (
        _site(rows, "MSR108850")["placement"] == "point_outside_state" and county["MSR108850"] == "Oktibbeha"
    )
    # no coordinate: the stated county
    assert county["LAR10Q622"] == "West Feliciana"
    # an exact point decides the county; the code's disagreement is recorded
    kati = _site(rows, "TXR1513ZL")
    assert county["TXR1513ZL"] == "Cameron" and kati["COUNTY_CODE"] == "TX489"
    assert kati["permits"][0]["county_code_matches_point"] is False
    assert placement_for("34", "-84", "GA")[0] == "coarse_coordinate"
    assert placement_for("", "", "GA")[0] == "no_coordinate"
    assert placement_for("0", "0", "GA")[0] == "no_coordinate"
    assert placement_for("38.995800", "-77.4876", "VA") == ("exact", 38.9958, -77.4876)
    assert county_from_code("TX489") == "Willacy" and county_from_code("VA510") == "Alexandria city"
    assert county_from_code("GU010") is None and county_from_code("") is None


# ------------------------------------------------------------------ layout
def test_layout_change_and_bad_payload_are_parse_errors() -> None:
    c = connector_for(SOURCE_ID)
    with pytest.raises(ParseError):
        c.parse(_snapshot(b"<html>not json</html>"))
    with pytest.raises(ParseError):
        c.parse(_snapshot(json.dumps({"facilities": {"columns": ["NAME"], "rows": []}}).encode()))
    doc = _doc()
    doc["permits"]["columns"] = [
        c2 if c2 != "PERMIT_STATUS_CODE" else "STATUS" for c2 in doc["permits"]["columns"]
    ]
    with pytest.raises(ParseError):
        c.parse(_snapshot(json.dumps(doc).encode()))
    with pytest.raises(ParseError):
        c.parse(_snapshot(b"[]"))
    doc = _doc()
    doc["reporting"]["columns"] = ["STATE_CODE", "MONTH"]
    with pytest.raises(ParseError):
        c.parse(_snapshot(json.dumps(doc).encode()))


def test_a_snapshot_without_filing_counts_judges_no_state() -> None:
    doc = _doc()
    del doc["reporting"], doc["file"]
    _, raw, _, df = _run(json.dumps(doc).encode())
    assert raw.meta["states_not_reporting"] is None
    assert not df["status_rule"].str.endswith("state_not_reporting").any()
    assert raw.meta["as_of"] == "2026-10-24"  # the retrieval date when the file's own is not stored


def test_the_zip_helpers_are_the_icis_air_connectors() -> None:
    assert npdes.central_directory is air.central_directory
    assert npdes.conditional_headers is air.conditional_headers
    assert npdes.county_for_point is air.county_for_point


# ------------------------------------------------------------------ zip member streams
def _zip(members: dict[str, bytes], method: int = zipfile.ZIP_DEFLATED) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=method) as z:
        for name, data in members.items():
            z.writestr(name, data)
    return buf.getvalue()


def _member_stream(blob: bytes, name: str) -> MemberStream:
    members = {m.name: m for m in npdes.central_directory(blob, len(blob))}
    m = members[name]
    return MemberStream(io.BytesIO(blob[m.header_offset :]), m)


def test_member_stream_inflates_checks_crc_and_size() -> None:
    data = b'"NPDES_ID","FACILITY_NAME"\n' + b'"X1","ALPHA DATA CENTER"\n' * 20_000
    blob = _zip({"A.csv": data, "B.csv": b"x" * 100})
    assert _member_stream(blob, "A.csv").read() == data
    stored = _zip({"A.csv": data}, zipfile.ZIP_STORED)
    assert _member_stream(stored, "A.csv").read() == data
    rows = list(npdes.csv_rows(_member_stream(blob, "A.csv")))
    assert rows[0] == ["NPDES_ID", "FACILITY_NAME"] and len(rows) == 20_001
    m = {z.name: z for z in npdes.central_directory(blob, len(blob))}["A.csv"]
    bad = npdes.MemberStream(
        io.BytesIO(blob[m.header_offset :]), m.__class__(**{**m.__dict__, "crc": m.crc ^ 1})
    )
    with pytest.raises(ParseError):
        bad.read()
    with pytest.raises(ParseError):
        MemberStream(io.BytesIO(blob[m.header_offset : m.header_offset + 500]), m).read()
    with pytest.raises(ParseError):
        MemberStream(io.BytesIO(b"not a local header" * 3), m)
    assert zlib.crc32(data) == m.crc


# ------------------------------------------------------------------ fetch against a local echo.epa.gov
# `FakeEcho` answers as echo.epa.gov (Apache) was measured to on 2026-10-23: ETag and Last-Modified
# on every 200/206, `Range` honoured (unless told to ignore it), and a 304 carrying only the ETag
# when `If-None-Match` matches. Its zip is built from the fixture's own rows, the way the upstream
# file holds them: every permit has its permittee (a placeholder here), masters are `NGP` permit
# rows whose PERMIT_NAME is their title (one version per date the master carries), one permit has a
# superseded version, and one facility, one permit and one component row belong to no candidate.
ROBOTS = b"User-agent: *\nCrawl-delay: 10\nAllow: /files/\n"


class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str]) -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.headers = headers

    def iter_content(self, chunk_size: int) -> Any:
        for i in range(0, len(self.content), chunk_size):
            yield self.content[i : i + chunk_size]

    def close(self) -> None:
        pass


class FakeEcho:
    def __init__(self, body: bytes, ignore_range: bool = False) -> None:
        self.version = 0
        self.put(body)
        self.ignore_range = ignore_range
        self.headers: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.after_tail: Any = None

    def put(self, body: bytes) -> None:
        self.version += 1
        self.body = body
        self.etag = f'"{len(body):x}-{self.version:x}"'
        self.modified = formatdate(1_792_000_000 + self.version * 86_400, usegmt=True)

    def request(
        self, method: str, url: str, headers: dict[str, str] | None = None, **kw: Any
    ) -> FakeResponse:
        sent = dict(headers or {})
        self.calls.append((url, sent))
        if url.endswith("/robots.txt"):
            return FakeResponse(200, ROBOTS, {"Content-Type": "text/plain"})
        assert url == ZIP_URL
        if sent.get("If-None-Match") == self.etag:
            return FakeResponse(304, b"", {"ETag": self.etag})
        base = {"ETag": self.etag, "Last-Modified": self.modified, "Content-Type": "application/zip"}
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", sent.get("Range", ""))
        if m is None or self.ignore_range:
            return FakeResponse(200, self.body, {**base, "Content-Length": str(len(self.body))})
        first, last = m.groups()
        n = len(self.body)
        start, end = (max(0, n - int(last)), n - 1) if first == "" else (int(first), min(n - 1, int(last)))
        part = self.body[start : end + 1]
        resp = FakeResponse(206, part, {**base, "Content-Range": f"bytes {start}-{end}/{n}"})
        if first == "" and self.after_tail is not None:
            self.after_tail(self)
        return resp

    def get(self, url: str, **kw: Any) -> FakeResponse:
        return self.request("GET", url, **kw)

    def zip_gets(self) -> list[dict[str, str]]:
        return [h for u, h in self.calls if u == ZIP_URL]


def _csv(columns: list[str], rows: list[list[str]]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n", quoting=csv.QUOTE_ALL)
    w.writerow(columns)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _master_rows(doc: dict[str, Any]) -> list[dict[str, str]]:
    """The NGP permit rows of the fixture's masters: one version per date the master carries."""
    out = []
    for master, title, status, dates in doc["masters"]["rows"]:
        for version, date in enumerate(dates.split("|") if dates else [""]):
            out.append(
                {
                    "EXTERNAL_PERMIT_NMBR": master,
                    "VERSION_NMBR": str(version),
                    "PERMIT_TYPE_CODE": "NGP",
                    "PERMIT_STATUS_CODE": status,
                    "ISSUE_DATE": date,
                    "PERMIT_NAME": title,
                }
            )
    return out


def upstream_zip(doc: dict[str, Any] | None = None) -> bytes:
    doc = doc or _doc()
    fac_cols = [*doc["facilities"]["columns"], "IMPAIRED_WATERS"]
    facilities = [[*r, ""] for r in doc["facilities"]["rows"]]
    facilities.append(["XX0000001", *[""] * 3, "FUNERAL HOME", *[""] * (len(fac_cols) - 5)])
    pcols = doc["permits"]["columns"]
    up_cols = [*pcols, "PERMIT_NAME", "STATE_WATER_BODY_NAME"]
    permits = [[*r, PLACEHOLDER, ""] for r in doc["permits"]["rows"]]
    old = list(doc["permits"]["rows"][0])
    old[pcols.index("VERSION_NMBR")], old[pcols.index("PERMIT_STATUS_CODE")] = "1", "RET"
    permits.append([*old, PLACEHOLDER, ""])  # a superseded version: never the current one
    blank = dict.fromkeys(pcols, "")
    for m in _master_rows(doc):
        permits.append([{**blank, **m}[c] for c in pcols] + [m["PERMIT_NAME"], ""])
    permits.append([{**blank, "EXTERNAL_PERMIT_NMBR": "XX0000001"}[c] for c in pcols] + [PLACEHOLDER, ""])
    comp_cols = [*doc["components"]["columns"], "COMPONENT_TYPE_DESC"]
    components = [[*r, ""] for r in doc["components"]["rows"]] + [["XX0000001", "SWC", ""]]
    return _zip(
        {
            FACILITIES_MEMBER: _csv(fac_cols, facilities),
            "NPDES_VIOLATION_ENFORCEMENTS.csv": b"x" * 3000,
            PERMITS_MEMBER: _csv(up_cols, permits),
            COMPONENTS_MEMBER: _csv(comp_cols, components),
        }
    )


@pytest.fixture()
def clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """One day per fetch, so each run's snapshot has its own timestamped key."""
    days = iter(range(1, 100))
    start = dt.datetime(2026, 10, 23, 6, 0, tzinfo=dt.UTC)
    monkeypatch.setattr(npdes, "utcnow", lambda: start + dt.timedelta(days=next(days)))


def _fetch_run(registry: Any, store: Store, echo: FakeEcho) -> Any:
    echo.calls.clear()
    http = PoliteSession(session=echo, sleep=lambda _s: None)  # type: ignore[arg-type]
    return run(SOURCE_ID, registry=registry, store=store, http=http)


def test_a_first_run_reads_three_members_by_range_and_stores_only_the_candidates(
    registry: Any, tmp_path: pathlib.Path, clock: None
) -> None:
    echo = FakeEcho(upstream_zip())
    first = _fetch_run(registry, Store(tmp_path), echo)
    assert first.status == "ok"
    gets = echo.zip_gets()
    assert gets[0]["Range"].startswith("bytes=-") and len(gets) == 4  # tail, then three members
    assert all(g["Range"] for g in gets) and not any("If-None-Match" in g for g in gets)
    stored = json.loads(first.paths["snapshot"].read_bytes())
    fixture = _doc()
    for table in ("facilities", "permits", "masters", "components"):
        assert stored[table] == fixture[table], table  # candidates, current versions, masters' dates
    # the file's own date is the server's Last-Modified, and the filing counts are the zip's own
    assert stored["file"] == {"last_modified": echo.modified, "as_of": "2026-10-15"}
    assert stored["reporting"]["columns"] == ["STATE_CODE", "MONTH", "FILINGS"]
    text = first.paths["snapshot"].read_text(encoding="utf-8")
    assert PLACEHOLDER not in text and "PERMIT_NAME" not in text and "IMPAIRED_WATERS" not in text
    meta = first.run["snapshot"]["meta"]
    assert meta["fetch_version"] == FETCH_VERSION == 2 and meta["upstream"] == "changed"
    assert meta["zip"]["etag"] == echo.etag and meta["zip"]["ranged"] is True
    assert meta["members"][FACILITIES_MEMBER]["rows_total"] == 70
    assert meta["members"][PERMITS_MEMBER]["rows_total"] == 69 + 1 + len(_master_rows(fixture)) + 1
    assert meta["candidates"] == 69
    assert first.run["rows_fetched"] == 31
    assert first.run["snapshot"]["requests_made"] == 5  # robots.txt + four ranged GETs


def test_filings_are_counted_by_state_and_month_under_construction_masters(
    registry: Any, tmp_path: pathlib.Path, clock: None
) -> None:
    """`reporting` counts every coverage of the permits member whose master is construction
    stormwater (its own SWC component, its title, or a coverage under it carrying SWC), by its
    latest date, for the two years before the file; other coverage and older filings are not."""
    cols = ["EXTERNAL_PERMIT_NMBR", *npdes.PERMIT_COLUMNS[1:], "PERMIT_NAME"]
    blank = dict.fromkeys(cols, "")

    def permit(pid: str, master: str = "", kind: str = "GPC", title: str = "", **dates: str) -> list[str]:
        row = {**blank, "EXTERNAL_PERMIT_NMBR": pid, "VERSION_NMBR": "0", "PERMIT_TYPE_CODE": kind}
        row.update(MASTER_EXTERNAL_PERMIT_NMBR=master, PERMIT_NAME=title, **dates)
        return [row[c] for c in cols]

    permits = [
        permit("TXR150000", kind="NGP"),  # construction by its own SWC component
        permit("KSR100000", kind="NGP", title="Construction Stormwater"),  # by its title
        permit("SCR100000", kind="NGP"),  # by a coverage under it carrying SWC
        permit("TXR050000", kind="NGP"),  # industrial stormwater
        permit("TXR15AAA1", "TXR150000", ISSUE_DATE="03/02/2026"),
        permit("TXR15AAA2", "TXR150000", ORIGINAL_ISSUE_DATE="01/05/2021", TERMINATION_DATE="03/09/2026"),
        permit("TXR15AAA3", "TXR150000", ISSUE_DATE="05/01/2023"),  # before the two years
        permit("KSR10AAA1", "KSR100000", EFFECTIVE_DATE="11/20/2025"),
        permit("SCR10AAA1", "SCR100000", ISSUE_DATE="09/01/2026"),
        permit("TXR05AAA1", "TXR050000", ISSUE_DATE="03/02/2026"),
    ]
    components = [["TXR150000", "SWC"], ["SCR10AAA1", "SWC"], ["TXR050000", "SWI"]]
    fac_cols = list(npdes.FACILITY_COLUMNS)
    body = _zip(
        {
            FACILITIES_MEMBER: _csv(
                fac_cols, [["TXR15AAA1", *[""] * 3, "ALPHA DATA CENTER", *[""] * 4, "TX"]]
            ),
            PERMITS_MEMBER: _csv(cols, permits),
            COMPONENTS_MEMBER: _csv(["EXTERNAL_PERMIT_NMBR", "COMPONENT_TYPE_CODE"], components),
        }
    )
    result = _fetch_run(registry, Store(tmp_path), FakeEcho(body))
    stored = json.loads(result.paths["snapshot"].read_bytes())
    assert stored["reporting"]["rows"] == [
        ["KS", "202511", "1"],
        ["SC", "202609", "1"],
        ["TX", "202603", "2"],
    ]


def test_an_unchanged_zip_is_one_conditional_request_and_an_unchanged_run(
    registry: Any, tmp_path: pathlib.Path, clock: None
) -> None:
    store = Store(tmp_path)
    echo = FakeEcho(upstream_zip())
    first = _fetch_run(registry, store, echo)
    second = _fetch_run(registry, store, echo)
    assert second.status == "unchanged"
    assert second.run["snapshot"]["sha256"] == first.run["snapshot"]["sha256"]
    gets = echo.zip_gets()
    assert len(gets) == 1 and gets[0]["If-None-Match"] == echo.etag
    meta = second.run["snapshot"]["meta"]
    assert meta["upstream"] == "unchanged" and meta["zip"]["not_modified"] is True
    assert meta["zip"]["last_modified"] == first.run["snapshot"]["meta"]["zip"]["last_modified"]
    # a changed file is read again in full and its change is a diff, not a new source
    doc = _doc()
    status = doc["permits"]["columns"].index("PERMIT_STATUS_CODE")
    for r in doc["permits"]["rows"]:
        if r[0] == "TXR1598ZP":
            r[status] = "EFF"
    echo.put(upstream_zip(doc))
    third = _fetch_run(registry, store, echo)
    assert third.status == "ok" and third.run["rows_changed"] == 1
    assert len(echo.zip_gets()) == 4


def test_a_previous_snapshot_of_the_first_fetch_version_is_fetched_in_full(
    registry: Any, tmp_path: pathlib.Path, clock: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A snapshot stored before the master dates and filing counts (fetch version 1) is never reused
    on a 304: the next run reads the file in full, so the lifecycle rules have what they need."""
    store = Store(tmp_path)
    echo = FakeEcho(upstream_zip())
    monkeypatch.setattr(npdes, "FETCH_VERSION", 1)
    _fetch_run(registry, store, echo)
    monkeypatch.setattr(npdes, "FETCH_VERSION", 2)
    again = _fetch_run(registry, store, echo)
    gets = echo.zip_gets()
    assert len(gets) == 4 and not any("If-None-Match" in g for g in gets)
    assert again.run["snapshot"]["meta"]["fetch_version"] == 2


def test_a_server_that_ignores_range_gives_the_same_snapshot(
    registry: Any, tmp_path: pathlib.Path, clock: None
) -> None:
    ranged = _fetch_run(registry, Store(tmp_path / "a"), FakeEcho(upstream_zip()))
    whole = _fetch_run(registry, Store(tmp_path / "b"), FakeEcho(upstream_zip(), ignore_range=True))
    assert whole.status == "ok"
    assert whole.run["snapshot"]["sha256"] == ranged.run["snapshot"]["sha256"]
    assert whole.run["snapshot"]["meta"]["zip"]["ranged"] is False


def test_a_file_that_changes_between_ranged_reads_fails_the_run(
    registry: Any, tmp_path: pathlib.Path, clock: None
) -> None:
    echo = FakeEcho(upstream_zip())
    echo.after_tail = lambda e: e.put(e.body)  # republished just after the tail was read
    result = _fetch_run(registry, Store(tmp_path), echo)
    assert result.status == "failed"
    assert "changed between ranged reads" in str(result.run["error"])
