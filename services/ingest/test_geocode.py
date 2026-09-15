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
from pathlib import Path

import pytest

from services.ingest.geocode import (
    CountyGazetteer,
    SubstationGazetteer,
    default_substation_gazetteer,
    geocode,
    normalize_substation_name,
)

FIXTURE = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "neso_tec_register.csv"


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
