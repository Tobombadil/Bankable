"""History connectors: monthly series from official archives, for the trends API (`docs/24-trends.md` §9).

Not record connectors (`base.py` says why): their own runner (`runner.py`, CLI
`python -m pipeline.connectors.history run <source_id> --mode backfill|monthly|auto`), their own
scheduler ticks (`infra/scheduler/history.py`) and their own documents
(`documents.py`, read by `services/trends/series.py`). The manifest rows they serve carry
`runner: history`, which keeps the generic bucket ticks from also queueing them
(`infra/scheduler/app.py::due_sources`).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.connectors.history.base import HistoryConnector

#: source id -> the module under this package that exposes `Connector`.
HISTORY_CONNECTORS: dict[str, str] = {
    "us.eia.860m.history": "eia860m.connector",
    "us.iso.ercot.gen_queue.history": "ercot_gis.connector",
}


def connector_class_for(source_id: str) -> type[HistoryConnector]:
    """The history connector class for `source_id`; `KeyError` for a source this family does not serve."""
    module = importlib.import_module(f"pipeline.connectors.history.{HISTORY_CONNECTORS[source_id]}")
    cls: type[HistoryConnector] = module.Connector
    if cls.source_id != source_id:
        raise KeyError(f"{module.__name__} declares {cls.source_id}, expected {source_id}")
    return cls


__all__ = ["HISTORY_CONNECTORS", "connector_class_for"]
