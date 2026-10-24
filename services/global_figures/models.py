"""The global electricity tables (migration 0045; `docs/26-global-figures.md` §6).

`global_electricity_load` holds one row per (dataset, route) currently served: Ember's yearly data, its
monthly data and its monthly wind and solar capacity, each from the API or from a person's download of
the CSV. A new load of the same dataset and route replaces the old one inside one transaction, so a
reader sees the previous load or the new one, never a mixture. `global_electricity_value` holds that
load's values, one per (area, period, source, metric), each with `source_id`, `licence_id`,
`source_url` and `retrieved_at` (CLAUDE.md), and the area's own name and kind as Ember gave them.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from pipeline.connectors.global_ember_electricity.areas import AREA_KINDS
from pipeline.connectors.global_ember_electricity.ember import DATASETS, METRICS, ORIGINS
from services.db.base import Base
from services.db.models import utcnow
from services.db.types import JSONVariant

GLOBAL_METRICS: tuple[str, ...] = tuple(METRICS)


def _in_list(column: str, values: tuple[str, ...]) -> str:
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class GlobalElectricityLoad(Base):
    __tablename__ = "global_electricity_load"

    #: `<run ts>:<dataset>:<origin>`: the run that wrote the values, so a load is never applied twice.
    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    dataset: Mapped[str] = mapped_column(sa.Text, nullable=False)
    origin: Mapped[str] = mapped_column(sa.Text, nullable=False)
    run_ts: Mapped[str] = mapped_column(sa.Text, nullable=False)
    run_id: Mapped[str | None] = mapped_column(sa.Text)
    #: The metrics this load carries, comma-separated (`generation_twh,share_of_generation_pct`).
    metrics: Mapped[str] = mapped_column(sa.Text, nullable=False)
    rows: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    areas: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    first_period: Mapped[str | None] = mapped_column(sa.Text)
    last_period: Mapped[str | None] = mapped_column(sa.Text)
    #: Year -> countries with a generation value (or a capacity value, for a capacity-only load).
    countries_by_year: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    loaded_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False, default=utcnow)

    __table_args__ = (
        sa.CheckConstraint(_in_list("dataset", DATASETS), name="dataset_vocab"),
        sa.CheckConstraint(_in_list("origin", ORIGINS), name="origin_vocab"),
        sa.UniqueConstraint("dataset", "origin"),
    )


class GlobalElectricityValue(Base):
    __tablename__ = "global_electricity_value"

    load_id: Mapped[str] = mapped_column(
        sa.ForeignKey("global_electricity_load.id", ondelete="CASCADE"), primary_key=True
    )
    area_id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    period: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    source: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    metric: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    dataset: Mapped[str] = mapped_column(sa.Text, nullable=False)
    area_name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    area_kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    iso3: Mapped[str | None] = mapped_column(sa.Text)
    year: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    month: Mapped[int | None] = mapped_column(sa.Integer)
    source_label: Mapped[str] = mapped_column(sa.Text, nullable=False)
    is_aggregate: Mapped[bool] = mapped_column(sa.Boolean, nullable=False, default=False)
    value: Mapped[float] = mapped_column(sa.Float, nullable=False)
    unit: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    __table_args__ = (
        sa.CheckConstraint(_in_list("area_kind", AREA_KINDS), name="area_kind_vocab"),
        sa.CheckConstraint(_in_list("metric", GLOBAL_METRICS), name="metric_vocab"),
        sa.Index("ix_global_electricity_value_area", "load_id", "area_id", "metric", "year"),
        sa.Index("ix_global_electricity_value_kind_year", "load_id", "area_kind", "metric", "year"),
    )


__all__ = ["GLOBAL_METRICS", "GlobalElectricityLoad", "GlobalElectricityValue"]
