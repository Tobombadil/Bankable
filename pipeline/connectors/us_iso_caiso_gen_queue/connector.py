"""us.iso.caiso.gen_queue — CAISO Public Queue Report (PublicQueueReport.xlsx).

Parse: gridstatus.CAISO's parser over the three sheets (queued / completed / withdrawn), which
drops the legend rows at the bottom of each sheet.
source_record_id: "Queue Position" (unique per report).
Reuse: attribution, raw withheld (docs/13 §1.2, docs/21 §8): derived fields only, credit
"California ISO", link out. The store layer enforces that; this connector only ingests.
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Kind
from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, RawSnapshot
from pipeline.connectors.iso_queue import gridstatus_rows, normalize_iso_rows

URL = "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx"


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.iso.caiso.gen_queue"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "xlsx"
    status_key: ClassVar[str] = "caiso"
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Queue ID",
        "Project Name",
        "Status",
        "Capacity (MW)",
        "Generation Type",
        "Interconnection Agreement Status",
    )

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        r = self.http.get(URL, timeout=180)
        if r.status_code != 200:
            raise ConnectorError(f"GET {URL} -> HTTP {r.status_code}")
        return RawSnapshot(
            content=r.content,
            content_type=r.headers.get("Content-Type", ""),
            url=URL,
            retrieved_at=dt.datetime.now(dt.UTC),
            http_status=r.status_code,
            ext="xlsx",
            headers=dict(r.headers),
            elapsed_s=round(time.monotonic() - t0, 2),
        )

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        return gridstatus_rows("CAISO", raw)

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        return normalize_iso_rows(self, "caiso", rows, raw)
