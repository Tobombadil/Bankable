"""`source.vintage` / `source.vintage_basis` — the release a source states, next to the date we fetched it.

Why a column at all
-------------------
The product printed one date beside its data — the newest `retrieved_at`, rendered as "sources
last fetched ..." — and readers took it for the data's age. On the load this migration was written
against, the EIA-860M workbook was `july_generator2026.xlsx`, EIA's July 2026 report, carrying a
`retrieved_at` of 2026-09-13. The two months between them had nowhere to be stated. A release is a
fact the source asserts; a fetch date is a fact about us; neither substitutes for the other, and
the substitution was silent.

Why on `source` and not on `proposal_source` / `opportunity_source`
-------------------------------------------------------------------
The link tables were the obvious alternative: a vintage per stored row, beside the `retrieved_at`
already there. Measured on the 2026-09-13 dev load before choosing:

* Every link row for a given source shares one `retrieved_at` — `MIN == MAX` for all five
  proposal sources and all three opportunity sources. The loader is a full refresh, so a per-link
  column would hold 10,409 + 707 copies of eight distinct values.
* `source` holds 21 rows.
* The divergence a per-link column would capture — a row last seen in an older release than the
  one now current — is already expressible with columns that exist: `retrieved_at` says when that
  row was last seen and `gone_at` says it stopped appearing. The vintage of the release it was
  last seen in is recoverable from the run, not lost.
* Nothing in the loader can currently *produce* a per-row vintage that differs from the
  source-level one, so the column would be a hundred thousand copies of a constant, and a
  backfill of historical rows would have no evidence to draw on beyond the run that wrote them.

If an incremental loader ever lands, a per-link column becomes meaningful and can be added then
with the source-level value as its default; adding it now would be storing a constant and
pretending it was a measurement.

Both columns are nullable, and the NULLs mean different things
--------------------------------------------------------------
* `vintage_basis IS NULL` — no load has examined this source. Unknown, not "none".
* `vintage_basis = 'not_stated'` with `vintage IS NULL` — examined; the source publishes no
  release label. This is an answer and renders as one.
* `vintage` set — the release, normalised to `YYYY-MM` or `YYYY` so `MIN()` over the table answers
  "the oldest release we are serving" without parsing dates.

A CHECK constraint pins the basis vocabulary (`services/db/models.py::SOURCE_VINTAGE_BASES`), so a
future loader cannot invent a fourth basis — in particular one meaning "derived from the fetch
date", which is the thing this whole change exists to make unrepresentable.

Backfill
--------
`upgrade()` fills the two columns from evidence already in the database rather than leaving the
existing load blank until a re-ingest (which needs live network and is not available in CI or in
a restore drill). The evidence is the artefact URL the loader already stored on every link row,
and `asset.attributes.source_vintage` for the Energy Atlas layers, read through the same
extractors the loader now calls — so the backfill cannot disagree with a subsequent load. Every
source that has any loaded row and states no release is set to `not_stated`, because the
distinction between "examined, states none" and "never examined" is the point. A source with no
rows at all is left NULL.

SQLite cannot ALTER a CHECK onto an existing table, so the constraint goes on inside
`batch_alter_table`, which recreates the table there and is a plain ALTER on PostgreSQL. 0015 and
0017 could skip the batch because their columns carried no constraint; this one cannot. The
recreate is safe because SQLAlchemy 2.0 reflects SQLite CHECK constraints (verified against 2.0.52
while writing this: `source` reflects both `ck_source_publish_state_vocab` and, once created,
`ck_source_vintage_basis_vocab`), so `publish_state_vocab` survives it.

Both directions are guarded on what is actually there rather than on what the revision number
implies, because a database built by `Base.metadata.create_all` and then stamped — how the
migration tests exercise this chain on SQLite — already has the columns *and* the constraint from
the ORM model, and neither adding them twice nor dropping a constraint that was never added may
raise.

Revision ID: 0018
Revises: 0017
Create Date: 2026-09-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from services.db.models import SOURCE_VINTAGE_BASES
from services.ingest.vintage import from_artefact_url, from_atlas_token

revision: str = "0018"
down_revision: str | None = "0017"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

COLUMNS = ("vintage", "vintage_basis")
CONSTRAINT = "vintage_basis_vocab"


def _existing_columns() -> set[str]:
    inspector = sa.inspect(op.get_bind())
    return {c["name"] for c in inspector.get_columns("source")}


def _has_table(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _backfill() -> None:
    """Resolve every source that has loaded rows, using the same extractors the loader uses."""
    bind = op.get_bind()
    resolved: dict[str, tuple[str | None, str]] = {}

    # Artefact URLs: one per connector run, written onto every link row it produced.
    for table in ("proposal_source", "opportunity_source"):
        if not _has_table(table):
            continue
        rows = bind.execute(
            sa.text(f"SELECT DISTINCT source_id, source_url FROM {table}")  # noqa: S608 -- fixed names
        ).fetchall()
        for source_id, source_url in rows:
            if source_id is None:
                continue
            vintage = from_artefact_url(source_url)
            current = resolved.get(source_id)
            if vintage.stated or current is None:
                resolved[source_id] = (vintage.value, vintage.basis)

    # Energy Atlas layers: the shapefile member token, already stored per asset row.
    if _has_table("asset"):
        rows = bind.execute(
            sa.text("SELECT DISTINCT source_id, attributes FROM asset WHERE attributes IS NOT NULL")
        ).fetchall()
        for source_id, attributes in rows:
            if source_id is None:
                continue
            token = None
            if isinstance(attributes, dict):
                token = attributes.get("source_vintage")
            elif isinstance(attributes, str):
                import json

                try:
                    parsed = json.loads(attributes)
                except ValueError:
                    parsed = None
                if isinstance(parsed, dict):
                    token = parsed.get("source_vintage")
            vintage = from_atlas_token(token)
            current = resolved.get(source_id)
            if vintage.stated or current is None:
                resolved[source_id] = (vintage.value, vintage.basis)

    for source_id, (value, basis) in resolved.items():
        bind.execute(
            sa.text("UPDATE source SET vintage = :v, vintage_basis = :b WHERE id = :id"),
            {"v": value, "b": basis, "id": source_id},
        )


def _check_constraint_name() -> str | None:
    """The reflected name of this revision's CHECK, or `None`. Matched by suffix because the
    metadata's naming convention prefixes it (`ck_source_...`) and a database built by the ORM
    and one built by this migration need not spell it identically."""
    inspector = sa.inspect(op.get_bind())
    try:
        names = [c["name"] for c in inspector.get_check_constraints("source") if c.get("name")]
    except (NotImplementedError, sa.exc.SQLAlchemyError):
        return None
    for name in names:
        if name == CONSTRAINT or name.endswith(f"_{CONSTRAINT}"):
            return str(name)
    return None


def upgrade() -> None:
    existing = _existing_columns()
    for column in COLUMNS:
        if column not in existing:
            op.add_column("source", sa.Column(column, sa.Text(), nullable=True))
    if _check_constraint_name() is None:
        with op.batch_alter_table("source") as batch:
            batch.create_check_constraint(
                CONSTRAINT,
                f"vintage_basis IS NULL OR vintage_basis IN {SOURCE_VINTAGE_BASES!r}",
            )
    _backfill()


def downgrade() -> None:
    existing = _existing_columns()
    constraint = _check_constraint_name()
    if not (set(COLUMNS) & existing) and constraint is None:
        return
    # One batch, so SQLite's table recreate happens once: dropping the constraint first is what
    # lets the columns go (SQLite refuses `DROP COLUMN` on a column named in a CHECK).
    with op.batch_alter_table("source") as batch:
        if constraint is not None:
            batch.drop_constraint(constraint, type_="check")
        for column in COLUMNS:
            if column in existing:
                batch.drop_column(column)
