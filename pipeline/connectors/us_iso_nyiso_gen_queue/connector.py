"""us.iso.nyiso.gen_queue — NYISO Interconnection Queue workbook.

Parse: gridstatus.NYISO's parser over the active, cluster, withdrawn and in-service sheets (the
workbook's "Load Projects" sheet is a separate large-load register, not ingested here). The
Withdrawn sheets are padded with 1,350 rows that carry no queue position, no name, no county and
no date — only the sheet's implied status; they are dropped, because a row with no identity is
not an observation and hashing them would produce 1,350 identical ids.
source_record_id: "Queue Pos."; the two positions that appear twice in the workbook are suffixed
`#2` in file order (`dedupe_strategy = "suffix"`) and the run records a DQ warning.
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
        rows = gridstatus_rows("NYISO", raw)
        return [r for r in rows if _identified(r)]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        return normalize_iso_rows(self, "nyiso", rows, raw)


def _identified(row: dict[str, Any]) -> bool:
    """A queue row needs a position or at least a project name to be an observation."""
    return any(not _blank(row.get(c)) for c in ("Queue ID", "Project Name"))


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan", "NaT")
