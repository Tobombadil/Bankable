"""ECB reference rates and USD equivalents (`pipeline/fx.py`, lane rfp-value) on a recorded slice
of the ECB's own history file (`tests/fixtures/ecb_eurofxref_hist.csv`). No network: `refresh` is
driven by a stub session."""

from __future__ import annotations

import datetime as dt
import io
import json
import pathlib
import zipfile
from typing import Any

import pytest

from pipeline import fx

FIXTURE = pathlib.Path(__file__).resolve().parent / "fixtures" / "ecb_eurofxref_hist.csv"


@pytest.fixture(scope="module")
def table() -> fx.RateTable:
    return fx.RateTable(fx.parse_hist_csv(FIXTURE.read_text(encoding="utf-8")), origin="fetched")


def test_the_ecb_layout_parses_with_its_trailing_comma_and_na_cells(table: fx.RateTable) -> None:
    assert len(table) == 22
    assert (table.first_date, table.last_date) == (dt.date(2015, 10, 12), dt.date(2026, 10, 21))
    latest = table.fixings[dt.date(2026, 10, 21)]
    assert latest["USD"] == 1.1234 and latest["RON"] == 5.3365 and latest["GBP"] == 0.83825
    # `N/A` is no quote, never zero: MTL left the table in 2008, ISK was not quoted in 2015.
    assert "MTL" not in latest and "ISK" not in table.fixings[dt.date(2015, 10, 13)]
    assert "" not in latest  # the trailing comma's empty header is not a currency


def test_a_file_that_is_not_the_ecb_layout_is_refused() -> None:
    with pytest.raises(fx.FxError):
        fx.parse_hist_csv("<html>Just a moment</html>")
    with pytest.raises(fx.FxError):
        fx.parse_hist_csv("Date,USD,\n")  # a header and no fixings


def test_euro_converts_at_the_usd_rate_of_the_publication_date(table: fx.RateTable) -> None:
    conv = table.convert(1_000_000, "EUR", dt.date(2026, 10, 6))
    assert conv is not None
    assert conv.usd_equivalent == 1_126_900.00  # 1,000,000 x 1.1269
    assert (conv.rate, conv.fx_date, conv.fx_source) == (1.1269, dt.date(2026, 10, 6), "eu.ecb.eurofxref")


def test_other_currencies_convert_through_the_euro_cross_rate(table: fx.RateTable) -> None:
    # 2026-10-21: USD 1.1234 and RON 5.3365 per euro, so RON 5,336,500 is EUR 1,000,000 is USD 1,123,400.
    ron = table.convert(5_336_500, "RON", dt.date(2026, 10, 21))
    assert ron is not None and ron.usd_equivalent == 1_123_400.00
    assert ron.rate == 1.1234 / 5.3365  # unrounded: the equivalent is reproducible from it
    huf = table.convert(365_000_000, "HUF", dt.date(2026, 10, 21))  # HUF 365 per euro that day
    assert huf is not None and huf.usd_equivalent == 1_123_400.00
    gbp = table.convert(800_000_000, "GBP", dt.date(2026, 10, 6))  # 1.1269 / 0.8488
    assert gbp is not None and gbp.usd_equivalent == 1_062_111_215.83


@pytest.mark.parametrize("currency", ["HUF", "CZK", "ISK", "PLN", "SEK", "GBP", "CHF"])
def test_the_published_rate_reproduces_the_equivalent_on_billion_scale_amounts(
    table: fx.RateTable, currency: str
) -> None:
    """Review of lane rfp-value (2026-10-22): `fx_rate` was rounded to eight significant digits
    while `usd_equivalent` came from the unrounded cross rate, so on a billion-scale HUF or CZK
    amount `amount x fx_rate` missed the published equivalent by dollars. Now it is the published
    equivalent, to the cent, from the published rate alone."""
    for amount in (17_100_000_000.0, 36_900_000_000.0, 123_456_789_012.34):
        conv = table.convert(amount, currency, dt.date(2026, 10, 6))
        assert conv is not None
        assert round(amount * conv.rate, 2) == conv.usd_equivalent
        found = table.usd_rate(currency, dt.date(2026, 10, 6))
        assert found is not None and found[0] == conv.rate


def test_a_day_without_a_fixing_takes_the_most_recent_earlier_one(table: fx.RateTable) -> None:
    friday = table.convert(10_000, "RON", dt.date(2026, 9, 4))
    for weekend in (dt.date(2026, 9, 5), dt.date(2026, 9, 6)):
        conv = table.convert(10_000, "RON", weekend)
        assert conv == friday and conv is not None and conv.fx_date == dt.date(2026, 9, 4)
    # After the table's last fixing: that fixing, named.
    later = table.convert(10_000, "RON", dt.date(2026, 10, 25))
    assert later is not None and later.fx_date == dt.date(2026, 10, 21)


