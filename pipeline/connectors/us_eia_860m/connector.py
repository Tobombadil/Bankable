"""us.eia.860m — EIA-860M Preliminary Monthly Electric Generator Inventory, "Planned" sheet, and its
"Canceled or Postponed" sheet when the status map names it.

Fetch: the index page lists `xls/<month>_generator<year>.xlsx` links in reverse chronological
order, including *future* months as placeholders that answer 200 with an HTML page (docs/02 §7;
observed 2026-09-12: december/october 2026 redirect to HTML, july 2026 is the real file). Links
under `/archive/` are dropped before any request: eia.gov's robots.txt disallows `/*archive/`
(a wildcard rule that Python's robots parser only honours from 3.13, so older interpreters
would have fetched them), and the current month is always published under `xls/`. The first
remaining link whose bytes start with the zip magic wins; HTML answers and any candidate the
robots check refuses are kept in `meta` and skipped. The winning candidate's `ETag` and
`Last-Modified` are kept in `meta` too, and the next request for that same URL is conditional on
them: www.eia.gov answers 304 (checked 2026-10-07), and the stored bytes, checked against their
SHA-256, stand in for the 13.9 MB download. The validators come from this source's own previous
snapshot, or from the source it shares the fetch with (`shares_fetch_with`:
`us.eia.860m.retirements` reads the same workbook). The placeholder months ahead of it are still
requested, so a month that has become real is found exactly as before.
Parse: sheet "Planned", header on the third row; trailing note rows (no Plant ID) dropped. The same
workbook's "Canceled or Postponed" sheet (same layout less the Status and planned-date columns;
1,745 units in September 2026) is read too, and its rows are loaded only while the eia860m block of
`pipeline/status_map.yaml` maps the sheet's name, `Canceled or Postponed`, to a lifecycle state:
that key is the one switch (docs/28 §11 item 2), and the map's comment says why it is off today.
Each such row carries `_sheet: "Canceled or Postponed"` in its raw payload (the convention
`us.eia.860m.retirements` uses), so its state is recomputed from `raw` alone; a Planned row's raw
payload is the sheet's row, unchanged. A workbook without the sheet is read as before. Every
parse writes what it found on that sheet to the snapshot's `meta["canceled_or_postponed"]` (units,
plants, nameplate MW, the state it was loaded as or null, and a read error if any), which the run
record keeps and `Store.last_snapshot` reads back. While the switch is off the sheet is optional:
any failure to read it is recorded there and logged, and the Planned load goes on; once the switch
is on, a sheet that cannot be read fails the run, because loading Planned alone would read every
canceled unit as removed.
source_record_id: `<Plant ID>-<Generator ID>`, the EIA identity that anchors resolution. A unit
keeps it from one sheet to the other, so a unit moving from Planned to Canceled or Postponed is a
lifecycle change of one record, never a removal plus a new record.
Columns: every column of both sheets names a plant, a generator, a place or a company (`Entity
Name` is the respondent: a utility, a cooperative, a developer's project company); none names a
person, so nothing is dropped at parse time (pinned by the connector test).
Reuse: US federal work, public domain.
"""

from __future__ import annotations

import datetime as dt
import io
import logging
import re
import time
from typing import Any, ClassVar
from urllib.parse import urljoin

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import (
    ConnectorError,
    Kind,
    ParseError,
    PreviousSnapshot,
    RawSnapshot,
    RemovalMeaning,
)
from pipeline.connectors.canonical import normalize_eia
from pipeline.connectors.http import HttpBlocked
from pipeline.normalize import EIA_CANCELED_SHEET

INDEX_URL = "https://www.eia.gov/electricity/data/eia860m/"
LINK_RE = re.compile(r'href="([^"]+?generator\d{4}\.xlsx)"', re.I)
XLSX_MAGIC = b"PK"
PLANNED_SHEET = "Planned"
#: The workbook's sheet of units EIA lists as "Canceled or Indefinitely Postponed" (its title row).
CANCELED_SHEET: str = EIA_CANCELED_SHEET
log = logging.getLogger(__name__)


def _sheet_rows(book: pd.ExcelFile, sheet: str) -> list[dict[str, Any]]:
    df = book.parse(sheet_name=sheet, header=2)
    df.columns = [str(c).strip() for c in df.columns]
    if "Plant ID" not in df.columns:
        raise ParseError(f"{sheet} sheet layout changed: {list(df.columns)[:8]}")
    df = df[df["Plant ID"].notna()]
    return [{str(k): v for k, v in r.items()} for r in df.to_dict("records")]


