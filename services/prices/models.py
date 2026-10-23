"""The price tables (migration 0041; docs/29 §7.1 as built for slice 1).

`price_location` is the catalogue (`pipeline/connectors/prices/locations.py`) with its placement;
`price_daily` one summary per location, market and market-local day; `price_stat` the trailing
statistics the statistics job derives from it; `price_latest` the newest interval per location and
market, written only for a source whose licence allows raw publication; `retail_price` EIA's state
averages by month. Each carries `source_id`/`licence_id`, the visibility predicate's two keys, and
`source_url`/`retrieved_at` (the provenance quartet, CLAUDE.md). The statistics are derived values;
their provenance is the source rows they summarise (`retrieved_at_max`, the newest of them).
"""

from __future__ import annotations

import datetime as dt
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from services.db.base import Base
from services.db.models import Licence, Source, utcnow
from services.db.types import JSONVariant

PRICE_LOCATION_KINDS = ("hub", "zone")
PRICE_MARKETS = ("da", "rt")
PLACEMENT_GRADES = ("point", "region")
#: The one statistics window slice 1 computes: the trailing year, or as much of it as history allows.
STAT_WINDOWS = ("trailing_12m",)
RETAIL_SECTORS = ("residential", "commercial", "industrial", "transportation", "total")


def _in_list(column: str, values: tuple[str, ...]) -> str:
    """`column IN ('a', 'b')` for a CHECK; a one-value tuple's repr would leave a trailing comma."""
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


class PriceLocation(Base):
    __tablename__ = "price_location"

    id: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    market: Mapped[str] = mapped_column(sa.Text, nullable=False)
    name: Mapped[str] = mapped_column(sa.Text, nullable=False)
    label: Mapped[str] = mapped_column(sa.Text, nullable=False)
    kind: Mapped[str] = mapped_column(sa.Text, nullable=False)
    timezone: Mapped[str] = mapped_column(sa.Text, nullable=False)
    currency: Mapped[str] = mapped_column(sa.Text, nullable=False)
    markets: Mapped[str] = mapped_column(sa.Text, nullable=False, default="da,rt")
    reference_id: Mapped[str | None] = mapped_column(sa.Text)
    note: Mapped[str | None] = mapped_column(sa.Text)
    placement_grade: Mapped[str] = mapped_column(sa.Text, nullable=False)
    placement_method: Mapped[str] = mapped_column(sa.Text, nullable=False)
    placement_basis: Mapped[str] = mapped_column(sa.Text, nullable=False)
    region_id: Mapped[str] = mapped_column(sa.Text, nullable=False)
    lat: Mapped[float | None] = mapped_column(sa.Float)
    lon: Mapped[float | None] = mapped_column(sa.Float)
    placement_evidence_url: Mapped[str | None] = mapped_column(sa.Text)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    source: Mapped[Source] = relationship(lazy="joined")
    licence: Mapped[Licence] = relationship(lazy="joined")

    __table_args__ = (
        sa.CheckConstraint(f"kind IN {PRICE_LOCATION_KINDS!r}", name="kind_vocab"),
        sa.CheckConstraint(f"placement_grade IN {PLACEMENT_GRADES!r}", name="placement_grade_vocab"),
        sa.CheckConstraint(
            "placement_grade = 'point' OR (lat IS NULL AND lon IS NULL)", name="point_only_coords"
        ),
        sa.Index("ix_price_location_market", "market"),
    )


class PriceDaily(Base):
    __tablename__ = "price_daily"

    location_id: Mapped[str] = mapped_column(sa.ForeignKey("price_location.id"), primary_key=True)
    market: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    day: Mapped[dt.date] = mapped_column(sa.Date, primary_key=True)
    intervals: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    expected_intervals: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    mean: Mapped[float] = mapped_column(sa.Float, nullable=False)
    min: Mapped[float] = mapped_column(sa.Float, nullable=False)
    max: Mapped[float] = mapped_column(sa.Float, nullable=False)
    negative_intervals: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    hours: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    top4_mean: Mapped[float | None] = mapped_column(sa.Float)
    bottom4_mean: Mapped[float | None] = mapped_column(sa.Float)
    basis_mean: Mapped[float | None] = mapped_column(sa.Float)
    basis_intervals: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    computed_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint(f"market IN {PRICE_MARKETS!r}", name="market_vocab"),
        sa.Index("ix_price_daily_source_day", "source_id", "day"),
    )


