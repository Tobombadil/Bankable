"""The candidate-pair review queue for proposal-proposal duplicates.

`docs/21-data-model.md` §3.11 `match` is proposal <-> **opportunity** (supply matched to demand);
its `opportunity_id` is `NOT NULL`, so it cannot hold a proposal-proposal duplicate candidate
without misusing the column. `docs/21` §6.4 already names the right table for this — a supporting
table called `resolution_decision`, holding "[human] decisions made ... on a candidate pair", not
yet built by any sprint. This module builds a minimal version of it rather than repurpose `match`
or add to `services/db/models.py` (out of this task's assigned paths).

Rows here are the gate's "below threshold, or failed the id-reuse guard" output (task step 2):
proposed for human review, never auto-applied. `status` starts `proposed`; a human (or, later, a
model-adjudication stage per docs/22 §9) moves it to `confirmed` or `rejected`. Nothing here is
ever deleted — a wrong decision would be corrected by a new row/event, not a mutation of history,
mirroring the append-only convention docs/21 §6.1 requires of `event`.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from services.db.base import Base
from services.db.models import new_uuid, utcnow
from services.db.types import GUID, JSONVariant

DECISION_STATUSES = ("proposed", "confirmed", "rejected")


class ResolutionDecision(Base):
    __tablename__ = "resolution_decision"

    id: Mapped[_uuid.UUID] = mapped_column(GUID(), primary_key=True, default=new_uuid)
    left_proposal_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("proposal.id"), nullable=False)
    right_proposal_id: Mapped[_uuid.UUID] = mapped_column(GUID(), sa.ForeignKey("proposal.id"), nullable=False)
    cluster_key: Mapped[str] = mapped_column(sa.Text, nullable=False)
    score: Mapped[float] = mapped_column(sa.Numeric(5, 2), nullable=False)
    rationale: Mapped[str] = mapped_column(sa.Text, nullable=False)
    gate_reason: Mapped[str] = mapped_column(sa.Text, nullable=False)
    status: Mapped[str] = mapped_column(sa.Text, nullable=False, default="proposed")
    created_by: Mapped[str] = mapped_column(sa.Text, nullable=False, default="pipeline")
    decided_by_user_id: Mapped[_uuid.UUID | None] = mapped_column(GUID())
    decided_at: Mapped[dt.datetime | None] = mapped_column(sa.DateTime(timezone=True))
    extra: Mapped[dict[str, Any]] = mapped_column(JSONVariant(), nullable=False, default=dict)
    created_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        sa.DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    __table_args__ = (
        sa.CheckConstraint(f"status IN {DECISION_STATUSES!r}", name="status_vocab"),
        sa.UniqueConstraint("left_proposal_id", "right_proposal_id", name="one_decision_per_pair"),
    )
