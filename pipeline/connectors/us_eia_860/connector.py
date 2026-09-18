"""us.eia.860 -- EIA-860 Annual Electric Generator Report, Schedule 4 "Ownership" sheet.

Fetch: the index page (`INDEX_URL`) lists `xls/eia860<year>.zip` (final) and
`xls/eia860<year>ER.zip` (early release) workbooks; older years live under `archive/xls/`.
eia.gov's robots.txt disallows `/*archive/` (verified 2026-09-18, same rule
`pipeline/connectors/us_eia_860m/connector.py` already honours), so those links are dropped
before any request is made -- `find_zip_links` never returns one. The remaining, non-archive
candidates are tried newest year first; the first whose bytes start with the zip magic `PK`
wins. As of 2026-09-18 the only non-archive candidate is `xls/eia8602025ER.zip`, which answers
HTTP 200 with a 55 KB HTML placeholder (the same "future/early-release month" shape
`us_eia_860m` already handles) -- `verified.note` in `data/sources.yaml` records this. The real
2024 file (22,100,342 bytes, confirmed 2026-09-18) sits at `archive/xls/eia8602024.zip`, a path
this connector must never request. `fetch()` therefore raises `ConnectorError` in the ordinary
case today; a human downloads the archive in a browser and hands it to
`pipeline/context/eia_owners.py --archive PATH`, which shares this module's parsers. A
robots-blocked or non-zip candidate is recorded in `meta["candidates"]` and skipped, never
aborts the run.

Parse: sheet "Ownership", header on the **second** row (row index 1) -- row 0 is the schedule's
title ("2024 Form EIA-860 Data - Schedule 4, 'Generator Ownership' ..."), row 1 is the real
column header. Verified against the real archive (`archive/xls/eia8602024.zip` ->
`4___Owner_Y2024.xlsx`, 495,686 bytes, 5,496 rows), 2026-09-18 -- this differs from the Planned/
Operating sheets' third-row header. `find_owner_member` locates the `4___Owner_Y<year>.xlsx`
member inside the zip (the sheet name and column layout are stable across recent years per
`LayoutY2024.xlsx`; a changed layout raises `ParseError`).

kind: the `Kind` literal in `pipeline/connectors/base.py` has no `"context"` value, so this uses
`"document"` as the least-wrong existing kind: an EIA-860 ownership row, like a docket filing
(`DOCUMENT_COLUMNS`'s own carve-out), reports an annual snapshot rather than a proposal or an
opportunity -- it has no lifecycle of its own, `lifecycle_state` is held constant and
`capacity_mw`/`proposed_cod` stay null (see `base.py`'s `DOCUMENT_COLUMNS` docstring). The
connector's own `fetch`/`parse`/`normalize` produce this snapshot-diff shape for the framework
(health checks, DQ gates); the ownership-graph parquet consumers actually join against is built
by `pipeline/context/eia_owners.py`, the same split `us_eia_860m` / `eia_plants.py` already use
for the Planned/Operating sheets.
source_record_id: a content hash of (Plant Code, Generator ID, Ownership ID) -- verified unique
across all 5,496 rows of the real 2024 sheet, 2026-09-18. Generator ID is free text (not always
numeric) and one real row has no Generator ID at all, so a hash tolerant of missing parts
(`content_hash`) is used rather than a literal composite key.
Reuse: US federal work, public domain.
"""

from __future__ import annotations

import datetime as dt
import io
import pathlib
import re
import time
import zipfile
from typing import Any, ClassVar
from urllib.parse import urljoin

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, ParseError, RawSnapshot, content_hash
from pipeline.connectors.http import HttpBlocked

INDEX_URL = "https://www.eia.gov/electricity/data/eia860/"
ZIP_LINK_RE = re.compile(r'href="([^"]+?eia860(\d{4})[A-Za-z]{0,3}\.zip)"', re.I)
ZIP_MAGIC = b"PK"

#: `4___Owner_Y2024.xlsx` -- three underscores, a literal `Y`, then the four-digit data year.
OWNER_MEMBER_RE = re.compile(r"^4_+Owner_Y(\d{4})\.xlsx$", re.I)


def find_zip_links(html: str, base: str = INDEX_URL) -> list[str]:
    """Candidate annual-archive URLs, newest year first, `/archive/` paths excluded.

    De-duplicated by URL. Ties (same or unknown year) keep page order, matching
    `us_eia_860m.connector.find_xlsx_links`'s stability guarantee.
    """
    seen: set[str] = set()
    dated: list[tuple[int, int, str]] = []
    for order, (href, year) in enumerate(ZIP_LINK_RE.findall(html)):
        url = href if href.startswith("http") else urljoin(base, href)
        if "/archive/" in url or url in seen:
            continue
        seen.add(url)
        dated.append((-int(year), order, url))
    dated.sort()
    return [url for _, _, url in dated]


