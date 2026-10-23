"""`/v1/prices/hubs`, `/v1/prices/hubs/{id}/series` and `/v1/prices/retail` over a database the
real runner, loader and statistics job built from the recorded fixtures
(`services/prices/conftest.py`): publication levels, the posture gate, credits, sort and filters."""

from __future__ import annotations

import pytest

from services.api import visibility
from services.api.prices import elexon_credit, sort_items
from services.posture import publishable_reuse_classes
from services.prices.conftest import NC, seeded_sessionmaker
from services.prices.conftest import price_store as price_store
from services.prices.conftest import price_template as price_template

ERCOT_IDS = {
    "ercot.hb_north",
    "ercot.hb_houston",
    "ercot.hb_south",
    "ercot.hb_west",
    "ercot.hb_pan",
    "ercot.hb_hubavg",
    "ercot.hb_busavg",
    "ercot.lz_houston",
    "ercot.lz_north",
    "ercot.lz_south",
    "ercot.lz_west",
    "ercot.lz_aen",
    "ercot.lz_cps",
    "ercot.lz_lcra",
    "ercot.lz_raybn",
}


@pytest.fixture()
def db_sessionmaker(price_template):
    """This module's database is a copy of the seeded one, so `client` and `db` read it."""
    with seeded_sessionmaker(price_template[0]) as factory:
        yield factory


@pytest.fixture()
def seeded(price_template):
    return price_template[1]


@pytest.fixture()
def noncommercial(monkeypatch):
    monkeypatch.setattr(visibility, "PUBLISHABLE_REUSE_CLASSES", NC)


def _by_id(body):
    return {item["id"]: item for item in body["data"]}


# ----------------------------------------------------------------------------------------- list
def test_the_commercial_posture_hides_spp_and_shows_the_rest(client, seeded) -> None:
    body = client.get("/v1/prices/hubs").json()
    items = _by_id(body)
    assert {i["market"] for i in body["data"]} == {"ERCOT", "CAISO", "NYISO", "GB"}
    assert ERCOT_IDS <= set(items) and len(items) == 34
    assert not any(i.startswith("spp.") for i in items)
    # SPP's rows are withheld under this posture, so it is a link-out with the reason (review
    # 2026-10-23), and the market filter's vocabulary does not offer it.
    assert [lo["market"] for lo in body["notes"]["link_outs"]] == ["PJM", "MISO", "ISO-NE", "SPP"]
    assert "commercial publication" in body["notes"]["link_outs"][-1]["reason"]
    assert all(lo["reason"] and lo["url"].startswith("https://") for lo in body["notes"]["link_outs"])
    assert body["notes"]["markets"] == ["ERCOT", "CAISO", "NYISO", "GB"]
    assert "not a forecast" in body["notes"]["screening_note"]


def test_under_the_noncommercial_posture_spp_appears_with_its_point(client, seeded, noncommercial) -> None:
    items = _by_id(client.get("/v1/prices/hubs").json())
    north = items["spp.sppnorth_hub"]
    assert north["publication"] == "raw_ok" and north["credit"] == "Source: Southwest Power Pool"
    assert north["placement"]["grade"] == "point" and north["placement"]["lat"] == pytest.approx(
        41.2032, abs=1e-3
    )
    assert north["latest"]["rt"]["price"] is not None
    # SPP's posted day-ahead prices are for the next operating day: none has begun at the time
    # the store was recorded, so none is the "latest" price (review 2026-10-23).
    assert "da" not in north["latest"]
    assert len(items) == 37
    body = client.get("/v1/prices/hubs").json()
    assert "SPP" not in [lo["market"] for lo in body["notes"]["link_outs"]]
    assert "SPP" in body["notes"]["markets"]


def test_derived_only_sources_serve_statistics_and_no_raw_price(client, seeded) -> None:
    items = _by_id(client.get("/v1/prices/hubs").json())
    for loc_id in ("caiso.th_sp15_gen_apnd", "nyiso.capitl"):
        item = items[loc_id]
        assert item["publication"] == "derived_only"
        assert item["latest"] is None and item["series_url"] is None
        assert (
            item["statistics"]["da"] is not None and item["statistics"]["da"]["provenance"]["derived"] is True
        )
    assert items["caiso.th_sp15_gen_apnd"]["credit"] == "Source: California ISO"
    assert items["nyiso.capitl"]["credit"] == "Source: NYISO"


