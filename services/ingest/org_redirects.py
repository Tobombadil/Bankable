"""Follow `organization.merged_into_id` to the surviving organisation, for every loader that looks an
organisation up by name (docs/22 §20.7, "Loader re-runs write onto redirects").

A merge (`services/resolve/merge.py::merge_organization`, and the curated merges
`services/ingest/organizations.py::load_merges` applies from `data/vendored/organizations/merges.yaml`)
retires the absorbed row: it stays, as a redirect, so the merge can be undone, and everything that
hung off it moves to the survivor. The name indexes the loaders build still held the redirect, so a
re-run of the sponsor, ownership, midstream, GHGRP or ethanol loaders against a merged store attached
new proposals, edges and aliases to the absorbed row, where no page reads them (`services/api/
orgtree.py` treats a merged row as "a redirect, not a company"), or, where an index skipped merged
rows, created a second copy of the absorbed organisation. Measured on a merged copy of the dev store
before this module: all three curated absorbed spellings resolved to the redirect (docs/22 §20.7).

`OrgRedirects.terminal(org)` is the row a lookup must return instead: the end of the
`merged_into_id` chain. A chain is followed to its end (a survivor may itself be merged later); a
cycle, which no merge writes but a hand-edited store could hold, is not followed forever -- the row
the lookup found is returned unchanged and the cycle is logged, which is no worse than before.
"""

from __future__ import annotations

import logging
import uuid as _uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.db.models import Organization

log = logging.getLogger(__name__)


class OrgRedirects:
    """The store's redirects, read once per loader call (one query over merged rows only)."""

    def __init__(self, session: Session) -> None:
        self._session = session
        rows = session.execute(
            select(Organization.id, Organization.merged_into_id).where(
                Organization.merged_into_id.is_not(None)
            )
        ).all()
        self._next: dict[_uuid.UUID, _uuid.UUID] = {row[0]: row[1] for row in rows}

    def terminal_id(self, org_id: _uuid.UUID) -> _uuid.UUID:
        seen = {org_id}
        current = org_id
        while (target := self._next.get(current)) is not None:
            if target in seen:
                log.warning("organization merge cycle through %s; lookup left on %s", target, org_id)
                return org_id
            seen.add(target)
            current = target
        return current

    def terminal(self, org: Organization) -> Organization:
        target_id = self.terminal_id(org.id)
        if target_id == org.id:
            return org
        target = self._session.get(Organization, target_id)
        return target if target is not None else org

    def is_redirect(self, org: Organization) -> bool:
        return org.id in self._next or org.merged_into_id is not None


__all__ = ["OrgRedirects"]
