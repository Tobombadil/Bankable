"""Tests for `services/ingest/geocode.py`: the pre-existing US county/state tier (`country=None`,
unchanged) and the GB substation tier added for Sprint 3 item 5 (docs/21 §3.7; `services/ingest/
data/README.md`).

Coverage numbers below were measured 2026-09-13 against `tests/fixtures/neso_tec_register.csv`
(the connector's own fixture) and, separately, the live TEC register fetched through the
connector's own CKAN `package_show` -> CSV path (`services/ingest/data/README.md` records the full
figures: 346/1,237 distinct sites, 739/2,198 rows). Only the fixture number is asserted here so
this test does not depend on network access; it will need updating if the fixture is ever changed.
"""

from __future__ import annotations

import csv
import tempfile
from pathlib import Path

import pytest

from services.ingest.geocode import (
    GB_SETTLEMENT_OUTCOMES,
    GB_TRANSMISSION_OWNER_REGIONS,
    CountyGazetteer,
    SettlementGazetteer,
    SubstationGazetteer,
    default_settlement_gazetteer,
    default_substation_gazetteer,
    gb_settlement_key,
    gb_settlement_match,
    geocode,
    normalize_settlement_name,
    normalize_substation_name,
)

FIXTURE = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "neso_tec_register.csv"


def _write_settlement_tsv(rows: list[str], tmp_path: Path | None = None) -> Path:
    """A one-off `gb_settlements.tsv` in the same five-column shape as the vendored table."""
    target = (tmp_path or Path(tempfile.mkdtemp())) / "gb_settlements.tsv"
    target.write_text("name\tlat\tlon\tdistrict\tplaces\n" + "\n".join(rows) + "\n", encoding="utf-8")
    return target


def _fixture_connection_sites() -> list[str]:
    with FIXTURE.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    return sorted({r["Connection Site"] for r in rows if r.get("Connection Site")})


# ------------------------------------------------------------------ US tier, unchanged (country=None)
def test_geocode_us_county_unchanged_by_the_gb_addition() -> None:
    point, precision = geocode("TX", "Travis")
    assert precision == "county_centroid"
    assert point is not None
    lon, lat = point
    assert -99 < lon < -96
    assert 29 < lat < 31


def test_geocode_us_state_fallback_unchanged() -> None:
    point, precision = geocode("TX", "Not A Real County")
    assert precision == "state_centroid"
    assert point is not None


def test_geocode_us_unknown_unchanged() -> None:
    point, precision = geocode("ZZ", "Nowhere County")
    assert point is None
    assert precision == "unknown"


# -------------------------------------------------------------- county_fips (added 2026-09-15)
def test_county_gazetteer_returns_fips_for_a_known_county() -> None:
    gaz = CountyGazetteer.load()
    assert gaz.county_fips("TX", "Travis") == "48453"


def test_county_gazetteer_same_county_name_two_states_gets_two_different_fips() -> None:
    """ "Washington County" exists in many states -- the FIPS must be state-specific, never guessed
    from the name alone (docs/21 §3.7)."""
    gaz = CountyGazetteer.load()
    al_fips = gaz.county_fips("AL", "Washington")
    ar_fips = gaz.county_fips("AR", "Washington")
    assert al_fips == "01129"
    assert ar_fips == "05143"
    assert al_fips != ar_fips


def test_county_gazetteer_fips_is_none_for_an_unresolvable_county() -> None:
    gaz = CountyGazetteer.load()
    assert gaz.county_fips("TX", "Not A Real County") is None


def test_county_gazetteer_fips_is_none_without_state() -> None:
    gaz = CountyGazetteer.load()
    assert gaz.county_fips(None, "Travis") is None


def test_geocode_country_none_ignores_a_gb_looking_county_string() -> None:
    """A `country=None` call never takes the GB path, even if `county` happens to look like a
    substation name -- the US tier just fails to resolve it, same as any other unmatched county."""
    point, precision = geocode(None, "Berkswell GSP")
    assert point is None
    assert precision == "unknown"


