"""The alert evaluation job (docs/23-api-spec-outline.md §3.2; docs/21-data-model.md §3.15-§3.16;
task brief: "given new change events since the last run, matches them against saved searches and
writes alert rows").

**Decision** (services/README.md): matching runs over the **event stream**, not a full table
scan of `proposal`/`opportunity` — `saved_search.watermark_seq` is `event.seq`-typed (docs/21
§3.15) precisely because the event log is the change feed the product sells (docs/21 §2's
"anything derived... is rebuildable from event"), and it makes the job exactly-once and cheap
regardless of table size. For `entity = proposal | opportunity` the query filters are evaluated
against the event's **subject** (the proposal/opportunity row itself, since `saved_search.query`
holds resource filters like `kind`/`jurisdiction`, not event filters); for `entity = event` they
are evaluated against the event row directly. `entity = match` is out of scope — no `match` writer
exists yet (services/README.md "Open decisions" 1, unchanged this sprint).

Every alert body carries attribution and the source link (task brief; docs/04 DA-2/D-34): each
matched item's line names its source and a deep link into the platform.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.alerts.matching import event_matches_query, matches_query
from services.api.auth import EmailPort
from services.api.common import WEB_HOST
from services.api.visibility import event_visibility_filter
from services.db.models import Account, Alert, Event, Opportunity, Proposal, SavedSearch, User
from services.ids import public_id


@dataclass
class MatchedItem:
    kind: str  # "proposal" | "opportunity" | "event"
    name: str
    url: str
    source_name: str | None
    attribution_text: str | None
    event_seq: int


def _subject_and_attribution(db: Session, event: Event) -> tuple[Proposal | Opportunity | None, str, str]:
    if event.subject_type == "proposal":
        p = db.get(Proposal, event.subject_id)
        if p is None:
            return None, "", WEB_HOST
        return p, p.name_canonical, f"{WEB_HOST}/proposals/{p.slug}"
    if event.subject_type == "opportunity":
        o = db.get(Opportunity, event.subject_id)
        if o is None:
            return None, "", WEB_HOST
        return o, o.title, f"{WEB_HOST}/opportunities/{o.slug}"
    return None, event.event_type, WEB_HOST


def _new_events_for_search(db: Session, search: SavedSearch, account: Account) -> list[Event]:
    subject_type = {"proposal": "proposal", "opportunity": "opportunity"}.get(search.entity)
    stmt = select(Event).where(
        Event.seq > search.watermark_seq, *event_visibility_filter(account.entitlement)
    )
    if subject_type is not None:
        stmt = stmt.where(Event.subject_type == subject_type)
    return list(db.scalars(stmt.order_by(Event.seq.asc())).all())


def evaluate_saved_search(db: Session, search: SavedSearch, account: Account) -> list[MatchedItem]:
    """Matches new events since `search.watermark_seq` and advances the watermark to the highest
    `seq` considered — whether or not it matched, so a non-matching flood of events is never
    rescanned (mirrors `docs/21` §3.10's `idempotency_key` "makes re-runs free" spirit for this
    job)."""
    events = _new_events_for_search(db, search, account)
    matched: list[MatchedItem] = []
    highest_seq = search.watermark_seq
    seen_subjects: set[_uuid.UUID] = set()
    for event in events:
        highest_seq = max(highest_seq, event.seq)
        if search.entity == "event":
            if not event_matches_query(event, search.query):
                continue
            source_name = event.source.name if event.source else None
            attribution = (
                (event.source.attribution_text or event.source.licence.attribution_text)
                if event.source and event.licence
                else None
            )
            matched.append(
                MatchedItem(
                    kind="event",
                    name=f"{event.subject_type}: {event.event_type}",
                    url=WEB_HOST,
                    source_name=source_name,
                    attribution_text=attribution,
                    event_seq=event.seq,
                )
            )
            continue
        subject, name, url = _subject_and_attribution(db, event)
        if subject is None or subject.id in seen_subjects:
            continue
        if not matches_query(search.entity, subject, search.query):
            continue
        source_name = event.source.name if event.source else None
        attribution = (
            (event.source.attribution_text or event.source.licence.attribution_text)
            if event.source and event.licence
            else None
        )
        matched.append(
            MatchedItem(
                kind=search.entity,
                name=name,
                url=url,
                source_name=source_name,
                attribution_text=attribution,
                event_seq=event.seq,
            )
        )
        seen_subjects.add(subject.id)
    search.watermark_seq = highest_seq
    return matched


def render_digest_body(search: SavedSearch, items: list[MatchedItem]) -> str:
    lines = [f'Saved search "{search.name}": {len(items)} new match(es).', ""]
    for item in items:
        credit = item.attribution_text or item.source_name or "the platform"
        lines.append(f"- {item.name} — {item.url} (source: {credit})")
    lines.append("")
    lines.append(f"Manage this saved search: {WEB_HOST}/account/saved-searches")
    return "\n".join(lines)


def run_alert_cycle(db: Session, *, email_port: EmailPort, now: dt.datetime | None = None) -> list[Alert]:
    """One pass over every active saved search: evaluate, and for each channel other than `rss`
    (served live from the current query, never stored as an `alert` row — docs/23 §9.2) write one
    digest `Alert` row grouping every match found this pass, per US-502's "digest mode" and the
    task brief's "digest grouping". `channel = webhook` on a saved search is out of scope this
    sprint — API-tier webhook delivery is the separate `webhook_endpoint` mechanism
    (`services/alerts/webhooks.py`), not a `saved_search.channels` entry, per docs/23 §9.1's own
    "a webhook is a saved search with a URL as its channel" framing describing that other
    mechanism, not this field (services/README.md "Pro tier and alerts" decision)."""
    now = now or dt.datetime.now(dt.UTC)
    created: list[Alert] = []
    searches = list(db.scalars(select(SavedSearch).where(SavedSearch.status == "active")).all())
    for search in searches:
        account = db.get(Account, search.account_id)
        user = db.get(User, search.user_id)
        if account is None or user is None:
            continue
        window_start = search.last_run_at or (now - dt.timedelta(days=1))
        items = evaluate_saved_search(db, search, account)
        search.last_run_at = now
        search.last_match_count = len(items)
        if not items:
            db.flush()
            continue
        body = render_digest_body(search, items)
        for channel in search.channels:
            if channel == "rss":
                continue
            if channel == "webhook":
                continue  # separate mechanism, see docstring
            alert = Alert(
                public_id="",
                saved_search_id=search.id,
                user_id=user.id,
                channel=channel,
                mode=search.delivery_mode if search.delivery_mode != "none" else "immediate",
                window_start=window_start,
                window_end=now,
                event_seqs=[item.event_seq for item in items],
                recipient=user.email if channel == "email" else None,
                subject=f'{len(items)} new match(es): "{search.name}"',
                status="queued",
                unsubscribe_token=f"ut_{_uuid.uuid4().hex}",
            )
            db.add(alert)
            db.flush()
            alert.public_id = public_id("alr", alert.id)
            if channel == "email" and user.email:
                sent = email_port.send(to=user.email, subject=alert.subject or "", body=body)
                alert.provider_message_id = sent.provider_message_id
                alert.status = "sent"
                alert.sent_at = now
            else:
                alert.status = "suppressed"
                alert.error = "no deliverable address for this channel"
            db.flush()
            created.append(alert)
    return created


__all__ = ["MatchedItem", "evaluate_saved_search", "render_digest_body", "run_alert_cycle"]
