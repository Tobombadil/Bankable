"""us.eia.860m — EIA-860M Preliminary Monthly Electric Generator Inventory, "Planned" sheet.

Fetch: the index page lists `xls/<month>_generator<year>.xlsx` links in reverse chronological
order, including *future* months as placeholders that answer 200 with an HTML page (docs/02 §7;
observed 2026-09-12: december/october 2026 redirect to HTML, july 2026 is the real file). The
first link whose bytes start with the zip magic wins; the HTML answers are kept in `meta`.
Parse: sheet "Planned", header on the third row; trailing note rows (no Plant ID) dropped.
source_record_id: `<Plant ID>-<Generator ID>`, the EIA identity that anchors resolution.
Reuse: US federal work, public domain.
"""

from __future__ import annotations

import datetime as dt
import io
import re
import time
from typing import Any, ClassVar
from urllib.parse import urljoin

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, ParseError, RawSnapshot
from pipeline.normalize import normalize_eia

INDEX_URL = "https://www.eia.gov/electricity/data/eia860m/"
LINK_RE = re.compile(r'href="([^"]+?generator\d{4}\.xlsx)"', re.I)
XLSX_MAGIC = b"PK"


def find_xlsx_links(html: str, base: str = INDEX_URL) -> list[str]:
    """Candidate workbook URLs in page order, de-duplicated."""
    out: list[str] = []
    for href in LINK_RE.findall(html):
        url = href if href.startswith("http") else urljoin(base, href)
        if url not in out:
            out.append(url)
    return out


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.eia.860m"
    kind: ClassVar[str] = "proposal"
    ext: ClassVar[str] = "xlsx"
    status_key: ClassVar[str] = "eia860m"
    key_source_columns: ClassVar[tuple[str, ...]] = ("Plant ID", "Generator ID", "Plant Name", "Status",
                                                     "Technology", "Nameplate Capacity (MW)", "Plant State")
    max_candidates: ClassVar[int] = 6

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        index = self.http.get(INDEX_URL)
        links = find_xlsx_links(index.text)
        if not links:
            raise ConnectorError("EIA-860M index page lists no generator workbooks")
        tried: list[dict[str, Any]] = []
        for url in links[: self.max_candidates]:
            r = self.http.get(url, timeout=300)
            ok = r.status_code == 200 and r.content.startswith(XLSX_MAGIC)
            tried.append({"url": url, "status": r.status_code, "content_type": r.headers.get("Content-Type", ""),
                          "bytes": len(r.content), "xlsx": ok})
            if ok:
                return RawSnapshot(content=r.content, content_type=r.headers.get("Content-Type", ""), url=url,
                                   retrieved_at=dt.datetime.now(dt.UTC), http_status=r.status_code, ext="xlsx",
                                   headers=dict(r.headers), elapsed_s=round(time.monotonic() - t0, 2),
                                   requests_made=1 + len(tried), meta={"index_url": INDEX_URL, "candidates": tried})
        raise ConnectorError(f"no real xlsx among the first {len(tried)} EIA-860M links: {tried}")

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        if not raw.content.startswith(XLSX_MAGIC):
            raise ParseError(f"{raw.url} answered HTML/other instead of an xlsx (placeholder month)")
        df = pd.read_excel(io.BytesIO(raw.content), sheet_name="Planned", header=2, engine="openpyxl")
        df.columns = [str(c).strip() for c in df.columns]
        if "Plant ID" not in df.columns:
            raise ParseError(f"Planned sheet layout changed: {list(df.columns)[:8]}")
        df = df[df["Plant ID"].notna()]
        return [dict(r) for r in df.to_dict("records")]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        df = normalize_eia(pd.DataFrame(rows), self.status_map, raw.retrieved_at_iso)
        df = df.drop(columns=["source_url"])
        return self.finalize(df, rows, raw)