# ------------------------------------------------------------------------------- gazetteer loading
def test_gazetteer_loads_the_vendored_tsv() -> None:
    gaz = SubstationGazetteer.load()
    assert len(gaz.points) > 300  # 371 rows vendored, ~363 distinct normalised keys


def test_default_substation_gazetteer_is_cached() -> None:
    assert default_substation_gazetteer() is default_substation_gazetteer()


# ------------------------------------------------------------------------- normalisation, on real
# ------------------------------------------------------------------------- fixture strings (>= 10)
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Berkswell GSP", "BERKSWELL"),
        ("Coatbridge GSP", "COATBRIDGE"),
        ("Grudie Bridge GSP", "GRUDIEBRIDGE"),
        ("Aberthaw 275kV Substation", "ABERTHAW"),
        ("Aberthaw B 275/132kV Substation", "ABERTHAWB"),
        ("Upperboat 132kV Substation", "UPPERBOAT"),  # one-word spelling variant of "Upper Boat"
        ("Clydesmill 275/33kV", "CLYDESMILL"),  # spelling variant of "Clyde's Mill"
        ("North Humber Connection Node C 132kV Substation", "NORTHHUMBERCONNECTIONNODEC"),
        ("SOUTH WALES WEST CONNECTION NODE A 132KV SUBSTATION", "SOUTHWALESWESTCONNECTIONNODEA"),
        ("AGS Harestonhill BESS 400kV Substation", "AGSHARESTONHILLBESS"),
        ("A'Chruach Wind Farm 275kV Substation", "ACHRUACHWINDFARM"),
        ("Braeside IDNO 132/33kV substation", "BRAESIDEIDNO"),
    ],
)
def test_normalize_substation_name_on_fixture_strings(raw: str, expected: str) -> None:
    assert normalize_substation_name(raw) == expected


def test_normalize_substation_name_blank_is_none() -> None:
    assert normalize_substation_name(None) is None
    assert normalize_substation_name("") is None


def test_normalize_substation_name_is_case_insensitive() -> None:
    assert normalize_substation_name("berkswell gsp") == normalize_substation_name("BERKSWELL GSP")


# ------------------------------------------------------------------------------ GB tier via geocode
def test_geocode_gb_resolves_a_known_grid_supply_point() -> None:
    point, precision = geocode(None, "Berkswell GSP", country="GB")
    assert precision == "county_centroid"  # a substation is not the project's location, docs/21 §3.7
    assert point is not None
    lon, lat = point
    assert -2.5 < lon < -1.0  # Berkswell, West Midlands
    assert 52 < lat < 53


def test_geocode_gb_matches_the_one_word_spelling_variant() -> None:
    """ "Upperboat" (TEC register) vs "Upper Boat" (gazetteer) -- same site, `docs/README.md`."""
    point, precision = geocode(None, "Upperboat 132kV Substation", country="GB")
    assert precision == "county_centroid"
    assert point is not None


def test_geocode_gb_country_prefix_match_is_case_insensitive_and_tolerates_a_region_suffix() -> None:
    a = geocode(None, "Berkswell GSP", country="GB")
    b = geocode(None, "Berkswell GSP", country="gb")
    c = geocode(None, "Berkswell GSP", country="GB-ENG")
    assert a == b == c


def test_geocode_gb_unmatched_substation_is_unknown_not_guessed() -> None:
    """A real fixture Connection Site with no entry in the gazetteer (a generator's own new
    substation, not a public Grid Supply Point) must stay `unknown` -- never a nearest-neighbour
    guess."""
    point, precision = geocode(None, "17 ACRES BESS 275KV SUBSTATION", country="GB")
    assert point is None
    assert precision == "unknown"


