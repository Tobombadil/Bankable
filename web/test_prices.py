"""`/prices` against the real API over a database seeded from the recorded fixtures by the real
runner, loader and statistics job (`services/prices/conftest.py`)."""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from services.api import visibility
from services.api.app import app as api_app
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.prices.conftest import NC, seeded_sessionmaker
from services.prices.conftest import price_store as price_store
from services.prices.conftest import price_template as price_template
from web.api_client import ApiClient
from web.app import app as web_app
from web.prices import flatten_hub, money, share


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def web_client(price_template) -> Iterator[TestClient]:
    with seeded_sessionmaker(price_template[0]) as factory:

        def _override_get_db():
            session = factory()
            try:
                yield session
            finally:
                session.close()

        api_app.dependency_overrides[get_db] = _override_get_db
        web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
        web_app.state.lag_days_default = None
        try:
            with TestClient(web_app, follow_redirects=False) as client:
                yield client
        finally:
            api_app.dependency_overrides.clear()
            del web_app.state.api_client


def _rows(html: str) -> list[str]:
    body = html.split('<table class="record-table record-table--compact price-table">', 1)[1].split(
        "</table>", 1
    )[0]
    return re.findall(r'<td data-label="Hub or zone" class="cell-title">([^<]+)<', body)


def test_the_page_explains_then_lists_then_credits(web_client: TestClient) -> None:
    resp = web_client.get("/prices")
    assert resp.status_code == 200
    html = resp.text
    order = [
        html.index(s)
        for s in (
            "Wholesale price (LMP)",
            "Basis",
            "Negative-price hours",
            "Daily spread",
            'id="hubs"',
            'id="linkouts"',
            'id="states"',
            'id="credits"',
        )
    ]
    assert order == sorted(order)
    assert "ERCOT North Hub" in html and "NYISO" in html and "Great Britain system price" in html
    assert "SPP North Hub" not in html  # the commercial posture: SPP's rows are withheld
    for credit in (
        "Source: ERCOT",
        "Source: California ISO",
        "Source: NYISO",
        "Contains BMRS data © Elexon Limited copyright and database right 2026",
        "Source: U.S. Energy Information Administration",
    ):
        assert credit in html, credit
    assert "For screening, not a forecast." in html
    assert "None" not in re.sub(r"<script.*?</script>", "", html, flags=re.S)


ELEXON_LICENCE = (
    "https://www.elexon.co.uk/bsc/data/balancing-mechanism-reporting-agent/copyright-licence-bmrs-data/"
)


def _credits(html: str) -> str:
    return html.split('id="price-credit-list"', 1)[1].split("</ul>", 1)[0]


def test_the_elexon_statement_links_its_licence(web_client: TestClient) -> None:
    """The BMRS licence: "Where possible You must also contain a link to this licence"; the rights
    end automatically if a condition is not met (docs/29 §2.8; review 2026-10-23)."""
    credits = _credits(web_client.get("/prices").text)
    item = next(li for li in credits.split("<li>") if "Contains BMRS data" in li)
    assert f'<a href="{ELEXON_LICENCE}" rel="noopener nofollow">licence' in item


def test_a_filtered_swap_brings_the_credits_of_its_rows(web_client: TestClient) -> None:
    partial = web_client.get("/prices?market=GB", headers={"HX-Request": "true", "HX-Target": "price-rows"})
    assert partial.status_code == 200 and "<html" not in partial.text
    assert 'id="price-credit-list" class="price-credit-list" hx-swap-oob="true"' in partial.text
    credits = _credits(partial.text)
    assert "Contains BMRS data © Elexon Limited" in credits and ELEXON_LICENCE in credits
    assert "Source: ERCOT" not in credits  # only the rows shown
    full = web_client.get("/prices").text
    assert "hx-swap-oob" not in full  # the full page carries its credits in place


def test_the_glossary_and_the_method_say_what_the_figures_are(web_client: TestClient) -> None:
    html = web_client.get("/prices").text
    assert "set the day before for each hour of the next day" in html
    assert "an hour ahead" not in html
    credits = html.split('id="credits"', 1)[1].split("</section>", 1)[0]
    assert 'href="#method"' in credits and 'href="/methodology"' not in credits  # that page has no prices
    method = html.split('id="method"', 1)[1].split("</section>", 1)[0]
    assert "pre-dispatch market (RTPD)" in method and "hourly integrated" in method
    assert "Pound prices sort after dollar prices" in method


def test_shared_columns_sort_on_the_figure_shown(web_client: TestClient) -> None:
    html = web_client.get("/prices").text.replace("&amp;", "&")
    for token in ("sort=-spread", "sort=-basis", "sort=-negative_share", "sort=-mean_30d"):
        assert f"/prices?{token}" in html, token
    rows = _rows(web_client.get("/prices?sort=-spread").text)
    assert "Great Britain system price" in rows
    # Great Britain's spread is shown, so it is not left among the rows without one.
    gb_html = web_client.get("/prices?sort=-spread").text
    assert gb_html.index("Great Britain system price") < gb_html.index('id="linkouts"')


