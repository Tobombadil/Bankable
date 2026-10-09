"""County FIPS for counties that share a name with an independent city (review 2026-10-07).

The Census Gazetteer holds "Fairfax County" (51059) and "Fairfax city" (51600). Both used to
normalise to ("VA", "FAIRFAX"); the city's row came later in the file and won, so all 36 Fairfax
data centres in the dev store carried 51600 and the city's centroid. The same collision hit
Franklin, Richmond and Roanoke (VA), Baltimore (MD) and St. Louis (MO).
"""

from __future__ import annotations

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Location
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.geocode import CountyGazetteer, correct_county_fips, county_lookup_keys
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.ingest.test_loader import open_source_entry, sample_proposal_row


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


@pytest.fixture(scope="module")
def gaz() -> CountyGazetteer:
    return CountyGazetteer.load()


@pytest.mark.parametrize(
    ("state", "name", "fips"),
    [
        ("VA", "Fairfax", "51059"),  # a bare name is the county
        ("VA", "Fairfax County", "51059"),
        ("VA", "FAIRFAX COUNTY", "51059"),
        ("VA", "Fairfax city", "51600"),
        ("VA", "Fairfax City", "51600"),
        ("VA", "Harrisonburg (city)", "51660"),
        ("VA", "Suffolk City", "51800"),  # EIA-860M's spelling
        ("VA", "Richmond", "51159"),
        ("VA", "Richmond city", "51760"),
        ("VA", "Roanoke County", "51161"),
        ("VA", "Roanoke city", "51770"),
        ("VA", "Franklin", "51067"),
        ("VA", "Franklin city", "51620"),
        ("VA", "James City", "51095"),  # James City County: no independent city of that name
        ("VA", "Charles City County", "51036"),
        ("VA", "Manassas city", "51683"),
        ("VA", "Loudoun", "51107"),
        ("VA", "Loudoun County", "51107"),
        ("MD", "Baltimore", "24005"),
        ("MD", "Baltimore city", "24510"),
        ("MO", "St. Louis", "29189"),
        ("MO", "St. Louis city", "29510"),
        ("NV", "Carson City", "32510"),  # consolidated city named "City" in the Census itself
        ("TX", "Travis", "48453"),
    ],
)
def test_county_and_independent_city_resolve_to_their_own_fips(gaz, state, name, fips):
    assert gaz.county_fips(state, name) == fips


def test_county_and_city_of_one_name_get_their_own_centroids(gaz):
    county = gaz.county_point("VA", "Fairfax County")
    city = gaz.county_point("VA", "Fairfax city")
    assert county is not None and city is not None and county != city
    assert gaz.county_point("VA", "Fairfax") == county


def test_lookup_keys():
    assert county_lookup_keys("Fairfax city") == ["FAIRFAX CITY", "FAIRFAX"]
    assert county_lookup_keys("Fairfax County") == ["FAIRFAX"]
    assert county_lookup_keys(None) == []


def test_correct_county_fips_repairs_rows_stored_with_the_city_code(session: Session, gaz):
    """`backfill_county_fips` only fills NULLs, so rows already stored with 51600 need this."""
    entry = open_source_entry("us.test.fairfax")
    src = upsert_licence_and_source(session, entry, "2026-10-07")
    rows = []
    for rid, county in (("F1", "Fairfax County"), ("F2", "Fairfax"), ("F3", "Fairfax city")):
        row = sample_proposal_row(rid)
        row.update(source_id=entry.id, record_id=f"{entry.id}:{rid}", state="VA", county=county)
        rows.append(row)
    load_dataframe(session, src, "proposal", pd.DataFrame(rows), None)
    city_point = gaz.county_point("VA", "Fairfax city")
    for loc in session.scalars(select(Location)):  # the state the old gazetteer left behind
        loc.county_fips = "51600"
        loc.geom = (city_point[1], city_point[0])
    session.commit()

    result = correct_county_fips(session, gaz)
    assert result["changed"] == 2 and result["changes"] == {"51600->51059": 2}
    assert result["centroids_moved"] == 2
    by_name = {loc.county_name: loc for loc in session.scalars(select(Location))}
    assert by_name["Fairfax County"].county_fips == by_name["Fairfax"].county_fips == "51059"
    assert by_name["Fairfax city"].county_fips == "51600"
    lat, lon = gaz.county_point("VA", "Fairfax County")
    assert tuple(by_name["Fairfax"].geom) == pytest.approx((lon, lat))
    assert correct_county_fips(session, gaz)["changed"] == 0  # idempotent