def test_geocode_gb_state_is_unused() -> None:
    """NESO carries no state; a `state` value must not change the outcome on the GB path."""
    with_state = geocode("Some State", "Berkswell GSP", country="GB")
    without_state = geocode(None, "Berkswell GSP", country="GB")
    assert with_state == without_state


def test_geocode_gb_accepts_an_injected_gazetteer() -> None:
    tiny = SubstationGazetteer()
    tiny.points["TESTSITE"] = (51.0, -1.0)
    point, precision = geocode(None, "Test Site", country="GB", gaz=tiny)
    assert precision == "county_centroid"
    assert point == (-1.0, 51.0)


# --------------------------------------------------------------------- measured fixture coverage
def test_fixture_connection_site_coverage_is_the_measured_number() -> None:
    """Measured 2026-09-13 against `tests/fixtures/neso_tec_register.csv`: 6 of the fixture's 30
    distinct Connection Site strings resolve to a gazetteer point (services/ingest/data/README.md
    has the full-register figure: 346/1,237 distinct sites, 739/2,198 rows). A change to the
    fixture or to `gb_substations.tsv` should update this number deliberately, not silently."""
    sites = _fixture_connection_sites()
    assert len(sites) == 30
    resolved = [s for s in sites if geocode(None, s, country="GB")[1] == "county_centroid"]
    assert sorted(resolved) == [
        "Aberthaw 275kV Substation",
        "Berkswell GSP",
        "Clydesmill 275/33kV",
        "Coatbridge GSP",
        "Grudie Bridge GSP",
        "Upperboat 132kV Substation",
    ]


def test_the_settlement_tier_adds_nothing_to_this_fixture_and_rejects_two_names() -> None:
    """Measured 2026-09-20: the settlement tier places nothing the GSP tier had not already placed
    in this fixture (its gains are all in the full register) and rejects two of the fixture's names
    as ambiguous -- "Blackhill" is five places, "East Kilbride" two. The list above is therefore
    still the whole fixture coverage."""
    gaz = default_settlement_gazetteer()
    sub = default_substation_gazetteer()
    assert [
        s for s in _fixture_connection_sites() if not sub.substation_point(s) and gaz.settlement_point(s)
    ] == []
    assert sorted(s for s in _fixture_connection_sites() if gaz.is_ambiguous(s)) == [
        "Blackhill 132/33kV",
        "East Kilbride B 275/33kV Substation",
    ]


# ================================================== GB settlement tier (added 2026-09-20)
# `gb_settlements.tsv` (ONS Index of Place Names GB 2024, OGL v3). Measured 2026-09-20 against the
# stored TEC register snapshot: 739 -> 1,068 of 2,198 rows placed, 346 -> 512 of 1,237 distinct
# Connection Sites, 56 sites (105 rows) rejected as ambiguous. `services/ingest/data/README.md`
# has the method and the known failure mode.


def test_settlement_gazetteer_loads_the_vendored_tsv() -> None:
    gaz = SettlementGazetteer.load()
    assert len(gaz.points) > 40_000
    assert len(gaz.ambiguous) > 4_000
    assert not set(gaz.points) & set(gaz.ambiguous)


def test_default_settlement_gazetteer_is_cached() -> None:
    assert default_settlement_gazetteer() is default_settlement_gazetteer()


# ------------------------------------------------------------------ suffix stripping on real strings
@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Cilfynydd 400kV Substation", "CILFYNYDD"),
        ("Chirk GSP", "CHIRK"),
        ("Navenby", "NAVENBY"),
        ("High Marnham 400kV Substation", "HIGH MARNHAM"),  # word boundary kept, unlike the GSP key
        ("Dumfries 132/33kV", "DUMFRIES"),
        ("Monk Fryston 275kV Substation", "MONK FRYSTON"),
        ("Alness 2 275/33 kV GSP Substation", "ALNESS"),  # trailing unit designator
        ("Aberthaw B 275/132kV Substation", "ABERTHAW"),
        ("Spittal 2 400kV Substation", "SPITTAL"),
        ("Fairburn Extension 132/33kV Substation", "FAIRBURN"),
        ("Ayrshire Grid 400kV Collector Substation", "AYRSHIRE GRID"),
        ("Keith 2 132/33kV GSP Substation", "KEITH"),
        ("NORTH HUMBER CONNECTION NODE C 132KV SUBSTATION", "NORTH HUMBER CONNECTION NODE"),
        ("17 ACRES BESS 275KV SUBSTATION", "17 ACRES BESS"),
    ],
)
def test_gb_settlement_key_strips_voltage_and_role_suffixes(raw: str, expected: str) -> None:
    assert gb_settlement_key(raw) == expected


