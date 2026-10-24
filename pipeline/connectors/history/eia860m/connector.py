"""us.eia.860m.history — EIA-860M, the Preliminary Monthly Electric Generator Inventory, one document
per month (`docs/24-trends.md` §9).

**What a month holds.** Every monthly workbook since July 2015 lists the US generators of 1 MW or
more on four sheets: Operating, Planned, Retired, and Canceled or Postponed. A document tallies
each sheet (generators counted, net summer MW summed) by technology, state and status, the
Planned sheet by expected year too, the Operating sheet's planned retirements by their year, and
the Retired sheet by retirement year. The Operating and Planned sheets are also tallied by state
and status together (`by_state_status`) and by technology and status together
(`by_technology_status`), each key the pair joined by `PAIR` (`US-TX|U`, `solar|TS`), so a
state's or a technology's planned capacity splits by stage as the national figure does
(`GET /v1/stats/planned-vs-operating`, docs/24-trends.md §11.1; parser 1.1.0, 2026-10-24).

**Like for like** (`documents.py`). Three things changed across the archive, and each is in a
block's `basis` so no month-on-month or year-on-year change crosses it:

- *Capacity measure.* The 2015 workbooks state net summer capacity only; nameplate capacity
  arrives later. Every block sums net summer MW, the one measure in every month; nameplate MW is
  kept beside it (`nameplate_mw`) where the workbook states it, and is absent, never zero, where
  it does not.
- *The Retired sheet.* "Starting with March 2017 data, the Preliminary Monthly Electric Generator
  Inventory includes a comprehensive list of generators which retired since 2002" (EIA's index
  page, read 2026-10-23). Before that the sheet lists recent retirements only, so the retired
  block's basis says `retired-since-2002` from 2017-03 and `retired-partial` before.
- *Puerto Rico.* "Starting with March 2018 data ... also includes data from Puerto Rico" (same
  page); the current workbooks carry it on separate `*_PR` sheets. Every block counts the 50 states
  and the District of Columbia only, wherever a Puerto Rico row appears, so the national figures
  mean the same thing in 2015 and 2026; the rows left out are counted under `excluded`.

**Routes, in order of preference** (`base.ROUTE_RANK`):

1. `origin`: the newest month from EIA's own index page. Links under `/archive/` are never
   requested: eia.gov's robots.txt disallows `/*archive/` (read 2026-10-23), a wildcard rule
   Python 3.11's robots parser cannot evaluate, so the rule is applied here (as
   `pipeline/connectors/us_eia_860m/connector.py` does). The request is conditional on the
   validators of the last copy fetched from the same URL, by this source or by `us.eia.860m`.
2. `shared`: every monthly workbook `us.eia.860m` already stored, read from the snapshot store
   without a request.
3. `inbox`: files a person downloaded in a browser into `<data root>/inbox/us.eia.860m.history/`
   (EIA's `<month>_generator<year>.xlsx` files, or the mirror's yearly zips). robots.txt binds
   crawlers, not a person; this is the route docs/13 §2.15 names for archive years.
4. `mirror` (backfill only, when the manifest switches it on, `enabled: true`): Catalyst
   Cooperative's public archive of the same EIA files on Zenodo (`mirror` in
   `data/sources.yaml`), one zip a year. Zenodo's
   robots.txt allows `/records/<id>/files/...` and asks a 10-second crawl delay, which the
   session honours. The published `source_url` is still EIA's own address for that month's file;
   `retrieved_from` names the mirror, its DOI and the member read.

source_url: EIA's address for the month: the `xls/` file for the month fetched from the index,
otherwise the `archive/xls/` file (`archive_url` is always that address).
Reuse: US federal work, public domain; EIA asks for "Source: U.S. Energy Information
Administration (<release month>)" (docs/13 §2.11).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
import tempfile
import zipfile
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urljoin, urlsplit

from pipeline.connectors.base import ConnectorError, ParseError
from pipeline.connectors.history.base import (
    BLANK_LABEL,
    UNSTATED,
    XLSX_MAGIC,
    ArchiveItem,
    Block,
    HeldPeriod,
    HistoryConnector,
    MonthFile,
    StoredSnapshot,
    cell,
    column,
    find_header,
    normalise_header,
    number,
    open_workbook,
    stored_snapshots,
    text,
    year_of,
)
from pipeline.connectors.history.documents import MONTH_NAMES, month_number, period_of
from pipeline.connectors.http import HttpBlocked, HttpFailed
from pipeline.normalize import classify_tech

log = logging.getLogger(__name__)

SOURCE_ID = "us.eia.860m.history"
SHARED_SOURCE_ID = "us.eia.860m"
INDEX_URL = "https://www.eia.gov/electricity/data/eia860m/"
ARCHIVE_URL = "https://www.eia.gov/electricity/data/eia860m/archive/xls/{month}_generator{year}.xlsx"
LINK_RE = re.compile(r'href="([^"]+?generator\d{4}\.xlsx)"', re.I)
URL_PERIOD = re.compile(r"/([a-z]+)_generator(\d{4})\.xlsx$", re.I)
MEMBER_PERIOD = re.compile(r"(\d{4})-(\d{2})\.xlsx$")
TITLE_PERIOD = re.compile(r"as of\s+([A-Za-z]+)\.?\s+(\d{4})", re.I)
#: The first month the Retired sheet lists every retirement since 2002 (module docstring).
RETIRED_COMPLETE_FROM = "2017-03"
#: Placeholder months answer HTML with a 200, so the first real workbook wins (docs/02 §7).
MAX_CANDIDATES = 6
INBOX = Path("inbox") / SOURCE_ID

US_STATES_DC = frozenset(
    "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM "
    "NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY".split()
)
#: The sheet of each block, by its exact (case-folded) name; `*_PR` sheets are never read.
SHEETS: dict[str, tuple[str, ...]] = {
    "operating": ("operating",),
    "planned": ("planned",),
    "retired": ("retired",),
    "canceled_postponed": ("canceled or postponed", "cancelled or postponed"),
}
#: EIA's status codes on each sheet ("(OP) Operating"); any other is counted and flagged.
KNOWN_STATUS: dict[str, frozenset[str]] = {
    "operating": frozenset({"OP", "SB", "OS", "OA"}),
    "planned": frozenset({"P", "L", "T", "U", "V", "TS", "OT"}),
}
STATUS_CODE = re.compile(r"^\(\s*([A-Za-z]{1,3})\s*\)")
#: EIA technology labels the shared classifier (`pipeline.normalize.classify_tech`) misreads, read
#: here by the label itself: "without Energy Storage" matches its storage rule, "Integrated
#: Gasification Combined Cycle" its combined-cycle rule, and a flywheel is storage. Every other
#: label is the register's reading (`technology_labels` in each document records the mapping used).
EIA_TECHNOLOGY: dict[str, str] = {
    "Coal Integrated Gasification Combined Cycle": "coal",
    "Solar Thermal with Energy Storage": "solar_thermal",
    "Solar Thermal without Energy Storage": "solar_thermal",
    "Flywheels": "storage",
}
NET_SUMMER = ("Net Summer Capacity (MW)",)
NAMEPLATE = ("Nameplate Capacity (MW)",)
BASIS_ROOT = "eia860m"
GEOGRAPHY = "50-states-dc"
MEASURE = "net-summer-mw"
#: Joins a state or a technology and a status code into one key of a paired tally (`US-TX|U`). Never
#: in a state key (`US-XX`) or a technology token (`[a-z_]+`), so the key splits at its first `|`.
PAIR = "|"
#: The paired tallies, each a dimension and the status (module docstring): `by_state_status` and
#: `by_technology_status` on the sheets that state a status.
PAIRED: dict[str, str] = {"state_status": "state", "technology_status": "technology"}
#: What each sheet is tallied by (`Block.dimensions`).
DIMENSIONS: dict[str, tuple[str, ...]] = {
    "operating": ("technology", "state", "status", *PAIRED),
    "planned": ("technology", "state", "status", "expected_year", *PAIRED),
    "retired": ("technology", "state", "retirement_year"),
    "canceled_postponed": ("technology", "state"),
}


def pair_key(key: str | None, status: str | None) -> str:
    """The paired-tally key of a state or a technology and a status code; a part the row leaves
    blank is `_unstated`, as `Block.add` counts it in the single tally, so each paired tally summed
    onto either key is that key's single tally."""
    return f"{key or UNSTATED}{PAIR}{status or UNSTATED}"


