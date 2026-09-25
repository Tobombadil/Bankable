"""us.tx.rrc.class_vi — Railroad Commission of Texas, Class VI (CO2 geologic storage) application list.

Texas holds Class VI primacy since 2025-12-15, so its applications are transferred out of
`us.epa.class_vi` and appear only here. This connector is the sibling of that one: same `kind`,
same technology constant, same `classify_tech` bypass, same null-over-guess rules.

**Gated.** The RRC is a state agency, so 17 U.S.C. §105 does not apply, and its site-policies
page grants copying "for noncommercial use" (quoted in `data/sources.yaml`); the manifest keeps
`reuse: unknown` / `publication: none` until an owner or counsel reads and classifies those
terms. The registry therefore refuses to instantiate this connector without
`allow_restricted=True`, and a run that passes the flag is routed to the quarantine store, which
cannot write a publishable path (`pipeline/connectors/registry.py`, `tests/test_connector_gating.py`).
Nothing this module produces reaches a public surface until the manifest entry changes.

Fetch: two GETs through the polite session, robots honoured (`www.rrc.texas.gov/robots.txt` is
200 and empty, measured 2026-09-22 and 2026-09-25).
 1. The CO2-storage landing page. Its "Class VI Application List" link points at a **dated**
    PDF whose path changes with every release — `/media/sutpod2t/class-vi-application-list-31126.pdf`
    (2026-03-11), `/media/r2ndw5md/class_vi_application-5122026.pdf` (2026-05-12),
    `/media/3ivkqhtb/class_vi_application-09222026.pdf` (2026-09-22) — and on 2026-09-25 the
    page still carried **all three** anchors, two of them with empty link text (invisible to a
    reader, present in the HTML). So the file name is never hard-coded: `find_list_links` returns
    every `class[-_]vi[-_]application*.pdf` anchor with its `title` and link text, and
    `choose_current` takes the one whose `title` (else file name) carries the latest date; if no
    candidate carries a date it falls back to the anchor a reader can see (non-empty text), then
    to page order. Every candidate and the rule that decided are written to the run record.
 2. The chosen PDF. Bytes and sha256 of the fetched file go into `meta`, the content must start
    with `%PDF`, and anything else is a `ConnectorError` (the runner records it as failed).

Parse: pdfplumber (ADR 0002, docs/20 §15) over an Acrobat-PDFMaker-for-Excel document, i.e. a real
vector table, not a scan. Two layouts have been seen and both are fixtures: the 2026-03-11 build
starts at the header row and ends with a trailer row (`Last Updated:` in the Docket column); the
2026-05-12 and 2026-09-22 builds put a title row and a `Last Updated:` row *above* the header and
a footnote legend under the table. Both carry the same 14 columns. The parser asserts the column
count and the header row (each cell against a tolerant pattern, in order) and raises
`ParseError` on drift rather than mis-aligning; a header repeated on a later page is skipped;
multi-line cells are re-joined (`OG-25-\n00029632` -> `OG-25-00029632`, `Pending RAD\nResponse`
-> `Pending RAD Response`); footnote markers glued to a status (`Draft Doc6`, `Pending RAD
Response**`) are stripped for the mapped value and kept verbatim in `application_status_raw`.
Parsed rows use canonical snake_case keys, not the verbatim header text, because the header text
itself changed between the March and May builds (`No . of Inj.\nWell` -> `No. of\nInj. Wells`)
and verbatim keys would have held the run for schema drift on a cosmetic change.

source_record_id: the RRC's own `RRC Tracking #` (e.g. `57803`). It is present on every row of all
three releases, unique within each, and survived an operator rename (57802: White Energy Carbon
Sol. LLC in March/May, North Texas Carbon, LLC in September) and a status change on every other
row that moved — so a refresh yields change events, not 18 new records. An amendment is filed under
its own tracking number (59502 "Brown Pelican (Amendment)" beside 55294 "Brown Pelican"), which is
how the RRC itself keys them; no content hash is needed and `dedupe_strategy` stays `hold`.

Grain: one row per **application** (a project's storage-facility permit), not per well; `No. of
Inj. Wells` carries the well count and stays in `raw`.

technology: `pipeline.normalize.classify_tech` is deliberately **not** called — its `\bstorage\b`
rule would call a CO2 storage facility a battery. `kind` is the constant `ccs`, `technology` the
constant `co2_geologic_sequestration`, `technology_raw` null (no source column to quote).

state: the constant `TX`. The table has no state column; every row is a Texas application by
construction of the source. `county` is the RRC's text verbatim (it is sometimes several counties
— "Liberty, Hardin & Jefferson" — and sometimes misspelt in the source, "Galvestone" in March,
corrected to "Galveston" by May); `county_norm` is the usual normalisation of that text.

capacity_mw / proposed_cod: null. The list carries a well count and an injection interval, neither
of which is a capacity, and no operation date; `Approval Date` is the permit decision date and
lives in `raw`.

Personal data: no column names an individual — operator, project, county, district, formation,
dates, status, docket. The PDF's *document metadata* does carry the RRC staff author's name
(`Author` in the Info dictionary and `dc:creator` in the XMP packet, both inside the file). The
connector never reads document metadata into `meta`, parsed rows or `raw`, and a test proves it;
the bytes themselves are kept intact as evidence (the Info dictionary sits in a compressed object
stream, so a byte-level scrub would reach one copy and not the other, and re-serialising the PDF
would destroy the sha256 the run record and the manifest vouch for).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import pathlib
import re
import time
from dataclasses import asdict, dataclass
from typing import Any, ClassVar
from urllib.parse import urljoin

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot
from pipeline.connectors.canonical import harmonise_status, norm_county, norm_name, norm_org

PAGE_URL = (
    "https://www.rrc.texas.gov/oil-and-gas/applications-and-permits/injection-storage-permits/co2-storage/"
)
PDF_MAGIC = b"%PDF"

_ANCHOR_RE = re.compile(r"<a\b([^>]*)>(.*?)</a>", re.I | re.S)
_HREF_RE = re.compile(r'href="([^"]+)"', re.I)
_TITLE_RE = re.compile(r'title="([^"]*)"', re.I)
_LIST_PDF_RE = re.compile(r"class[-_ ]?vi[-_ ]?application[^\"]*\.pdf$", re.I)
_TAG_RE = re.compile(r"<[^>]+>")
#: `3.11.26`, `5.12.2026`, `09.22.2026` — the dotted US date the RRC puts in the anchor title.
_DOTTED_DATE_RE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})")
#: `-31126.pdf`, `-5122026.pdf`, `-09222026.pdf` — the same date squashed into the file name.
_FILENAME_DIGITS_RE = re.compile(r"[-_](\d{5,8})\.pdf$", re.I)

#: The 14 columns, in order: (canonical key, pattern the whitespace-collapsed lower-case header
#: cell must fully match). Tolerant to the punctuation and plural drift seen between builds.
COLUMNS: tuple[tuple[str, str], ...] = (
    ("rrc_tracking_no", r"rrc\s*tracking\s*#"),
    ("operator_name", r"operator\s*name"),
    ("project_name", r"project\s*name"),
    ("county", r"county"),
    ("district", r"district"),
    ("formation", r"formation"),
    ("injection_wells", r"no\s*\.?\s*of\s*inj\.?\s*wells?"),
    ("injection_interval_tvd", r"inj\.?\s*interval\s*\(\s*tvd\s*\)"),
    ("submittal_date", r"submittal\s*date"),
    ("application_status", r"application\s*status"),
    ("protested", r"protested"),
    ("referred_to_hearing_date", r"referred\s*to\s*hearing\s*date"),
    ("docket_no", r"docket\s*#"),
    ("approval_date", r"approval\s*date"),
)
COLUMN_KEYS: tuple[str, ...] = tuple(k for k, _ in COLUMNS)
_COLUMN_PATTERNS: tuple[re.Pattern[str], ...] = tuple(re.compile(p, re.I) for _, p in COLUMNS)
_LAST_UPDATED_RE = re.compile(r"last\s*updated\s*:?\s*(\d{1,2}/\d{1,2}/\d{2,4})?", re.I)
_TRAILING_FOOTNOTE_RE = re.compile(r"(?<=[A-Za-z])\s*[\d*]+$")
_DATE_FORMATS = ("%m/%d/%Y", "%m/%d/%y")


# ------------------------------------------------------------------ discovery
@dataclass(frozen=True)
class Candidate:
    """One `class VI application` PDF anchor on the landing page."""

    url: str
    title: str
    text: str
    date: str | None  # ISO date parsed from the title, else the file name, else None


def _dotted_date(text: str) -> str | None:
    m = _DOTTED_DATE_RE.search(text)
    if not m:
        return None
    month, day, year = (int(g) for g in m.groups())
    return _iso(month, day, year)


def _filename_date(url: str) -> str | None:
    m = _FILENAME_DIGITS_RE.search(url)
    if not m:
        return None
    digits = m.group(1)
    # The RRC squashes m.d.yy / m.dd.yyyy / mm.dd.yyyy with no separator: try each split that
    # yields a real date, month first, and prefer the four-digit year.
    for month_len in (2, 1):
        for year_len in (4, 2):
            day_len = len(digits) - month_len - year_len
            if day_len not in (1, 2):
                continue
            month = int(digits[:month_len])
            day = int(digits[month_len : month_len + day_len])
            year = int(digits[month_len + day_len :])
            iso = _iso(month, day, year)
            if iso:
                return iso
    return None


def _iso(month: int, day: int, year: int) -> str | None:
    if year < 100:
        year += 2000
    try:
        return dt.date(year, month, day).isoformat()
    except ValueError:
        return None


def find_list_links(html: str, base: str = PAGE_URL) -> list[Candidate]:
    """Every application-list PDF anchor on the page, in page order, de-duplicated by URL."""
    out: list[Candidate] = []
    seen: set[str] = set()
    for attrs, inner in _ANCHOR_RE.findall(html):
        href = _HREF_RE.search(attrs)
        if not href:
            continue
        url = urljoin(base, href.group(1).strip())
        if not _LIST_PDF_RE.search(url.split("?", 1)[0]) or url in seen:
            continue
        seen.add(url)
        title_m = _TITLE_RE.search(attrs)
        title = (title_m.group(1) if title_m else "").strip()
        text = re.sub(r"\s+", " ", _TAG_RE.sub(" ", inner)).strip()
        date = _dotted_date(title) or _filename_date(url)
        out.append(Candidate(url=url, title=title, text=text, date=date))
    return out


def choose_current(candidates: list[Candidate]) -> tuple[Candidate, str]:
    """The release to ingest and the rule that picked it.

    `title_date`: the latest date in any anchor title / file name wins (the page keeps stale
    anchors around, two of three with empty link text on 2026-09-25). `anchor_text`: no candidate
    carries a date, so the one a reader can see wins. `page_order`: neither, first link wins.
    """
    if not candidates:
        raise ConnectorError("no Class VI application-list PDF link on the RRC CO2 storage page")
    dated = [c for c in candidates if c.date]
    if dated:
        return max(dated, key=lambda c: c.date or ""), "title_date"
    visible = [c for c in candidates if c.text]
    if visible:
        return visible[0], "anchor_text"
    return candidates[0], "page_order"


# ------------------------------------------------------------------ table -> rows
def _cell(value: Any) -> str | None:
    """A table cell as text: multi-line cells re-joined, `-`/blank as None.

    A line break after a hyphen is a hyphenation break (`OG-25-\\n00029632`, `(BBW-\\nP1)`) and is
    closed up; any other break is a wrapped word and becomes one space.
    """
    if value is None:
        return None
    text = str(value).replace("\r", "\n")
    text = re.sub(r"-\s*\n\s*", "-", text)
    text = re.sub(r"\s*\n\s*", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return None if text in ("", "-", "–", "—") else text


def _header_key(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\n", " ")).strip().lower()


def _is_header(row: list[Any]) -> bool:
    return len(row) == len(COLUMNS) and all(
        p.fullmatch(_header_key(c)) is not None for p, c in zip(_COLUMN_PATTERNS, row, strict=True)
    )


def clean_status(value: str | None) -> str | None:
    """The mapped status: footnote markers the spreadsheet glues on (`Draft Doc6`,
    `Pending RAD Response**`) removed, whitespace collapsed. The verbatim text is kept beside it."""
    if value is None:
        return None
    return _TRAILING_FOOTNOTE_RE.sub("", value).strip() or None


def rows_from_tables(tables: list[list[list[Any]]]) -> tuple[list[dict[str, Any]], str | None]:
    """Application rows from pdfplumber tables (all pages), plus the list's `Last Updated` date.

    Raises ParseError when a table does not have exactly 14 columns, when no header row is found,
    or when a row that is neither the header, a preamble/trailer line nor blank has no tracking
    number (the signature of a mis-aligned extraction).
    """
    rows: list[dict[str, Any]] = []
    last_updated: str | None = None
    header_seen = False
    for t_index, table in enumerate(tables):
        for r_index, row in enumerate(table):
            if len(row) != len(COLUMNS):
                raise ParseError(
                    f"RRC Class VI table {t_index} row {r_index} has {len(row)} cells, expected "
                    f"{len(COLUMNS)}: {[_cell(c) for c in row][:6]}"
                )
            if _is_header(row):
                header_seen = True  # first page's header, or a repeat on a later page
                continue
            cells = [_cell(c) for c in row]
            present = [c for c in cells if c]
            joined = " ".join(present)
            if not present:
                continue
            if not header_seen or not (cells[0] or "").isdigit():
                # Title ("Texas RRC Class VI Application Tracker — Project Summary"), the
                # `Last Updated:` line above or below the table, or a footnote legend.
                m = _LAST_UPDATED_RE.search(joined)
                if m:
                    last_updated = _date_iso(m.group(1)) or last_updated
                    continue
                if not header_seen or len(present) == 1:
                    continue
                raise ParseError(
                    f"RRC Class VI table {t_index} row {r_index} is neither the header nor an "
                    f"application row (no tracking number): {cells[:6]}"
                )
            record = dict(zip(COLUMN_KEYS, cells, strict=True))
            record["application_status_raw"] = record["application_status"]
            record["application_status"] = clean_status(record["application_status"])
            rows.append(record)
    if not header_seen:
        first = [_header_key(c) for c in tables[0][0]] if tables and tables[0] else []
        raise ParseError(f"RRC Class VI header row not found; first row read {first[:6]}")
    return rows, last_updated


def _date_iso(text: str | None) -> str | None:
    if not text:
        return None
    for fmt in _DATE_FORMATS:
        try:
            return dt.datetime.strptime(text.strip(), fmt).date().isoformat()  # noqa: DTZ007 - a date, no clock
        except ValueError:
            continue
    return None


def _timestamp(text: str | None) -> pd.Timestamp:
    iso = _date_iso(text)
    return pd.Timestamp(iso) if iso else pd.NaT


def _cross_refs(row: dict[str, Any]) -> str:
    """`RRC_DOCKET:<docket>` when the application has been referred to a hearing."""
    docket = (row.get("docket_no") or "").strip()
    return f"RRC_DOCKET:{docket.upper()}" if docket else ""


# ------------------------------------------------------------------ connector
class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.tx.rrc.class_vi"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "pdf"
    status_key: ClassVar[str] = "tx_rrc_class_vi"
    status_map_path: ClassVar[pathlib.Path | None] = pathlib.Path(__file__).with_name("status_map.yaml")
    dq_required_fields: ClassVar[tuple[str, ...]] = (
        "name_canonical",
        "sponsor_name",
        "status_raw",
        "queue_date",
    )
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "rrc_tracking_no",
        "operator_name",
        "project_name",
        "county",
        "submittal_date",
        "application_status",
    )

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        page = self.http.get(PAGE_URL)
        if page.status_code != 200:
            raise ConnectorError(f"GET {PAGE_URL} -> HTTP {page.status_code}")
        candidates = find_list_links(page.text)
        chosen, rule = choose_current(candidates)

        r = self.http.get(chosen.url, timeout=120)
        if r.status_code != 200:
            raise ConnectorError(f"GET {chosen.url} -> HTTP {r.status_code}")
        if not r.content.startswith(PDF_MAGIC):
            raise ConnectorError(
                f"{chosen.url} answered {r.headers.get('Content-Type', '')!r}, "
                f"{len(r.content)} bytes, not a PDF"
            )
        return RawSnapshot(
            content=r.content,
            content_type=r.headers.get("Content-Type", "application/pdf"),
            url=chosen.url,
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=r.status_code,
            ext="pdf",
            headers=dict(r.headers),
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=2,
            meta={
                "page_url": PAGE_URL,
                "page_bytes": len(page.content),
                "page_sha256": hashlib.sha256(page.content).hexdigest(),
                "candidates": [asdict(c) for c in candidates],
                "chosen_url": chosen.url,
                "chosen_by": rule,
                "pdf_bytes": len(r.content),
                "pdf_sha256": hashlib.sha256(r.content).hexdigest(),
            },
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        if not raw.content.startswith(PDF_MAGIC):
            raise ParseError(f"{raw.url} is not a PDF ({raw.content[:16]!r})")
        import pdfplumber  # lazy: the module imports without the PDF stack (registry, API scans)

        try:
            with pdfplumber.open(io.BytesIO(raw.content)) as pdf:
                tables = [table for page in pdf.pages for table in page.extract_tables()]
                pages = len(pdf.pages)
        except Exception as exc:  # pdfminer raises a zoo of its own types on a damaged file
            raise ParseError(f"{raw.url} could not be read as a PDF: {exc!r}") from exc
        if not tables:
            raise ParseError(f"{raw.url}: no table found in {pages} page(s) (a scan, or the layout changed)")
        rows, last_updated = rows_from_tables(tables)
        if not rows:
            raise ParseError(f"{raw.url}: table found but no application rows")
        for row in rows:
            row["list_last_updated"] = last_updated
            row["page_url"] = PAGE_URL
        return rows

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        def g(key: str) -> list[Any]:
            return [r.get(key) for r in rows]

        harmonised = [
            harmonise_status(self.status_key, {"status_raw": s}, self.status_map)
            for s in g("application_status")
        ]
        df = pd.DataFrame(
            {
                "source_record_id": g("rrc_tracking_no"),
                "kind": "ccs",
                "name_canonical": g("project_name"),
                "name_norm": [norm_name(v) for v in g("project_name")],
                "sponsor_name": g("operator_name"),
                "sponsor_norm": [norm_org(v) for v in g("operator_name")],
                "technology": "co2_geologic_sequestration",
                "technology_raw": None,
                "capacity_mw": pd.array([None] * len(rows), dtype="Float64"),
                "storage_mwh": pd.array([None] * len(rows), dtype="Float64"),
                "iso": None,
                "state": "TX",
                "county": g("county"),
                "county_norm": [norm_county(v) for v in g("county")],
                "lifecycle_state": [s for s, _ in harmonised],
                "status_raw": g("application_status"),
                "status_rule": [r for _, r in harmonised],
                "status_conflict": False,
                "queue_date": [_timestamp(v) for v in g("submittal_date")],
                "proposed_cod": pd.NaT,
                "queue_id": g("rrc_tracking_no"),
                "eia_plant_id": None,
                "eia_generator_id": None,
                "cross_refs": [_cross_refs(r) for r in rows],
            }
        )
        return self.finalize(df, rows, raw)