def test_gb_settlement_key_blank_is_none() -> None:
    assert gb_settlement_key(None) is None
    assert gb_settlement_key("") is None
    assert gb_settlement_key("400kV Substation") is None


def test_gb_settlement_key_is_case_insensitive() -> None:
    assert gb_settlement_key("navenby 400kv substation") == gb_settlement_key("NAVENBY 400KV SUBSTATION")


def test_gb_settlement_key_keeps_word_boundaries_unlike_the_gsp_key() -> None:
    """The GSP tier folds "Upperboat" onto "Upper Boat" across 371 hand-checked names. Doing that
    across 52,110 settlement names put the TEC register's "Whitelee" (the wind farm south of
    Glasgow) on "White Lee" in Kirklees, 220 km away, so this key keeps the spaces."""
    assert gb_settlement_key("Whitelee 275/33kV") == "WHITELEE"
    assert normalize_settlement_name("White Lee") == "WHITE LEE"
    assert geocode(None, "Whitelee 275/33kV", country="GB", region="SPT") == (None, "unknown")
    assert gb_settlement_match("Whitelee 275/33kV", "SPT")[1] == "not_a_settlement"


# ---------------------------------------------------------------------------- resolution behaviour
def test_geocode_gb_resolves_a_settlement_the_gsp_list_does_not_name() -> None:
    """ "Cilfynydd 400kV Substation" is no Grid Supply Point, but Cilfynydd is a village in
    Rhondda Cynon Taf and the substation is named after it. NGET is the owner the register
    names for it, and Rhondda Cynon Taf is in NGET's area, so the region precondition holds."""
    point, precision = geocode(None, "Cilfynydd 400kV Substation", country="GB", region="NGET")
    assert precision == "county_centroid"
    assert point is not None
    lon, lat = point
    assert -3.4 < lon < -3.2
    assert 51.5 < lat < 51.7


def test_a_gsp_match_beats_a_settlement_match() -> None:
    """A substation's own coordinates always win over a settlement centroid: the GSP lookup runs
    first, and the settlement gazetteer is never consulted for a site the GSP list names. Both
    gazetteers know "Berkswell"; the answer must be the GSP's point."""
    gsp_point = default_substation_gazetteer().substation_point("Berkswell GSP")
    settlement_point = default_settlement_gazetteer().settlement_point("Berkswell GSP")
    assert gsp_point is not None
    assert settlement_point is not None
    assert gsp_point != settlement_point
    point, precision = geocode(None, "Berkswell GSP", country="GB")
    assert precision == "county_centroid"
    assert point == (gsp_point[1], gsp_point[0])


def test_an_ambiguous_settlement_name_stays_unplaced() -> None:
    """ "Thornton" is six different places up to five degrees apart; "Overton" is sixteen. A name
    borne by more than one place is never tie-broken -- it is left `unknown` (docs/04 D-8)."""
    gaz = default_settlement_gazetteer()
    for site in ("Thornton 400kV Substation", "Overton 275kV", "Coddington 400kV Substation"):
        assert gaz.is_ambiguous(site), site
        assert gaz.settlement_point(site) is None, site
        # a region is supplied, so `unknown` here is the ambiguity and not the precondition
        assert gb_settlement_match(site, "NGET")[1] == "ambiguous", site
        assert geocode(None, site, country="GB", region="NGET") == (None, "unknown"), site


