"""Parser and fetch tests for us.va.loudoun.data_centers (Loudoun County's data-centre pipeline layer).

Fixture `fixtures/loudoun_pipeline_data_centers.json` is the connector's own snapshot of 2026-10-24
(run 6e6fe541, sha256 849240ad…aafa: the item licence, the layer metadata, all 85 applications and the 206
Virginia DEQ data-centre air sites) with the connector's redaction applied (4 subdivisions withheld and
flagged), trimmed to 24 applications and 12 DEQ air sites (fixtures/README.md). No other value is edited.
The owner, editor and person-shaped values these tests feed in are made up and live in memory only. No
network: the fetch tests run the connector against `FakeArcGIS`, which serves the fixture's own rows.
The last section checks the lane's two drafted owner letters (`docs/66-owner-actions.md`) against
`data/sources.yaml`, reading those files only.
"""

from __future__ import annotations

import datetime as dt
import json
import pathlib
import re
from typing import Any
from urllib.parse import urlsplit

import pandas as pd
import pytest
import yaml
from shapely.geometry import Point, Polygon

from conftest import connector_for
from pipeline import resolve
from pipeline.connectors.base import BlockedError, ConnectorError, ParseError, RawSnapshot
from pipeline.connectors.canonical import norm_name
from pipeline.connectors.http import PoliteSession
from pipeline.connectors.registry import Registry
from pipeline.connectors.runner import run
from pipeline.connectors.store import Store
from pipeline.connectors.us_va_loudoun_data_centers import connector as lou
from pipeline.connectors.us_va_loudoun_data_centers.connector import (
    DEQ_QUERY_URL,
    FIELDS,
    ITEM_URL,
    LAYER_URL,
    LICENCE_SHA256,
    QUERY_URL,
    SERVICE_URL,
    WITHHELD_FLAG,
    Application,
    Site,
    application_order,
    canon_application,
    chained,
    group_same_land,
    held_link,
    inside_point,
    keep_subdivision,
    licence_sha256,
    link_held,
    record_url,
    withhold_subdivision,
)
from services.resolve.merge import MERGE_SCORE_THRESHOLD

SOURCE_ID = "us.va.loudoun.data_centers"
FIXTURE = pathlib.Path(__file__).with_name("fixtures") / "loudoun_pipeline_data_centers.json"
RETRIEVED = dt.datetime(2026, 10, 24, 0, 39, 56, tzinfo=dt.UTC)
#: Made-up values for the personal-data tests; no real person.
MADE_UP_PERSON = "MARY QUILLFEATHER"
MADE_UP_ACCOUNT = "made.up.account@example.invalid"
LINKED = {
    "STMP-2022-0016": "icis_air:VA0000005110774284",  # DEQ Operating
    "STPL-2022-0012": "icis_air:VA0000005110774296",  # DEQ Planned
    "STPL-2022-0040": "icis_air:VA0000005110774351",  # DEQ Planned
    "LEGI-2024-0039": "icis_air:VA0000005110700839",  # DEQ Operating
    "EPLAN-2025-0102": "icis_air:VA0000005110774205",  # DEQ Operating
}


def _doc() -> dict[str, Any]:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def _snapshot(content: bytes | None = None) -> RawSnapshot:
    return RawSnapshot(
        content=content if content is not None else FIXTURE.read_bytes(),
        content_type="application/json",
        url=LAYER_URL,
        retrieved_at=RETRIEVED,
        http_status=200,
        ext="json",
    )


def _run(content: bytes | None = None) -> tuple[Any, RawSnapshot, list[dict[str, Any]], pd.DataFrame]:
    c = connector_for(SOURCE_ID)
    raw = _snapshot(content)
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def _by_id(df: pd.DataFrame) -> dict[str, pd.Series]:
    return {str(r["source_record_id"]): r for _, r in df.iterrows()}


# ---------------------------------------------------------------------------------------------- sites
def test_applications_on_the_same_land_are_one_site():
    _, raw, rows, df = _run()
    assert raw.meta["applications"] == 24
    assert len(rows) == len(df) == 20
    apps = {r["site_key"]: [a["Application"] for a in r["applications"]] for r in rows}
    # each pair is a legislative application and a site plan on identical land; the founding one keys it
    assert apps["EPLAN-2023-0083"] == ["EPLAN-2023-0083", "LEGI-2024-0047"]
    assert apps["LEGI-2023-0048"] == ["LEGI-2023-0048", "EPLAN-2025-0195"]
    assert apps["STPL-2023-0008"] == ["STPL-2023-0008", "LEGI-2024-0058"]
    assert apps["EPLAN-2024-0193"] == ["EPLAN-2024-0193", "LEGI-2025-0027"]
    # neighbours that share a sliver of boundary stay two sites
    assert "EPLAN-2024-0163" in apps and "STMP-2022-0018" in apps
    assert sum(len(v) for v in apps.values()) == 24


def test_group_same_land_needs_half_the_smaller_land():
    square = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    a = Application("STPL-2022-0001", {}, square)
    b = Application("LEGI-2024-0001", {}, Polygon([(0.4, 0), (0.4, 1), (1.4, 1), (1.4, 0)]))  # 60 % shared
    c = Application("EPLAN-2025-0001", {}, Polygon([(0.8, 0), (0.8, 1), (1.8, 1), (1.8, 0)]))  # 20 % with a
    sites = group_same_land([c, b, a])
    # a~b (60 %) and b~c (60 %) chain into one site, keyed by the earliest application; a and c share
    # only 20 %, so the group is a chain, which parse refuses (next tests)
    assert [s.numbers for s in sites] == [["STPL-2022-0001", "LEGI-2024-0001", "EPLAN-2025-0001"]]
    assert chained(sites[0])
    far = Application("EPLAN-2025-0002", {}, Polygon([(5, 5), (5, 6), (6, 6), (6, 5)]))
    assert [s.key for s in group_same_land([far, a])] == ["STPL-2022-0001", "EPLAN-2025-0002"]


