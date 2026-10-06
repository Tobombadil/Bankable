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

**Visibility** (docs/50-audit-2026-09-18.md §3.1): the candidate events are selected through
`services.alerts.visibility.event_with_visible_subject_filter` — the event's own predicate *and*
the record-level predicate on the proposal/opportunity it describes, for the account's
entitlement. Before this, `_new_events_for_search` gated the event alone and then fetched the
subject with `db.get`, so a hidden or restricted record whose event happened to be publishable
reached the digest with its name, URL and transition. The webhook and private-feed paths apply the
same composed filter (`services/alerts/webhooks.py`, `services/alerts/feed.py`).

**Sending** (`services/alerts/mail.py`): every email goes through `deliver` with
`List-Unsubscribe`/`List-Unsubscribe-Post` headers, a legal-sender postal line and the
data notice in the body; the recipient is checked against the suppression store first;
and the rendered body is refused if it carries a bare `None` token (`services/social/textgate.py`).

**What a line says** (owner decision 2026-09-30; content audit F7): every item names its record,
says what changed (`status filed → permitted`, `capacity 100 MW → 150 MW`, `new record`) and gives
the record's size, technology and place, then links to the record's own page. An `entity = event`
search resolves the event's subject the way a proposal search does; before, it printed
`proposal: status_change — https://infraque.com` with neither the name nor a working link.

**Cadence** (`is_due`): `daily` and `weekly` are digests, evaluated once per day or week (less one
scheduler tick of slack, so a tick landing a few seconds early does not slip a whole period);
`immediate` is evaluated on every tick, as every mode was before. Between digests the watermark does
not move, so the next digest carries everything since the last one.
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

from sqlalchemy import select
from sqlalchemy.orm import Session

from services.alerts.mail import (
    FROM_ADDRESS,
    OutboundEmail,
    SenderIdentity,
    delayed_data_notice,
    deliver,
    product_name,
    sender_identity,
    strict_for,
    unsubscribe_headers,
)
from services.alerts.matching import event_matches_query, matches_query
from services.alerts.suppression import is_suppressed
from services.alerts.visibility import event_with_visible_subject_filter
from services.api.common import WEB_HOST
from services.api.visibility import gated_opportunity, gated_proposal
from services.db.models import Account, Alert, Event, Opportunity, Proposal, SavedSearch, User
from services.ids import public_id
from services.social.textgate import reject_bare_none

DIGEST_TEMPLATE_ID = "digest.email.v3"
#: Lines listed in one digest; the rest are counted and linked, so a broad search after a monthly
#: register release (EIA-860M: ~300 events in one load) is still a readable email.
DIGEST_MAX_ITEMS = 50
#: One scheduler tick (`infra/scheduler/app.py` runs `alert_tick` every 15 minutes).
_CADENCE_SLACK = dt.timedelta(minutes=15)
_CADENCE: dict[str, dt.timedelta] = {"daily": dt.timedelta(days=1), "weekly": dt.timedelta(days=7)}


@dataclass
class MatchedItem:
    kind: str  # "proposal" | "opportunity" | "event"
    name: str
    url: str
    source_name: str | None
    attribution_text: str | None
    event_seq: int
    #: What the triggering event changed, in words (`describe_change`).
    change: str | None = None
    #: Size, technology and place of the record (`describe_subject`).
    context: str | None = None


def is_due(search: SavedSearch, now: dt.datetime) -> bool:
    """Whether this cycle evaluates `search` (module docstring, "Cadence")."""
    period = _CADENCE.get(search.delivery_mode)
    if period is None or search.last_run_at is None:
        return True
    last = search.last_run_at if search.last_run_at.tzinfo else search.last_run_at.replace(tzinfo=dt.UTC)
    return now - last >= period - _CADENCE_SLACK


#: Words for the fields the change detector emits (`pipeline/diff.py`: `lifecycle_state`,
#: `capacity_mw`, `proposed_cod`); any other key prints with its underscores as spaces.
_FIELD_LABELS = {
    "lifecycle_state": "status",
    "status": "status",
    "capacity_mw": "capacity",
    "proposed_cod": "target online date",
    "proposed_online_date": "target online date",
    "due_at": "due date",
}
_DATE_FIELDS = frozenset({"proposed_cod", "proposed_online_date", "due_at"})


def _fmt_value(key: str, value: Any) -> str:
    if key.endswith("_mw") and isinstance(value, int | float) and not isinstance(value, bool):
        number = float(value)
        return f"{number:,.0f} MW" if number.is_integer() else f"{number:,.1f} MW"
    text = str(value)
    if key in _DATE_FIELDS and len(text) >= 10 and text[4] == "-":
        return text[:10]
    return text.replace("_", " ")