class PriceStat(Base):
    __tablename__ = "price_stat"

    location_id: Mapped[str] = mapped_column(sa.ForeignKey("price_location.id"), primary_key=True)
    market: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    #: Not `window`, a reserved word in Postgres.
    window_kind: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    window_start: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    window_end: Mapped[dt.date] = mapped_column(sa.Date, nullable=False)
    days: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    sample_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    expected_count: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    interval_minutes: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    mean: Mapped[float | None] = mapped_column(sa.Float)
    mean_30d: Mapped[float | None] = mapped_column(sa.Float)
    window_30d_start: Mapped[dt.date | None] = mapped_column(sa.Date)
    samples_30d: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    expected_30d: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    basis_mean: Mapped[float | None] = mapped_column(sa.Float)
    basis_reference_id: Mapped[str | None] = mapped_column(sa.Text)
    basis_samples: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    negative_share: Mapped[float | None] = mapped_column(sa.Float)
    spread_mean: Mapped[float | None] = mapped_column(sa.Float)
    spread_days: Mapped[int] = mapped_column(sa.Integer, nullable=False, default=0)
    #: Statistic name -> why it is withheld (coverage under 90%, no reference hub, ...).
    withheld: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at_max: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    computed_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint(f"market IN {PRICE_MARKETS!r}", name="market_vocab"),
        sa.CheckConstraint(_in_list("window_kind", STAT_WINDOWS), name="window_kind_vocab"),
    )


class PriceLatest(Base):
    __tablename__ = "price_latest"

    location_id: Mapped[str] = mapped_column(sa.ForeignKey("price_location.id"), primary_key=True)
    market: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    interval_start: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    interval_end: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    price: Mapped[float] = mapped_column(sa.Float, nullable=False)
    currency: Mapped[str] = mapped_column(sa.Text, nullable=False)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)

    __table_args__ = (sa.CheckConstraint(f"market IN {PRICE_MARKETS!r}", name="market_vocab"),)


class RetailPrice(Base):
    __tablename__ = "retail_price"

    state: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    sector: Mapped[str] = mapped_column(sa.Text, primary_key=True)
    period: Mapped[str] = mapped_column(sa.Text, primary_key=True)  # YYYY-MM
    price_cents_kwh: Mapped[float | None] = mapped_column(sa.Float)
    revenue_thousand_usd: Mapped[float | None] = mapped_column(sa.Float)
    sales_mwh: Mapped[float | None] = mapped_column(sa.Float)
    customers: Mapped[float | None] = mapped_column(sa.Float)
    data_status: Mapped[str | None] = mapped_column(sa.Text)
    source_id: Mapped[str] = mapped_column(sa.ForeignKey("source.id"), nullable=False)
    licence_id: Mapped[str] = mapped_column(sa.ForeignKey("licence.id"), nullable=False)
    source_url: Mapped[str] = mapped_column(sa.Text, nullable=False)
    retrieved_at: Mapped[dt.datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    #: The file's own release date (its `Last-Modified`): EIA's acknowledgment names it.
    released: Mapped[str | None] = mapped_column(sa.Text)

    __table_args__ = (
        sa.CheckConstraint(f"sector IN {RETAIL_SECTORS!r}", name="sector_vocab"),
        sa.Index("ix_retail_price_period", "period"),
    )


__all__ = [
    "PLACEMENT_GRADES",
    "PRICE_LOCATION_KINDS",
    "PRICE_MARKETS",
    "RETAIL_SECTORS",
    "STAT_WINDOWS",
    "PriceDaily",
    "PriceLatest",
    "PriceLocation",
    "PriceStat",
    "RetailPrice",
]
