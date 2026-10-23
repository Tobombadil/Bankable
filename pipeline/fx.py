"""eu.ecb.eurofxref — the ECB's euro foreign exchange reference rates, for an approximate USD
equivalent of a notice's stated value.

Why: notices state their value in their own currency (on 2026-10-20 the store held TED notices in
EUR, RON, PLN, CZK, NOK, DKK, ISK, SEK, CHF, HUF and MKD, Find a Tender in GBP, World Bank and
Grants.gov in USD). `GET /v1/opportunities?sort=-value` ranks them on one scale, the stated amount
converted to US dollars. The stated amount and currency stay the record's value; the USD figure is
an approximation for sorting and filtering, labelled as one wherever it is shown.

Rates. On each TARGET business day, at about 16:00 CET, the ECB publishes how many units of each of
about thirty currencies one euro buys. The USD rate of a currency C is the cross rate
`(USD per EUR) / (C per EUR)` on one fixing date: EUR converts at `USD per EUR`, USD at 1 with no
fixing (nothing is converted). The cross rate is our calculation, which the ECB's terms require us
to say (`data/sources.yaml` `changes_statement`).

Date rule (`RateTable.convert`). The fixing of the notice's publication date, or of the most recent
earlier business day when that date had none (a weekend or TARGET holiday). When the table holds no
fixing on or before that date (a notice older than the vendored seed, before the first refresh
fetches the full history), the earliest fixing quoting the currency is used, the nearest in time;
when the notice states no date, the latest. `fx_date` always names the fixing used, so a reader sees
how far it is from the notice's date.

A currency the ECB does not quote has no USD equivalent and nothing is substituted. Of the store's
currencies that is MKD (the 2026-10-22 table quotes none), which sorts with the unstated values.

Fetch (`refresh`). One GET of `eurofxref-hist.zip` (about 640 KB, every fixing since 1999-01-04),
conditional on the previous `Last-Modified`. robots.txt allows `/stats/eurofxref/` and asks a
crawl delay of 5 s; a daily refresh makes one request. Stored under the data root at
`snapshots/eu.ecb.eurofxref/eurofxref-hist.csv` with a `.json` beside it (`retrieved_at`, URL,
SHA-256 of the zip, `Last-Modified`). `data/vendored/fx/eurofxref-hist-from-2025.csv` (fixings from
2025-01-02 to 2026-10-21, retrieved 2026-10-22) answers until the first refresh; a fetched fixing
replaces the vendored one of the same date.

Terms (ECB "Disclaimer & Copyright", read 2026-10-22, quoted in `data/sources.yaml`): free use,
provided the information appears accurately and the ECB is cited as the source; a modification
(here, the USD cross rate) must be stated; and a document that is sold must say the information is
free from the ECB's website. The rates are "published for information purposes only".
"""

from __future__ import annotations

import argparse
import bisect
import dataclasses
import datetime as dt
import hashlib
import io
import json
import math
import os
import pathlib
import sys
import zipfile
from collections.abc import Mapping
from typing import Any

from pipeline.connectors.http import PoliteSession

SOURCE_ID = "eu.ecb.eurofxref"
HIST_ZIP_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip"
HIST_MEMBER = "eurofxref-hist.csv"
ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDORED_CSV = ROOT / "data" / "vendored" / "fx" / "eurofxref-hist-from-2025.csv"
#: robots.txt `Crawl-delay: 5` (read 2026-10-22).
ECB_RPS = 0.2


class FxError(Exception):
    """The ECB file could not be read as the reference-rate history."""


def data_root() -> pathlib.Path:
    """The connector data root (`pipeline.connectors.store.DATA_DIR`), read at call time so a
    process that sets `INFRAQUE_DATA_DIR` after import is honoured."""
    return pathlib.Path(os.environ.get("INFRAQUE_DATA_DIR") or ROOT / "data")


def cache_dir(root: pathlib.Path | None = None) -> pathlib.Path:
    return (root or data_root()) / "snapshots" / SOURCE_ID


