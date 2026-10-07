"""Load a `us.iso.ercot.large_load_queue` run: a catalogue watch, never a project.

What the connector emits (`pipeline/connectors/us_iso_ercot_large_load_queue/connector.py`): one
row per public ERCOT EMIL *product* whose name or description reads like the large-load
interconnection status report that Nodal Protocol 3.2.7 (NPRR1267) requires. It does not emit
large-load requests, because ERCOT publishes none as data: rechecked 2026-10-07, the large-load
page, the Large Load Working Group meeting pages and the board decks carry the queue only as
system-wide charts in PDF decks (MW by study status and by year), and per-project status goes to
TSPs only (docs/02, docs/25 §3.10). Every run so far has had zero rows.

The generic loader would write each such product as a `kind = load` proposal named after the report
("Large Load Interconnection Status Report", sponsor ERCOT): a fake project on the public load list,
and a `new` event in alerts. This loader is the `SPECIALISED_LOADERS` branch for the source. It
records the run like any other, so the source's health and run history stay right, and writes no
proposal. When the catalogue does list a product, the load warns and lists the products, which is
the signal to write the row parser against the real file. What that parser should produce is
settled in docs/25 §3.10: request-level rows become proposals, and aggregate rows (by zone, TSP,
status) become totals, never projects.
"""

from __future__ import annotations

import logging
import pathlib

import pandas as pd
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from pipeline.connectors.store import Store
from services.ingest.loader import (
    GateRefused,
    LoadResult,
    _run_for_load,
    load_refusal,
    upsert_licence_and_source,
)

log = logging.getLogger(__name__)

SOURCE_ID = "us.iso.ercot.large_load_queue"


def load_catalogue_watch_run(
    session: Session,
    source_id: str,
    ts: str,
    *,
    data_root: pathlib.Path = pathlib.Path("data"),
    registry: Registry | None = None,
    store: Store | None = None,
    kind: object = None,
) -> LoadResult:
    """`load_from_files`' signature. Refuses a gated source, records the run, writes no proposal,
    and warns when the watched catalogue lists a product (module docstring)."""
    registry = registry or Registry()
    entry = registry.get(source_id)
    refusal = load_refusal(entry)
    if refusal:
        raise GateRefused(refusal)
    store = store if store is not None else Store(data_root)
    normalized_path = store.normalized_path(source_id, ts)
    if not store.exists(normalized_path):
        raise FileNotFoundError(store.locate(normalized_path))
    records_df = store.read_parquet(normalized_path)
    events_path = store.events_path(source_id, ts)
    events_df = store.read_parquet(events_path) if store.exists(events_path) else None
    run_path = store.run_path(source_id, ts)
    run_record = store.read_json(run_path) if store.exists(run_path) else None
    source = upsert_licence_and_source(session, entry, registry.version)
    run = _run_for_load(session, source, run_record, records_df, events_df, entry.egress)
    result = LoadResult(source_run_id=run.id, kind="proposal")
    if len(records_df):
        names = sorted({str(n) for n in records_df.get("name_canonical", pd.Series(dtype=str)).dropna()})
        message = (
            f"{source_id}: the ERCOT catalogue lists {len(records_df)} large-load product(s) "
            f"({'; '.join(names)}); nothing is published until a row parser exists for the real file"
        )
        result.warnings.append(message)
        log.warning(message, extra={"source_id": source_id})
    return result