def test_a_date_before_every_fixing_takes_the_earliest_and_no_date_the_latest(table: fx.RateTable) -> None:
    """Before the table's first fixing the nearest one in time is the earliest (review of lane
    rfp-value, 2026-10-22: the latest gave a 2015 notice a 2026 rate)."""
    before = table.convert(10_000, "RON", dt.date(2015, 10, 1))
    undated = table.convert(10_000, "RON", None)
    assert before is not None and undated is not None
    assert before.fx_date == dt.date(2015, 10, 12) and before.usd_equivalent == 2577.16
    assert undated.fx_date == dt.date(2026, 10, 21)
    # Per currency: ISK has no 2015 quote here, so a 2015 notice in ISK converts at its earliest.
    isk = table.convert(10_000, "ISK", dt.date(2015, 10, 13))
    assert isk is not None and isk.fx_date == dt.date(2026, 9, 4) and isk.usd_equivalent == 82.54


def test_usd_is_not_converted_and_an_unquoted_currency_has_no_equivalent(table: fx.RateTable) -> None:
    usd = table.convert(5_000_000, "usd", dt.date(2026, 10, 6))
    assert usd == fx.Conversion(5_000_000.0, 1.0, None, None)
    assert table.convert(10, "MKD", dt.date(2026, 10, 6)) is None  # the ECB quotes no MKD
    assert table.convert(None, "EUR", None) is None
    assert table.convert(10, None, None) is None
    assert table.convert(float("nan"), "EUR", None) is None


def test_the_conversion_serialises_with_its_date_and_source(table: fx.RateTable) -> None:
    conv = table.convert(247_670, "RON", dt.date(2026, 10, 6))
    assert conv is not None
    assert conv.as_json() == {
        "usd_equivalent": 52_158.35,
        "fx_rate": conv.rate,
        "fx_date": "2026-10-06",
        "fx_source": "eu.ecb.eurofxref",
    }


def test_the_vendored_seed_is_the_ecb_layout_and_covers_the_store_currencies() -> None:
    seed = fx.table_from_csv(fx.VENDORED_CSV, origin="vendored")
    assert seed.first_date is not None and seed.first_date <= dt.date(2025, 1, 3)
    assert seed.last_date == dt.date(2026, 10, 21)
    store = {"EUR", "RON", "PLN", "CZK", "NOK", "DKK", "ISK", "SEK", "CHF", "HUF", "GBP", "USD"}
    assert store <= seed.currencies()
    assert "MKD" not in seed.currencies()


def test_a_fetched_history_lays_over_the_seed_date_by_date(tmp_path: pathlib.Path) -> None:
    seed = tmp_path / "seed.csv"
    seed.write_text("Date,USD,RON,\n2026-10-21,1.0000,5.0000,\n2026-10-20,1.1215,5.3308,\n", encoding="utf-8")
    directory = fx.cache_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / fx.HIST_MEMBER).write_text(FIXTURE.read_text(encoding="utf-8"), encoding="utf-8")
    (directory / f"{fx.HIST_MEMBER}.json").write_text(json.dumps({"retrieved_at": "2026-10-22T01:01:38Z"}))
    merged = fx.load_table(tmp_path, vendored=seed)
    assert merged.origin == "fetched" and merged.retrieved_at == "2026-10-22T01:01:38Z"
    assert merged.fixings[dt.date(2026, 10, 21)]["USD"] == 1.1234  # the fetched fixing wins
    assert len(merged) == 22
    # Without a fetched file the seed answers, with its own provenance.
    alone = fx.load_table(tmp_path / "empty", vendored=seed)
    assert alone.origin == "vendored" and alone.fixings[dt.date(2026, 10, 21)]["USD"] == 1.0