def test_under_the_commercial_posture_spp_is_a_link_out_not_a_filter(web_client: TestClient) -> None:
    html = web_client.get("/prices").text
    assert '<option value="SPP"' not in html
    section = html.split('id="linkouts"', 1)[1].split("</section>", 1)[0]
    assert "Southwest Power Pool prices" in section and "commercial publication" in section


def test_link_out_markets_name_their_reason(web_client: TestClient) -> None:
    html = web_client.get("/prices").text
    section = html.split('id="linkouts"', 1)[1].split("</section>", 1)[0]
    assert "PJM Interconnection prices" in section and "Redistribution License" in section
    assert "MISO prices" in section and "ISO New England prices" in section


def test_latest_column_only_where_raw_rows_are_allowed(web_client: TestClient) -> None:
    html = web_client.get("/prices").text
    rows = {
        m.group(1): m.group(0)
        for m in re.finditer(
            r'<tr>\s*<td data-label="Hub or zone" class="cell-title">([^<]+)<.*?</tr>', html, re.S
        )
    }
    withheld = '<span class="price-withheld">not published</span>'
    caiso = next(row for label, row in rows.items() if label.startswith("CAISO SP15"))
    assert withheld in caiso  # derived-only: the cell says so instead of a price
    north = rows["ERCOT North Hub"]
    assert withheld not in north and re.search(r'<span class="cell-unit">latest </span>\$\d', north)
    only_caiso = web_client.get("/prices?market=CAISO").text
    assert "Latest real-time" not in only_caiso  # no row may show one: the column is left out


def test_headings_sort_largest_first_and_the_partial_swaps_rows(web_client: TestClient) -> None:
    html = web_client.get("/prices").text
    assert 'aria-sort="descending"' in html  # the default sort: day-ahead average, largest first
    assert "/prices?sort=-rt_mean" in html.replace("&amp;", "&")
    partial = web_client.get("/prices?sort=name", headers={"HX-Request": "true", "HX-Target": "price-rows"})
    assert partial.status_code == 200 and "<html" not in partial.text
    labels = _rows(
        '<table class="record-table record-table--compact price-table">'
        + partial.text.split('<table class="record-table record-table--compact price-table">', 1)[1]
    )
    assert labels == sorted(labels, key=str.lower)


def test_filters_and_a_bad_filter_falls_back(web_client: TestClient) -> None:
    nyiso = _rows(web_client.get("/prices?market=NYISO&kind=zone").text)
    assert len(nyiso) == 11 and all(label.startswith("NYISO") for label in nyiso)
    bad = web_client.get("/prices?market=PJM")
    assert bad.status_code == 200 and len(_rows(bad.text)) == 34


def test_the_state_table_sorts_on_its_own_parameter(web_client: TestClient) -> None:
    html = web_client.get("/prices?rsort=state").text
    states = html.split('id="states"', 1)[1].split("</table>", 1)[0]
    names = re.findall(r'<td data-label="State" class="cell-title">([^<]+)<', states)
    assert names == sorted(names) and "Texas" in names
    assert "rsort=-industrial_latest" in states.replace("&amp;", "&")
    assert "12-month average" in states and "¢" in states


def test_under_the_noncommercial_posture_spp_appears(web_client: TestClient, monkeypatch) -> None:
    monkeypatch.setattr(visibility, "PUBLISHABLE_REUSE_CLASSES", NC)
    html = web_client.get("/prices?market=SPP").text
    assert sorted(_rows(html)) == ["SPP North Hub", "SPP RTO West Hub", "SPP South Hub"]
    assert "Source: Southwest Power Pool" in html
    assert '<option value="SPP"' in html
    section = html.split('id="linkouts"', 1)[1].split("</section>", 1)[0]
    assert "Southwest Power Pool" not in section


def test_a_short_history_is_marked_in_its_cell(web_client: TestClient) -> None:
    """SPP's day-ahead average rests on 30 days, beside other markets' twelve months (review
    2026-10-23): the cell says so. The recorded store holds one day of ERCOT real time (its
    day-ahead day is not yet complete, so real time leads), so that average is marked; a full year
    is not."""
    html = web_client.get("/prices?market=ERCOT").text
    assert re.search(r'time </span>\$[\d.,]+<span class="price-short"> \(1 day\)</span>', html)

    def hub(days: int) -> dict[str, object]:
        stat = {"mean": 24.1, "days": days, "withheld": {}}
        return flatten_hub({"id": "spp.x", "lead_market": "da", "statistics": {"da": stat, "rt": None}})

    assert hub(30)["short_window"] == "30 days" and hub(365)["short_window"] is None


def test_formatting() -> None:
    assert money(-4.1) == "−$4.10" and money(2.05, signed=True) == "+$2.05" and money(None) is None
    assert money(99.5, "GBP") == "£99.50" and share(0.0329) == "3.3%"
    gb = flatten_hub(
        {
            "id": "gb.system",
            "currency": "GBP",
            "markets": ["rt"],
            "lead_market": "rt",
            "statistics": {"da": None, "rt": {"mean": 99.5, "withheld": {}}},
            "latest": None,
            "reference": None,
        }
    )
    assert (
        gb["da"] is None
        and gb["da_why"] == "No such market at this location."
        and gb["lead_market"] == "real-time"
    )
