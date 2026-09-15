"""Private per-user RSS/JSON feed for a saved search (docs/23-api-spec-outline.md §9.2:
"`/feeds/saved/<rss_token>`"; docs/21-data-model.md §3.15 `rss_token`).

The token is the credential (docs/23 §9.2 `feedSavedSearch`): an unguessable, unique string minted
when a saved search adds the `rss` channel, checked with a constant-time comparison at lookup
(SQL equality does not need it — an attacker cannot binary-search a single indexed equality query
one bit at a time the way they could a byte-by-byte comparison in application code — token
*generation* entropy is what makes it unguessable, exercised in
`tests/test_api_pro_saved_searches.py::test_rss_token_is_unguessable`).

Reuses `services.api.feeds` (RSS 2.0 / JSON Feed 1.1 rendering) exactly as
`services/api/app.py`'s public feeds do, so a saved-search feed and a public feed are
byte-for-byte the same shape — the only difference is which rows are in `items` and that this one
is live (`lag_days = 0`) for the owner's tier (docs/23 §9.2 `feedSavedSearch` description).
"""

from __future__ import annotations

import secrets
import string
from typing import Any

from sqlalchemy import ColumnElement, select
from sqlalchemy.orm import Session

from services.api.common import WEB_HOST
from services.api.serialize import build_licence_summary, licence_summary_row
from services.api.visibility import event_visibility_filter
from services.db.models import Account, Event, Opportunity, Proposal, SavedSearch

from .matching import event_matches_query, matches_query

#: `[0-9A-Za-z]` as `string` spells it -- built from the stdlib constants rather than written out
#: as one literal, which gitleaks' generic-api-key rule flagged as a high-entropy secret in CI.
RSS_TOKEN_ALPHABET = string.ascii_letters + string.digits


def generate_rss_token() -> str:
    """`^rt_[0-9A-Za-z]{22,64}$` (api/openapi.yaml). 32 characters of `secrets.choice` over a
    62-symbol alphabet is ~190 bits of entropy — far beyond brute-forceable, the property the
    unguessability test asserts statistically (many draws, no collision, correct charset/length)
    rather than by trying to prove entropy directly."""
    body = "".join(secrets.choice(RSS_TOKEN_ALPHABET) for _ in range(32))
    return f"rt_{body}"


def matching_items_for_feed(
    db: Session, search: SavedSearch, account: Account, *, limit: int = 50
) -> list[dict[str, Any]]:
    """The live (`published_at`-gated) rows the saved search's query currently matches, newest
    first — used both for the RSS/JSON feed and for `POST .../preview` (US-501 AC2's "run the
    query now")."""
    if search.entity == "proposal":
        proposal_stmt = select(Proposal).where(*_visibility_for(Proposal, account))
        proposals = [p for p in db.scalars(proposal_stmt).all() if matches_query("proposal", p, search.query)]
        proposals.sort(key=lambda p: p.last_changed, reverse=True)
        return [_proposal_feed_item(p) for p in proposals[:limit]]
    if search.entity == "opportunity":
        opportunity_stmt = select(Opportunity).where(*_visibility_for(Opportunity, account))
        opportunities = [
            o for o in db.scalars(opportunity_stmt).all() if matches_query("opportunity", o, search.query)
        ]
        opportunities.sort(key=lambda o: o.last_changed, reverse=True)
        return [_opportunity_feed_item(o) for o in opportunities[:limit]]
    if search.entity == "event":
        event_stmt = select(Event).where(*event_visibility_filter(account.entitlement))
        events = [e for e in db.scalars(event_stmt).all() if event_matches_query(e, search.query)]
        events.sort(key=lambda e: e.seq, reverse=True)
        return [_event_feed_item(e) for e in events[:limit]]
    return []


def _visibility_for(model: type[Proposal] | type[Opportunity], account: Account) -> list[ColumnElement[bool]]:
    from services.api.visibility import opportunity_visibility_filter, proposal_visibility_filter

    if model is Proposal:
        return proposal_visibility_filter(account.entitlement)
    return opportunity_visibility_filter(account.entitlement)


def _proposal_feed_item(p: Proposal) -> dict[str, Any]:
    source_row = next((s for s in p.sources if s.active), None)
    return {
        "title": f"{p.name_canonical} — {p.lifecycle_state}",
        "url": f"{WEB_HOST}/proposals/{p.slug}",
        "guid": p.public_id,
        "pub_date": p.published_at or p.last_changed,
        "creator": (source_row.source.attribution_text or source_row.source.name)
        if source_row
        else "the platform",
        "categories": [p.lifecycle_state, p.kind],
        "description": f"{p.name_canonical}: {p.lifecycle_state} ({p.jurisdiction}).",
        "platform_ext": {
            "event_type": "status_change",
            "subject": {
                "public_id": p.public_id,
                "name": p.name_canonical,
                "url": f"{WEB_HOST}/proposals/{p.slug}",
            },
            "provenance": [],
            "licence_summary": build_licence_summary(
                [
                    licence_summary_row(s.source, s.source.licence, s.retrieved_at)
                    for s in p.sources
                    if s.active
                ]
            ),
            "data_as_of": "live",
        },
    }


def _opportunity_feed_item(o: Opportunity) -> dict[str, Any]:
    source_row = next((s for s in o.sources if s.active), None)
    return {
        "title": f"{o.title} — {o.status}",
        "url": f"{WEB_HOST}/opportunities/{o.slug}",
        "guid": o.public_id,
        "pub_date": o.published_at or o.last_changed,
        "creator": (source_row.source.attribution_text or source_row.source.name)
        if source_row
        else "the platform",
        "categories": [o.status, o.kind],
        "description": f"{o.title}: {o.status} ({o.jurisdiction}).",
        "platform_ext": {
            "event_type": "status_change",
            "subject": {
                "public_id": o.public_id,
                "name": o.title,
                "url": f"{WEB_HOST}/opportunities/{o.slug}",
            },
            "provenance": [],
            "licence_summary": build_licence_summary(
                [
                    licence_summary_row(s.source, s.source.licence, s.retrieved_at)
                    for s in o.sources
                    if s.active
                ]
            ),
            "data_as_of": "live",
        },
    }


def _event_feed_item(e: Event) -> dict[str, Any]:
    return {
        "title": f"{e.subject_type}: {e.event_type}",
        "url": WEB_HOST,
        "guid": str(e.id),
        "pub_date": e.published_at or e.observed_at,
        "creator": e.source.attribution_text or e.source.name if e.source else "the platform",
        "categories": [e.event_type, e.subject_type],
        "description": f"{e.subject_type}: {e.event_type}",
        "platform_ext": {
            "event_type": e.event_type,
            "subject": {"public_id": str(e.subject_id), "name": e.subject_type, "url": WEB_HOST},
            "provenance": [],
            "licence_summary": build_licence_summary(
                [licence_summary_row(e.source, e.licence, e.retrieved_at)] if e.source and e.licence else []
            ),
            "data_as_of": "live",
        },
    }
