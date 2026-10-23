"""An opportunity's value on RFPs & funding and on its record page (lane pages, 2026-10-23), from the
API's `value` block (lane rfp-value).

Held here, against a hand-written fake of the API (no database):

- the list's Value column: the amount in its own currency in a short form (`€300M`), the approximate
  US-dollar figure beside it when it was converted, and what the figure is under it ("est. contract
  value", "award ceiling"); a placeholder is a dash with the number the notice states; the heading
  sorts by the API's `value` token, largest first; the ECB's credit is in the sources line whenever
  the page's licence summary carries it;
- the record page: the full amount, the US-dollar figure with the ECB fixing and rate, the other
  figures the notice states, the expected number of awards, where the amount was read, and the ECB's
  credit verbatim; a placeholder and an unquoted currency each say why there is no figure;
- the short forms themselves (`money_compact`, `money_full`) and the reference registers' short
  names, every word of which is in the register's `data/sources.yaml` name.

The fake is duplicated rather than imported, per this repo's convention that no `web/test_*.py`
imports another.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from web.api_client import ApiClient
from web.app import app as web_app
from web.viewmodels import (
    REFERENCE_SOURCE_LABELS,
    flatten_opportunity,
    money_compact,
    money_full,
    opportunity_value_view,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
ECB_CREDIT = (
    "Source: European Central Bank, euro foreign exchange reference rates. USD equivalents are calculated by "
    "Inframation from the ECB's euro reference rates; they are approximate and are not published by the ECB"
)
FX_SOURCE = {
    "source_id": "eu.ecb.eurofxref",
    "source_name": "ECB euro foreign exchange reference rates",
    "source_url": "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip",
    "retrieved_at": "2026-10-22T01:01:38Z",
    "licence_id": "eu.ecb.eurofxref#x",
    "licence_url": "https://www.ecb.europa.eu/services/using-our-site/disclaimer/html/index.en.html",
    "reuse_class": "attribution",
    "attribution_text": ECB_CREDIT,
    "rate_date": "2026-10-07",
}
TED = {
    "source_id": "eu.ted.api",
    "source_name": "EU Tenders Electronic Daily (TED) API v3",
    "source_url": "https://ted.europa.eu/en/notice/-/detail/691918-2026",
    "retrieved_at": "2026-10-09T19:53:28Z",
    "licence_id": "eu.ted.api#x",
    "reuse_class": "attribution",
    "attribution_text": "Source: Publications Office of the EU",
    "source_field": "total-value",
    "endpoint": None,
}


def _value(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "amount": 300_000_000.0,
        "currency": "EUR",
        "kind": "estimated_value",
        "kind_label": "Estimated value",
        "usd_equivalent": 335_310_000.0,
        "usd_is_approximate": True,
        "fx_rate": 1.1177,
        "fx_date": "2026-10-07",
        "fx_source": FX_SOURCE,
        "fx_note": None,
        "amount_note": None,
        "also_stated": [],
        "expected_awards": None,
        "provenance": TED,
    }
    base.update(overrides)
    return base


def _opportunity(slug: str = "heat-network", **overrides: Any) -> dict[str, Any]:
    value = overrides.pop("value", _value())
    amount = value["amount"] if value else None
    base: dict[str, Any] = {
        "public_id": f"opp_{slug}",
        "slug": slug,
        "title": f"Heat network concession {slug}",
        "kind": "tender",
        "status": "open",
        "jurisdiction": "FR",
        "technologies": ["heat_networks"],
        "budget_amount": amount,
        "budget_currency": value["currency"] if value else None,
        "value": value,
        "identifiers": {},
        "provenance": [{**TED, "source_name": "EU Tenders Electronic Daily (TED) API v3"}],
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------------------------- the short forms
@pytest.mark.parametrize(
    ("amount", "currency", "text"),
    [
        (300_000_000.0, "EUR", "€300M"),
        (335_310_000.0, "USD", "$335M"),
        (12_530_000.0, "GBP", "£12.5M"),
        (1_234_567.0, "PLN", "PLN 1.23M"),
        (999_960_000.0, "USD", "$1B"),
        (2_500_000_000.0, "USD", "$2.5B"),
        (950_000.0, "USD", "$950k"),
        (640.0, "EUR", "€640"),
        (4_200_000.0, None, "4.2M"),
        (0.0, "USD", None),
        (-1.0, "USD", None),
        (None, "USD", None),
    ],
)
def test_a_list_cell_prints_an_amount_to_three_figures_in_its_currency(
    amount: Any, currency: Any, text: str | None
) -> None:
    assert money_compact(amount, currency) == text


def test_the_record_page_prints_the_whole_amount() -> None:
    assert money_full(20_000_000.0, "USD") == "$20,000,000"
    assert money_full(300_000_000.0, "EUR") == "€300,000,000"
    assert money_full(20_000_000.0, "MKD") == "MKD 20,000,000"
    assert money_full(None, "USD") is None


def test_the_view_of_a_converted_amount_carries_the_rate_and_the_ecb_credit() -> None:
    view = opportunity_value_view(_opportunity())
    assert view is not None
    assert (view["label"], view["short"], view["full"], view["compact"]) == (
        "Estimated value",
        "est. contract value",
        "€300,000,000",
        "€300M",
    )
    assert (view["usd_full"], view["usd_compact"], view["fx_rate"], view["fx_date"]) == (
        "$335,310,000",
        "$335M",
        "1 EUR = 1.1177 USD",
        "2026-10-07",
    )
    assert view["fx_credit"] == {
        "name": "ECB",
        "text": ECB_CREDIT,
        "url": FX_SOURCE["source_url"],
        "licence_url": FX_SOURCE["licence_url"],
        "rate_date": "2026-10-07",
    }
    assert view["source"] == {
        "name": "TED",
        "url": TED["source_url"],
        "field": "total-value",
        "retrieved_at": TED["retrieved_at"],
        "endpoint": None,
    }


def test_a_dollar_amount_has_no_second_figure_and_no_ecb_credit() -> None:
    view = opportunity_value_view(
        _opportunity(
            value=_value(
                amount=20_000_000.0,
                currency="USD",
                kind="award_ceiling",
                kind_label="Award ceiling",
                usd_equivalent=20_000_000.0,
                usd_is_approximate=False,
                fx_rate=1.0,
                fx_date=None,
                fx_source=None,
                expected_awards=5,
                also_stated=[
                    {
                        "kind": "total_programme_funding",
                        "kind_label": "Estimated total programme funding",
                        "amount": 2_147_483_647.0,
                        "currency": "USD",
                        "source_field": "synopsis.estimatedFunding",
                        "amount_note": "the largest number the source's field holds, not a figure",
                    },
                    {
                        "kind": "award_floor",
                        "kind_label": "Award floor",
                        "amount": 1_000_000.0,
                        "currency": "USD",
                    },
                ],
            )
        )
    )
    assert view is not None
    assert (view["short"], view["compact"], view["usd_compact"], view["fx_credit"], view["fx_rate"]) == (
        "award ceiling",
        "$20M",
        None,
        None,
        None,
    )
    assert view["expected_awards"] == 5
    assert [(a["label"], a["text"], a["note"] is not None) for a in view["also_stated"]] == [
        ("Estimated total programme funding", None, True),
        ("Award floor", "$1,000,000", False),
    ]


def test_a_placeholder_and_an_unquoted_currency_each_say_why_there_is_no_figure() -> None:
    placeholder = opportunity_value_view(
        _opportunity(
            value=_value(
                amount=1.0,
                usd_equivalent=None,
                usd_is_approximate=False,
                fx_source=None,
                amount_note="under 100, a unit price or placeholder rather than a total",
            )
        )
    )
    assert placeholder is not None
    assert (
        placeholder["placeholder"],
        placeholder["stated"],
        placeholder["compact"],
        placeholder["fx_note"],
    ) == (
        True,
        "1 EUR",
        None,
        None,
    )
    unquoted = opportunity_value_view(
        _opportunity(
            value=_value(
                amount=20_000_000.0,
                currency="MKD",
                usd_equivalent=None,
                usd_is_approximate=False,
                fx_rate=None,
                fx_source=None,
                fx_note="no ECB reference rate for MKD",
            )
        )
    )
    assert unquoted is not None
    assert (unquoted["compact"], unquoted["usd_compact"], unquoted["fx_note"]) == (
        "MKD 20M",
        None,
        "no ECB reference rate for MKD",
    )
    assert opportunity_value_view(_opportunity(value=None)) is None
    assert opportunity_value_view({"public_id": "x"}) is None  # an API without `value`
    assert flatten_opportunity(_opportunity())["value_view"]["compact"] == "€300M"


def test_the_reference_registers_short_names_are_words_of_their_registered_names() -> None:
    sources = {
        s["id"]: s for s in yaml.safe_load((REPO_ROOT / "data" / "sources.yaml").read_text())["sources"]
    }
    for source_id, label in REFERENCE_SOURCE_LABELS.items():
        name = sources[source_id]["name"]
        assert all(word in name for word in label.split()), (source_id, label, name)


# ------------------------------------------------------------------------------- the pages
class FakeTransport:
    """Canned responses keyed by path; every call recorded with its params."""

    def __init__(self, responses: Mapping[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def get(self, url: str, *, params: Mapping[str, Any] | None = None, **_: Any) -> httpx.Response:
        self.calls.append((url, {k: v for k, v in (params or {}).items() if v is not None}))
        if url not in self.responses:
            return httpx.Response(404, json={"title": "not_found", "detail": url})
        status, body = self.responses[url]
        return httpx.Response(status, json=body)

    def post(self, url: str, **_: Any) -> httpx.Response:
        return httpx.Response(404, json={"title": "not_found"})

    def request(
        self, method: str, url: str, *, params: Mapping[str, Any] | None = None, **_: Any
    ) -> httpx.Response:
        return self.get(url, params=params)

    def close(self) -> None:
        pass


@pytest.fixture()
def client() -> Iterator[TestClient]:
    _reset()
    with TestClient(web_app) as c:
        yield c
    _reset()


def _reset() -> None:
    for key in ("api_client", "lag_days_default", "coverage_facts", "build_info", "sitemap_cache"):
        web_app.state.__dict__.pop(key, None)


def _install(rows: list[dict[str, Any]], *, credits: list[dict[str, Any]] | None = None) -> FakeTransport:
    transport = FakeTransport(
        {
            "/v1/health": (200, {"status": "ok", "lag_days_default": {"supply": 0, "opportunities": 0}}),
            "/v1/meta/vocabularies": (
                200,
                {
                    "data": {
                        "opportunity_kind": [{"value": "tender"}],
                        "opportunity_status": [{"value": "open"}],
                    }
                },
            ),
            "/v1/sources": (200, {"data": []}),
            "/v1/opportunities": (
                200,
                {
                    "data": rows,
                    "page": {"has_more": False, "next_cursor": None, "prev_cursor": None},
                    "meta": {"total": len(rows), "total_is_estimate": False},
                    "licence_summary": {"sources": credits or []},
                },
            ),
        }
    )
    web_app.state.api_client = ApiClient(transport)
    web_app.state.lag_days_default = None
    return transport


def _value_cell(body: str, slug: str) -> str:
    row = re.search(r"<tr>(?:(?!</tr>).)*?/opportunities/" + re.escape(slug) + r'".*?</tr>', body, re.S)
    assert row, slug
    cell = re.search(r'<td data-label="Value".*?</td>', row.group(0), re.S)
    assert cell
    return cell.group(0)


def test_the_list_shows_each_value_with_what_it_is_and_sorts_by_it(client: TestClient) -> None:
    placeholder = _opportunity(
        "unit-price",
        value=_value(
            amount=1.0,
            usd_equivalent=None,
            usd_is_approximate=False,
            fx_source=None,
            amount_note="a unit price",
        ),
    )
    ceiling = _opportunity(
        "grants",
        value=_value(
            amount=20_000_000.0,
            currency="USD",
            kind="award_ceiling",
            kind_label="Award ceiling",
            usd_equivalent=20_000_000.0,
            usd_is_approximate=False,
            fx_source=None,
        ),
    )
    credits = [
        {
            "source_id": "eu.ted.api",
            "name": "TED",
            "attribution_text": "Source: Publications Office of the EU",
        },
        {"source_id": "eu.ecb.eurofxref", "name": FX_SOURCE["source_name"], "attribution_text": ECB_CREDIT},
    ]
    transport = _install(
        [_opportunity(), ceiling, placeholder, _opportunity("no-value", value=None)], credits=credits
    )

    body = client.get("/opportunities", params={"sort": "-value"}).text

    assert transport.calls[[u for u, _ in transport.calls].index("/v1/opportunities")][1]["sort"] == "-value"
    heading = re.search(
        r'<th scope="col" class="sortable num is-sorted" aria-sort="descending">'
        r'<a class="sort-link" href="([^"]+)"',
        body,
    )
    assert heading and heading.group(1).endswith("sort=value")  # the second click: smallest first
    converted = _value_cell(body, "heat-network")
    assert '<span class="cell-value__amount">€300M</span>' in converted
    assert '<span class="cell-value__usd"> &asymp;&#8239;$335M</span>' in converted
    assert '<span class="cell-value__kind"> est. contract value</span>' in converted
    assert "about $335 million at the ECB reference rate of 7 Oct 2026" in converted
    dollars = _value_cell(body, "grants")
    assert "$20M" in dollars and "award ceiling" in dollars and "cell-value__usd" not in dollars
    stated = _value_cell(body, "unit-price")
    assert "—" in stated and "stated as 1 EUR" in stated and "€1" not in stated
    assert 'is-empty">—</td>' in _value_cell(body, "no-value")
    # A converted figure on the page: the ECB's credit, verbatim, in the sources line.
    assert re.search(
        r"Sources of these rows:.*ECB \(" + re.escape(ECB_CREDIT.replace("'", "&#39;")), body, re.S
    )


def test_the_value_heading_sorts_largest_first_on_a_first_click(client: TestClient) -> None:
    _install([_opportunity()])

    body = client.get("/opportunities").text

    assert re.search(
        r'<a class="sort-link" href="/opportunities\?sort=-value" '
        r'title="Sort by value in US dollars, largest first"',
        body,
    )


def _record_page(client: TestClient, entity: dict[str, Any]) -> str:
    _install([entity])
    resp = client.get(f"/opportunities/{entity['slug']}")
    assert resp.status_code == 200, resp.text[:300]
    match = re.search(r'<div class="field-grid__wide" id="opportunity-value">.*?</dd></div>', resp.text, re.S)
    assert match
    return match.group(0) + resp.text[match.end() : match.end() + 400]


def test_the_record_page_shows_the_value_with_its_source_the_fx_date_and_the_ecb_credit(
    client: TestClient,
) -> None:
    row = _record_page(client, _opportunity())

    assert "<dt>Estimated value</dt>" in row
    assert '<span class="value-figure tnum">€300,000,000</span>' in row
    # Three significant figures: a reference rate makes the further digits noise (fix round).
    assert "About $335 million at the ECB euro reference rate of 7 Oct 2026 (1 EUR = 1.1177 USD)" in row
    assert re.search(
        r'From <a href="https://ted.europa.eu/[^"]+"[^>]*>TED</a>, '
        r'field <span class="mono">total-value</span>, read 9 Oct 2026\.',
        row,
    )
    credit = re.search(r'<span class="value-source" id="opportunity-value-credit">(.*?)</span>', row, re.S)
    # The credit exactly as the register states it: nothing appended to it (fix round).
    assert credit and credit.group(1).startswith(ECB_CREDIT.replace("'", "&#39;") + " &middot; <a ")
    assert FX_SOURCE["licence_url"] in credit.group(1)


def test_the_record_page_lists_the_other_figures_and_the_expected_awards(client: TestClient) -> None:
    entity = _opportunity(
        "grants",
        value=_value(
            amount=20_000_000.0,
            currency="USD",
            kind="award_ceiling",
            kind_label="Award ceiling",
            usd_equivalent=20_000_000.0,
            usd_is_approximate=False,
            fx_source=None,
            expected_awards=5,
            also_stated=[
                {"kind": "award_floor", "kind_label": "Award floor", "amount": 1_000_000.0, "currency": "USD"}
            ],
        ),
    )

    row = _record_page(client, entity)

    assert '<span class="value-figure tnum">$20,000,000</span>' in row
    assert "About $" not in row and 'id="opportunity-value-credit"' not in row  # nothing converted
    assert 'Also stated: award floor <span class="tnum">$1,000,000</span>' in row
    assert '<div><dt>Expected awards</dt><dd class="tnum">5</dd></div>' in row


def test_the_record_page_says_why_a_figure_is_missing(client: TestClient) -> None:
    unquoted = _opportunity(
        "skopje",
        value=_value(
            amount=20_000_000.0,
            currency="MKD",
            kind="awarded_value",
            kind_label="Total awarded value",
            usd_equivalent=None,
            usd_is_approximate=False,
            fx_rate=None,
            fx_source=None,
            fx_note="no ECB reference rate for MKD",
        ),
    )
    row = _record_page(client, unquoted)
    assert "MKD 20,000,000" in row and "No US-dollar figure: no ECB reference rate for MKD" in row

    _reset()
    placeholder = _opportunity(
        "unit-price",
        value=_value(
            amount=1.0,
            usd_equivalent=None,
            usd_is_approximate=False,
            fx_source=None,
            amount_note="a unit price",
        ),
    )
    row = _record_page(client, placeholder)
    assert (
        "&mdash; <small>stated as 1 EUR: a unit price</small>" in row
        or "— <small>stated as 1 EUR: a unit price</small>" in row
    )
    assert "value-figure" not in row


# ------------------------------------------------------------------------- wording (fix round)
@pytest.mark.parametrize(
    ("amount", "text"),
    [
        (184_896_928.0, "$185 million"),
        (335_310_000.0, "$335 million"),
        (999_960_000.0, "$1 billion"),
        (1_254_000_000.0, "$1.25 billion"),
        (48_912.0, "$48,900"),
        (640.4, "$640"),
        (12.34, "$12.3"),
        (0.0, None),
        (None, None),
    ],
)
def test_an_approximate_dollar_figure_is_given_to_three_significant_figures(amount: Any, text: Any) -> None:
    """ "About $184,896,928" claims a precision a reference rate does not have (reviewer's nit)."""
    import web.viewmodels as viewmodels

    assert viewmodels.money_approx(amount, "USD") == text


def test_an_also_stated_label_keeps_its_capitals_in_mid_sentence(client: TestClient) -> None:
    entity = _opportunity(
        value=_value(
            also_stated=[
                {
                    "kind": "estimated_value_incl_vat",
                    "kind_label": "Estimated value including VAT",
                    "amount": 360_000_000.0,
                    "currency": "EUR",
                }
            ]
        )
    )

    row = _record_page(client, entity)

    assert 'Also stated: estimated value including VAT <span class="tnum">€360,000,000</span>' in row
    assert "including vat" not in row
