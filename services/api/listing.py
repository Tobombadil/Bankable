"""Whether any register still lists a proposal (2026-10-10, lane P): `listed` and `delisted_at`.

A row that leaves its register gets `proposal_source.gone_at` and nothing else: the record keeps
the lifecycle state the register last stated (`services/ingest/loader.py`; docs/51 §2.7 item 1, a
removal is not a withdrawal). So a project every register has dropped still reads `filed` or
`announced`, and counted as active pipeline. These two derived fields say so, computed at read
time from the links, never stored:

- `listed` is true while at least one of the record's links the caller may see is still in its
  register (`gone_at` is null);
- `delisted_at` is the latest `gone_at` among those links when none is, else null.

**Which links.** Active links whose source and licence the caller's tier may read: the links the
`provenance` array prints and `source_count` counts (`visibility.visible_source_links`; its SQL
twin `visible_source_link_filter`). A register the tier may not see neither keeps a record listed
nor makes it unlisted: that would disclose a link the Sources panel withholds (docs/21 §8 item 3).
The list filter (`listed_clause`) and the served fields (`listing_state`) read the same links, so
`?listed=false` returns exactly the records served with `listed: false`.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable

from sqlalchemy import ColumnElement, exists, select

from services.api.errors import validation_error
from services.api.visibility import visible_source_link_filter
from services.db.models import Proposal, ProposalSource


def listing_state(links: Iterable[ProposalSource]) -> tuple[bool, dt.datetime | None]:
    """`(listed, delisted_at)` over `links`, which the caller has already narrowed to the tier's
    visible active links. A record with no such link is not listed and has no date."""
    gone: list[dt.datetime] = []
    for link in links:
        if link.gone_at is None:
            return True, None
        gone.append(link.gone_at)
    return False, max(gone) if gone else None


def listed_clause(entitlement: str = "public") -> ColumnElement[bool]:
    """SQL twin of `listing_state(...)[0]`, correlated on `Proposal.id`: some link the tier may see
    is still in its register. Indexed on `proposal_source.proposal_id`, as the visibility
    predicate's own EXISTS is."""
    return exists(
        select(ProposalSource.id).where(
            ProposalSource.proposal_id == Proposal.id,
            ProposalSource.gone_at.is_(None),
            *visible_source_link_filter(ProposalSource, entitlement),
        )
    )


def listed_param(raw: str | None, instance: str) -> bool | None:
    """`?listed=true|false` as a boolean, `None` for no filter (absent or empty, as every sibling
    filter reads an empty value). Anything else is a `400 validation_error` naming `listed`, spelt
    as `slipped` is."""
    if not raw:
        return None
    if raw not in ("true", "false"):
        raise validation_error("listed", f"unknown listed value {raw!r}; expected true or false", instance)
    return raw == "true"
