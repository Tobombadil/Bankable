"""Shared plumbing for the ISO queue connectors (CAISO, ERCOT, NYISO). The workbook parsers are
gridstatus 0.36.0's, vendored in `pipeline/vendor/gridstatus` (BSD 3-Clause) so the library and the
dependency pins it carried are gone; each connector fetches the bytes politely and `parse` runs the
vendored parser over them, so it is pure and runs unchanged on recorded fixtures."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import pandas as pd

from pipeline.connectors.base import Connector, ParseError, RawSnapshot
from pipeline.connectors.canonical import harmonise_iso_frame, normalize_iso
from pipeline.vendor.gridstatus import queues

XLSX_MAGIC = b"PK"

#: Connector-facing ISO name -> vendored parser (the names gridstatus's classes had).
PARSERS: dict[str, Callable[[bytes], pd.DataFrame]] = {
    "CAISO": queues.caiso_queue,
    "Ercot": queues.ercot_queue,
    "NYISO": queues.nyiso_queue,
}


def queue_rows(iso_name: str, raw: RawSnapshot) -> list[dict[str, Any]]:
    """The vendored gridstatus queue parser for `iso_name` over `raw.content`, as row dicts."""
    if not raw.content.startswith(XLSX_MAGIC):
        raise ParseError(f"{raw.url} is not an xlsx (first bytes {raw.content[:16]!r})")
    try:
        df = PARSERS[iso_name](raw.content)
    except queues.QueueLayoutError as e:
        raise ParseError(f"{raw.url}: {e}") from e
    df.columns = [str(c) for c in df.columns]
    return [{str(k): v for k, v in r.items()} for r in df.to_dict("records")]


def normalize_iso_rows(
    connector: Connector, key: str, rows: list[dict[str, Any]], raw: RawSnapshot
) -> pd.DataFrame:
    df = normalize_iso(pd.DataFrame(rows), key, connector.status_map, raw.retrieved_at_iso)
    df = df.drop(columns=["source_url"])  # finalize stamps the fetched file URL
    return connector.finalize(df, rows, raw)


def restate_iso_status(connector: Connector, key: str, df: pd.DataFrame) -> pd.DataFrame | None:
    """`Connector.restate_status` for an ISO queue: re-harmonise each stored row's
    own `raw` payload (the parser's row, kept by `finalize`) under the connector's current status
    map. None when the frame carries no `raw` column, so the caller diffs it as stored."""
    if "raw" not in df.columns:
        return None
    rows = [json.loads(r) if isinstance(r, str) and r else {} for r in df["raw"]]
    harmonised = harmonise_iso_frame(pd.DataFrame(rows, index=df.index), key, connector.status_map)
    return pd.DataFrame(
        {"lifecycle_state": [s for s, _ in harmonised], "status_rule": [r for _, r in harmonised]},
        index=df.index,
    )