def test_a_name_that_is_not_a_settlement_stays_unplaced() -> None:
    """A project-specific substation name resolves to nothing and is not reported ambiguous
    either -- it is not a place at all, which is a different fact about it."""
    gaz = default_settlement_gazetteer()
    for site in ("17 ACRES BESS 275KV SUBSTATION", "AGS Harestonhill BESS 400kV Substation"):
        assert not gaz.is_ambiguous(site), site
        assert gb_settlement_match(site, "NGET")[1] == "not_a_settlement", site
        assert geocode(None, site, country="GB", region="NGET") == (None, "unknown"), site


def test_the_settlement_tier_never_reports_exact() -> None:
    """The project is near a substation which is near the town this names: `county_centroid` is
    the grade, `exact` is never available from this tier (docs/21 §3.7)."""
    gaz = SettlementGazetteer()
    gaz.points["TEST PLACE"] = (51.0, -1.0)  # southern England, so NGET's area
    point, precision = geocode(
        None, "Test Place 400kV Substation", country="GB", region="NGET", settlements=gaz
    )
    assert precision == "county_centroid"
    assert precision != "exact"
    assert point == (-1.0, 51.0)


def test_geocode_gb_accepts_an_injected_settlement_gazetteer() -> None:
    """The injected gazetteer is what is consulted -- an empty one places nothing even when the
    region precondition is satisfied and the vendored table would have resolved the name."""
    empty = SettlementGazetteer()
    assert geocode(None, "Cilfynydd 400kV Substation", country="GB", region="NGET", settlements=empty) == (
        None,
        "unknown",
    )
    assert gb_settlement_match("Cilfynydd 400kV Substation", "NGET", settlements=empty) == (
        None,
        "not_a_settlement",
    )


def test_settlement_gazetteer_treats_two_spellings_of_one_key_as_ambiguous() -> None:
    """Two vendored rows folding to one key are two entries under one name, so the key is
    ambiguous even though each row claims a single place."""
    gaz = SettlementGazetteer.load(
        _write_settlement_tsv(
            [
                "Newton-le-Willows\t53.4500\t-2.6200\tSt. Helens\t1",
                "Newton le Willows\t54.2500\t-1.5300\tHambleton\t1",
            ]
        )
    )
    assert "NEWTON LE WILLOWS" not in gaz.points
    assert gaz.ambiguous["NEWTON LE WILLOWS"] == 2


def test_settlement_gazetteer_drops_the_point_for_a_multi_place_row() -> None:
    gaz = SettlementGazetteer.load(_write_settlement_tsv(["Thornton\t\t\t\t6"]))
    assert gaz.points == {}
    assert gaz.ambiguous == {"THORNTON": 6}


# ------------------------------------------------------- the new path must be load-bearing
def test_removing_the_settlement_tier_would_fail_this_test() -> None:
    """A guard on the tier itself: these Connection Sites resolve only through the settlement
    gazetteer (the GSP list does not name them), so deleting that lookup from `geocode()` fails
    here rather than silently dropping 329 rows back to `unknown`."""
    sub = default_substation_gazetteer()
    sites = [
        "Cilfynydd 400kV Substation",
        "High Marnham 400kV Substation",
        "Navenby 400kV Substation",
        "Necton 400kV Substation",
        "Biggleswade 400kV Substation",
        "Chirk GSP",
        "Langage 400kV Substation",
        "Monk Fryston 275kV Substation",
        "Penwortham 400kV Substation",
        "Rhigos 400kV Substation",
    ]
    for site in sites:
        assert sub.substation_point(site) is None, f"{site} is a GSP; it does not test this tier"
        point, precision = geocode(None, site, country="GB", region="NGET")
        assert point is not None, site
        assert precision == "county_centroid", site


