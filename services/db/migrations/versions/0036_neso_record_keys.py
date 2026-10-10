"""NESO TEC register links move to the record keys parser 2.0.0 emits (lane F2, 2026-10-10).

Parser 1.x keyed every single-row NESO project `<Project ID>/h<12 hex>`, the hash taken over MW
Effective From, Project Status, MW Connected and MW Increase: the fields whose changes are the
news. Parser 2.0.0 (`pipeline/connectors/gb_neso_tec_register/connector.py::record_keys`) keys a
single-row project on its Project ID alone. `make reparse` restates the stored frames under the
new keys with no events (the restatement suppresses the 1,959 `removed` + 1,959 `new` pairs), but
the loader closes a link only on a `removed` event and does not read `<pid>/h…` as a variant of
`<pid>`. Loading the restated frame into a store that already holds NESO would therefore create
1,959 new proposals beside the 1,959 old links, which stay active: every UK project twice.

What it does
------------
Renames `proposal_source.source_record_id` from `<pid>/h<12 hex>` to `<pid>` for source
`gb.neso.tec_register`, only where the result is unambiguous: the project has exactly one active
hash-keyed link and no other link (any state) keyed `<pid>`, `<pid>/<stage>` or `<pid>#<n>`.
Inactive hash-keyed links are history and keep their key. The proposal, its public id, its
events and its other links are untouched, so record URLs and alerts do not move. A fresh store
(no NESO links yet) is a no-op. Measured on the 2026-10-10 rehearsal store: 1,959 links, one per
project, no collisions.

Downgrade
---------
A no-op. The hash cannot be rebuilt from the stored row without the 1.x parser, and parser 1.x
re-keys the rows itself on its next restatement.

Revision ID: 0036
Revises: 0035
Create Date: 2026-10-10
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0036"
down_revision: str | None = "0035"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_ID = "gb.neso.tec_register"
HASH_KEY = re.compile(r"^([^/#]+)/h[0-9a-f]{12}$")
PROJECT_ID = re.compile(r"^([^/#]+)(?:[/#].*)?$")


def upgrade() -> None:
    bind = op.get_bind()
    # A partial schema (the migration tests build one from an early baseline) has no links to move.
    if "proposal_source" not in sa.inspect(bind).get_table_names():
        return
    rows = bind.execute(
        sa.text("SELECT id, source_record_id, active FROM proposal_source WHERE source_id = :source"),
        {"source": SOURCE_ID},
    ).all()
    hashed: dict[str, list[object]] = defaultdict(list)
    others: dict[str, int] = defaultdict(int)
    for link_id, key, active in rows:
        match = HASH_KEY.match(key)
        if match:
            if active:
                hashed[match.group(1)].append(link_id)
            continue
        project = PROJECT_ID.match(key)
        if project:
            others[project.group(1)] += 1
    for pid, link_ids in hashed.items():
        if len(link_ids) != 1 or others.get(pid):
            continue
        bind.execute(
            sa.text("UPDATE proposal_source SET source_record_id = :pid WHERE id = :id"),
            {"pid": pid, "id": link_ids[0]},
        )


def downgrade() -> None:
    """Nothing to undo; see the module docstring."""
