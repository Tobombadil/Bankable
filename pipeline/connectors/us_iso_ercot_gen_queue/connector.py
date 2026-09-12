"""us.iso.ercot.gen_queue — ERCOT GIS Report (EMIL PG7-200-ER, MIS reportTypeId 15933).

Fetch: the public MIS document list (`IceDocListJsonWS?reportTypeId=15933`) lists monthly
`GIS_Report_<Month><Year>.xlsx` files (and the co-located battery report, which is skipped); the
newest GIS_Report is downloaded via `mirDownload?doclookupId=<DocID>`.
Parse: gridstatus.Ercot's parser over the "Project Details - Large Gen" sheet.
source_record_id: the ERCOT INR ("Queue ID"), unique within a report.
Reuse: open (Website User Agreement clause 5) — raw-ok. Rate: 0.5 rps (clause 6).
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, ParseError, RawSnapshot
from pipeline.connectors.iso_queue import gridstatus_rows, normalize_iso_rows

DOC_LIST = "https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=15933"
DOWNLOAD = "https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId={doc_id}"


def latest_gis_document(doc_list: dict[str, Any]) -> dict[str, Any]:
    docs = [d["Document"] for d in doc_list["ListDocsByRptTypeRes"]["DocumentList"]]
    gis = [d for d in docs if "GIS_Report" in d.get("ConstructedName", "") and d.get("Extension") == "xlsx"]
    if not gis:
        raise ParseError("no GIS_Report xlsx in the ERCOT document list")
    return max(gis, key=lambda d: str(d.get("PublishDate", "")))


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.iso.ercot.gen_queue"
    kind: ClassVar[str] = "proposal"
    ext: ClassVar[str] = "xlsx"
    honour_robots: ClassVar[bool] = False  # MIS servlets are an API, not a crawlable site
    status_key: ClassVar[str] = "ercot"
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Queue ID",
        "Project Name",
        "Status",
        "Capacity (MW)",
        "Generation Type",
        "IA Signed",
        "Approved for Energization",
    )

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        listing = self.http.get(DOC_LIST, honour_robots=False)
        try:
            doc = latest_gis_document(listing.json())
        except (ValueError, KeyError) as e:
            raise ConnectorError(f"ERCOT document list unreadable: {e!r}") from e
        url = DOWNLOAD.format(doc_id=doc["DocID"])
        r = self.http.get(url, honour_robots=False, timeout=180)
        if r.status_code != 200:
            raise ConnectorError(f"GET {url} -> HTTP {r.status_code}")
        return RawSnapshot(
            content=r.content,
            content_type=r.headers.get("Content-Type", ""),
            url=url,
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=r.status_code,
            ext="xlsx",
            headers=dict(r.headers),
            elapsed_s=round(time.monotonic() - t0, 2),
            requests_made=2,
            meta={
                "doc_id": doc["DocID"],
                "friendly_name": doc.get("FriendlyName"),
                "publish_date": doc.get("PublishDate"),
                "product": "PG7-200-ER",
            },
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        return gridstatus_rows("Ercot", raw)

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        return normalize_iso_rows(self, "ercot", rows, raw)