# ------------------------------------------------------------------------------------- parsing
def parse_hist_csv(text: str) -> dict[dt.date, dict[str, float]]:
    """`Date,USD,JPY,...` rows (the ECB's own layout: a trailing comma on every line, `N/A` where a
    currency was not quoted that day) as `{date: {currency: units per EUR}}`. A non-positive or
    non-numeric cell is no quote. Raises `FxError` when the header is not the ECB's."""
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        raise FxError("empty reference-rate file")
    header = [h.strip() for h in lines[0].split(",")]
    if not header or header[0] != "Date" or "USD" not in header:
        raise FxError(f"not the ECB reference-rate layout: {lines[0][:80]!r}")
    fixings: dict[dt.date, dict[str, float]] = {}
    for line in lines[1:]:
        cells = [c.strip() for c in line.split(",")]
        try:
            day = dt.date.fromisoformat(cells[0])
        except ValueError as exc:
            raise FxError(f"bad fixing date {cells[0]!r}") from exc
        quotes: dict[str, float] = {}
        for code, cell in zip(header[1:], cells[1:], strict=False):
            if not code or cell in ("", "N/A"):
                continue
            try:
                value = float(cell)
            except ValueError:
                continue
            if math.isfinite(value) and value > 0:
                quotes[code] = value
        if quotes:
            fixings[day] = quotes
    if not fixings:
        raise FxError("no fixings in the reference-rate file")
    return fixings


def hist_csv_from_zip(content: bytes) -> str:
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as z:
            return z.read(HIST_MEMBER).decode("utf-8")
    except (zipfile.BadZipFile, KeyError, UnicodeDecodeError) as exc:
        raise FxError(f"not the ECB history archive: {exc}") from exc


# --------------------------------------------------------------------------------- conversion
@dataclasses.dataclass(frozen=True)
class Conversion:
    """A stated amount in US dollars: `usd_equivalent = amount * rate`, `rate` being USD per one
    unit of the stated currency on `fx_date` (None, with `fx_source` None, for an amount already
    stated in USD)."""

    usd_equivalent: float
    rate: float
    fx_date: dt.date | None
    fx_source: str | None

    def as_json(self) -> dict[str, Any]:
        return {
            "usd_equivalent": self.usd_equivalent,
            "fx_rate": self.rate,
            "fx_date": self.fx_date.isoformat() if self.fx_date else None,
            "fx_source": self.fx_source,
        }