def split_pair(key: str) -> tuple[str, str]:
    """`("US-TX", "U")` for `US-TX|U`: the inverse of `pair_key`."""
    first, _, status = key.partition(PAIR)
    return first, status


def archive_url(period: str) -> str:
    year, month = period.split("-")
    return ARCHIVE_URL.format(month=MONTH_NAMES[int(month) - 1], year=year)


def period_from_url(url: str) -> str | None:
    match = URL_PERIOD.search(urlsplit(url).path)
    if match is None:
        return None
    month = month_number(match.group(1))
    return period_of(int(match.group(2)), month) if month else None


def period_from_name(name: str) -> str | None:
    """`eia860m-2015-07.xlsx` (the mirror's members) or `july_generator2015.xlsx` (EIA's names)."""
    match = MEMBER_PERIOD.search(name)
    if match is not None and 1 <= int(match.group(2)) <= 12:
        return period_of(int(match.group(1)), int(match.group(2)))
    return period_from_url("/" + name.rsplit("/", 1)[-1])


def find_xlsx_links(html: str, base: str = INDEX_URL) -> list[str]:
    """Candidate workbook URLs in page order, de-duplicated, `/archive/` paths dropped before any
    request (module docstring)."""
    out: list[str] = []
    for href in LINK_RE.findall(html):
        url = href if href.startswith("http") else urljoin(base, href)
        if "/archive/" in url or url in out:
            continue
        out.append(url)
    return out


