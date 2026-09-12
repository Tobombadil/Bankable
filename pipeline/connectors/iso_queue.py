"""Shared plumbing for the gridstatus-wrapped ISO queue connectors (docs/20 §3.1: "gridstatus is
wrapped, not replaced"). gridstatus fetches inside `get_interconnection_queue`; we hand it the
bytes we fetched politely instead, so its parser runs unchanged on recorded fixtures."""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

from pipeline.connectors.base import Connector, ParseError, RawSnapshot
from pipeline.connectors.canonical import normalize_iso

XLSX_MAGIC = b"PK"


def gridstatus_rows(iso_name: str, raw: RawSnapshot) -> list[dict[str, Any]]:
    """Run gridstatus.<iso_name>().get_interconnection_queue() over `raw.content`."""
    if not raw.content.startswith(XLSX_MAGIC):
        raise ParseError(f"{raw.url} is not an xlsx (first bytes {raw.content[:16]!r})")
    import gridstatus  # slow import, keep it out of module load

    iso = getattr(gridstatus, iso_name)()
    content = raw.content
    iso.get_raw_interconnection_queue = lambda *a, **k: io.BytesIO(content)  # type: ignore[method-assign]  # inject bytes
    df: pd.DataFrame = iso.get_interconnection_queue()
    df.columns = [str(c) for c in df.columns]
    return [{str(k): v for k, v in r.items()} for r in df.to_dict("records")]


def normalize_iso_rows(
    connector: Connector, key: str, rows: list[dict[str, Any]], raw: RawSnapshot
) -> pd.DataFrame:
    df = normalize_iso(pd.DataFrame(rows), key, connector.status_map, raw.retrieved_at_iso)
    df = df.drop(columns=["source_url"])  # finalize stamps the fetched file URL
    return connector.finalize(df, rows, raw)