def test_each_fixing_keeps_the_read_time_of_the_file_that_supplied_it(tmp_path: pathlib.Path) -> None:
    """Review of lane rfp-value (2026-10-22): a fixing only the vendored seed holds was credited with
    the newest file's read time (`merged_with` took the fetched file's for every date). A fetched
    history older than the seed leaves the seed's later fixings in force, read when the seed was."""
    seed = tmp_path / "seed.csv"
    seed.write_text("Date,USD,RON,\n2026-10-21,1.1234,5.3365,\n2026-10-20,1.1215,5.3308,\n", encoding="utf-8")
    directory = fx.cache_dir(tmp_path)
    directory.mkdir(parents=True)
    (directory / fx.HIST_MEMBER).write_text("Date,USD,RON,\n2026-10-20,1.1215,5.3308,\n", encoding="utf-8")
    (directory / f"{fx.HIST_MEMBER}.json").write_text(json.dumps({"retrieved_at": "2026-10-20T16:19:00Z"}))
    merged = fx.load_table(tmp_path, vendored=seed)
    assert merged.retrieved_at == "2026-10-20T16:19:00Z"
    assert merged.retrieved_at_on(dt.date(2026, 10, 20)) == "2026-10-20T16:19:00Z"  # the fetched file
    assert merged.retrieved_at_on(dt.date(2026, 10, 21)) == fx.VENDORED_RETRIEVED_AT  # only the seed
    assert merged.retrieved_at_on(None) == merged.retrieved_at
    from services.ingest.opportunity_value import value_block

    block = value_block(None, 1_000_000.0, "EUR", dt.date(2026, 10, 21), merged)
    assert block is not None and block["fx_date"] == "2026-10-21"
    assert block["fx_retrieved_at"] == fx.VENDORED_RETRIEVED_AT


def test_no_readable_file_is_an_empty_table_not_an_error(tmp_path: pathlib.Path) -> None:
    empty = fx.load_table(tmp_path, vendored=tmp_path / "missing.csv")
    assert len(empty) == 0 and empty.convert(1, "EUR", None) is None
    assert empty.convert(1, "USD", None) == fx.Conversion(1.0, 1.0, None, None)


class _Response:
    def __init__(self, status: int, content: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.status_code = status
        self.content = content
        self.headers = headers or {}


class _Session:
    """Answers the history URL from a queue of responses and records each request's headers."""

    def __init__(self, *responses: _Response) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, **kwargs: Any) -> _Response:
        self.calls.append((url, dict(kwargs.get("headers") or {})))
        return self.responses.pop(0)


def _zip(text: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(fx.HIST_MEMBER, text)
    return buf.getvalue()


def test_refresh_stores_the_history_then_asks_conditionally(tmp_path: pathlib.Path) -> None:
    body = _zip(FIXTURE.read_text(encoding="utf-8"))
    modified = "Wed, 21 Oct 2026 13:56:26 GMT"
    now = dt.datetime(2026, 10, 22, 1, 1, 38, tzinfo=dt.UTC)
    session = _Session(_Response(200, body, {"Last-Modified": modified}), _Response(304))
    first = fx.refresh(session, tmp_path, now=now)  # type: ignore[arg-type]
    assert (first.status, first.fixings, first.last_date) == ("ok", 22, "2026-10-21")
    assert session.calls[0] == (fx.HIST_ZIP_URL, {})
    meta = json.loads((fx.cache_dir(tmp_path) / f"{fx.HIST_MEMBER}.json").read_text())
    assert meta["last_modified"] == modified and meta["retrieved_at"] == "2026-10-22T01:01:38+00:00"
    assert meta["source_id"] == "eu.ecb.eurofxref" and len(meta["sha256"]) == 64
    second = fx.refresh(session, tmp_path, now=now + dt.timedelta(days=1))  # type: ignore[arg-type]
    assert second.status == "unchanged"
    assert session.calls[1][1] == {"If-Modified-Since": modified}
    assert fx.load_table(tmp_path).fixings[dt.date(2026, 10, 21)]["USD"] == 1.1234


def test_refresh_refuses_a_body_that_is_not_the_archive_and_keeps_the_stored_file(
    tmp_path: pathlib.Path,
) -> None:
    good = _zip(FIXTURE.read_text(encoding="utf-8"))
    fx.refresh(_Session(_Response(200, good)), tmp_path)  # type: ignore[arg-type]
    stored = (fx.cache_dir(tmp_path) / fx.HIST_MEMBER).read_bytes()
    for bad in (
        _Response(200, b"<html>maintenance</html>"),
        _Response(200, _zip("not,the,layout\n")),
        _Response(503),
    ):
        with pytest.raises(fx.FxError):
            fx.refresh(_Session(bad), tmp_path)  # type: ignore[arg-type]
    assert (fx.cache_dir(tmp_path) / fx.HIST_MEMBER).read_bytes() == stored


def test_the_registry_row_matches_the_module(tmp_path: pathlib.Path) -> None:
    from pipeline.connectors.registry import Registry

    entry = Registry().get(fx.SOURCE_ID)
    assert entry.url == fx.HIST_ZIP_URL
    assert entry.reuse == "attribution" and entry.publication == "raw_ok"
    assert entry.max_rps == pytest.approx(fx.ECB_RPS)  # robots.txt Crawl-delay 5, from the notes
    assert entry.credit_text is not None and "European Central Bank" in entry.credit_text
    assert "calculated by Inframation" in entry.credit_text  # the cross rate is stated as ours