class RateTable:
    """Fixings by date, with the cross-rate lookups `convert` needs. `retrieved_at` and `origin`
    say where the newest fixings came from (`fetched`, `vendored`); `retrieved_at_on(day)` says
    when the file that supplied one date's fixing was read (`read_at`, by date; `retrieved_at`
    for a date it does not name)."""

    def __init__(
        self,
        fixings: Mapping[dt.date, Mapping[str, float]],
        *,
        retrieved_at: str | None = None,
        origin: str = "vendored",
        read_at: Mapping[dt.date, str | None] | None = None,
    ) -> None:
        self.fixings: dict[dt.date, dict[str, float]] = {d: dict(q) for d, q in fixings.items()}
        self.retrieved_at = retrieved_at
        self.origin = origin
        self._read_at: dict[dt.date, str | None] = dict(read_at or {})
        self._series: dict[str, list[dt.date]] = {}

    def __len__(self) -> int:
        return len(self.fixings)

    @property
    def first_date(self) -> dt.date | None:
        return min(self.fixings) if self.fixings else None

    @property
    def last_date(self) -> dt.date | None:
        return max(self.fixings) if self.fixings else None

    def currencies(self) -> set[str]:
        return {c for q in self.fixings.values() for c in q} | {"EUR"}

    def retrieved_at_on(self, day: dt.date | None) -> str | None:
        """When the file that supplied `day`'s fixing was read (the table's `retrieved_at` for no
        date). A fetched history laid over the vendored seed keeps each date's own: a fixing only
        the seed holds was read when the seed was (review of lane rfp-value, 2026-10-22: every
        fixing was credited with the newest file's read time)."""
        if day is None:
            return self.retrieved_at
        return self._read_at.get(day, self.retrieved_at)

    def merged_with(self, newer: RateTable) -> RateTable:
        """This table with `newer`'s fixings laid over it, date by date; `newer`'s provenance for
        the table, and each date's own read time (`retrieved_at_on`)."""
        combined = dict(self.fixings)
        combined.update(newer.fixings)
        read_at = {d: self.retrieved_at_on(d) for d in self.fixings}
        read_at.update({d: newer.retrieved_at_on(d) for d in newer.fixings})
        return RateTable(combined, retrieved_at=newer.retrieved_at, origin=newer.origin, read_at=read_at)

    def _dates_quoting(self, currency: str) -> list[dt.date]:
        """Sorted fixing dates on which both USD and `currency` are quoted (EUR: every USD date)."""
        if currency not in self._series:
            need = {"USD"} if currency == "EUR" else {"USD", currency}
            self._series[currency] = sorted(d for d, q in self.fixings.items() if need <= q.keys())
        return self._series[currency]

    def fixing_for(self, currency: str, on: dt.date | None) -> dt.date | None:
        """The fixing `convert` uses (module docstring "Date rule"), or None when no fixing quotes
        the currency at all."""
        dates = self._dates_quoting(currency)
        if not dates:
            return None
        if on is None:
            return dates[-1]
        i = bisect.bisect_right(dates, on)
        return dates[i - 1] if i else dates[0]

    def _quotes(self, currency: str, on: dt.date | None) -> tuple[float, float, dt.date] | None:
        """(USD per EUR, currency per EUR, fixing date) for `currency`'s fixing for `on`."""
        day = self.fixing_for(currency, on)
        if day is None:
            return None
        quotes = self.fixings[day]
        return quotes["USD"], 1.0 if currency == "EUR" else quotes[currency], day

    def usd_rate(self, currency: str, on: dt.date | None) -> tuple[float, dt.date] | None:
        """USD per one unit of `currency` at its fixing for `on` (the ECB's USD per euro over the
        currency's units per euro, unrounded), and that fixing's date."""
        found = self._quotes(currency, on)
        if found is None:
            return None
        usd_per_eur, per_eur, day = found
        return usd_per_eur / per_eur, day

    def convert(self, amount: float | None, currency: str | None, on: dt.date | None) -> Conversion | None:
        """`amount` of `currency` in US dollars, rounded to the cent, or None when there is no
        amount, no currency, or no ECB rate for the currency. USD converts to itself. The rate is
        the ECB's USD per euro over the currency's units per euro, unrounded, and the equivalent is
        exactly `round(amount * rate, 2)`, so anyone can reproduce it from the two published figures
        (review of lane rfp-value, 2026-10-22: a rate rounded to eight significant digits beside an
        equivalent computed from the unrounded one disagreed by dollars on billion-scale HUF or CZK
        amounts)."""
        if amount is None or currency is None:
            return None
        if not math.isfinite(float(amount)):
            return None
        code = str(currency).strip().upper()
        if code == "USD":
            return Conversion(round(float(amount), 2), 1.0, None, None)
        found = self._quotes(code, on)
        if found is None:
            return None
        usd_per_eur, per_eur, day = found
        rate = usd_per_eur / per_eur
        return Conversion(round(float(amount) * rate, 2), rate, day, SOURCE_ID)


def table_from_csv(path: pathlib.Path, *, origin: str, retrieved_at: str | None = None) -> RateTable:
    return RateTable(
        parse_hist_csv(path.read_text(encoding="utf-8")), retrieved_at=retrieved_at, origin=origin
    )