def test_raw_ok_sources_serve_the_latest_price_with_provenance(client, seeded) -> None:
    item = _by_id(client.get("/v1/prices/hubs").json())["ercot.hb_north"]
    assert item["publication"] == "raw_ok" and item["credit"] == "Source: ERCOT"
    latest = item["latest"]["rt"]
    assert latest["interval_start"].startswith("2026-10-22") and latest["source_url"].endswith(
        "real_time_spp.html"
    )
    assert item["placement"] == {
        **item["placement"],
        "grade": "region",
        "region_id": "US-TX",
        "lat": None,
        "lon": None,
    }
    assert "label only" in item["placement"]["label_point"]["method"]
    assert item["provenance"][0]["source_id"] == "us.iso.ercot.prices"
    stat = item["statistics"]["rt"]
    for key in ("window_start", "window_end", "sample_count", "expected_count", "coverage", "withheld"):
        assert key in stat


def test_the_elexon_credit_names_the_years_of_the_data_shown(client, seeded) -> None:
    item = _by_id(client.get("/v1/prices/hubs").json())["gb.system"]
    assert item["credit"] == "Contains BMRS data © Elexon Limited copyright and database right 2026"
    assert item["currency"] == "GBP" and item["unit"] == "£/MWh"
    assert elexon_credit(
        "Contains BMRS data © Elexon Limited copyright and database right 2026", [2025, 2026]
    ) == ("Contains BMRS data © Elexon Limited copyright and database right 2025-2026")


def test_each_credit_comes_with_its_licence_link(client, seeded) -> None:
    """Elexon's licence: "Where possible You must also contain a link to this licence", and the
    rights end automatically if a condition is not met (docs/29 §2.8; review 2026-10-23)."""
    credits = {c["source_id"]: c for c in client.get("/v1/prices/hubs").json()["notes"]["credits"]}
    gb = credits["gb.elexon.bmrs_prices"]
    assert gb["text"] == "Contains BMRS data © Elexon Limited copyright and database right 2026"
    assert gb["licence_url"] == (
        "https://www.elexon.co.uk/bsc/data/balancing-mechanism-reporting-agent/copyright-licence-bmrs-data/"
    )
    assert set(credits) == {
        "us.iso.ercot.prices",
        "us.iso.caiso.oasis_prices",
        "us.iso.nyiso.prices",
        "gb.elexon.bmrs_prices",
    }
    assert all(c["text"] for c in credits.values())


def test_a_shared_column_sorts_on_the_figure_shown_and_never_mixes_currencies(client, seeded) -> None:
    """Great Britain has no day-ahead market: its spread, basis and 30-day figures are real-time,
    and `sort=spread` (the lead market's) sorts it by the spread the page shows, after every dollar
    row; `sort=da_spread` leaves it last (it has none)."""
    data = client.get("/v1/prices/hubs?sort=-spread").json()["data"]
    gb = next(i for i in data if i["id"] == "gb.system")
    assert gb["lead_market"] == "rt" and gb["statistics"]["rt"]["spread"] is not None
    currencies = [
        i["currency"] for i in data if (i["statistics"][i["lead_market"]] or {}).get("spread") is not None
    ]
    assert currencies == sorted(currencies, key=["USD", "GBP"].index)  # every $ row, then £
    usd = [
        i["statistics"][i["lead_market"]]["spread"]
        for i in data
        if i["currency"] == "USD" and (i["statistics"][i["lead_market"]] or {}).get("spread") is not None
    ]
    assert usd == sorted(usd, reverse=True)
    by_da = client.get("/v1/prices/hubs?sort=-da_spread").json()["data"]
    assert by_da[-1]["id"] == "gb.system" or (by_da[-1]["statistics"]["da"] or {}).get("spread") is None
    assert next(i for i in data if i["id"] == "caiso.th_np15_gen_apnd")["market_labels"]["rt"].startswith(
        "real-time, 15-minute pre-dispatch"
    )