def _square(x: float, y: float, size: float) -> list[list[float]]:
    """A clockwise Esri ring (outer land) with its lower-left corner at (x, y)."""
    return [[x, y], [x, y + size], [x + size, y + size], [x + size, y], [x, y]]


def _parcels_doc(parcels: dict[str, list[list[float]]]) -> bytes:
    feats = [
        {"attributes": {"Application": number, "FIRST_Status": "Active"}, "geometry": {"rings": [ring]}}
        for number, ring in parcels.items()
    ]
    return json.dumps({"pages": [{"features": feats}], "held": {"pages": [{"features": []}]}}).encode()


def test_a_large_application_chaining_separate_sites_fails_the_run():
    """Review of 2026-10-24: a rezoning over two separate site plans joined all three into one site,
    silently. The two site plans share no land, so parse now refuses the group."""
    c = connector_for(SOURCE_ID)
    content = _parcels_doc(
        {
            "LEGI-2026-0001": _square(-77.50, 39.00, 0.010),  # the rezoning
            "STPL-2026-0001": _square(-77.499, 39.001, 0.002),  # a site plan inside it
            "STPL-2026-0002": _square(-77.493, 39.006, 0.002),  # another, on separate land
        }
    )
    with pytest.raises(ParseError, match="do not all share"):
        c.parse(_snapshot(content))


def test_applications_that_all_share_one_piece_of_land_stay_one_site(monkeypatch: pytest.MonkeyPatch):
    c = connector_for(SOURCE_ID)
    same = _square(-77.50, 39.00, 0.004)
    raw = _snapshot(_parcels_doc({"LEGI-2026-0001": same, "STPL-2026-0001": same, "SPAM-2026-0001": same}))
    rows = c.parse(raw)
    assert [len(r["applications"]) for r in rows] == [3]
    assert raw.meta["largest_site"] == 3
    # a chain a reviewer accepted is loaded as one site
    chain = _parcels_doc(
        {
            "LEGI-2026-0001": _square(-77.50, 39.00, 0.010),
            "STPL-2026-0001": _square(-77.499, 39.001, 0.002),
            "STPL-2026-0002": _square(-77.493, 39.006, 0.002),
        }
    )
    accepted = frozenset({frozenset({"LEGI-2026-0001", "STPL-2026-0001", "STPL-2026-0002"})})
    monkeypatch.setattr(lou, "ACCEPTED_CHAINS", accepted)
    assert [r["site_key"] for r in c.parse(_snapshot(chain))] == ["LEGI-2026-0001"]


def test_records_are_load_proposals_with_provenance():
    c, _, _, df = _run()
    assert list(df.columns) == c.columns
    assert set(df["kind"]) == {"load"}
    assert set(df["technology"]) == {"load"}
    assert set(df["state"]) == {"VA"}
    assert set(df["county"]) == {"Loudoun"}
    assert df["capacity_mw"].isna().all()
    assert df["sponsor_name"].isna().all()
    for col in ("source_id", "source_url", "retrieved_at", "licence_id", "raw"):
        assert df[col].notna().all(), col
    assert set(df["source_id"]) == {SOURCE_ID}
    assert set(df["licence_id"]) == {c.source.licence_id}
    assert set(df["retrieved_at"]) == {"2026-10-24T00:39:56Z"}
    assert df["record_id"].is_unique


def test_identity_is_the_founding_application_number():
    _, _, _, df = _run()
    rows = _by_id(df)
    pair = rows["EPLAN-2023-0083"]
    assert pair["record_id"] == f"{SOURCE_ID}:EPLAN-2023-0083"
    # "LEGI-2024-0047 " arrives with a trailing space; the record names and links the trimmed number
    assert pair["name_canonical"] == "EPLAN-2023-0083 and LEGI-2024-0047 data center applications"
    # the link matches the numbers as the layer stores them, trailing space included: the hosted layer
    # refuses TRIM in `where` ("'where' parameter is invalid", measured 2026-10-24), so no SQL function
    assert json.loads(pair["raw"])["published_numbers"] == ["EPLAN-2023-0083", "LEGI-2024-0047 "]
    assert pair["source_url"] == record_url(["EPLAN-2023-0083", "LEGI-2024-0047 "])
    assert "Application%20IN%20%28%27EPLAN-2023-0083%27%2C%27LEGI-2024-0047%20%27%29" in str(
        pair["source_url"]
    )
    for url in df["source_url"]:
        assert "TRIM" not in str(url) and "UPPER" not in str(url)
    assert record_url(["O'BRIEN-2024-0001"]).count("%27%27") == 1  # a quote in a number is escaped
    assert canon_application(" legi-2024-0047  ") == "LEGI-2024-0047"
    assert canon_application(None) == ""
    # the Town of Leesburg writes no dash after its prefix; the order still reads the year
    assert application_order("TLREZN2024-0003") == (2024, "TLREZN", 3, "TLREZN2024-0003")
    assert application_order("STPL-2023-0008") < application_order("LEGI-2024-0058")
    assert application_order("not a number")[0] == 9999
    assert rows["TLREZN2024-0003"]["name_canonical"] == "TLREZN2024-0003 data center application"


def test_the_name_leads_with_the_application_number():
    """The resolver blocks fuzzy candidates on a name's first token; a subdivision or operator first
    ("AMAZON DATA SERVICES ...") would pair these rows with that operator's other campuses."""
    _, _, _, df = _run()
    first = {str(n).split()[0] for n in df["name_norm"]}
    assert first <= {"EPLAN", "LEGI", "STPL", "STMP", "SPAM", "ZMAP", "TLREZN2024", "TLSPEX2025"}
    assert _by_id(df)["STMP-2022-0016"]["name_canonical"] == (
        "STMP-2022-0016 data center application, VANTAGE DATA CENTERS VA31 subdivision"
    )