# ============================================ region precondition on the settlement tier (2026-09-20)
# `gb_substations.tsv` gained a `region` column derived from NESO's own "GSP Group" (`_P` north
# Scotland, `_N` south Scotland, the other twelve England and Wales). The settlement tier places
# nothing unless the caller names the transmission owner the register carries in `HOST TO`, and
# refuses a match whose point falls in another owner's area. Measured 2026-09-20 on the stored
# snapshot: 310 rows / 153 sites placed (was 329 / 166 unconstrained); the constraint refused 19
# rows / 13 sites, of which ~8 were demonstrably wrong placements.


def test_the_substation_gazetteer_carries_a_region_for_every_row() -> None:
    gaz = SubstationGazetteer.load()
    assert len(gaz.sites) == 371
    regions = {region for _lat, _lon, region in gaz.sites}
    assert regions == {"north_scotland", "south_scotland", "england_wales"}


@pytest.mark.parametrize(
    ("lat", "lon", "expected", "where"),
    [
        (57.5082, -1.7835, "north_scotland", "Peterhead, Aberdeenshire"),
        (55.5865, -5.4944, "north_scotland", "Carradale, Kintyre -- north of a latitude band's reach"),
        (55.0698, -3.6093, "south_scotland", "Dumfries"),
        (55.8886, -3.0807, "south_scotland", "Dalkeith, Midlothian"),
        (51.6210, -3.3154, "england_wales", "Cilfynydd, Rhondda Cynon Taf"),
        (53.6872, -2.2180, "england_wales", "Greens, Rossendale"),
        (55.6318, -2.1694, "south_scotland", "Branxton, Northumberland -- fed from Eccles/Berwick"),
    ],
)
def test_region_of_point_classifies_by_nearest_grid_supply_point(
    lat: float, lon: float, expected: str, where: str
) -> None:
    assert default_substation_gazetteer().region_of_point(lat, lon) == expected, where


def test_the_settlement_tier_places_nothing_without_a_region() -> None:
    """The precondition, and the reason it is one: wiring `country` through at a call site must
    not by itself switch on unconstrained settlement placement. Cilfynydd resolves perfectly well
    -- it is withheld solely because no region was named."""
    assert geocode(None, "Cilfynydd 400kV Substation", country="GB") == (None, "unknown")
    assert gb_settlement_match("Cilfynydd 400kV Substation", None) == (None, "no_region")
    assert gb_settlement_match("Cilfynydd 400kV Substation", "") == (None, "no_region")
    assert gb_settlement_match("Cilfynydd 400kV Substation", "   ") == (None, "no_region")
    # ... and the same call with a region does place it, so the difference is the region alone
    assert geocode(None, "Cilfynydd 400kV Substation", country="GB", region="NGET")[0] is not None


def test_an_offshore_transmission_owner_places_nothing() -> None:
    """`OFTO` is a real `HOST TO` value (15 rows in the stored snapshot) with no onshore licence
    area, so it is deliberately absent from the mapping and can never satisfy the precondition."""
    assert "OFTO" not in GB_TRANSMISSION_OWNER_REGIONS
    assert gb_settlement_match("Cilfynydd 400kV Substation", "OFTO") == (None, "unknown_region")
    assert gb_settlement_match("Cilfynydd 400kV Substation", "NOT A TO") == (None, "unknown_region")
    assert geocode(None, "Cilfynydd 400kV Substation", country="GB", region="OFTO") == (
        None,
        "unknown",
    )