def read_sheets(
    content: bytes,
    sheets: tuple[str, ...],
    optional: tuple[str, ...] = (),
    errors: dict[str, str] | None = None,
) -> dict[str, list[dict[str, Any]] | None]:
    """Each named sheet of the workbook (opened once) as source-shaped rows: header on the third
    row, column names stripped, the trailing note rows (no Plant ID) dropped. None for a sheet the
    workbook does not have. A sheet that cannot be read (its header lost `Plant ID`, or anything
    else goes wrong reading it) raises, unless it is `optional` (read for counting only): that one
    is logged, its error is put in `errors` when given, and it reads as None."""
    book = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    out: dict[str, list[dict[str, Any]] | None] = {}
    for sheet in sheets:
        if sheet not in book.sheet_names:
            out[sheet] = None
            continue
        try:
            out[sheet] = _sheet_rows(book, sheet)
        except Exception as exc:
            if sheet not in optional:
                raise
            message = f"{type(exc).__name__}: {exc}"[:300]
            log.warning("EIA-860M %s sheet not read: %s", sheet, message)
            if errors is not None:
                errors[sheet] = message
            out[sheet] = None
    return out


def canceled_sheet_summary(
    rows: list[dict[str, Any]] | None, state: str | None, error: str | None
) -> dict[str, Any]:
    """What one parse found on the "Canceled or Postponed" sheet, for the snapshot's `meta`: units,
    plants and nameplate MW (null when the sheet is absent or unreadable), the state its units were
    loaded as (null while the switch is off) and the read error, if any."""
    if rows is None:
        return {"units": None, "plants": None, "nameplate_mw": None, "loaded_as": state, "error": error}
    mw = pd.to_numeric(
        pd.Series([r.get("Nameplate Capacity (MW)") for r in rows], dtype="object"), errors="coerce"
    )
    return {
        "units": len(rows),
        "plants": len({r.get("Plant ID") for r in rows}),
        "nameplate_mw": round(float(mw.sum()), 1),
        "loaded_as": state,
        "error": error,
    }


def canceled_sheet_state(status_map: dict[str, Any], status_key: str = "eia860m") -> str | None:
    """The lifecycle state the status map gives the "Canceled or Postponed" sheet, or None while it
    names none (the sheet is then parsed and counted, and nothing from it is loaded)."""
    block = (status_map.get("sources") or {}).get(status_key) or {}
    state = (block.get("map") or {}).get(CANCELED_SHEET)
    return str(state) if state else None