def test_a_company_named_plat_reads_as_the_subdivision_not_the_applicant():
    """Review of 2026-10-24: some kept subdivisions are company names that may be past landowners
    ("NA DULLES REAL ESTATE", "PHILIPS ELECTRONICS NA CORP"); the name labels them as the plat."""
    _, _, _, df = _run()
    rows = _by_id(df)
    assert rows["STPL-2022-0012"]["name_canonical"] == (
        "STPL-2022-0012 data center application, NA DULLES REAL ESTATE subdivision"
    )
    with_plat = [str(n) for n in df["name_canonical"] if "," in str(n)]
    assert with_plat and all(n.endswith(" subdivision") for n in with_plat)
    assert lou.site_name(["STPL-2023-0007"], "PHILIPS ELECTRONICS NA CORP") == (
        "STPL-2023-0007 data center application, PHILIPS ELECTRONICS NA CORP subdivision"
    )
    assert lou.site_name(["LEGI-2023-0048", "EPLAN-2025-0195"], None) == (
        "LEGI-2023-0048 and EPLAN-2025-0195 data center applications"
    )


# ------------------------------------------------------------------------------------------ lifecycle
def test_status_map_covers_every_value_and_says_what_the_filing_proves():
    _, _, _, df = _run()
    assert not df["status_rule"].str.endswith(".unmapped").any()
    by_raw = dict(zip(df["status_raw"], df["lifecycle_state"], strict=False))
    assert by_raw == {"Active": "filed", "Approved": "permitted", "Approved: Conditionally": "permitted"}
    assert not set(df["lifecycle_state"]) & {"under_construction", "built"}
    assert (df["lifecycle_state"] == "filed").sum() == 7
    assert (df["lifecycle_state"] == "permitted").sum() == 13


def test_a_site_takes_its_most_advanced_application():
    _, _, _, df = _run()
    site = _by_id(df)["LEGI-2023-0048"]  # LEGI-2023-0048 Approved, EPLAN-2025-0195 Active
    statuses = {a["Application"]: a["FIRST_Status"] for a in json.loads(site["raw"])["applications"]}
    assert statuses == {"LEGI-2023-0048": "Approved", "EPLAN-2025-0195": "Active"}
    assert (site["lifecycle_state"], site["status_raw"]) == ("permitted", "Approved")


def test_an_unknown_status_stays_visible_to_the_vocabulary_check():
    doc = _doc()
    for page in doc["pages"]:
        for feat in page["features"]:
            if canon_application(feat["attributes"]["Application"]) == "EPLAN-2025-0195":
                feat["attributes"]["FIRST_Status"] = "Withdrawn"
            if canon_application(feat["attributes"]["Application"]) == "STMP-2022-0018":
                feat["attributes"]["FIRST_Status"] = "Denied"
    _, _, _, df = _run(json.dumps(doc).encode())
    rows = _by_id(df)
    # the approved application on the same land still decides the state; the unknown value is flagged
    assert rows["LEGI-2023-0048"]["lifecycle_state"] == "permitted"
    assert rows["LEGI-2023-0048"]["status_rule"] == "loudoun_dc_pipeline.unmapped"
    assert (rows["STMP-2022-0018"]["lifecycle_state"], rows["STMP-2022-0018"]["status_rule"]) == (
        "unknown",
        "loudoun_dc_pipeline.unmapped",
    )


def test_restate_status_recomputes_from_raw():
    c, _, _, df = _run()
    restated = c.restate_status(df)
    assert restated is not None
    assert restated["lifecycle_state"].tolist() == df["lifecycle_state"].tolist()
    assert restated["status_rule"].tolist() == df["status_rule"].tolist()


# --------------------------------------------------------------------------------------- held links
def test_a_deq_air_site_inside_the_land_links_the_site_to_the_held_facility():
    _, raw, rows, df = _run()
    refs = {k: v for k, v in zip(df["source_record_id"], df["cross_refs"], strict=True) if v}
    assert refs == LINKED
    assert raw.meta["held_air_sites_read"] == 12
    assert raw.meta["held_linked"] == 5 and raw.meta["held_unlinked"] == 0
    by_key = {r["site_key"]: r for r in rows}
    assert by_key["STPL-2022-0012"]["held_air_sites"] == [
        {"AIR_OP_STATUS": "Planned", "PLA_ICIS_ID": "VA0000005110774296", "PLA_REG_NUM": "74296"}
    ]
    assert by_key["STPL-2022-0012"]["held_link_basis"] == "one_deq_air_site_inside"
    # 1 m outside the land is outside: 22995 Wilder Court lies inside LEGI-2024-0039, not SPAM-2018-0039
    assert by_key["SPAM-2018-0039"]["held_air_sites"] == []
    # DEQ points 11-35 m outside, or 30-75 m from a large legislative area, link nothing
    for key in ("STPL-2022-0051", "STPL-2022-0052", "LEGI-2023-0097"):
        assert by_key[key]["held_link"] is None and by_key[key]["held_link_basis"] is None


def test_ambiguous_held_sites_are_never_linked():
    square = Polygon([(0, 0), (0, 1), (1, 1), (1, 0)])
    both = [
        Site([Application("A-2024-1", {}, square)], square),
        Site([Application("B-2024-1", {}, square)], square),
    ]
    h = {"PLA_REG_NUM": "1", "PLA_ICIS_ID": "VA1", "AIR_OP_STATUS": "Operating", "point": Point(0.5, 0.5)}
    link_held(both, [h])  # covered by two sites: neither takes it
    assert [s.held for s in both] == [[], []]

    one = Site([Application("C-2024-1", {}, square)], square)
    other = {**h, "PLA_REG_NUM": "2", "PLA_ICIS_ID": "VA2", "point": Point(0.2, 0.2)}
    link_held([one], [h, other])
    assert held_link(one) == (None, "several_deq_air_sites_inside")

    same = Site([Application("D-2024-1", {}, square)], square)
    link_held([same], [h, {**h, "PLA_REG_NUM": "3", "point": Point(0.3, 0.3)}])  # two registrations, one id
    assert held_link(same) == ("icis_air:VA1", "one_deq_air_site_inside")

    no_id = Site([Application("E-2024-1", {}, square)], square)
    link_held([no_id], [{**h, "PLA_ICIS_ID": None}])
    assert held_link(no_id) == (None, "deq_air_site_without_icis_id")
    assert held_link(Site([Application("F-2024-1", {}, square)], square)) == (None, None)


