"""Price connectors: one family package for the hub and zone prices of docs/29 slice 1.

Not record connectors (`base.py` says why): they have their own runner (`runner.py`, CLI
`python -m pipeline.connectors.prices run <source_id> --mode latest|daily|backfill`), their own
scheduler ticks (`infra/scheduler/prices.py`) and their own loader (`services/prices`). The
manifest rows they serve carry `runner: prices`, which keeps the generic bucket ticks from also
queueing them (`infra/scheduler/app.py::due_sources`).
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pipeline.connectors.prices.base import PriceConnector

#: source id -> module under this package that exposes `Connector`.
PRICE_CONNECTORS: dict[str, str] = {
    "us.iso.ercot.prices": "ercot",
    "us.iso.spp.prices": "spp",
    "us.iso.caiso.oasis_prices": "caiso",
    "us.iso.nyiso.prices": "nyiso",
    "gb.elexon.bmrs_prices": "elexon",
    "us.eia.retail_sales": "eia_retail",
}


def connector_class_for(source_id: str) -> type[PriceConnector]:
    """The price connector class for `source_id`; `KeyError` for a source this family does not serve."""
    module = importlib.import_module(f"pipeline.connectors.prices.{PRICE_CONNECTORS[source_id]}")
    cls: type[PriceConnector] = module.Connector
    if cls.source_id != source_id:
        raise KeyError(f"{module.__name__} declares {cls.source_id}, expected {source_id}")
    return cls


__all__ = ["PRICE_CONNECTORS", "connector_class_for"]