def describe_change(event: Event) -> str:
    """`status announced → filed`, `capacity 100 MW → 150 MW`, `new record`, `withdrawn`. Built only
    from the event row's own `before`/`after`/`changed_keys` (docs/21 §3.10)."""
    if event.event_type == "created":
        return "new record"
    before = event.before or {}
    after = event.after or {}
    keys = list(event.changed_keys or []) or sorted(set(before) | set(after))
    parts: list[str] = []
    for key in keys:
        label = _FIELD_LABELS.get(key, key.replace("_", " "))
        b, a = before.get(key), after.get(key)
        if b is not None and a is not None:
            parts.append(f"{label} {_fmt_value(key, b)} → {_fmt_value(key, a)}")
        elif a is not None:
            parts.append(f"{label} now {_fmt_value(key, a)}")
        elif b is not None:
            parts.append(f"{label} {_fmt_value(key, b)} no longer reported")
    if event.event_type == "withdrawn" and not parts:
        parts.append("withdrawn")
    return "; ".join(parts) or event.event_type.replace("_", " ")


def _technology_words(value: str | None) -> str | None:
    if not value:
        return None
    return "large load" if value == "load" else value.replace("_", " ")


def describe_subject(subject: Proposal | Opportunity) -> str:
    """`101 MW bess li ion, US-TX` for a proposal; `solar, wind, US-CA, due 2026-11-01` for an
    opportunity. Fields that are not recorded are left out, never printed as blanks."""
    parts: list[str] = []
    if isinstance(subject, Proposal):
        size = (
            _fmt_value("capacity_mw", float(subject.capacity_mw)) if subject.capacity_mw is not None else None
        )
        head = " ".join(p for p in (size, _technology_words(subject.technology)) if p)
        if head:
            parts.append(head)
        parts.append(subject.jurisdiction)
    else:
        techs = ", ".join(t for t in (_technology_words(t) for t in subject.technologies or []) if t)
        if techs:
            parts.append(techs)
        parts.append(subject.jurisdiction)
        if subject.due_at is not None:
            parts.append(f"due {subject.due_at.date().isoformat()}")
    return ", ".join(p for p in parts if p)


def _subject_and_attribution(
    db: Session, event: Event, entitlement: str = "public"
) -> tuple[Proposal | Opportunity | None, str, str]:
    """The event's subject and its served name at the owner's tier (`visibility.GatedRecord`)."""
    if event.subject_type == "proposal":
        p = db.get(Proposal, event.subject_id)
        if p is None:
            return None, "", WEB_HOST
        return p, gated_proposal(p, entitlement).name_canonical, f"{WEB_HOST}/proposals/{p.slug}"
    if event.subject_type == "opportunity":
        o = db.get(Opportunity, event.subject_id)
        if o is None:
            return None, "", WEB_HOST
        return o, gated_opportunity(o, entitlement).title, f"{WEB_HOST}/opportunities/{o.slug}"
    return None, event.event_type, WEB_HOST


def _attribution(event: Event) -> str | None:
    if event.source is None or event.licence is None:
        return None
    return event.source.attribution_text or event.source.licence.attribution_text