def find_xlsx_links(html: str, base: str = INDEX_URL) -> list[str]:
    """Candidate workbook URLs in page order, de-duplicated, `/archive/` paths excluded."""
    out: list[str] = []
    for href in LINK_RE.findall(html):
        url = href if href.startswith("http") else urljoin(base, href)
        if "/archive/" in url or url in out:
            continue
        out.append(url)
    return out


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.eia.860m"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "xlsx"
    status_key: ClassVar[str] = "eia860m"
    #: Not `completed`: a unit leaves the Planned sheet when it starts operating (docs/22 §3) *or*
    #: when it moves to the workbook's "Canceled or Postponed" sheet (1,743 rows in August 2026,
    #: docs/27 §R1.1; 1,745 in September). While that sheet is not loaded (`canceled_sheet_state`)
    #: the removal alone cannot say which. Once it is, a move to it is a lifecycle change of the
    #: same record, and a removal from both sheets is still `unknown`: EIA also drops units from the
    #: Canceled sheet and re-files units under new generator ids.
    removal_meaning: ClassVar[RemovalMeaning] = "unknown"
    #: Every column a diffed field, the identity or the placement reads (audit 2026-09-30 F5: a
    #: renamed `Planned Operation Year` used to publish 25 `cod_change` events to null).
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Plant ID",
        "Generator ID",
        "Plant Name",
        "Status",
        "Technology",
        "Nameplate Capacity (MW)",
        "Plant State",
        "County",
        "Entity Name",
        "Planned Operation Year",
        "Planned Operation Month",
        "Balancing Authority Code",
    )
    max_candidates: ClassVar[int] = 6

    def _stored_workbook(self, url: str) -> tuple[dict[str, str], PreviousSnapshot] | None:
        """Conditional-request headers for `url`, and the stored snapshot they describe: this
        source's own previous snapshot first, then the shared source's, whichever holds the
        workbook fetched from that very URL with a validator recorded. None: ask unconditionally."""
        for stored in (self.previous, self.shared):
            if stored is None or (stored.record.get("snapshot") or {}).get("fetched_url") != url:
                continue
            for candidate in stored.meta.get("candidates") or []:
                if candidate.get("url") != url or not candidate.get("xlsx"):
                    continue
                headers = {}
                if candidate.get("etag"):
                    headers["If-None-Match"] = str(candidate["etag"])
                if candidate.get("last_modified"):
                    headers["If-Modified-Since"] = str(candidate["last_modified"])
                if headers:
                    return headers, stored
        return None

    def _reuse(self, stored: PreviousSnapshot) -> bytes | None:
        """The stored workbook a 304 points at, or None when it cannot be read or fails its SHA-256
        (the caller then downloads it: one full fetch, no more)."""
        try:
            body = stored.content()
        except Exception as e:  # an unusable stored object costs one download, no more
            log.warning("stored workbook not reusable: %r", e, extra={"source_id": self.source_id})
            return None
        return body if body.startswith(XLSX_MAGIC) else None

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        index = self.http.get(INDEX_URL)
        links = find_xlsx_links(index.text)
        if not links:
            raise ConnectorError("EIA-860M index page lists no generator workbooks")
        tried: list[dict[str, Any]] = []
        requests_made = 1
        for url in links[: self.max_candidates]:
            conditional = self._stored_workbook(url)
            try:
                if conditional is not None:
                    r = self.http.get(url, timeout=300, headers=conditional[0])
                else:
                    r = self.http.get(url, timeout=300)
            except HttpBlocked as exc:
                tried.append({"url": url, "blocked": str(exc)})
                requests_made += 1  # counted as before: one per candidate tried
                continue
            requests_made += 1
            content: bytes = r.content
            reused: dict[str, Any] | None = None
            if r.status_code == 304 and conditional is not None:
                stored = conditional[1]
                body = self._reuse(stored)
                if body is not None:
                    content = body
                    reused = {
                        "source_id": (stored.record.get("source_id") or self.source_id),
                        "run_id": stored.record.get("id"),
                        "sha256": stored.sha256,
                    }
                else:
                    r = self.http.get(url, timeout=300)
                    requests_made += 1
                    content = r.content
            ok = (r.status_code == 200 or reused is not None) and content.startswith(XLSX_MAGIC)
            entry: dict[str, Any] = {
                "url": url,
                "status": r.status_code,
                "content_type": r.headers.get("Content-Type", ""),
                "bytes": len(content),
                "xlsx": ok,
            }
            if ok:
                previous_validators = {
                    k: v
                    for c in (conditional[1].meta.get("candidates") or [] if conditional else [])
                    if c.get("url") == url
                    for k, v in (("etag", c.get("etag")), ("last_modified", c.get("last_modified")))
                    if v
                }
                entry["etag"] = r.headers.get("ETag") or previous_validators.get("etag")
                entry["last_modified"] = r.headers.get("Last-Modified") or previous_validators.get(
                    "last_modified"
                )
                if reused is not None:
                    entry["not_modified"] = True
                    entry["reused"] = reused
            tried.append(entry)
            if ok:
                return RawSnapshot(
                    content=content,
                    content_type=(
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
                        if reused is not None
                        else r.headers.get("Content-Type", "")
                    ),
                    url=url,
                    retrieved_at=dt.datetime.now(dt.UTC),
                    http_status=r.status_code,
                    ext="xlsx",
                    headers=dict(r.headers),
                    elapsed_s=round(time.monotonic() - t0, 2),
                    requests_made=requests_made,
                    meta={"index_url": INDEX_URL, "candidates": tried},
                )
        raise ConnectorError(f"no real xlsx among the first {len(tried)} EIA-860M links: {tried}")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        if not raw.content.startswith(XLSX_MAGIC):
            raise ParseError(f"{raw.url} answered HTML/other instead of an xlsx (placeholder month)")
        state = canceled_sheet_state(self.status_map, self.status_key)
        errors: dict[str, str] = {}
        sheets = read_sheets(
            raw.content,
            (PLANNED_SHEET, CANCELED_SHEET),
            optional=() if state else (CANCELED_SHEET,),
            errors=errors,
        )
        planned = sheets[PLANNED_SHEET]
        if planned is None:
            raise ParseError(f"{raw.url} has no {PLANNED_SHEET} sheet")
        summary = canceled_sheet_summary(sheets[CANCELED_SHEET], state, errors.get(CANCELED_SHEET))
        # The run record keeps the snapshot's `meta`, so the count can be read back per run
        # (`Store.last_snapshot`), not only from the log.
        raw.meta["canceled_or_postponed"] = summary
        canceled = sheets[CANCELED_SHEET] or []
        log.info(
            "EIA-860M %s sheet: %s units, %s",
            CANCELED_SHEET,
            summary["units"],
            f"loaded as {state}" if state else "not loaded (no state in the status map)",
            extra={"source_id": self.source_id},
        )
        if state is None:
            return planned
        return planned + [{**row, "_sheet": CANCELED_SHEET} for row in canceled]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        df = normalize_eia(pd.DataFrame(rows), self.status_map, raw.retrieved_at_iso)
        df = df.drop(columns=["source_url"])
        return self.finalize(df, rows, raw)