def test_the_resolver_folds_linked_sites_into_held_records_and_nothing_else(tmp_path: pathlib.Path):
    """`pipeline.resolve.run` over this frame plus held records: the five linked sites pair with the
    facility they cite (D3), and no fuzzy pass pairs a Loudoun site with an operator's other campus."""
    _, _, _, df = _run()
    base = df.iloc[0]

    def held(rid: str, source: str, name: str, ref: str) -> dict[str, Any]:
        row = {c: base[c] for c in df.columns}
        row.update(
            record_id=f"{source}:{rid}",
            source_id=source,
            source_record_id=rid,
            name_canonical=name,
            name_norm=norm_name(name),
            lifecycle_state="built",
            status_raw="Operating",
            cross_refs=ref,
            raw="{}",
        )
        return row

    deq, icis = "us.va.deq.data_center_air_sites", "us.epa.echo.icis_air"
    extra = pd.DataFrame(
        [
            held("74205", deq, "Amazon Data Services Inc IAD-120/121/122", "icis_air:VA0000005110774205"),
            held(
                "VA0000005110774205",
                icis,
                "AMAZON DATA SERVICES INC IAD-120/121/122",
                "icis_air:VA0000005110774205",
            ),
            held("74296", deq, "Kincora Data Center", "icis_air:VA0000005110774296"),
            # another campus of the same operator, same county, a different facility id
            held("74999", deq, "Amazon Data Services Inc IAD-999", "icis_air:VA0000005110799999"),
            held("74998", deq, "Vantage Data Centers VA31", "icis_air:VA0000005110799998"),
        ]
    )
    frame = pd.concat([df, extra], ignore_index=True)
    path = tmp_path / "normalized.parquet"
    frame.to_parquet(path, index=False)
    matches, _ = resolve.run(MERGE_SCORE_THRESHOLD, path)
    mine = matches[(matches["left_source"] == SOURCE_ID) | (matches["right_source"] == SOURCE_ID)]
    accepted = mine[mine["accepted"]]
    pairs = {
        tuple(sorted((str(a), str(b))))
        for a, b in zip(accepted["left_id"], accepted["right_id"], strict=True)
    }
    assert pairs == {
        (f"{deq}:74205", f"{SOURCE_ID}:EPLAN-2025-0102"),
        (f"{icis}:VA0000005110774205", f"{SOURCE_ID}:EPLAN-2025-0102"),
        (f"{deq}:74296", f"{SOURCE_ID}:STPL-2022-0012"),
    }
    assert set(accepted["pass"]) == {"D3_xref"}


# ------------------------------------------------------------------------------------------- location
def test_points_lie_inside_the_land_at_six_decimals():
    _, _, rows, _ = _run()
    doc = _doc()
    land = {}
    for page in doc["pages"]:
        for feat in page["features"]:
            land[canon_application(feat["attributes"]["Application"])] = lou.polygon_from_rings(
                feat["geometry"]["rings"]
            )
    for r in rows:
        lat, lon = r["Latitude"], r["Longitude"]
        assert round(lat, 6) == lat and round(lon, 6) == lon
        assert 38.8 < lat < 39.4 and -77.97 < lon < -77.3  # Loudoun County
        assert any(land[a["Application"]].buffer(1e-6).covers(Point(lon, lat)) for a in r["applications"])
    assert "rings" not in json.dumps(rows)  # parcel outlines are not stored in rows


def test_inside_point_falls_back_when_the_centroid_is_outside():
    u_shape = Polygon([(0, 0), (0, 3), (1, 3), (1, 1), (2, 1), (2, 3), (3, 3), (3, 0)])
    assert not u_shape.covers(u_shape.centroid)
    lon, lat = inside_point(u_shape)
    assert u_shape.covers(Point(lon, lat))


def test_esri_rings_build_outer_land_minus_holes():
    outer = [[0, 0], [0, 4], [4, 4], [4, 0], [0, 0]]  # clockwise: land
    hole = [[1, 1], [3, 1], [3, 3], [1, 3], [1, 1]]  # counter-clockwise: a hole
    geom = lou.polygon_from_rings([outer, hole])
    assert geom.area == pytest.approx(12.0)
    assert not geom.covers(Point(2, 2))
    assert lou.polygon_from_rings([]) is None
    assert lou.polygon_from_rings([hole]) is None  # a hole alone is no land


# -------------------------------------------------------------------------------------- personal data
def test_subdivisions_are_kept_only_when_their_structure_proves_an_organisation():
    assert keep_subdivision("LOUDOUN TECH CENTER H1") == "LOUDOUN TECH CENTER H1"
    assert keep_subdivision("VANTAGE DATA CENTERS VA31") == "VANTAGE DATA CENTERS VA31"
    assert keep_subdivision(MADE_UP_PERSON) is None
    assert keep_subdivision("BEAUMEADE") is None  # no structure: withheld, though it is a business park
    assert keep_subdivision("  ") is None and keep_subdivision(None) is None
    _, _, rows, df = _run()
    pair = next(r for r in rows if r["site_key"] == "EPLAN-2023-0083")
    assert [a["FIRST_Subdivision"] for a in pair["applications"]] == [None, None]
    assert pair["subdivisions_withheld"] == 2  # from the flag: the value itself was never stored
    assert sum(r["subdivisions_withheld"] for r in rows) == 4
    # "BEAUMEADE" alone has no structure, so it is withheld before storage; "BEAUMEADE PARCEL C2" is kept
    assert '"BEAUMEADE"' not in FIXTURE.read_text(encoding="utf-8")
    assert "BEAUMEADE" not in " ".join(df["raw"]).replace("BEAUMEADE PARCEL C2", "")


