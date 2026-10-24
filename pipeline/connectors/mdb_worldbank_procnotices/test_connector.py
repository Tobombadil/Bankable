"""Parser tests for mdb.worldbank.procnotices against a recorded API response."""

from __future__ import annotations

import json

import pytest

from conftest import connector_for, snapshot
from pipeline.connectors.base import ParseError
from pipeline.connectors.mdb_worldbank_procnotices.connector import (
    REGION_NAMES,
    SECTOR_CODES,
    WB_REGION_CODES,
    jurisdiction,
    technologies,
)
from pipeline.connectors.opportunity import work_profile

SOURCE_ID = "mdb.worldbank.procnotices"
URL = "https://search.worldbank.org/api/v2/procnotices"


@pytest.fixture(scope="module")
def parsed():
    c = connector_for(SOURCE_ID)
    raw = snapshot("worldbank_procnotices.json", URL, "application/json")
    rows = c.parse(raw)
    return c, raw, rows, c.normalize(rows, raw)


def test_the_confirmed_sector_filter_is_the_dotted_path():
    c = connector_for(SOURCE_ID)
    params = c.params(0)
    assert "sector.sector_code" in params
    assert params["sector.sector_code"] == "^".join(SECTOR_CODES)
    assert "LU" in SECTOR_CODES and "LT" in SECTOR_CODES


def test_every_recorded_notice_is_in_an_energy_sector(parsed):
    _, _, rows, _ = parsed
    for r in rows:
        codes = {s["sector_code"] for s in r.get("sector") or []}
        assert codes & set(SECTOR_CODES)


def test_notice_id_is_the_record_id(parsed):
    _, _, rows, df = parsed
    assert set(df["source_record_id"]) == {str(r["id"]) for r in rows}
    assert not df["record_id"].duplicated().any()


def test_notice_types_map_onto_the_opportunity_vocabulary(parsed):
    _, _, _, df = parsed
    assert set(df["status"]) <= {"announced", "open", "closed", "cancelled", "awarded", "unknown"}
    assert (df.loc[df["status_raw"] == "Contract Award", "status"] == "awarded").all()
    assert df["status_rule"].str.startswith("worldbank.").all()


def test_an_open_notice_past_its_deadline_is_closed(parsed):
    _, _, _, df = parsed
    open_types = df[df["status_raw"].isin(["Invitation for Bids", "Request for Expression of Interest"])]
    passed = open_types[open_types["due_at"] < open_types["retrieved_at"].iloc[0]]
    assert (passed["status"] == "closed").all()


def test_contact_personal_data_is_dropped_at_parse_and_redact(parsed):
    c, _, rows, _ = parsed
    assert not any(k.startswith("contact_") and k != "contact_organization" for r in rows for k in r)
    payload = json.dumps(
        {
            "pages": [
                {
                    "procnotices": [
                        {
                            "id": "OP1",
                            "contact_email": "a@b.org",
                            "contact_name": "A Person",
                            "contact_organization": "Ministry",
                        }
                    ]
                }
            ]
        }
    ).encode()
    cleaned = json.loads(c.redact(payload))
    notice = cleaned["pages"][0]["procnotices"][0]
    assert "contact_email" not in notice and "contact_name" not in notice
    assert notice["contact_organization"] == "Ministry"


def test_sector_codes_and_country_reach_the_canonical_record(parsed):
    _, _, _, df = parsed
    ids = json.loads(df["identifiers"].iloc[0])
    assert ids["sector_codes"]
    assert df["jurisdiction"].map(lambda j: len(j) == 2 or j.startswith("region:") or j == "global").all()


@pytest.mark.parametrize(
    ("code", "expected"),
    [
        # docs/28 B-17: the Bank's own country codes (names read from the API on 2026-10-24).
        ("ZR", "CD"),
        ("TP", "TL"),
        ("GZ", "PS"),
        ("RY", "YE"),
        ("TJ", "TJ"),
        ("XK", "XK"),
        # Regional projects: a region token, outside any country list.
        ("3W", "region:western_central_africa"),
        ("3E", "region:eastern_southern_africa"),
        ("6R", "region:caribbean"),
        ("1W", "region:world"),
        ("9Z", "region:wb_9z"),
        ("", "global"),
        (None, "global"),
    ],
)
def test_world_bank_codes_become_iso_countries_or_region_tokens(code, expected):
    assert jurisdiction(code) == expected


def test_every_region_token_is_named():
    assert set(WB_REGION_CODES.values()) == set(REGION_NAMES)