def test_a_match_outside_the_owners_region_is_refused() -> None:
    """The measured failure the constraint exists for. "Greens 400kV Substation" is a SHET site
    serving the Caledonia and Stromar offshore farms in the Moray Firth; the only "Greens" in the
    gazetteer is in Rossendale, Lancashire, 400 km away and in NGET's area. Unconstrained, that
    name resolves -- so this test fails if the region check is removed, not merely if it changes."""
    point = default_settlement_gazetteer().settlement_point("Greens 400kV Substation")
    assert point is not None  # the name itself resolves; the region is what refuses it
    assert default_substation_gazetteer().region_of_point(*point) == "england_wales"
    assert gb_settlement_match("Greens 400kV Substation", "SHET") == (None, "outside_region")
    assert geocode(None, "Greens 400kV Substation", country="GB", region="SHET") == (None, "unknown")
    # the same name under the owner whose area it is in does place
    assert gb_settlement_match("Greens 400kV Substation", "NGET")[1] == "placed"


@pytest.mark.parametrize(
    ("site", "region"),
    [
        ("Torness 400/132kV", "SPT"),  # East Lothian station; only "Torness" in the index is Highland
        ("Griffin 132/33kV Substation", "SHET"),  # Perthshire wind farm vs a Griffin in Lancashire
        ("Nant 132kV Substation", "SHET"),  # Argyll vs a Nant in Wrexham
        ("Fairburn Extension 132/33kV Substation", "SHET"),  # Highland vs Fairburn, North Yorkshire
        ("Blacklaw 132/33kV", "SPT"),  # South Lanarkshire vs a Blacklaw in Aberdeenshire
    ],
)
def test_the_named_wrong_placements_are_all_refused(site: str, region: str) -> None:
    assert default_settlement_gazetteer().settlement_point(site) is not None, site
    assert gb_settlement_match(site, region) == (None, "outside_region"), site
    assert geocode(None, site, country="GB", region=region) == (None, "unknown"), site


def test_the_gsp_tier_takes_no_region_and_is_unchanged_by_it() -> None:
    """The asymmetry, asserted rather than assumed: the GSP tier is NESO's own list of the
    substations the register connects into, so a name there is the site itself, not a place that
    happens to share its name. It resolves with no region, and naming one cannot change it."""
    bare = geocode(None, "Berkswell GSP", country="GB")
    assert bare[1] == "county_centroid"
    assert bare[0] is not None
    for region in ("NGET", "SPT", "SHET", "OFTO", None):
        assert geocode(None, "Berkswell GSP", country="GB", region=region) == bare, region


def test_every_outcome_in_the_vocabulary_is_reachable() -> None:
    """One real Connection Site per outcome, so the vocabulary cannot drift from what the function
    can actually return."""
    reached = {
        gb_settlement_match("Cilfynydd 400kV Substation", "NGET")[1],
        gb_settlement_match("Cilfynydd 400kV Substation", None)[1],
        gb_settlement_match("Cilfynydd 400kV Substation", "OFTO")[1],
        gb_settlement_match("17 ACRES BESS 275KV SUBSTATION", "NGET")[1],
        gb_settlement_match("Thornton 400kV Substation", "NGET")[1],
        gb_settlement_match("Greens 400kV Substation", "SHET")[1],
    }
    assert reached == set(GB_SETTLEMENT_OUTCOMES)


def test_region_codes_are_matched_case_and_space_insensitively() -> None:
    for spelling in ("NGET", "nget", " NGET "):
        assert gb_settlement_match("Cilfynydd 400kV Substation", spelling)[1] == "placed", spelling


def test_the_fixture_host_to_column_agrees_with_the_vendored_regions() -> None:
    """The mapping `HOST TO` -> transmission area, validated where both sides are known
    independently: every fixture Connection Site the GSP tier places has a NESO-sourced region on
    the gazetteer row and an owner code on the register row. Measured over the full stored
    snapshot the agreement is 345 of 347 site/owner pairs; the fixture's share must be perfect."""
    sub = default_substation_gazetteer()
    with FIXTURE.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    checked = 0
    for row in rows:
        point = sub.substation_point(row["Connection Site"])
        want = GB_TRANSMISSION_OWNER_REGIONS.get(row["HOST TO"])
        if point is None or want is None:
            continue
        checked += 1
        assert sub.region_of_point(*point) == want, row["Connection Site"]
    assert checked >= 5