def test_a_person_shaped_subdivision_is_withheld_before_the_snapshot_is_stored():
    c = connector_for(SOURCE_ID)
    feat: dict[str, Any] = {
        "attributes": {"Application": "STPL-2024-0001", "FIRST_Subdivision": MADE_UP_PERSON}
    }
    assert withhold_subdivision(feat) is True
    assert feat["attributes"]["FIRST_Subdivision"] is None and feat[WITHHELD_FLAG] is True
    kept: dict[str, Any] = {"attributes": {"FIRST_Subdivision": "LOUDOUN TECH CENTER H1"}}
    assert withhold_subdivision(kept) is False and WITHHELD_FLAG not in kept
    assert withhold_subdivision({"attributes": {"FIRST_Subdivision": "  "}}) is False
    # redact, the runner's last step before storage, withholds it from bytes that reach it another way
    doc = _doc()
    feats = doc["pages"][0]["features"]
    i = next(n for n, f in enumerate(feats) if not f.get(WITHHELD_FLAG))
    feats[i]["attributes"]["FIRST_Subdivision"] = MADE_UP_PERSON
    cleaned = c.redact(json.dumps(doc).encode())
    assert MADE_UP_PERSON.encode() not in cleaned
    assert json.loads(cleaned)["pages"][0]["features"][i][WITHHELD_FLAG] is True
    rows = c.parse(_snapshot(cleaned))
    assert sum(r["subdivisions_withheld"] for r in rows) == 5  # the fixture's four and this one


def test_a_person_shaped_subdivision_never_reaches_a_row():
    doc = _doc()
    feat = doc["pages"][0]["features"][0]
    feat["attributes"]["FIRST_Subdivision"] = MADE_UP_PERSON
    _, _, rows, df = _run(json.dumps(doc).encode())
    assert MADE_UP_PERSON not in json.dumps(rows)
    assert not df["name_canonical"].str.contains("QUILLFEATHER").any()
    assert not df["raw"].str.contains("QUILLFEATHER").any()


def test_owner_and_editor_columns_never_reach_the_snapshot_or_a_row():
    c = connector_for(SOURCE_ID)
    doc = _doc()
    feat = doc["pages"][0]["features"][0]
    feat["attributes"].update(
        {"Owner": MADE_UP_PERSON, "OwnerName2": MADE_UP_PERSON, "Creator": MADE_UP_ACCOUNT}
    )
    content = json.dumps(doc).encode()
    cleaned = c.redact(content)
    assert MADE_UP_PERSON.encode() not in cleaned and MADE_UP_ACCOUNT.encode() not in cleaned
    assert set(json.loads(cleaned)["pages"][0]["features"][0]["attributes"]) <= set(FIELDS)
    rows = c.parse(_snapshot(content))  # parse drops them too, whatever was stored
    assert MADE_UP_PERSON not in json.dumps(rows) and MADE_UP_ACCOUNT not in json.dumps(rows)
    assert c.redact(FIXTURE.read_bytes()) == FIXTURE.read_bytes()  # nothing to strip: bytes untouched


def test_the_fixture_holds_only_the_requested_fields():
    doc = _doc()
    attrs = {k for p in doc["pages"] for f in p["features"] for k in f["attributes"]}
    assert attrs == set(FIELDS)
    held = {k for p in doc["held"]["pages"] for f in p["features"] for k in f["attributes"]}
    assert held == set(lou.DEQ_FIELDS)  # no facility name, no staff column
    assert set(doc["terms"]) == {"item", "licence", "sha256"}  # the item's account field is never kept
    assert licence_sha256(doc["terms"]["licence"]) == LICENCE_SHA256 == doc["terms"]["sha256"]
    # every subdivision the fixture still holds is one the rule keeps; the withheld ones are null + flagged
    feats = [f for p in doc["pages"] for f in p["features"]]
    for f in feats:
        value = f["attributes"]["FIRST_Subdivision"]
        assert not str(value or "").strip() or keep_subdivision(value) is not None, value
        assert set(f) <= {"attributes", "geometry", WITHHELD_FLAG}
    flagged = [f for f in feats if f.get(WITHHELD_FLAG)]
    assert len(flagged) == 4 and all(f["attributes"]["FIRST_Subdivision"] is None for f in flagged)


# ----------------------------------------------------------------------------------------------- parse
def test_error_body_and_layout_change_are_parse_errors():
    c = connector_for(SOURCE_ID)
    for body in (
        b"<html>not json</html>",
        b'{"pages": [], "held": {"pages": []}}',
        b'{"pages": [{"error": {"code": 400}}], "held": {"pages": []}}',
        b'{"pages": [{"features": [{"attributes": {"Application": "X"}}]}], "held": {"pages": []}}',
        b'{"pages": [{"features": []}]}',
        b'{"pages": [{"nofeatures": 1}], "held": {"pages": []}}',
        b'{"pages": [{"features": []}], "held": {"pages": [{"x": 1}]}}',
    ):
        with pytest.raises(ParseError):
            c.parse(_snapshot(body))


def test_rows_without_a_number_or_land_are_counted_not_kept():
    doc = _doc()
    feats = doc["pages"][0]["features"]
    feats[0]["attributes"]["Application"] = "  "
    feats[1]["geometry"] = {"rings": []}
    _, raw, _, _ = _run(json.dumps(doc).encode())
    assert raw.meta["skipped"] == {"no_application": 1, "no_geometry": 1}
    assert raw.meta["applications"] == 22


