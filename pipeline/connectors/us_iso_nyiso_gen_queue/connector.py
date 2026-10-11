"""us.iso.nyiso.gen_queue — NYISO Interconnection Queue workbook.

Parse: gridstatus 0.36.0's NYISO parser (vendored, `pipeline/vendor/gridstatus`) over the active,
cluster, withdrawn and in-service sheets (the workbook's "Load Projects" sheet is a separate
large-load register, not ingested here). The Withdrawn sheets are padded with 1,350 rows that
carry no queue position, no name, no county and no date — only the sheet's implied status; they
are dropped, because a row with no identity is not an observation and hashing them would produce
1,350 identical ids.
source_record_id: "Queue Pos."; the two positions that appear twice in the workbook are suffixed
`#2` in file order (`dedupe_strategy = "suffix"`) and the run records a DQ warning.
Reuse: attribution (derived-only until counsel sign-off, docs/13 §1.5).
"""

from __future__ import annotations

import datetime as dt
import time
from typing import Any, ClassVar, Literal

import pandas as pd

from pipeline.connectors.base import Connector as BaseConnector
from pipeline.connectors.base import ConnectorError, Kind, RawSnapshot
from pipeline.connectors.dedupe import split_key
from pipeline.connectors.iso_queue import normalize_iso_rows, queue_rows

URL = "https://www.nyiso.com/documents/20142/1407078/NYISO-Interconnection-Queue.xlsx"


class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.iso.nyiso.gen_queue"
    kind: ClassVar[Kind] = "proposal"
    ext: ClassVar[str] = "xlsx"
    status_key: ClassVar[str] = "nyiso"
    #: A row that leaves the workbook is announced as `delisted` (owner, 2026-10-10), never as a
    #: withdrawal: withdrawn and in-service projects have sheets of their own, so a project that
    #: disappears altogether left for a reason the workbook does not give. `removal_meaning` stays
    #: `unknown` (base contract).
    announce_removals: ClassVar[bool] = True
    register_name: ClassVar[str | None] = "NYISO"
    dedupe_strategy: ClassVar[Literal["hold", "suffix"]] = "suffix"
    key_source_columns: ClassVar[tuple[str, ...]] = (
        "Queue ID",
        "Project Name",
        "Status",
        "Capacity (MW)",
        "Generation Type",
    )

    @classmethod
    def project_root(cls, record_key: str) -> str:
        """The queue position without the content suffix a repeated position carries (`0031#h...`;
        `pipeline/connectors/dedupe.py`). When one of Astoria Energy's two phases under 0031 leaves
        the workbook, the other is still listed and the suffix shifts, so the old key is a re-key,
        not a departure. A letter is part of the position, not a suffix: 0225 (Ithaca Transmission)
        and 0225A (SII Rotterdam Junction) are different projects (workbook of 2026-10-09)."""
        return split_key(record_key)[0]

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
        rows = queue_rows("NYISO", raw)
        return [r for r in rows if _identified(r)]

    def normalize(self, rows: list[dict[str, Any]], raw: RawSnapshot) -> pd.DataFrame:
        return normalize_iso_rows(self, "nyiso", rows, raw)


def _identified(row: dict[str, Any]) -> bool:
    """A queue row needs a position or at least a project name to be an observation."""
    return any(not _blank(row.get(c)) for c in ("Queue ID", "Project Name"))


def _blank(v: Any) -> bool:
    return v is None or (isinstance(v, float) and pd.isna(v)) or str(v).strip() in ("", "nan", "NaT")