@pytest.mark.parametrize(
    ("query", "expect"),
    [
        ("market=ERCOT", {"ERCOT"}),
        ("market=ercot,gb", {"ERCOT", "GB"}),
        ("market=NYISO&kind=zone", {"NYISO"}),
    ],
)
def test_filters(client, seeded, query: str, expect: set[str]) -> None:
    body = client.get(f"/v1/prices/hubs?{query}").json()
    assert {i["market"] for i in body["data"]} == expect
    if "kind=zone" in query:
        assert {i["kind"] for i in body["data"]} == {"zone"}


@pytest.mark.parametrize("query", ["market=PJM", "kind=node", "sort=-price", "colour=red"])
def test_bad_parameters_are_400(client, seeded, query: str) -> None:
    assert client.get(f"/v1/prices/hubs?{query}").status_code == 400


@pytest.mark.parametrize("token", ["rt_basis", "-rt_basis"])
def test_a_location_without_the_figure_sorts_last_in_both_directions(client, seeded, token: str) -> None:
    data = client.get(f"/v1/prices/hubs?sort={token}").json()["data"]
    values = [(i["statistics"]["rt"] or {}).get("basis") for i in data]
    first_missing = values.index(None)
    assert all(v is None for v in values[first_missing:])
    present = values[:first_missing]
    assert present and present == sorted(present, reverse=token.startswith("-"))


def test_sort_items_orders_ties_by_label() -> None:
    items = [
        {"label": "b", "market": "X", "statistics": {"da": {"mean": 1.0}}},
        {"label": "a", "market": "X", "statistics": {"da": {"mean": 1.0}}},
        {"label": "c", "market": "X", "statistics": {"da": None}},
    ]
    assert [i["label"] for i in sort_items(items, "-da_mean")] == ["a", "b", "c"]


# --------------------------------------------------------------------------------------- series
def test_series_for_a_raw_ok_location(client, seeded) -> None:
    body = client.get("/v1/prices/hubs/ercot.hb_north/series").json()
    data = body["data"]
    assert data["series_withheld_reason"] is None
    rt = {row["day"]: row for row in data["series"]["rt"]}
    assert rt["2026-10-21"]["intervals"] == 96 and rt["2026-10-21"]["spread"] is not None
    assert "2026-10-22" not in rt  # today so far (the latest page) is a partial day, not a daily mean
    assert {row["day"] for row in data["series"]["da"]} >= {"2026-10-22"}


def test_a_day_still_in_progress_is_not_in_the_series(client, seeded, db) -> None:
    """Great Britain's newest day had 47 of its 48 periods at the last fetch: the 90% rule admits
    it, but a day not over is no daily mean (review 2026-10-23)."""
    import datetime as dt

    from services.prices.models import PriceDaily

    done = db.get(PriceDaily, ("gb.system", "rt", dt.date(2026, 10, 21)))
    assert done is not None
    partial = PriceDaily(
        **{c.name: getattr(done, c.name) for c in PriceDaily.__table__.columns if c.name != "day"},
    )
    partial.day, partial.intervals = dt.date(2026, 10, 22), 47
    db.add(partial)
    db.commit()
    days = [
        row["day"] for row in client.get("/v1/prices/hubs/gb.system/series").json()["data"]["series"]["rt"]
    ]
    assert days == ["2026-10-21"]


def test_series_for_a_derived_only_location_answers_the_statistics_and_the_reason(client, seeded) -> None:
    data = client.get("/v1/prices/hubs/caiso.th_sp15_gen_apnd/series").json()["data"]
    assert data["series"] is None and "derived values only" in data["series_withheld_reason"]
    assert data["statistics"]["da"] is not None and data["latest"] is None