# ----------------------------------------------------------------------------------------------- fetch
class FakeResponse:
    def __init__(self, status: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.content = body
        self.text = body.decode("utf-8", "replace")
        self.headers = headers or {"Content-Type": "application/json"}

    def json(self) -> Any:
        return json.loads(self.content)

    def close(self) -> None:
        pass


class FakeArcGIS:
    """The four endpoints the connector reads, served from the fixture's own rows. The item carries a
    made-up account name and the layer rows a made-up owner, which must never be stored. Where the
    fixture's subdivision was withheld, the fake serves a made-up person's name in its place, as the
    county would serve the real value, and drops the flag (the county never sends it)."""

    def __init__(self) -> None:
        doc = _doc()
        self.licence = doc["terms"]["licence"]
        self.item_url = SERVICE_URL
        self.features = []
        for f in doc["pages"][0]["features"]:
            attrs = dict(f["attributes"])
            if f.get(WITHHELD_FLAG):
                attrs["FIRST_Subdivision"] = MADE_UP_PERSON
            self.features.append({"attributes": attrs, "geometry": f["geometry"]})
        self.held = doc["held"]["pages"][0]["features"]
        self.fields = list(FIELDS)
        self.error: dict[str, Any] | None = None
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.headers: dict[str, str] = {}

    def request(self, method: str, url: str, params: dict[str, str] | None = None, **kw: Any) -> FakeResponse:
        params = dict(params or {})
        self.calls.append((url, params))
        if url.endswith("/robots.txt"):
            return FakeResponse(404, b"not found", {"Content-Type": "text/html"})
        if url == ITEM_URL:
            body: dict[str, Any] = {
                "url": self.item_url,
                "licenseInfo": self.licence,
                "owner": MADE_UP_ACCOUNT,
            }
        elif url == LAYER_URL:
            body = {
                "name": "Proposed_DC_Parcels_March1_SINGLE",
                "fields": [{"name": f} for f in self.fields],
                "editingInfo": {"dataLastEditDate": 1779394229747},
            }
        elif url == QUERY_URL:
            assert params["outFields"] == ",".join(FIELDS) and "*" not in params["outFields"]
            offset = int(params["resultOffset"])
            chunk = self.features[offset : offset + 15]
            served = [{**f, "attributes": {**f["attributes"], "Owner": MADE_UP_PERSON}} for f in chunk]
            body = {"features": served, "exceededTransferLimit": offset + 15 < len(self.features)}
        elif url == DEQ_QUERY_URL:
            assert "PLA_DATA_CENTER_YN = 'Y'" in params["where"]
            body = {"features": self.held}
        else:
            raise AssertionError(f"unexpected URL {url}")
        if self.error is not None and url == QUERY_URL:
            body = {"error": self.error}
        return FakeResponse(200, json.dumps(body).encode())

    def get(self, url: str, **kw: Any) -> FakeResponse:
        return self.request("GET", url, **kw)


def _http(fake: FakeArcGIS) -> PoliteSession:
    return PoliteSession(session=fake, sleep=lambda s: None)  # type: ignore[arg-type]


def test_fetch_reads_terms_layer_rows_and_held_sites_and_stores_only_what_it_needs():
    fake = FakeArcGIS()
    c = connector_for(SOURCE_ID, http=_http(fake))
    c.clock = lambda: RETRIEVED
    raw = c.fetch()
    hosts = [urlsplit(u).netloc for u, _ in fake.calls if not u.endswith("/robots.txt")]
    assert hosts == [
        "www.arcgis.com",
        "services1.arcgis.com",
        "services1.arcgis.com",
        "services1.arcgis.com",
        "gisdata.deq.virginia.gov",
    ]
    assert raw.meta["pages"] == 2 and raw.meta["licence_sha256"] == LICENCE_SHA256
    assert raw.meta["data_last_edit"] == "2026-05-21T20:10:29+00:00"
    assert raw.retrieved_at == RETRIEVED
    for secret in (MADE_UP_PERSON, MADE_UP_ACCOUNT):
        assert secret.encode() not in raw.content
    assert raw.content.count(f'"{WITHHELD_FLAG}": true'.encode()) == 4
    df = c.normalize(c.parse(raw), raw)
    _, _, _, expected = _run()
    assert df["record_id"].tolist() == expected["record_id"].tolist()
    assert df["cross_refs"].tolist() == expected["cross_refs"].tolist()
    assert df["raw"].tolist() == expected["raw"].tolist()  # the fetched rows are the fixture's rows
    assert (
        c.http.rate_limits["www.arcgis.com"] == 0.5 and c.http.rate_limits["gisdata.deq.virginia.gov"] == 0.5
    )


def test_a_changed_licence_or_item_blocks_the_run_before_any_row_is_read():
    fake = FakeArcGIS()
    fake.licence = fake.licence.replace("available to the public", "for internal county use only")
    with pytest.raises(BlockedError, match="licence"):
        connector_for(SOURCE_ID, http=_http(fake)).fetch()
    assert all(u in (ITEM_URL,) or u.endswith("/robots.txt") for u, _ in fake.calls)
    moved = FakeArcGIS()
    moved.item_url = "https://services1.arcgis.com/elsewhere/FeatureServer"
    with pytest.raises(BlockedError, match="not the reviewed layer"):
        connector_for(SOURCE_ID, http=_http(moved)).fetch()


def test_a_missing_field_or_an_error_body_fails_the_run():
    fake = FakeArcGIS()
    fake.fields.remove("FIRST_Status")
    with pytest.raises(ConnectorError, match="lacks"):
        connector_for(SOURCE_ID, http=_http(fake)).fetch()
    broken = FakeArcGIS()
    broken.error = {"code": 400, "message": "Invalid query"}
    with pytest.raises(ConnectorError, match="error"):
        connector_for(SOURCE_ID, http=_http(broken)).fetch()


def test_an_end_to_end_run_passes_its_gates_and_an_unchanged_layer_is_unchanged(tmp_path: pathlib.Path):
    fake = FakeArcGIS()
    store = Store(tmp_path)
    first = run(SOURCE_ID, registry=Registry(), store=store, http=_http(fake), now=RETRIEVED)
    assert first.status == "ok", first
    assert first.run["rows_seen"] == 20 and first.run["dq_status"] == "pass"
    second = run(
        SOURCE_ID, registry=Registry(), store=store, http=_http(fake), now=RETRIEVED + dt.timedelta(days=7)
    )
    assert second.status == "unchanged", second


# --------------------------------------------------------------------------------- owner letters
# The owner actions this lane drafted (docs/66-owner-actions.md items 26 and 27, letters A and B),
# against the register. Review of 2026-10-24: (1) the letters denied any download or use while
# data/sources.yaml records that the survey read both sources on 2026-10-23; a letter the owner signs
# says what was read and when, and says nothing appears on Inframation only while that is true;
# (2) the lane's item numbers clashed with rows already on the branch, so each register reference to
# "item N, letter X" pointed at another action; (3) letter B and its register row said no copy of the
# advisories was kept while the survey's downloads were still held, so a letter may say copies were
# deleted only where the register records that deletion with its date; (4) letter A opened "I run
# Inframation" and signed for another entity, so both letters use the docs/39 §0.1 sender block and
# nothing else. Reads the three documents and the register only.
REPO = pathlib.Path(__file__).resolve().parents[3]
OWNER_ACTIONS = REPO / "docs" / "66-owner-actions.md"
SOURCES = REPO / "data" / "sources.yaml"
LEGAL = REPO / "docs" / "13-legal-data-rights.md"
SEND_KIT = REPO / "docs" / "39-utility-rfp-permission-requests.md"
#: The register rows whose letters this lane drafted.
LETTER_ROWS = {"A": "us.va.pwc.data_center_campuses", "B": "us.ga.epd.air_permit_advisories"}
#: "item 26, letter A" in a register row or a docs/13 row, after a docs/66 mention.
ITEM_REF = re.compile(r"docs/66(?:-owner-actions\.md)?\W{1,3}(?:\()?items? (\d+), letter ([A-Z])\b")
#: Phrases that deny any earlier read of the source.
DENIALS = re.compile(
    r"\b(?:have|has) not (?:yet )?(?:downloaded|read|used|accessed|opened|fetched|parsed|looked at)\b"
    r"|\bnever (?:downloaded|read|used|accessed|opened)\b",
    re.IGNORECASE,
)
SURVEY_READ = re.compile(r"[Ss]urvey reads?\b.{0,120}?(\d{4}-\d{2}-\d{2})", re.DOTALL)
NOTHING_SHOWN = re.compile(r"nothing from (?:them|the layers?|it) appears on Inframation", re.IGNORECASE)
#: A claim that no copy of the source's data is held: "kept no copy", "no copy is kept", "deleted the copy".
NO_COPY = re.compile(
    r"\bkept no cop(?:y|ies)\b|\bno cop(?:y|ies) (?:of [\w ]+ )?(?:is|was|were|are) kept\b"
    r"|\bdeleted (?:the|those|these|our|its|all) (?:\w+ )?cop(?:y|ies)\b",
    re.IGNORECASE,
)
#: The register's record of a deletion: "... deleted after counting on 2026-10-23".
DELETION = re.compile(r"\bdeleted\b[^.;]{0,80}?(\d{4}-\d{2}-\d{2})")
#: A sender placeholder as docs/39 §0.1 writes it, in backticks in its table: `[Owner name]`.
SENDER_PLACEHOLDER = re.compile(r"`(\[[^\]`]+\])`")
#: A placeholder in a letter body: "[Owner name]", "[Role]", "[Your company]".
LETTER_PLACEHOLDER = re.compile(r"\[[A-Z][A-Za-z]*(?: [A-Za-z]+)*\]")


def _owner_actions() -> str:
    return OWNER_ACTIONS.read_text(encoding="utf-8")


def _action_rows(text: str) -> list[tuple[str, str]]:
    """(number, row) for every row of the `| # | Action | ...` tables."""
    rows: list[tuple[str, str]] = []
    in_table = False
    for line in text.splitlines():
        if line.startswith("| # | Action |"):
            in_table = True
            continue
        if in_table and not line.startswith("|"):
            in_table = False
        if in_table and not line.startswith("|---"):
            rows.append((line.split("|")[1].strip(), line))
    return rows


def _letters(text: str) -> dict[str, str]:
    """Letter id -> its section, from its `### X.` heading to the next heading."""
    out: dict[str, str] = {}
    for m in re.finditer(r"^### ([A-Z])\. .*?(?=^##)", text, re.MULTILINE | re.DOTALL):
        out[m.group(1)] = m.group(0)
    return out


def _body(section: str) -> str:
    """The quoted letter itself (the `> ` lines), joined."""
    return " ".join(line[1:].strip() for line in section.splitlines() if line.startswith(">"))


def _register() -> dict[str, dict[str, object]]:
    doc = yaml.safe_load(SOURCES.read_text(encoding="utf-8"))
    return {str(s["id"]): s for s in doc["sources"]}


def _as_letter_date(iso: str) -> str:
    d = dt.date.fromisoformat(iso)
    return f"{d.day} {d.strftime('%B')} {d.year}"


def test_action_numbers_are_unique():
    numbers = [n for n, _ in _action_rows(_owner_actions())]
    assert numbers, "no action table found in docs/66"
    duplicates = sorted({n for n in numbers if numbers.count(n) > 1})
    assert not duplicates, f"docs/66 action numbers used twice: {duplicates}"


def test_each_register_reference_lands_on_the_action_that_sends_that_letter():
    text = _owner_actions()
    actions: dict[str, list[str]] = {}
    for number, line in _action_rows(text):
        actions.setdefault(number, []).append(line)
    letters = _letters(text)
    register = _register()
    legal_rows = [line for line in LEGAL.read_text(encoding="utf-8").splitlines() if line.startswith("| `")]
    refs: list[tuple[str, str, str]] = []
    for source_id, row in register.items():
        for m in ITEM_REF.finditer(yaml.safe_dump(row, width=10_000)):
            refs.append((source_id, m.group(1), m.group(2)))
    for line in legal_rows:
        source_id = line.split("`")[1]
        for m in ITEM_REF.finditer(line):
            refs.append((source_id, m.group(1), m.group(2)))
    assert {(s, letter) for s, _, letter in refs} >= {(s, letter) for letter, s in LETTER_ROWS.items()}
    for source_id, number, letter in refs:
        assert number in actions, f"{source_id} cites docs/66 item {number}, which does not exist"
        for action in actions[number]:  # every row with that number, so a clash cannot hide
            assert f"letter {letter}" in action, (
                f"{source_id} cites docs/66 item {number}, letter {letter}; an item {number} sends no "
                f"letter {letter}: {action[:120]}"
            )
        assert letter in letters, f"docs/66 has no letter {letter}"
        assert f"Register row: `{source_id}`" in letters[letter], (
            f"letter {letter} does not name {source_id} as its register row"
        )


@pytest.mark.parametrize("letter", sorted(LETTER_ROWS))
def test_a_letter_states_the_earlier_read_and_denies_none(letter: str):
    section = _letters(_owner_actions())[letter]
    source_id = LETTER_ROWS[letter]
    recorded = yaml.safe_dump(_register()[source_id].get("verified"), width=10_000)
    reads = SURVEY_READ.findall(recorded)
    assert reads, f"{source_id}: the register records no survey read; this test expects one"
    body = _body(section)
    denial = DENIALS.search(body)
    assert denial is None, f"letter {letter} denies an earlier read the register records: {denial.group(0)!r}"
    for iso in sorted(set(reads)):
        assert _as_letter_date(iso) in body, f"letter {letter} does not say the source was read on {iso}"
    assert f"Register row: `{source_id}`" in section


@pytest.mark.parametrize("letter", sorted(LETTER_ROWS))
def test_nothing_appears_on_the_site_only_while_true(letter: str):
    """A letter may tell the agency nothing from its data is on the site only while no connector
    exists for that source or the register publishes nothing from it. When one ships before the
    letter is sent, the sentence is updated first (docs/66, the letters' header)."""
    body = _body(_letters(_owner_actions())[letter])
    entry = Registry().get(LETTER_ROWS[letter])
    if NOTHING_SHOWN.search(body):
        assert entry.publication == "none" or not entry.implemented, (
            f"letter {letter} says nothing appears on Inframation, but {entry.id} has a connector "
            f"and publication {entry.publication!r}"
        )


def _verified_note(source_id: str) -> str:
    return yaml.safe_dump(_register()[source_id].get("verified"), width=10_000)


@pytest.mark.parametrize("letter", sorted(LETTER_ROWS))
def test_a_letter_says_copies_were_deleted_only_where_the_register_records_it(letter: str):
    """Review of 2026-10-24 (round 2): letter B said "we kept no copy" of the Georgia advisories, and its
    register row "No copy of them is kept", while the survey's downloads were still held. A letter, or
    the register row behind it, may say no copy is held only beside a dated record of the deletion, and
    "the same day" only when that date is the day of the read."""
    source_id = LETTER_ROWS[letter]
    body = _body(_letters(_owner_actions())[letter])
    note = _verified_note(source_id)
    deleted = sorted(set(DELETION.findall(note)))
    for text, where in ((body, f"letter {letter}"), (note, f"{source_id} verified note")):
        claim = NO_COPY.search(text)
        if claim:
            assert deleted, (
                f"{where} says {claim.group(0)!r}, but the register records no dated deletion for {source_id}"
            )
    if NO_COPY.search(body) and "the same day" in body:
        assert deleted == sorted(set(SURVEY_READ.findall(note))), (
            f"letter {letter} says the copies were deleted the same day as the read; the register says "
            f"read {SURVEY_READ.findall(note)}, deleted {deleted}"
        )


def _sender_placeholders() -> set[str]:
    """The placeholders of the docs/39 §0.1 sender block, from its table."""
    text = SEND_KIT.read_text(encoding="utf-8")
    m = re.search(r"^### 0\.1 .*?(?=^##)", text, re.MULTILINE | re.DOTALL)
    assert m, "docs/39 §0.1 (the sender block) not found"
    rows = [line for line in m.group(0).splitlines() if line.startswith("| `")]
    return {p for line in rows for p in SENDER_PLACEHOLDER.findall(line)}


@pytest.mark.parametrize("letter", sorted(LETTER_ROWS))
def test_a_letter_writes_as_the_docs39_sender_and_no_other_entity(letter: str):
    """Review of 2026-10-24 (round 2): letter A opened "I run Inframation" and its first draft signed for
    "[Your company]". The owner writes as a named person holding a role at Inframation, which
    [Organisation] runs (docs/39 §0.1): the letter carries exactly those placeholders, opens and signs as
    the send kit's drafts do, and names no other sender."""
    body = _body(_letters(_owner_actions())[letter])
    expected = _sender_placeholders()
    assert {"[Owner name]", "[Role]", "[Organisation]", "[Postal address]"} <= expected
    assert set(LETTER_PLACEHOLDER.findall(body)) == expected
    assert "I'm [Owner name], [Role] at Inframation (https://inframation.io)" in body
    assert "Inframation is run by [Organisation]." in body
    assert "[Owner name] [Role], Inframation ([Organisation])" in body  # the signature lines, joined
    assert not re.search(r"\bI run Inframation\b|\bwhich runs Inframation\b", body)