def _read_meta(directory: pathlib.Path) -> dict[str, Any]:
    try:
        meta = json.loads((directory / f"{HIST_MEMBER}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return meta if isinstance(meta, dict) else {}


#: VENDORED retrieval date (data/vendored/fx/README.md).
VENDORED_RETRIEVED_AT = "2026-10-22T01:01:38Z"


def load_table(root: pathlib.Path | None = None, *, vendored: pathlib.Path | None = None) -> RateTable:
    """The vendored seed with the last fetched history laid over it (module docstring). An empty
    table when neither file can be read: every non-USD amount then has no equivalent."""
    seed_path = vendored if vendored is not None else VENDORED_CSV
    try:
        table = table_from_csv(seed_path, origin="vendored", retrieved_at=VENDORED_RETRIEVED_AT)
    except (OSError, FxError):
        table = RateTable({}, origin="none")
    directory = cache_dir(root)
    fetched_path = directory / HIST_MEMBER
    if fetched_path.exists():
        meta = _read_meta(directory)
        try:
            fetched = table_from_csv(fetched_path, origin="fetched", retrieved_at=meta.get("retrieved_at"))
        except (OSError, FxError):
            return table
        table = table.merged_with(fetched)
    return table


_DEFAULT: dict[str, Any] = {}


def default_table() -> RateTable:
    """`load_table()` for this process, re-read when either file changes (mtime and size)."""
    paths = [VENDORED_CSV, cache_dir() / HIST_MEMBER]
    key = tuple(
        (str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else (str(p), 0, 0) for p in paths
    )
    if _DEFAULT.get("key") != key:
        _DEFAULT["key"] = key
        _DEFAULT["table"] = load_table()
    table: RateTable = _DEFAULT["table"]
    return table


# ------------------------------------------------------------------------------------- refresh
@dataclasses.dataclass
class RefreshResult:
    status: str  # "ok" | "unchanged"
    fixings: int
    last_date: str | None
    retrieved_at: str
    path: str

    def as_json(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


def refresh(
    http: PoliteSession | None = None, root: pathlib.Path | None = None, now: dt.datetime | None = None
) -> RefreshResult:
    """Fetch the history archive (one conditional GET; robots.txt honoured) and store its CSV and
    metadata under the data root. Raises `FxError` on anything that is not the ECB's archive and
    leaves the stored file as it was."""
    from urllib.parse import urlsplit

    session = http or PoliteSession(rate_limits={urlsplit(HIST_ZIP_URL).netloc: ECB_RPS})
    directory = cache_dir(root)
    meta = _read_meta(directory)
    stamp = (now or dt.datetime.now(dt.UTC)).astimezone(dt.UTC).isoformat(timespec="seconds")
    headers = {}
    stored = directory / HIST_MEMBER
    if meta.get("last_modified") and stored.exists():
        headers["If-Modified-Since"] = str(meta["last_modified"])
    resp = session.get(HIST_ZIP_URL, headers=headers, timeout=120)
    if resp.status_code == 304 and stored.exists():
        table = table_from_csv(stored, origin="fetched")
        meta["checked_at"] = stamp
        (directory / f"{HIST_MEMBER}.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
        last = table.last_date.isoformat() if table.last_date else None
        return RefreshResult(
            "unchanged", len(table), last, str(meta.get("retrieved_at") or stamp), str(stored)
        )
    if resp.status_code != 200:
        raise FxError(f"GET {HIST_ZIP_URL} -> HTTP {resp.status_code}")
    content = bytes(resp.content)
    text = hist_csv_from_zip(content)
    fixings = parse_hist_csv(text)  # validates before anything is written
    directory.mkdir(parents=True, exist_ok=True)
    tmp = directory / f"{HIST_MEMBER}.tmp"
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(stored)
    last_date = max(fixings).isoformat()
    new_meta = {
        "source_id": SOURCE_ID,
        "url": HIST_ZIP_URL,
        "retrieved_at": stamp,
        "checked_at": stamp,
        "sha256": hashlib.sha256(content).hexdigest(),
        "last_modified": resp.headers.get("Last-Modified"),
        "fixings": len(fixings),
        "last_fixing": last_date,
    }
    (directory / f"{HIST_MEMBER}.json").write_text(json.dumps(new_meta, indent=1), encoding="utf-8")
    return RefreshResult("ok", len(fixings), last_date, stamp, str(stored))


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - thin CLI over tested functions
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("refresh", help="fetch the ECB history archive into the data root")
    show = sub.add_parser("show", help="the table in force, or one conversion")
    show.add_argument("--amount", type=float)
    show.add_argument("--currency")
    show.add_argument("--date", type=dt.date.fromisoformat)
    args = parser.parse_args(argv)
    if args.cmd == "refresh":
        sys.stdout.write(json.dumps(refresh().as_json(), indent=1) + "\n")
        return 0
    table = load_table()
    out: dict[str, Any] = {
        "origin": table.origin,
        "retrieved_at": table.retrieved_at,
        "fixings": len(table),
        "first": table.first_date.isoformat() if table.first_date else None,
        "last": table.last_date.isoformat() if table.last_date else None,
    }
    if args.amount is not None and args.currency:
        conv = table.convert(args.amount, args.currency, args.date)
        out["conversion"] = conv.as_json() if conv else None
    sys.stdout.write(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