def technology_token(label: str | None) -> str:
    """The technology bucket of an EIA label (`EIA_TECHNOLOGY`, else the register's classifier)."""
    if not label:
        return "unknown"
    return EIA_TECHNOLOGY.get(label) or classify_tech(label)[0]


def status_code(value: Any) -> str | None:
    label = text(value)
    if label is None:
        return None
    match = STATUS_CODE.match(label)
    return match.group(1).upper() if match else label.upper()


class Connector(HistoryConnector):
    source_id: ClassVar[str] = SOURCE_ID
    shared_source_id: ClassVar[str | None] = SHARED_SOURCE_ID
    #: 1.1.0 (2026-10-24): the paired tallies (module docstring); every stored month is restated
    #: from its own snapshot (`raw.snapshot`) on the next run.
    parser_version: ClassVar[str] = "1.1.0"

    def rate_limits(self) -> dict[str, float]:
        limits = {self.source.host: self.source.max_rps}
        mirror = self.mirror
        if mirror:
            host = urlsplit(str(mirror.get("datapackage_url") or "")).netloc
            if host:
                limits[host] = float(mirror.get("rate_limit_rps") or 0.1)
        return limits

    @property
    def mirror(self) -> Mapping[str, Any] | None:
        """The manifest's `mirror` block, when it is switched on (`enabled: true`): the block records
        the mirror and its terms either way, and `enabled` is the one switch (docs/24 §9.1)."""
        value = self.source.raw.get("mirror")
        if not isinstance(value, Mapping) or not value.get("datapackage_url"):
            return None
        return value if value.get("enabled") is True else None

    def backfill_key(self) -> str | None:
        """The mirror record a backfill reads, so switching it on or moving it to a newer record
        runs the backfill again (`runner.effective_mode`)."""
        mirror = self.mirror
        return f"mirror:{mirror.get('doi') or mirror.get('datapackage_url')}" if mirror else None

    @property
    def inbox(self) -> Path:
        return self.store.base_root / INBOX

    # ---------------------------------------------------------------------------------- plan
    def plan(self, mode: str, held: Mapping[str, HeldPeriod]) -> list[ArchiveItem]:
        self._held = held
        self.plan_errors = []
        items = self.restate_items(held)
        for snap in stored_snapshots(self.store, SHARED_SOURCE_ID):
            period = period_from_url(snap.fetched_url)
            if period is None or snap.sha256 in {h.sha256 for h in held.values()}:
                continue
            items.append(
                ArchiveItem(
                    key=f"shared:{snap.sha256[:16]}",
                    route="shared",
                    url=snap.key,
                    periods=(period,),
                    meta={
                        "sha256": snap.sha256,
                        "fetched_url": snap.fetched_url,
                        "retrieved_at": snap.retrieved_at,
                    },
                )
            )
        items.extend(self._inbox_items())
        items.append(ArchiveItem(key="origin:index", route="origin", url=INDEX_URL))
        if mode == "backfill" and self.mirror:
            try:
                items.extend(self._mirror_items(held))
            except (ConnectorError, HttpBlocked, HttpFailed) as exc:
                # The archive route failing leaves every other route to run (`plan_errors`).
                self.plan_errors.append({"route": "mirror", "error": repr(exc)[:500]})
        return items

    def _mirror_items(self, held: Mapping[str, HeldPeriod]) -> list[ArchiveItem]:
        """One item per yearly zip of the mirror that holds a month no document holds yet, read from
        the mirror's own `datapackage.json` (its file list, sizes, md5 and months)."""
        mirror = self.mirror or {}
        url = str(mirror["datapackage_url"])
        listing = self.http.get(url, timeout=60)
        if listing.status_code != 200:
            raise ConnectorError(f"GET {url} -> HTTP {listing.status_code}")
        try:
            resources = list(json.loads(listing.content.decode("utf-8"))["resources"])
        except (ValueError, KeyError, TypeError) as exc:
            raise ParseError(f"mirror datapackage unreadable: {exc!r}") from exc
        items = []
        for resource in sorted(resources, key=lambda r: str(r.get("name") or "")):
            name = str(resource.get("name") or "")
            months = tuple(str(m) for m in ((resource.get("parts") or {}).get("year_month") or []))
            path = str(resource.get("path") or "")
            if not name.endswith(".zip") or not months or all(m in held for m in months):
                continue
            if not path.startswith("https://"):
                raise ParseError(f"mirror resource {name} has no https path")
            items.append(
                ArchiveItem(
                    key=f"mirror:{name}",
                    route="mirror",
                    url=path,
                    periods=months,
                    meta={
                        "mirror": mirror.get("name"),
                        "doi": mirror.get("doi"),
                        "file": name,
                        "md5": resource.get("hash"),
                        "bytes": resource.get("bytes"),
                    },
                )
            )
        return items

    def _processed_inbox(self) -> set[str]:
        done: set[str] = set()
        for record in self.previous_runs:
            for entry in record.get("items") or []:
                if entry.get("route") == "inbox" and entry.get("status") in ("ok", "unchanged"):
                    sha = (entry.get("meta") or {}).get("sha256")
                    if sha:
                        done.add(str(sha))
        return done

    def _inbox_items(self) -> list[ArchiveItem]:
        if not self.inbox.is_dir():
            return []
        done = self._processed_inbox()
        items = []
        for path in sorted(self.inbox.iterdir()):
            if (
                path.name.startswith(".")
                or not path.is_file()
                or path.suffix.lower() not in (".xlsx", ".zip")
            ):
                continue
            sha = _file_sha256(path)
            if sha in done:
                continue
            items.append(
                ArchiveItem(
                    key=f"inbox:{sha[:16]}",
                    route="inbox",
                    url=str(path),
                    periods=tuple(p for p in (period_from_name(path.name),) if p),
                    meta={"name": path.name, "sha256": sha},
                )
            )
        return items

    # --------------------------------------------------------------------------------- fetch
    def fetch(self, item: ArchiveItem) -> Iterator[MonthFile]:
        if item.route == "restate":
            held = self._held.get(item.periods[0]) if item.periods else None
            yield self.fetch_restated(
                item,
                source_url=(held.source_url if held else "") or archive_url(item.periods[0]),
                retrieved_at=held.retrieved_at if held else self.now().isoformat(),
            )
        elif item.route == "shared":
            yield self._fetch_shared(item)
        elif item.route == "inbox":
            yield from self._fetch_inbox(item)
        elif item.route == "origin":
            yield self._fetch_origin()
        elif item.route == "mirror":
            yield from self._fetch_mirror(item)
        else:
            raise ConnectorError(f"unknown route {item.route!r}")

    def _fetch_shared(self, item: ArchiveItem) -> MonthFile:
        content = self.store.backend.get(item.url)
        if hashlib.sha256(content).hexdigest() != item.meta.get("sha256"):
            raise ConnectorError(f"stored {SHARED_SOURCE_ID} snapshot {item.url} fails its sha256")
        return MonthFile(
            content=content,
            source_url=str(item.meta.get("fetched_url") or ""),
            retrieved_at=_time(str(item.meta.get("retrieved_at") or "")) or self.now(),
            route="shared",
            item_key=item.key,
            period_hint=item.periods[0] if item.periods else None,
            snapshot_key=item.url,
            via={"source_id": SHARED_SOURCE_ID, "snapshot": item.url},
        )

    def _fetch_inbox(self, item: ArchiveItem) -> Iterator[MonthFile]:
        path = Path(item.url)
        stamp = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
        via = {"file": item.meta.get("name"), "sha256": item.meta.get("sha256")}
        if path.suffix.lower() == ".zip":
            yield from self._zip_members(path, item, route="inbox", retrieved_at=stamp, via=via)
            return
        content = path.read_bytes()
        hint = period_from_name(path.name)
        yield MonthFile(
            content=content,
            source_url=archive_url(hint) if hint else INDEX_URL,
            retrieved_at=stamp,
            route="inbox",
            item_key=item.key,
            period_hint=hint,
            via=via,
        )

    def _validators_for(self, url: str) -> tuple[dict[str, str], str | None, str | None]:
        """Conditional headers for `url`, with the stored snapshot (its object key and sha256) a
        304 points at: this source's own last origin fetch first, then `us.eia.860m`'s."""
        for record in reversed(self.previous_runs):
            origin = record.get("origin") or {}
            if origin.get("url") == url and origin.get("snapshot_key") and origin.get("sha256"):
                if not self.store.backend.exists(str(origin["snapshot_key"])):
                    break  # a 304 would point at bytes no longer stored: ask unconditionally
                headers = _conditional(origin.get("etag"), origin.get("last_modified"))
                if headers:
                    return headers, str(origin["snapshot_key"]), str(origin["sha256"])
        shared: list[StoredSnapshot] = stored_snapshots(self.store, SHARED_SOURCE_ID)
        for snap in reversed(shared):
            if snap.fetched_url != url:
                continue
            for candidate in snap.meta.get("candidates") or []:
                if candidate.get("url") == url and candidate.get("xlsx"):
                    headers = _conditional(candidate.get("etag"), candidate.get("last_modified"))
                    if headers:
                        return headers, snap.key, snap.sha256
        return {}, None, None

    def _fetch_origin(self) -> MonthFile:
        index = self.http.get(INDEX_URL)
        if index.status_code != 200:
            raise ConnectorError(f"GET {INDEX_URL} -> HTTP {index.status_code}")
        links = find_xlsx_links(index.text)
        if not links:
            raise ParseError("EIA-860M index page lists no generator workbooks")
        tried: list[dict[str, Any]] = []
        for url in links[:MAX_CANDIDATES]:
            if "/archive/" in url:  # never: robots.txt `Disallow: /*archive/`
                continue
            headers, stored_key, stored_sha = self._validators_for(url)
            try:
                r = self.http.get(url, timeout=300, headers=headers or None)
            except HttpBlocked as exc:
                tried.append({"url": url, "blocked": str(exc)})
                continue
            validators = {
                k: v
                for k, v in (
                    ("etag", r.headers.get("ETag")),
                    ("last_modified", r.headers.get("Last-Modified")),
                )
                if v
            }
            if r.status_code == 304 and stored_key:
                content = self.store.backend.get(stored_key)
                if hashlib.sha256(content).hexdigest() == stored_sha:
                    return MonthFile(
                        content=content,
                        source_url=url,
                        retrieved_at=self.now(),
                        route="origin",
                        item_key="origin:index",
                        period_hint=period_from_url(url),
                        snapshot_key=stored_key,
                        # A 304 may repeat only some validators (EIA's repeats the ETag, not
                        # Last-Modified): the ones it omits stay valid, so both go to the next
                        # request. Measured 2026-10-24: asked with If-None-Match alone, EIA's
                        # server answered 200 and the full 14 MB workbook.
                        validators={**_validators_from_headers(headers), **validators},
                        via={"url": url, "http_status": 304, "candidates": tried},
                    )
                r = self.http.get(url, timeout=300)
            content = bytes(r.content)
            ok = r.status_code == 200 and content.startswith(XLSX_MAGIC)
            tried.append({"url": url, "status": r.status_code, "bytes": len(content), "xlsx": ok})
            if ok:
                return MonthFile(
                    content=content,
                    source_url=url,
                    retrieved_at=self.now(),
                    route="origin",
                    item_key="origin:index",
                    period_hint=period_from_url(url),
                    validators=validators,
                    via={"url": url, "http_status": r.status_code, "candidates": tried},
                )
        raise ConnectorError(f"no real xlsx among the first {len(tried)} EIA-860M links: {tried}")

    def _fetch_mirror(self, item: ArchiveItem) -> Iterator[MonthFile]:
        """One yearly zip: downloaded to a temporary file on the data volume, checked against the
        mirror's md5, then each month no document holds yet read from it."""
        via = {k: item.meta.get(k) for k in ("mirror", "doi", "file", "md5", "bytes")}
        with tempfile.TemporaryDirectory(dir=self._tmp_dir()) as tmp:
            path = Path(tmp) / Path(urlsplit(item.url).path).name
            self._download(f"{item.url}?download=1", path, md5=str(item.meta.get("md5") or ""))
            yield from self._zip_members(
                path, item, route="mirror", retrieved_at=self.now(), via=via, skip=set(self._held)
            )

    def _tmp_dir(self) -> Path:
        path = self.store.base_root / "tmp" / SOURCE_ID
        path.mkdir(parents=True, exist_ok=True)
        return path

    def _download(self, url: str, path: Path, *, md5: str) -> None:
        r = self.http.get(url, timeout=900, stream=True)
        if r.status_code != 200:
            raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
        digest = hashlib.md5()  # noqa: S324 -- the mirror publishes md5; integrity, not security
        with path.open("wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
                digest.update(chunk)
        expected = md5.removeprefix("md5:")
        if expected and digest.hexdigest() != expected:
            raise ConnectorError(f"{url} md5 {digest.hexdigest()} is not the mirror's {expected}")

    def _zip_members(
        self,
        path: Path,
        item: ArchiveItem,
        *,
        route: str,
        retrieved_at: dt.datetime,
        via: Mapping[str, Any],
        skip: set[str] | None = None,
    ) -> Iterator[MonthFile]:
        try:
            archive = zipfile.ZipFile(path)
        except zipfile.BadZipFile as exc:
            raise ParseError(f"{path.name} is not a zip: {exc}") from exc
        with archive:
            for info in sorted(archive.infolist(), key=lambda i: i.filename):
                if not info.filename.lower().endswith(".xlsx"):
                    continue
                hint = period_from_name(info.filename)
                if hint is not None and skip is not None and hint in skip:
                    continue
                content = archive.read(info)
                yield MonthFile(
                    content=content,
                    source_url=archive_url(hint) if hint else INDEX_URL,
                    retrieved_at=retrieved_at,
                    route=route,
                    item_key=item.key,
                    period_hint=hint,
                    via={**via, "member": info.filename},
                )

    # ----------------------------------------------------------------------------- summarise
    def summarise(self, month: MonthFile) -> dict[str, Any]:
        wb = open_workbook(month.content)
        try:
            titles = [ws.title for ws in wb.worksheets]
            by_name = {ws.title.strip().lower(): ws for ws in wb.worksheets}
            period: str | None = None
            title_text: str | None = None
            blocks: dict[str, Block] = {}
            layout: dict[str, Any] = {"sheets": titles, "header_rows": {}}
            dq: dict[str, Any] = {
                "rows": {},
                "duplicate_keys": {},
                "capacity_missing": {},
                "technology_blank": {},
                "technology_blank_mw": {},
            }
            unknown_status: dict[str, set[str]] = {}
            labels: dict[str, str] = {}
            nameplate = False
            for block_name, names in SHEETS.items():
                ws = next((by_name[n] for n in names if n in by_name), None)
                if ws is None:
                    raise ParseError(f"no {block_name} sheet in {titles}")
                rows = ws.iter_rows(values_only=True)
                header_index, header, before = find_header(rows, ("Plant ID", "Generator ID"), within=8)
                layout["header_rows"][block_name] = header_index
                for row in before:
                    found = _title_period(row)
                    if found and period is None:
                        period, title_text = found
                summary = self._tally_sheet(block_name, header, rows)
                blocks.update(summary.blocks)
                nameplate = nameplate or summary.nameplate
                dq["rows"][block_name] = summary.rows
                dq["duplicate_keys"][block_name] = summary.duplicates
                dq["capacity_missing"][block_name] = summary.capacity_missing
                dq["technology_blank"][block_name] = summary.technology_blank
                dq["technology_blank_mw"][block_name] = round(summary.technology_blank_mw, 3)
                if summary.unknown_status:
                    unknown_status[block_name] = summary.unknown_status
                labels.update(summary.labels)
        finally:
            wb.close()
        if period is None:
            period = month.period_hint
            if period is None:
                raise ParseError("the workbook names no month and the file name gives none")
        elif month.period_hint and month.period_hint != period:
            # The month is the workbook's own title, so the file name alone is wrong: a warning.
            dq["period_hint_mismatch"] = {
                "title": period,
                "file": month.period_hint,
                "period_from": "content",
            }
        retired_basis = "retired-since-2002" if period >= RETIRED_COMPLETE_FROM else "retired-partial"
        blocks["retired"].basis = f"{blocks['retired'].basis}/{retired_basis}"
        if period >= RETIRED_COMPLETE_FROM:
            # Every retirement since 2002: a list that only grows, and whose years before last year
            # are settled (`dq.py` `cumulative_shrank`, `cumulative_restated`).
            dq["cumulative"] = {
                "retired": {"dimension": "retirement_year", "open_from": str(int(period[:4]) - 1)}
            }
        layout.update(
            {
                "title": title_text,
                "measure": MEASURE,
                "nameplate_stated": nameplate,
                "puerto_rico_sheets": any(t.strip().lower().endswith("_pr") for t in titles),
            }
        )
        dq["unknown_status"] = {k: sorted(v) for k, v in sorted(unknown_status.items())}
        dq["unmapped_technology"] = sorted(raw for raw, bucket in labels.items() if bucket == "unknown")
        source_url = month.source_url
        if month.route not in ("origin", "shared", "restate") or "/xls/" not in source_url:
            source_url = archive_url(period)
        return {
            "period": period,
            "source_url": source_url,
            "archive_url": archive_url(period),
            "layout": layout,
            "blocks": {name: block.as_dict() for name, block in sorted(blocks.items())},
            "technology_labels": dict(sorted(labels.items())),
            "dq_facts": dq,
        }

    def _tally_sheet(
        self, block_name: str, header: Sequence[str], rows: Iterator[Sequence[Any]]
    ) -> _SheetSummary:
        idx = {
            "plant": column(header, "Plant ID"),
            "generator": column(header, "Generator ID"),
            "state": column(header, "Plant State", "State"),
            "technology": column(header, "Technology"),
            "mw": column(header, *NET_SUMMER),
            "nameplate": column(header, *NAMEPLATE),
            "status": column(header, "Status"),
            "planned_year": column(header, "Planned Operation Year"),
            "retirement_year": column(header, "Retirement Year"),
            "planned_retirement_year": column(header, "Planned Retirement Year"),
        }
        if idx["mw"] is None or idx["state"] is None or idx["technology"] is None:
            raise ParseError(f"{block_name} sheet lacks a column the tallies read: {list(header)[:24]}")
        has_nameplate = idx["nameplate"] is not None
        alt = "nameplate_mw" if has_nameplate else None
        dimensions = DIMENSIONS[block_name]
        block = Block(
            basis=f"{BASIS_ROOT}/{block_name}/{MEASURE}/{GEOGRAPHY}",
            dimensions=dimensions,
            alt_name=alt,
        )
        retirements = (
            Block(
                basis=f"{BASIS_ROOT}/operating-planned-retirements/{MEASURE}/{GEOGRAPHY}",
                dimensions=("technology", "state", "planned_retirement_year"),
                alt_name=alt,
            )
            if block_name == "operating"
            else None
        )
        out = _SheetSummary(blocks={block_name: block})
        if retirements is not None:
            out.blocks["planned_retirements"] = retirements
        out.nameplate = has_nameplate
        keys_seen: set[tuple[str, str]] = set()
        known = KNOWN_STATUS.get(block_name)
        for row in rows:
            plant = cell(row, idx["plant"])
            state = (text(cell(row, idx["state"])) or "").upper()
            if number(plant) is None:
                # Trailing note rows carry no Plant ID and nothing else a generator row has. A row
                # that names a technology and a state or MW is a generator EIA lists without one
                # (the Retired sheet's early nuclear units: Indian Point 1, Fort St. Vrain, Shoreham,
                # 12 rows in 2026-08): it has no key to count by, so it is left out and counted.
                if text(cell(row, idx["technology"])) and (state or number(cell(row, idx["mw"])) is not None):
                    reason = (
                        "no_plant_id"
                        if state in US_STATES_DC
                        else "puerto_rico"
                        if state == "PR"
                        else "outside_50_states_dc"
                    )
                    block.exclude(reason, number(cell(row, idx["mw"])))
                continue
            out.rows += 1
            key = (str(plant).strip(), str(cell(row, idx["generator"]) or "").strip())
            if key in keys_seen:
                out.duplicates += 1
            keys_seen.add(key)
            mw = number(cell(row, idx["mw"]))
            nameplate = number(cell(row, idx["nameplate"])) if has_nameplate else None
            if mw is None:
                out.capacity_missing += 1
            if state not in US_STATES_DC:
                reason = "puerto_rico" if state == "PR" else "outside_50_states_dc"
                block.exclude(reason, mw)
                continue
            raw_label = text(cell(row, idx["technology"]))
            token = technology_token(raw_label)
            out.labels[raw_label or BLANK_LABEL] = token
            if raw_label is None:
                out.technology_blank += 1
                out.technology_blank_mw += mw or 0.0
            keys: dict[str, str | None] = {"technology": token, "state": f"US-{state}"}
            if idx["status"] is not None:
                code = status_code(cell(row, idx["status"]))
                keys["status"] = code
                if code and known is not None and code not in known:
                    out.unknown_status.add(code)
            for paired, first in PAIRED.items():
                if paired in dimensions:
                    keys[paired] = pair_key(keys[first], keys.get("status"))
            if block_name == "planned":
                keys["expected_year"] = _year_key(cell(row, idx["planned_year"]))
            if block_name == "retired":
                keys["retirement_year"] = _year_key(cell(row, idx["retirement_year"]))
            block.add(keys, mw, nameplate)
            if retirements is not None and idx["planned_retirement_year"] is not None:
                year = _year_key(cell(row, idx["planned_retirement_year"]))
                if year is not None:
                    retirements.add({**keys, "planned_retirement_year": year}, mw, nameplate)
        return out


class _SheetSummary:
    def __init__(self, blocks: dict[str, Block]) -> None:
        self.blocks = blocks
        self.rows = 0
        self.duplicates = 0
        self.capacity_missing = 0
        self.nameplate = False
        self.unknown_status: set[str] = set()
        self.labels: dict[str, str] = {}
        #: Rows of the 50 states and DC with a blank Technology (counted, in `unknown`) and their
        #: net summer MW (warned from `dq.BLANK_MW_WARN` up).
        self.technology_blank = 0
        self.technology_blank_mw = 0.0


def _year_key(value: Any) -> str | None:
    year = year_of(value)
    return str(year) if year is not None else None


def _title_period(row: Sequence[Any]) -> tuple[str, str] | None:
    for value in row:
        label = normalise_header(value)
        match = TITLE_PERIOD.search(label)
        if match:
            month = month_number(match.group(1))
            if month:
                return period_of(int(match.group(2)), month), label
    return None


def _conditional(etag: Any, last_modified: Any) -> dict[str, str]:
    headers: dict[str, str] = {}
    if etag:
        headers["If-None-Match"] = str(etag)
    if last_modified:
        headers["If-Modified-Since"] = str(last_modified)
    return headers


def _validators_from_headers(headers: Mapping[str, str]) -> dict[str, str]:
    out = {}
    if headers.get("If-None-Match"):
        out["etag"] = headers["If-None-Match"]
    if headers.get("If-Modified-Since"):
        out["last_modified"] = headers["If-Modified-Since"]
    return out


def _time(value: str) -> dt.datetime | None:
    try:
        when = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = [
    "ARCHIVE_URL",
    "DIMENSIONS",
    "INDEX_URL",
    "PAIR",
    "PAIRED",
    "SHARED_SOURCE_ID",
    "SOURCE_ID",
    "Connector",
    "archive_url",
    "find_xlsx_links",
    "pair_key",
    "period_from_name",
    "period_from_url",
    "split_pair",
    "status_code",
]
