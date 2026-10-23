"""`site_finder_run` and `site_candidate`: the powered-land site finder's precomputed segments (owner
decision 2026-10-22, docs/00-PLAN.md: sites within 1 mile of a 69 kV-or-higher line, within 5 miles
of a substation, outside the FEMA floodplain; all three adjustable).

`site_candidate` holds one row per segment of about half a mile of an OpenStreetMap line of 69 kV
and up (or of unstated voltage, flagged), with the nearest substation and its distance, the band's
floodplain share and flood-data coverage at four half-widths (0.25, 0.5, 1 and 2 miles), the county
and state, the retiring and retired plant MW nearby, the protected-land shares, and the ids of the
other context within five miles. `site_finder_run` records each computation; the API serves the
newest `ok` run only (`services/site_finder/compute.py`, `services/api/site_finder.py`).

The provenance quartet on `site_candidate` is the line's; every other value names its source in
`context` or in the run's `inputs`. Nothing aggregated over proposals is stored (docs/21 §8 item 4).

Both foreign keys to `asset` follow the asset's withdrawal (`services/ingest/osm_power.py` deletes
withdrawn rows): the line's segments go with it (CASCADE) and a withdrawn substation leaves the
segment without one (SET NULL) until the next run.

Guards follow 0037/0038: the SQLite round trips build today's `Base.metadata` (which already has
both tables) and stamp an earlier revision, so each create checks what is present. Downgrade drops
both tables; every row is re-derivable by running `python -m services.site_finder` again.

`coords` is the segment's own vertices as the run read them, so a band is drawn from the run that
measured it, never from a line geometry a later context load has changed.

**`down_revision`.** 0041 (prices) is the newest revision on this lane's base: 0040 (private
projects) revises 0038, 0041 revises 0040, and 0039 sits on top, so the chain has one head. The
coordinator relinks it if another revision lands first.

Revision ID: 0039
Revises: 0041
Create Date: 2026-10-23
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.types import GUID, JSONVariant

revision: str = "0039"
down_revision: str | None = "0041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Frozen here rather than imported from `services.db.models` (0020-0038's convention).
RUN_STATUSES = ("running", "ok", "failed")
DISTANCE_KEYS = ("025", "050", "100", "200")


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def upgrade() -> None:
    if not _has_table("site_finder_run"):
        op.create_table(
            "site_finder_run",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column("status", sa.Text, nullable=False, server_default="running"),
            sa.Column(
                "started_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.Column("finished_at", sa.TIMESTAMP(timezone=True), nullable=True),
            sa.Column("segments", sa.Integer, nullable=False, server_default="0"),
            sa.Column("inputs", JSONVariant(), nullable=False, server_default="{}"),
            sa.Column("layers", JSONVariant(), nullable=False, server_default="{}"),
            sa.Column("stats", JSONVariant(), nullable=False, server_default="{}"),
            sa.Column("error", sa.Text, nullable=True),
            sa.CheckConstraint(f"status IN {RUN_STATUSES!r}", name="ck_site_finder_run_status_vocab"),
        )
        op.create_index("ix_site_finder_run_status_finished", "site_finder_run", ["status", "finished_at"])
    if not _has_table("site_candidate"):
        flood = [
            col
            for key in DISTANCE_KEYS
            for col in (
                sa.Column(f"flood_share_{key}", sa.Float, nullable=True),
                sa.Column(f"flood_cover_{key}", sa.Float, nullable=False, server_default="0"),
            )
        ]
        op.create_table(
            "site_candidate",
            sa.Column("id", GUID(), primary_key=True),
            sa.Column(
                "run_id", GUID(), sa.ForeignKey("site_finder_run.id", ondelete="CASCADE"), nullable=False
            ),
            sa.Column("line_asset_id", GUID(), sa.ForeignKey("asset.id", ondelete="CASCADE"), nullable=False),
            sa.Column("segment_index", sa.Integer, nullable=False),
            sa.Column("start_mi", sa.Float, nullable=False),
            sa.Column("length_mi", sa.Float, nullable=False),
            sa.Column("min_lon", sa.Float, nullable=False),
            sa.Column("min_lat", sa.Float, nullable=False),
            sa.Column("max_lon", sa.Float, nullable=False),
            sa.Column("max_lat", sa.Float, nullable=False),
            sa.Column("mid_lon", sa.Float, nullable=False),
            sa.Column("mid_lat", sa.Float, nullable=False),
            sa.Column("voltage_kv", sa.Float, nullable=True),
            sa.Column("voltage_unknown", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("underground", sa.Boolean, nullable=False, server_default=sa.false()),
            sa.Column("state_code", sa.Text, nullable=True),
            sa.Column("county_fips", sa.String(5), nullable=True),
            sa.Column("county_name", sa.Text, nullable=True),
            sa.Column(
                "substation_asset_id", GUID(), sa.ForeignKey("asset.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("substation_mi", sa.Float, nullable=True),
            *flood,
            sa.Column("flood_reason", sa.Text, nullable=True),
            sa.Column("retiring_mw", sa.Float, nullable=True),
            sa.Column("retiring_count", sa.Integer, nullable=False, server_default="0"),
            sa.Column("protected_share", sa.Float, nullable=True),
            sa.Column("hard_protected_share", sa.Float, nullable=True),
            sa.Column("coords", JSONVariant(), nullable=False, server_default="[]"),
            sa.Column("context", JSONVariant(), nullable=False, server_default="{}"),
            sa.Column("prospect", JSONVariant(), nullable=False, server_default="{}"),
            sa.Column("source_id", sa.Text, sa.ForeignKey("source.id"), nullable=False),
            sa.Column("source_url", sa.Text, nullable=False),
            sa.Column("retrieved_at", sa.TIMESTAMP(timezone=True), nullable=False),
            sa.Column("licence_id", sa.Text, sa.ForeignKey("licence.id"), nullable=False),
            sa.Column(
                "computed_at", sa.TIMESTAMP(timezone=True), nullable=False, server_default=sa.func.now()
            ),
            sa.UniqueConstraint("run_id", "line_asset_id", "segment_index", name="uq_site_candidate_segment"),
        )
        op.create_index(
            "ix_site_candidate_filter", "site_candidate", ["run_id", "voltage_kv", "substation_mi"]
        )
        op.create_index("ix_site_candidate_place", "site_candidate", ["run_id", "state_code", "county_fips"])
        op.create_index("ix_site_candidate_mid", "site_candidate", ["run_id", "mid_lon", "mid_lat"])
        op.create_index("ix_site_candidate_line_asset_id", "site_candidate", ["line_asset_id"])
        op.create_index("ix_site_candidate_substation_asset_id", "site_candidate", ["substation_asset_id"])


def downgrade() -> None:
    if _has_table("site_candidate"):
        op.drop_table("site_candidate")
    if _has_table("site_finder_run"):
        op.drop_table("site_finder_run")