def test_series_404s(client, seeded) -> None:
    assert client.get("/v1/prices/hubs/pjm.western_hub/series").status_code == 404
    assert client.get("/v1/prices/hubs/spp.sppnorth_hub/series").status_code == 404  # commercial posture
    assert client.get("/v1/prices/hubs/ercot.hb_north/series?x=1").status_code == 400


def test_series_for_spp_under_the_noncommercial_posture(client, seeded, noncommercial) -> None:
    data = client.get("/v1/prices/hubs/spp.sppnorth_hub/series").json()["data"]
    assert [row["intervals"] for row in data["series"]["da"]] and data[
        "credit"
    ] == "Source: Southwest Power Pool"


# --------------------------------------------------------------------------------------- retail
def test_retail_by_state_with_the_twelve_month_average_and_release(client, seeded) -> None:
    body = client.get("/v1/prices/retail").json()
    states = {i["state"]: i for i in body["data"]}
    assert set(states) == {"TX", "CA", "NY", "OK", "NE", "DC"}
    tx = states["TX"]["sectors"]["industrial"]
    assert tx["latest_period"] == "2026-07" and tx["months"] == 12
    assert (tx["avg_12m_from"], tx["avg_12m_to"]) == ("2025-08", "2026-07")
    assert states["TX"]["latest_cents_kwh"] == tx["latest_cents_kwh"]
    assert body["notes"]["credit"].endswith("released 2026-09-24")
    assert body["notes"]["sector"] == "industrial"
    values = [i["latest_cents_kwh"] for i in body["data"]]
    assert values == sorted(values, reverse=True)


def test_retail_average_is_revenue_over_sales(client, seeded, db) -> None:
    import sqlalchemy as sa

    from services.prices.models import RetailPrice

    rows = db.scalars(
        sa.select(RetailPrice)
        .where(RetailPrice.state == "CA", RetailPrice.sector == "residential")
        .order_by(RetailPrice.period)
    ).all()[-12:]
    expected = round(sum(r.revenue_thousand_usd for r in rows) / sum(r.sales_mwh for r in rows) * 100, 2)
    body = client.get("/v1/prices/retail?sector=residential&sort=state").json()
    ca = next(i for i in body["data"] if i["state"] == "CA")
    assert ca["avg_12m_cents_kwh"] == expected
    assert [i["state"] for i in body["data"]] == sorted(i["state"] for i in body["data"])


def test_a_missing_month_withholds_the_twelve_month_average(client, seeded, db) -> None:
    """The window is the twelve calendar months to the latest one (review 2026-10-23): with one
    gone it is withheld and says why, rather than reaching back to an older month."""
    import sqlalchemy as sa

    from services.prices.models import RetailPrice

    db.execute(
        sa.delete(RetailPrice).where(
            RetailPrice.state == "TX", RetailPrice.sector == "industrial", RetailPrice.period == "2026-01"
        )
    )
    db.commit()
    tx = next(i for i in client.get("/v1/prices/retail").json()["data"] if i["state"] == "TX")
    block = tx["sectors"]["industrial"]
    assert block["avg_12m_cents_kwh"] is None and "2026-01" in block["avg_12m_withheld"]
    assert block["latest_cents_kwh"] is not None  # the latest month still stands

    db.execute(
        sa.update(RetailPrice)
        .where(RetailPrice.state == "CA", RetailPrice.sector == "industrial", RetailPrice.period == "2026-03")
        .values(revenue_thousand_usd=None)
    )
    db.commit()
    ca = next(i for i in client.get("/v1/prices/retail").json()["data"] if i["state"] == "CA")
    assert ca["sectors"]["industrial"]["avg_12m_cents_kwh"] is None
    assert "no revenue or sales" in ca["sectors"]["industrial"]["avg_12m_withheld"]


@pytest.mark.parametrize("query", ["sector=transportation", "sort=price", "state=TX"])
def test_retail_bad_parameters_are_400(client, seeded, query: str) -> None:
    assert client.get(f"/v1/prices/retail?{query}").status_code == 400


def test_the_noncommercial_set_is_the_posture_s(monkeypatch) -> None:
    assert frozenset(publishable_reuse_classes("noncommercial")) == NC
