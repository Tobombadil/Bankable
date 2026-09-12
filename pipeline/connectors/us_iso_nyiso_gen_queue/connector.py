"""us.iso.nyiso.gen_queue — NYISO Interconnection Queue workbook.

Parse: gridstatus.NYISO's parser over the active, cluster, withdrawn and in-service sheets (the
workbook's "Load Projects" sheet is a separate large-load register, not ingested here).
source_record_id: "Queue Pos." where present; 1,350 withdrawn rows carry no position and get a
content hash of name/county/state/MW/date/status (docs/20 §3.1). Two positions are duplicated in
the source file, so `dedupe_strategy = "suffix"` (#2 in file order) and the run records a warning.
Reuse: attribution (derived-only until counsel sign-off, docs/13 §1.5).
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, ClassVar

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, RawSnapshot
from pipeline.connectors.iso_queue import gridstatus_rows, normalize_iso_rows

URL = "https://www.nyiso.com/documents/20142/1407078/NYISO-Interconnection-Queue.xlsx"


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.iso.nyiso.gen_queue"
    kind: ClassVar[str] = "proposal"
    ext: ClassVar[str] = "xlsx"
    status_key: ClassVar[str] = "nyiso"
    dedupe_strategy: ClassVar[str] = "suffix"
    key_source_columns: ClassVar[tuple[str, ...]] = ("Queue ID", "Project Name", "Status", "Capacity (MW)",
                                                     "Generation Type")

    def fetch(self) -> RawSnapshot:
        t0 = time.monotonic()
        r = self.http.get(URL, timeout=180)
        if r.status_code != 200:
            raise ConnectorError(f"GET {URL} -> HTTP {r.status_code}")
        return RawSnapshot(content=r.content, content_type=r.headers.get("Content-Type", ""), url=URL,
                           retrieved_at=dt.datetime.now(dt.UTC), http_status=r.status_code, ext="xlsx",
                           headers=dict(r.headers), elapsed_s=round(time.monotonic() - t0, 2))

    def parse(self, raw: RawSnapshot) -> list[dict[str, Any]]:
        return gridstatus_rows("NYISO", raw)

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        return normalize_iso_rows(self, "nyiso", rows, raw)