def _new_events_for_search(db: Session, search: SavedSearch, account: Account) -> list[Event]:
    subject_type = {"proposal": "proposal", "opportunity": "opportunity"}.get(search.entity)
    stmt = select(Event).where(
        Event.seq > search.watermark_seq, *event_with_visible_subject_filter(account.entitlement)
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
            subject, name, url = _subject_and_attribution(db, event)
            matched.append(
                MatchedItem(
                    kind="event",
                    # The visibility filter above already requires a visible proposal or
                    # opportunity subject; the fallback only names an event on any other subject.
                    name=name if subject is not None else event.subject_type.replace("_", " "),
                    url=url,
                    source_name=event.source.name if event.source else None,
                    attribution_text=_attribution(event),
                    event_seq=event.seq,
                    change=describe_change(event),
                    context=describe_subject(subject) if subject is not None else None,
                )
            )
            continue
        subject, name, url = _subject_and_attribution(db, event, account.entitlement)
        if subject is None or subject.id in seen_subjects:
            continue
        if not matches_query(search.entity, subject, search.query, account.entitlement):
            continue
        matched.append(
            MatchedItem(
                kind=search.entity,
                name=name,
                url=url,
                source_name=event.source.name if event.source else None,
                attribution_text=_attribution(event),
                event_seq=event.seq,
                change=describe_change(event),
                context=describe_subject(subject),
            )
        )
        seen_subjects.add(subject.id)
    search.watermark_seq = highest_seq
    return matched


def render_digest_body(
    search: SavedSearch,
    items: list[MatchedItem],
    *,
    unsubscribe_token: str,
    identity: SenderIdentity | None = None,
    entitlement: str = "pro",
) -> str:
    """`unsubscribe_token` is `Alert.unsubscribe_token` for the digest this body belongs to (the
    caller creates that row first so the token exists here — module docstring, US-908/US-502 AC3).
    The footer is docs/32 §2.2's "Email footer (every send)" plus the delayed-data notice: the
    sender identity (`alerts@` plus the legal name and postal address from `identity`), why the
    reader is receiving it, the one-click unsubscribe link built from that alert's own token
    (never a shared or guessable one), and the manage link. `identity` defaults to the
    non-strict placeholder so a direct caller can preview a body; `run_alert_cycle` passes the
    strict one. The finished text is refused if any field rendered as a bare `None`."""
    identity = identity or sender_identity(strict=False)
    lines = [f'Saved search "{search.name}": {len(items)} new match(es).', ""]
    for item in items[:DIGEST_MAX_ITEMS]:
        credit = item.attribution_text or item.source_name or "the platform"
        head = f"- {item.name}: {item.change}" if item.change else f"- {item.name}"
        if item.context:
            head = f"{head} ({item.context})"
        lines.append(head)
        lines.append(f"  {item.url} (source: {credit})")
    if len(items) > DIGEST_MAX_ITEMS:
        lines.append(f"- and {len(items) - DIGEST_MAX_ITEMS} more: {results_url(search)}")
    lines.append("")
    lines.append(
        f"You are receiving this because you subscribed at {WEB_HOST}. Data derived from public "
        "sources cited above; see each item's source and licence."
    )
    lines.append(delayed_data_notice(entitlement))
    lines.append(f"Unsubscribe (one click): {WEB_HOST}/unsubscribe?token={unsubscribe_token}")
    lines.append(f"Manage your alerts: {WEB_HOST}/alerts")
    lines.append(f"Sent by {product_name()} <{FROM_ADDRESS}> on behalf of {identity.postal_line}")
    return reject_bare_none("\n".join(lines), template_id=DIGEST_TEMPLATE_ID)


def results_url(search: SavedSearch) -> str:
    """The public list page showing this search's records (the site's list pages take the API's
    filter names verbatim, `web/app.py::PROPOSAL_PASSTHROUGH_FILTERS`)."""
    path = {"proposal": "/proposals", "opportunity": "/opportunities"}.get(search.entity, "/proposals")
    query = urlencode({k: v for k, v in (search.query or {}).items() if v is not None and v != ""})
    return f"{WEB_HOST}{path}" + (f"?{query}" if query else "")


def run_alert_cycle(db: Session, *, email_port: Any, now: dt.datetime | None = None) -> list[Alert]:
    """One pass over every active saved search: evaluate, and for each channel other than `rss`
    (served live from the current query, never stored as an `alert` row — docs/23 §9.2) write one
    digest `Alert` row grouping every match found this pass, per US-502's "digest mode" and the
    task brief's "digest grouping". `channel = webhook` on a saved search is out of scope this
    sprint — API-tier webhook delivery is the separate `webhook_endpoint` mechanism
    (`services/alerts/webhooks.py`), not a `saved_search.channels` entry, per docs/23 §9.1's own
    "a webhook is a saved search with a URL as its channel" framing describing that other
    mechanism, not this field (services/README.md "Pro tier and alerts" decision)."""
    now = now or dt.datetime.now(dt.UTC)
    # Resolved once per cycle, before any search is evaluated: when the legal sender line is
    # missing where a real send could happen this raises (`services/alerts/mail.py`), and no
    # watermark moves — the next cycle picks the same events up once the environment is fixed.
    identity = sender_identity(strict=strict_for(email_port))
    created: list[Alert] = []
    searches = list(db.scalars(select(SavedSearch).where(SavedSearch.status == "active")).all())
    for search in searches:
        account = db.get(Account, search.account_id)
        user = db.get(User, search.user_id)
        if account is None or user is None:
            continue
        if not is_due(search, now):
            continue
        window_start = search.last_run_at or (now - dt.timedelta(days=1))
        items = evaluate_saved_search(db, search, account)
        search.last_run_at = now
        search.last_match_count = len(items)
        if not items:
            db.flush()
            continue
        for channel in search.channels:
            if channel == "rss":
                continue
            if channel == "webhook":
                continue  # separate mechanism, see docstring
            # The alert row (and its `unsubscribe_token`) is created before the digest body is
            # rendered -- the reverse of the previous order -- so that an email body can carry
            # *this alert's own* unsubscribe link (US-908/US-502 AC3) rather than a link with no
            # token to point to.
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
            if channel == "email" and user.email and is_suppressed(db, user.email):
                # The suppression store wins over the saved search's own channel list: an erased
                # or unsubscribed address is never written to again, whatever the row says.
                alert.status = "suppressed"
                alert.error = "recipient is on the suppression list"
            elif channel == "email" and user.email:
                body = render_digest_body(
                    search,
                    items,
                    unsubscribe_token=alert.unsubscribe_token,
                    identity=identity,
                    entitlement=account.entitlement,
                )
                message = OutboundEmail(
                    to=user.email,
                    subject=alert.subject or "",
                    body=body,
                    headers=unsubscribe_headers(alert.unsubscribe_token),
                )
                sent = deliver(email_port, message)
                alert.provider_message_id = sent.provider_message_id
                alert.status = "sent"
                alert.sent_at = now
            else:
                alert.status = "suppressed"
                alert.error = "no deliverable address for this channel"
            db.flush()
            created.append(alert)
    return created


__all__ = [
    "DIGEST_MAX_ITEMS",
    "DIGEST_TEMPLATE_ID",
    "MatchedItem",
    "describe_change",
    "describe_subject",
    "evaluate_saved_search",
    "is_due",
    "render_digest_body",
    "results_url",
    "run_alert_cycle",
]
