"""Energy prices, slice 1 (docs/29 §7.1; owner decision 2026-10-22, hubs and zones first).

Five tables, each row carrying `source_id`/`licence_id` (the visibility predicate's keys) and its
`source_url`/`retrieved_at`:

* `price_location` — the hubs and zones (ERCOT 15, SPP 3, CAISO 7, NYISO 11, GB 1) with their
  placement: `point` only for a position the market itself publishes (SPP's price map), else
  `region` (the state or country) with no coordinate (`point_only_coords`).
* `price_daily` — one summary per location, market (`da`, `rt`) and market-local day.
* `price_stat` — the trailing statistics the daily job derives (window, sample count, coverage,
  the figures, and the ones withheld with their reason).
* `price_latest` — the newest interval per location and market, written only for a source whose
  licence allows raw publication.
* `retail_price` — EIA-861M average retail price by state, sector and month.

Prices are context data with no lifecycle (docs/29 §7.1): no `event` rows, no link to
`asset.attributes` (the ADR 0008 §4 amendment the decision row records). The migration writes no
rows; the price loader does (`services/prices/load.py`).

Revision: 0041. `down_revision` is 0040 (private projects), the newest revision on this lane's
base since 0040 merged; 0039 (site finder) is still held by its lane, so whichever of 0039 and 0041
lands second re-parents onto the other, keeping one head (docs/29 §9 P3). Guards follow 0037/0038:
a SQLite store built by `create_all` already has the tables and is stamped. Downgrade drops the
tables (every row is re-derivable from the interval store).

Revision ID: 0041
Revises: 0040
Create Date: 2026-10-22
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import JSONVariant

revision: str = "0041"
down_revision: str | None = "0040"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.prices.models` (0020-0038's convention).
KINDS = ("hub", "zone")
MARKETS = ("da", "rt")
GRADES = ("point", "region")
SECTORS = ("residential", "commercial", "industrial", "transportation", "total")
TABLES = ("price_latest", "price_stat", "price_daily", "price_location", "retail_price")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _provenance(*, retrieved: str = "retrieved_at") -> list[sa.Column]:
    return [
        sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
        sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
        sa.Column("source_url", sa.Text, nullable=False),
        sa.Column(retrieved, sa.TIMESTAMP(timezone=True), nullable=False),
    ]


def upgrade() -> None:
    if not _has_table("price_location"):
        op.create_table(
            "price_location",
            sa.Column("id", sa.Text, primary_key=True),
            sa.Column("market", sa.Text, nullable=False),
            sa.Column("name", sa.Text, nullable=False),
            sa.Column("label", sa.Text, nullable=False),
            sa.Column("kind", sa.Text, nullable=False),
            sa.Column("timezone", sa.Text, nullable=False),
            sa.Column("currency", sa.Text, nullable=False),
            sa.Column("markets", sa.Text, nullable=False, server_default="da,rt"),
            sa.Column("reference_id", sa.Text, nullable=True),
            sa.Column("note", sa.Text, nullable=True),
            sa.Column("placement_grade", sa.Text, nullable=False),
            sa.Column("placement_method", sa.Text, nullable=False),
            sa.Column("placement_basis", sa.Text, nullable=False),
            sa.Column("region_id", sa.Text, nullable=False),
            sa.Column("lat", sa.Float, nullable=True),
            sa.Column("lon", sa.Float, nullable=True),
            sa.Column("placement_evidence_url", sa.Text, nullable=True),
            *_provenance(),
            sa.Column("updated_at", sa.TIMESTAMP(timezone=True), nullable=True, server_default=sa.func.now()),
            sa.CheckConstraint(f"kind IN {KINDS!r}", name="ck_price_location_kind_vocab"),
            sa.CheckConstraint(
                f"placement_grade IN {GRADES!r}", name="ck_price_location_placement_grade_vocab"
            ),
            sa.CheckConstraint(
                "placement_grade = 'point' OR (lat IS NULL AND lon IS NULL)",
                name="ck_price_location_point_only_coords",
            ),
        )
        op.create_index("ix_price_location_market", "price_location", ["market"])
    if not _has_table("price_daily"):
        op.create_table(
            "price_daily",
            sa.Column("location_id", sa.Text, sa.ForeignKey("price_location.id"), primary_key=True),
            sa.Column("market", sa.Text, primary_key=True),
            sa.Column("day", sa.Date, primary_key=True),
            sa.Column("intervals", sa.Integer, nullable=False),
            sa.Column("expected_intervals", sa.Integer, nullable=False),
            sa.Column("interval_minutes", sa.Integer, nullable=False),
            sa.Column("mean", sa.Float, nullable=False),
            sa.Column("min", sa.Float, nullable=False),
            sa.Column("max", sa.Float, nullable=False),
            sa.Column("negative_intervals", sa.Integer, nullable=False),
            sa.Column("hours", sa.Integer, nullable=False),
            sa.Column("top4_mean", sa.Float, nullable=True),
            sa.Column("bottom4_mean", sa.Float, nullable=True),
            sa.Column("basis_mean", sa.Float, nullable=True),
            sa.Column("basis_intervals", sa.Integer, nullable=False, server_default="0"),
            *_provenance(),
            sa.Column(
                "computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.CheckConstraint(f"market IN {MARKETS!r}", name="ck_price_daily_market_vocab"),
        )
        op.create_index("ix_price_daily_source_day", "price_daily", ["source_id", "day"])
    if not _has_table("price_stat"):
        op.create_table(
            "price_stat",
            sa.Column("location_id", sa.Text, sa.ForeignKey("price_location.id"), primary_key=True),
            sa.Column("market", sa.Text, primary_key=True),
            sa.Column("window_kind", sa.Text, primary_key=True),
            sa.Column("window_start", sa.Date, nullable=False),
            sa.Column("window_end", sa.Date, nullable=False),
            sa.Column("days", sa.Integer, nullable=False),
            sa.Column("sample_count", sa.Integer, nullable=False),
            sa.Column("expected_count", sa.Integer, nullable=False),
            sa.Column("interval_minutes", sa.Integer, nullable=False),
            sa.Column("mean", sa.Float, nullable=True),
            sa.Column("mean_30d", sa.Float, nullable=True),
            sa.Column("window_30d_start", sa.Date, nullable=True),
            sa.Column("samples_30d", sa.Integer, nullable=False, server_default="0"),
            sa.Column("expected_30d", sa.Integer, nullable=False, server_default="0"),
            sa.Column("basis_mean", sa.Float, nullable=True),
            sa.Column("basis_reference_id", sa.Text, nullable=True),
            sa.Column("basis_samples", sa.Integer, nullable=False, server_default="0"),
            sa.Column("negative_share", sa.Float, nullable=True),
            sa.Column("spread_mean", sa.Float, nullable=True),
            sa.Column("spread_days", sa.Integer, nullable=False, server_default="0"),
            sa.Column("withheld", JSONVariant(), nullable=False, server_default="{}"),
            *_provenance(retrieved="retrieved_at_max"),
            sa.Column(
                "computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.CheckConstraint(f"market IN {MARKETS!r}", name="ck_price_stat_market_vocab"),
            sa.CheckConstraint("window_kind IN ('trailing_12m')", name="ck_price_stat_window_kind_vocab"),
        )
    if not _has_table("price_latest"):
        op.create_table(
            "price_latest",
            sa.Column("location_id", sa.Text, sa.ForeignKey("price_location.id"), primary_key=True),
            sa.Column("market", sa.Text, primary_key=True),
            sa.Column("interval_start", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("interval_end", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("price", sa.Float, nullable=False),
            sa.Column("currency", sa.Text, nullable=False),
            *_provenance(),
            sa.CheckConstraint(f"market IN {MARKETS!r}", name="ck_price_latest_market_vocab"),
        )
    if not _has_table("retail_price"):
        op.create_table(
            "retail_price",
            sa.Column("state", sa.Text, primary_key=True),
            sa.Column("sector", sa.Text, primary_key=True),
            sa.Column("period", sa.Text, primary_key=True),
            sa.Column("price_cents_kwh", sa.Float, nullable=True),
            sa.Column("revenue_thousand_usd", sa.Float, nullable=True),
            sa.Column("sales_mwh", sa.Float, nullable=True),
            sa.Column("customers", sa.Float, nullable=True),
            sa.Column("data_status", sa.Text, nullable=True),
            *_provenance(),
            sa.Column("released", sa.Text, nullable=True),
            sa.CheckConstraint(f"sector IN {SECTORS!r}", name="ck_retail_price_sector_vocab"),
        )
        op.create_index("ix_retail_price_period", "retail_price", ["period"])


def downgrade() -> None:
    for table in TABLES:
        if _has_table(table):
            op.drop_table(table)