def find_owner_member(names: list[str]) -> tuple[str, int] | None:
    """The `4___Owner_Y<year>.xlsx` member name and its year, or `None` if absent."""
    for name in names:
        m = OWNER_MEMBER_RE.match(pathlib.PurePosixPath(name).name)
        if m:
            return name, int(m.group(1))
    return None


def parse_owner_sheet(content: bytes) -> pd.DataFrame:
    """Sheet "Ownership" of a `4___Owner_Y<year>.xlsx` member, header on the second row.

    Docstring-level contract shared with `pipeline/context/eia_owners.py`: raises `ParseError`
    if the two identity columns the rest of the pipeline keys on are gone (schema drift).
    """
    df = pd.read_excel(io.BytesIO(content), sheet_name="Ownership", header=1, engine="openpyxl")
    df.columns = [str(c).strip() for c in df.columns]
    if "Plant Code" not in df.columns or "Generator ID" not in df.columns:
        raise ParseError(f"Ownership sheet layout changed: {list(df.columns)[:8]}")
    return df


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.eia.860"
    kind: ClassVar[Kind] = "document"  # no "context" kind exists yet -- see module docstring
    ext: ClassVar[str] = "zip"
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Plant Code",
        "Generator ID",
        "Owner Name",
        "Percent Owned",
    )
    max_candidates: ClassVar[int] = 6

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        index = self.http.get(INDEX_URL)
        links = find_zip_links(index.text)
        if not links:
            raise ConnectorError("EIA-860 index page lists no non-archive annual zip archives")
        tried: list[dict[str, Any]] = []
        for url in links[: self.max_candidates]:
            try:
                r = self.http.get(url, timeout=300)
            except HttpBlocked as exc:
                tried.append({"url": url, "blocked": str(exc)})
                continue
            ok = r.status_code == 200 and r.content.startswith(ZIP_MAGIC)
            tried.append(
                {
                    "url": url,
                    "status": r.status_code,
                    "content_type": r.headers.get("Content-Type", ""),
                    "bytes": len(r.content),
                    "zip": ok,
                }
            )
            if ok:
                return RawSnapshot(
                    content=r.content,
                    content_type=r.headers.get("Content-Type", ""),
                    url=url,
                    retrieved_at=dt.datetime.now(dt.UTC),
                    http_status=r.status_code,
                    ext="zip",
                    headers=dict(r.headers),
                    elapsed_s=round(time.monotonic() - t0, 2),
                    requests_made=1 + len(tried),
                    meta={"index_url": INDEX_URL, "candidates": tried},
                )
        raise ConnectorError(f"no real zip among the first {len(tried)} EIA-860 links: {tried}")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        if not raw.content.startswith(ZIP_MAGIC):
            raise ParseError(f"{raw.url} answered HTML/other instead of a zip (placeholder release)")
        with zipfile.ZipFile(io.BytesIO(raw.content)) as zf:
            found = find_owner_member(zf.namelist())
            if found is None:
                raise ParseError(f"no 4___Owner_Y<year>.xlsx member in {raw.url}")
            member, _year = found
            content = zf.read(member)
        df = parse_owner_sheet(content)
        return [{str(k): v for k, v in r.items()} for r in df.to_dict("records")]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        df = pd.DataFrame(rows)
        if df.empty:
            df["source_record_id"] = pd.Series(dtype="string")
        else:
            df["source_record_id"] = [
                content_hash(p, g, o)
                for p, g, o in zip(
                    df.get("Plant Code"), df.get("Generator ID"), df.get("Ownership ID"), strict=True
                )
            ]
        df["doc_type"] = "eia860_schedule4_owner"
        df["title"] = [
            f"EIA-860 Schedule 4 ownership: plant {p} generator {g}"
            for p, g in zip(df.get("Plant Code"), df.get("Generator ID"), strict=True)
        ]
        df["lifecycle_state"] = "filed"
        df["identifiers"] = [
            {"plant_code": p, "generator_id": g, "ownership_id": o}
            for p, g, o in zip(
                df.get("Plant Code"), df.get("Generator ID"), df.get("Ownership ID"), strict=True
            )
        ]
        return self.finalize(df, rows, raw)