@pytest.mark.parametrize(
    ("title", "sectors", "expected"),
    [
        # The project's sector is context, not the notice's technology (2026-10-23 register).
        ("Social Specialist", ["LT", "LU"], []),
        ("Microsoft 365 E3 Office (150 licenses)", ["LZ", "LP", "LT"], []),
        ("Adquisición de Útiles de Oficina", ["LT", "LP"], []),
        ("Regulatory Development Consultancy", ["LP", "LU", "LZ"], []),
        # ... and it adds its technology where the notice's own words point at it.
        (
            "Procurement of Line Installation Contractors - (Batch 2) -Lots 9-11",
            ["LT", "LP", "LZ", "LU"],
            ["transmission"],
        ),
        (
            "lot-10: 33KV/0.4 distribution Network Construction/EEU-PPMO-OGU/ADELE/NCB-004 - RE2",
            ["LU", "LP", "LT"],
            ["transmission"],
        ),
        ("Lenders Technical Advisor of the ROGUN HPP (LTA)", ["LH"], ["hydro"]),
        (
            "Package 2:Hydro electro Mechanical (HEM)Works for the Mt. Coffee Hydropower Plant Extension",
            ["LU", "LZ", "LP", "LT"],
            ["hydro"],
        ),
        (
            "Redesign, Supply, Installation, Testing, and Commissioning of replaced "
            "Wooden Poles with Lattice Towers "
            "for 11kV and 33kV Overhead Lines in Swampy Areas, River and Road Crossings",
            ["LH", "LT"],
            ["transmission"],
        ),
        (
            "Fourniture et distribution de kits solaires domestiques (kits SHS)",
            ["LU", "LB", "LP", "AT"],
            ["solar_pv"],
        ),
    ],
)
def test_sector_technologies_need_the_notices_own_words(title, sectors, expected):
    assert technologies(title, sectors) == expected


def test_the_procurement_group_decides_the_type_of_work(parsed):
    """`procurement_group` (CS consulting, CW works, GO goods) is the structured field the type
    of work is read from first (owner, 2026-10-24)."""
    _, _, rows, df = parsed
    by_id = {str(r["id"]): r for r in rows}
    seen = set()
    for rec in df.to_dict("records"):
        raw = by_id[rec["source_record_id"]]
        notice = {**rec, "source_id": SOURCE_ID, "identifiers": json.loads(rec["identifiers"])}
        profile = work_profile(notice, raw)
        group = raw.get("procurement_group")
        if group == "CS":
            assert "services" in profile.work_type_keys and "consultants" in profile.audience_keys
        elif group == "CW":
            assert "works" in profile.work_type_keys and "contractors" in profile.audience_keys
        elif group == "GO":
            # Goods are equipment only when the title names plant (review of 2026-10-24, finding
            # 4): the gas generators are; software and unnamed "équipements" are `other`.
            expected = GOODS_WORK[str(rec["title"]).split()[0]]
            assert profile.work_type_keys == expected, rec["title"]
        seen.add(group)
        assert not profile.unknown
    assert {"CS", "CW", "GO"} <= seen


#: The recorded fixture's goods notices, by their title's first word.
GOODS_WORK = {
    "Gas": ["equipment"],  # "Gas cogeneration units and gas generators"
    "Supply": ["other"],  # "Supply and Installation of PSSE Software Power System Modeling ..."
    "Fourniture": ["other"],  # "Fourniture et Mise en Exploitation des équipements ... Revenus"
}


def test_an_unaccented_reseau_is_a_grid_cue():
    """Review of 2026-10-24: "RESEAU SNEL" (upper case, no accent) missed the `réseaux?` cue."""
    title = "TRAVAUX DE REHABILITATION DU RESEAU SNEL DE LA VILLE DE GBADOLITE"
    assert technologies(title, ["LT"]) == ["transmission"]


def test_a_page_without_notices_is_a_parse_error():
    c = connector_for(SOURCE_ID)
    raw = snapshot("worldbank_procnotices.json", URL, "application/json")
    raw.content = json.dumps({"pages": [{"error": "bad"}]}).encode()
    with pytest.raises(ParseError):
        c.parse(raw)


def test_a_zero_or_negative_estimate_is_an_unknown_budget(parsed):
    c, raw, rows, _ = parsed
    edited = [dict(r) for r in rows]
    edited[0].update({"bid_estimate_amount": "0", "bid_currency_code": "USD"})
    edited[1].update({"bid_estimate_amount": -1, "bid_currency_code": "USD"})
    edited[2].update({"bid_estimate_amount": "150000", "bid_currency_code": "USD"})
    df = c.normalize(edited, raw)
    assert df["budget_amount"].isna()[0] and df["budget_amount"].isna()[1]
    assert df.loc[2, "budget_amount"] == 150000.0
    assert list(df.loc[:1, "budget_currency"]) == ["USD", "USD"]


def test_the_bid_estimate_is_labelled_an_estimate_on_every_notice_type(parsed):
    """Lane rfp-value: `bid_estimate_amount` is the package's estimate on a contract award too."""
    _, _, _, df = parsed
    by_id = df.set_index("source_record_id")
    award = by_id.loc["OP00468071"]  # a Contract Award
    assert (award["budget_amount"], award["budget_currency"]) == (2_000_000.0, "USD")
    assert json.loads(award["identifiers"])["value"] == {
        "kind": "estimated_value",
        "source_field": "bid_estimate_amount",
    }
    bids = by_id.loc["OP00468087"]  # an Invitation for Bids
    assert json.loads(bids["identifiers"])["value"]["kind"] == "estimated_value"
    assert bids["budget_amount"] == 16_661_000.0
