"""Tests for `services.social.worker` (the `draft_posts_tick` scheduler contract) and
`services.social.db_events` (the DB `event` -> `editorial.SocialEvent` bridge). Sprint 3 item 4.

Entity factories (`make_open_licence`, `make_public_source`, `make_visible_proposal`, ...) are
imported directly from `services.api.conftest` -- plain functions, not fixtures, the same reuse
pattern `tests/conftest.py` already documents. `db`/`db_sessionmaker` come from `tests/conftest.py`
(a seeded in-memory SQLite on a single shared connection -- StaticPool -- so writes made through
`db` are visible to the fresh session `draft_posts_tick` opens from `db_sessionmaker` without an
explicit commit, though tests commit anyway for a clean transaction boundary).
"""

from __future__ import annotations

import datetime as dt
import uuid as _uuid

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from services.api.common import WEB_HOST
from services.api.conftest import (
    make_attribution_licence,
    make_event,
    make_location,
    make_open_licence,
    make_org,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import ChannelConfig, Event, Licence, Post, WorkerWatermark
from services.social import worker as worker_module
from services.social.db_events import SubjectNotFoundError, social_event_from_db
from services.social.worker import draft_posts_tick

UTC = dt.UTC


def _make_opportunity_event(
    session: Session,
    opportunity,
    source,
    *,
    event_type: str = "opened",
    published_at: dt.datetime | None = None,
) -> Event:
    """`services.api.conftest.make_event` is proposal-only (its `proposal` parameter is typed
    `Proposal`); this is the same shape for an opportunity subject."""
    now = dt.datetime.now(UTC)
    resolved_published_at = (now - dt.timedelta(hours=1)) if published_at is None else published_at
    ev = Event(
        subject_type="opportunity",
        subject_id=opportunity.id,
        event_type=event_type,
        observed_at=now - dt.timedelta(hours=2),
        source_id=source.id,
        source_url=source.url,
        retrieved_at=now - dt.timedelta(hours=2),
        licence_id=source.licence_id,
        changed_keys=["status"],
        public_at=now - dt.timedelta(hours=1),
        published_at=resolved_published_at,
        idempotency_key=f"{source.id}:{opportunity.public_id}:{event_type}:{now.isoformat()}",
    )
    session.add(ev)
    session.flush()
    return ev


def _restricted_derived_licence(session: Session) -> Licence:
    """`reuse_class="attribution"` (so `editorial`'s own `PUBLISHABLE_REUSE_CLASSES` check and the
    visibility filter's `min_reuse_class` check both pass) but `allows_derived_publication=False`
    -- isolates the worker's own licence gate from the reuse-class checks other layers already
    make."""
    lic = Licence(
        id="attr-no-derive",
        name="Attribution, no derived publication",
        reuse_class="attribution",
        attribution_required=True,
        allows_derived_publication=False,
        allows_raw_publication=False,
        gate_flag=False,
        evidence_url="https://example.org/terms-no-derive",
        evidence_retrieved_at=dt.datetime(2026, 9, 1, tzinfo=UTC),
        classified_by="legal-compliance",
    )
    session.add(lic)
    session.flush()
    return lic


# =============================================================================== worker: gates & drafting


def test_tick_drafts_one_post_per_channel_for_a_new_proposal(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0  # clears the 50 MW bar, not the 200 MW LinkedIn bar
    db.flush()
    event = make_event(db, proposal, source, event_type="created")
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.events_scanned == 1
    assert report.events_eligible == 1
    assert report.posts_created == 2  # bluesky + x, per editorial.channels_for_event
    assert report.posts_skipped_gate == 0
    assert report.posts_skipped_duplicate == 0
    assert report.errors == ()
    assert report.watermark_seq == event.seq
    assert report.started_at <= report.finished_at

    posts = db.scalars(select(Post).where(Post.event_id == event.id)).all()
    assert {p.channel for p in posts} == {"bluesky", "x"}
    for post in posts:
        assert post.state == "draft"
        assert post.auto_published is False
        assert post.approved_by_user_id is None
        assert post.public_id.startswith("post_")
        assert post.template_id.startswith("proposal.new.")
        assert post.link_url.startswith(f"{WEB_HOST}/proposals/{proposal.slug}")
        assert f"utm_source={post.channel}" in post.link_url
        assert "utm_medium=social" in post.link_url
        assert "utm_campaign=proposal.new" in post.link_url
        assert post.credit_line == f"Source: {source.name}"
        assert source.name in post.body
        assert f"{WEB_HOST}/proposals/{proposal.slug}" in post.body  # the bare page_url, no UTM
        assert post.metrics == {}
        assert float(post.cost_usd) == 0.0


def test_second_tick_scans_nothing_new(db: Session, db_sessionmaker: sessionmaker[Session]) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    db.flush()
    make_event(db, proposal, source, event_type="created")
    db.commit()

    first = draft_posts_tick(db_sessionmaker)
    assert first.posts_created == 2

    second = draft_posts_tick(db_sessionmaker)

    assert second.events_scanned == 0
    assert second.posts_created == 0
    assert second.watermark_seq == first.watermark_seq


def test_restricted_derived_only_licence_is_a_gate_skip(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = _restricted_derived_licence(db)
    source = make_public_source(db, licence, id_="us.test.restricted_derived")
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    db.flush()
    make_event(db, proposal, source, event_type="created")
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.posts_created == 0
    assert report.posts_skipped_gate == 1
    assert report.events_eligible == 0
    assert db.scalar(select(Post)) is None


def test_unpublished_record_is_a_gate_skip(db: Session, db_sessionmaker: sessionmaker[Session]) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    proposal.publish_state = "unpublished"
    db.flush()
    make_event(db, proposal, source, event_type="created")
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.posts_created == 0
    assert report.posts_skipped_gate == 1


def test_event_with_no_published_at_is_a_gate_skip(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    db.flush()
    event = make_event(db, proposal, source, event_type="created")
    event.published_at = None
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.posts_created == 0
    assert report.posts_skipped_gate == 1


def test_auto_publish_channel_creates_an_approved_post(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    db.flush()
    make_event(db, proposal, source, event_type="created")
    db.add(ChannelConfig(channel="bluesky", auto_publish=True, disclosure_label="Automated — Bankable"))
    db.commit()

    report = draft_posts_tick(db_sessionmaker)
    assert report.posts_created == 2

    bluesky_post = db.scalar(select(Post).where(Post.channel == "bluesky"))
    x_post = db.scalar(select(Post).where(Post.channel == "x"))
    assert bluesky_post is not None
    assert bluesky_post.state == "approved"
    assert bluesky_post.auto_published is True
    assert bluesky_post.approved_by_user_id is None
    assert bluesky_post.disclosure_label == "Automated — Bankable"
    assert x_post is not None
    assert x_post.state == "draft"
    assert x_post.auto_published is False


def test_duplicate_content_within_seven_days_is_skipped(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    db.flush()
    make_event(db, proposal, source, event_type="created")
    db.commit()
    first = draft_posts_tick(db_sessionmaker)
    assert first.posts_created == 2

    # A second, distinct `created` event on the *same* proposal -- nothing about the proposal
    # changed, so it renders byte-for-byte the same body/link as the first. The content-hash
    # guard, not the (event_id, channel) exact-duplicate check or the watermark, is what must
    # catch this one (docs/32 §4.8).
    make_event(db, proposal, source, event_type="created")
    db.commit()

    second = draft_posts_tick(db_sessionmaker)

    assert second.events_scanned == 1
    assert second.events_eligible == 1
    assert second.posts_created == 0
    assert second.posts_skipped_duplicate == 2
    assert db.scalar(select(Post).where(Post.event_id.isnot(None))) is not None  # the first tick's posts


def test_an_unmapped_event_type_is_scanned_but_not_eligible(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    db.flush()
    event = make_event(db, proposal, source, event_type="field_changed")
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.events_scanned == 1
    assert report.events_eligible == 0
    assert report.posts_created == 0
    assert report.posts_skipped_gate == 0
    # `WorkerWatermark` advances past every *examined* event, not just posted ones (the
    # coordinator's livelock fix) -- an event that never earns a post is still marked seen and is
    # not rescanned on a later tick.
    assert report.watermark_seq == event.seq

    second = draft_posts_tick(db_sessionmaker)
    assert second.events_scanned == 0


def test_a_large_gated_backlog_does_not_starve_a_later_eligible_event(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    """Coordinator fix: a backlog of more than `limit` events that earn no post used to livelock
    the tick (the old post-derived watermark never advanced past any of them, so every future tick
    rescanned the same unpostable prefix and a real event behind it was never reached). 600 gated
    events followed by one eligible event, `limit=500`: the first tick's watermark still advances
    past all 500 it examines (none of them earn a post), so the second tick reaches the rest of the
    backlog and the eligible event behind it."""
    gated_licence = _restricted_derived_licence(db)
    gated_source = make_public_source(db, gated_licence, id_="us.test.backlog_gated")
    gated_proposal = make_visible_proposal(db, gated_source, public_id_suffix="1")
    gated_proposal.capacity_mw = 150.0
    db.flush()
    for i in range(600):
        ev = Event(
            subject_type="proposal",
            subject_id=gated_proposal.id,
            event_type="created",
            observed_at=dt.datetime.now(UTC),
            source_id=gated_source.id,
            source_url=gated_source.url,
            retrieved_at=dt.datetime.now(UTC),
            licence_id=gated_source.licence_id,
            published_at=dt.datetime.now(UTC),
            idempotency_key=f"backlog:{gated_source.id}:{gated_proposal.id}:{i}",
        )
        db.add(ev)
        db.flush()  # Event.seq is assigned by a before_insert listener that reads MAX(seq) from
        # rows already inserted *in this transaction* (services/db/models.py) -- batching these
        # adds into one flush would let several rows compute the same MAX() and collide.
    db.commit()

    open_licence = make_open_licence(db)
    open_source = make_public_source(db, open_licence, id_="us.test.backlog_eligible")
    eligible_proposal = make_visible_proposal(db, open_source, public_id_suffix="2")
    eligible_proposal.capacity_mw = 150.0
    db.flush()
    eligible_event = make_event(db, eligible_proposal, open_source, event_type="created")
    db.commit()

    first = draft_posts_tick(db_sessionmaker, limit=500)
    assert first.events_scanned == 500
    assert first.posts_created == 0
    assert first.posts_skipped_gate == 500

    second = draft_posts_tick(db_sessionmaker, limit=500)
    assert second.events_scanned == 101  # the remaining 100 gated events + the eligible one
    assert second.posts_created == 2  # bluesky + x, drafted -- not starved behind the backlog
    assert second.watermark_seq == eligible_event.seq


def test_watermark_advances_even_when_zero_posts_are_created(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = _restricted_derived_licence(db)
    source = make_public_source(db, licence, id_="us.test.watermark_zero_posts")
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    db.flush()
    event = make_event(db, proposal, source, event_type="created")
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.posts_created == 0
    assert report.watermark_seq == event.seq

    row = db.get(WorkerWatermark, "social.draft_posts")
    assert row is not None
    assert row.seq == event.seq


def test_bootstraps_watermark_from_post_derived_seq_when_no_row_exists(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source, public_id_suffix="1")
    proposal.capacity_mw = 150.0
    db.flush()
    legacy_event = make_event(db, proposal, source, event_type="created")
    # Simulate a pre-existing deployment: a `post` row already exists for an earlier event, but no
    # `worker_watermark` row has ever been written (that table is new -- migration 0006).
    db.add(
        Post(
            public_id="post_legacybootstrap0",
            channel="bluesky",
            event_id=legacy_event.id,
            subject_type="proposal",
            subject_id=proposal.id,
            template_id="proposal.new.bluesky",
            template_version="v1",
            body="legacy post, drafted before WorkerWatermark existed",
            link_url=f"{WEB_HOST}/proposals/{proposal.slug}",
            credit_line=f"Source: {source.name}",
            state="draft",
            gate_checked_at=dt.datetime.now(UTC),
        )
    )
    db.commit()
    assert db.get(WorkerWatermark, "social.draft_posts") is None

    new_proposal = make_visible_proposal(db, source, public_id_suffix="2")
    new_proposal.capacity_mw = 150.0
    db.flush()
    new_event = make_event(db, new_proposal, source, event_type="created")
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    # Only the new event is scanned: the bootstrap started at `legacy_event.seq` (the post-derived
    # max), not 0, so the already-posted legacy event is never rescanned or re-drafted.
    assert report.events_scanned == 1
    assert report.posts_created == 2

    row = db.get(WorkerWatermark, "social.draft_posts")
    assert row is not None
    assert row.seq == new_event.seq


def test_missing_subject_row_records_an_error_and_continues(
    db: Session, db_sessionmaker: sessionmaker[Session]
) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    bad_proposal = make_visible_proposal(db, source, public_id_suffix="1")
    bad_proposal.capacity_mw = 150.0
    good_proposal = make_visible_proposal(db, source, public_id_suffix="2")
    good_proposal.capacity_mw = 150.0
    db.flush()
    bad_event = make_event(db, bad_proposal, source, event_type="created")
    good_event = make_event(db, good_proposal, source, event_type="created")
    db.commit()

    # The event log is append-only (services/db/models.py `Event` docstring); the referenced
    # proposal disappearing afterwards is the scenario under test, not something the worker
    # itself could have caused.
    for link in list(bad_proposal.sources):
        db.delete(link)
    db.flush()
    db.delete(bad_proposal)
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.events_scanned == 2
    assert len(report.errors) == 1
    assert f"seq={bad_event.seq}" in report.errors[0]
    assert "SubjectNotFoundError" in report.errors[0]
    # the good event, scanned after the bad one in the same tick, is still drafted
    assert report.posts_created == 2
    assert report.watermark_seq == good_event.seq


def test_tick_drafts_an_open_rfp_opportunity(db: Session, db_sessionmaker: sessionmaker[Session]) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.opportunity_tick")
    org = make_org(db, name="State Utility Commission")
    opportunity = make_visible_opportunity(db, source)
    opportunity.issuer_org_id = org.id
    db.flush()
    event = _make_opportunity_event(db, opportunity, source, event_type="opened")
    db.commit()

    report = draft_posts_tick(db_sessionmaker)

    assert report.events_eligible == 1
    assert report.posts_skipped_gate == 0
    assert report.posts_created == 3  # opportunity.rfp_opened -> bluesky, linkedin, x

    posts = db.scalars(select(Post).where(Post.event_id == event.id)).all()
    assert {p.channel for p in posts} == {"bluesky", "linkedin", "x"}
    for post in posts:
        assert post.subject_type == "opportunity"
        assert post.subject_id == opportunity.id
        assert opportunity.title in post.body
        assert post.link_url.startswith(f"{WEB_HOST}/opportunities/{opportunity.slug}")


def test_main_writes_a_one_line_summary_to_stdout(
    db: Session,
    db_sessionmaker: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence)
    proposal = make_visible_proposal(db, source)
    proposal.capacity_mw = 150.0
    db.flush()
    make_event(db, proposal, source, event_type="created")
    db.commit()

    monkeypatch.setattr(worker_module, "get_engine", lambda: None)
    monkeypatch.setattr(worker_module, "get_sessionmaker", lambda _engine: db_sessionmaker)

    exit_code = worker_module.main()

    out = capsys.readouterr().out
    assert exit_code == 0
    assert out.startswith("social draft tick:")
    assert "scanned=1" in out
    assert "created=2" in out
    assert out.endswith("\n")


# ========================================================================= db_events: field mapping


def test_social_event_from_db_maps_proposal_fields(db: Session) -> None:
    licence = make_attribution_licence(db)
    source = make_public_source(db, licence, id_="us.test.mapping_proposal")
    org = make_org(db, name="Acme Power LLC")
    location = make_location(db, source, licence, county_name="Travis", state_code="US-TX")
    proposal = make_visible_proposal(db, source, sponsor=org, location=location)
    proposal.iso = "ERCOT"
    proposal.identifiers = {"queue_ids": [{"iso": "ERCOT", "id": "Q-123"}]}
    db.flush()
    event = make_event(db, proposal, source, event_type="created")
    db.commit()

    social_event = social_event_from_db(db, event)

    assert social_event is not None
    assert social_event.event_type == "proposal.new"
    assert social_event.subject_type == "proposal"
    assert social_event.subject_id == str(proposal.id)
    assert social_event.source_name == source.name
    assert social_event.source_url == event.source_url
    assert social_event.reuse_class == licence.reuse_class
    assert social_event.page_url == f"{WEB_HOST}/proposals/{proposal.slug}"
    assert social_event.lag_days == 14
    assert social_event.capacity_mw == float(proposal.capacity_mw)
    assert social_event.technology == proposal.technology
    assert social_event.county == "Travis"
    assert social_event.state == "TX"
    assert social_event.iso_rto == "ERCOT"
    assert social_event.queue_id == "Q-123"
    assert social_event.developer_org == "Acme Power LLC"


def test_social_event_from_db_state_is_none_without_location_or_jurisdiction(db: Session) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.no_state")
    proposal = make_visible_proposal(db, source)  # no `location=` -> location_id stays null
    proposal.jurisdiction = ""
    db.flush()
    event = make_event(db, proposal, source, event_type="created")
    db.commit()

    social_event = social_event_from_db(db, event)

    assert social_event is not None
    assert social_event.county is None
    assert social_event.state is None


def test_social_event_from_db_maps_status_change_before_after(db: Session) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.mapping_status")
    proposal = make_visible_proposal(db, source)
    db.flush()
    event = make_event(db, proposal, source, event_type="status_change")
    db.commit()

    social_event = social_event_from_db(db, event)

    assert social_event is not None
    assert social_event.event_type == "proposal.status_changed"
    assert social_event.status_from == "announced"
    assert social_event.status_to == "filed"


def test_social_event_from_db_maps_opportunity_fields(db: Session) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.mapping_opportunity")
    org = make_org(db, name="State Utility Commission")
    opportunity = make_visible_opportunity(db, source)
    opportunity.issuer_org_id = org.id
    db.flush()
    event = _make_opportunity_event(db, opportunity, source, event_type="opened")
    db.commit()

    social_event = social_event_from_db(db, event)

    assert social_event is not None
    assert social_event.event_type == "opportunity.rfp_opened"
    assert social_event.subject_type == "opportunity"
    assert social_event.subject_id == str(opportunity.id)
    assert social_event.solicitation_title == opportunity.title
    assert social_event.issuer_org == "State Utility Commission"
    assert social_event.deadline_date == opportunity.due_at.date()
    assert social_event.page_url == f"{WEB_HOST}/opportunities/{opportunity.slug}"
    assert social_event.lag_days == 7
    assert social_event.capacity_mw is None  # make_visible_opportunity leaves capacity_sought_mw unset
    assert social_event.technology == opportunity.technologies[0]


@pytest.mark.parametrize("event_type", ["closed", "due_date_changed"])
def test_social_event_from_db_maps_opportunity_closing_variants(db: Session, event_type: str) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_=f"us.test.mapping_closing_{event_type}")
    org = make_org(db)
    opportunity = make_visible_opportunity(db, source)
    opportunity.issuer_org_id = org.id
    db.flush()
    event = _make_opportunity_event(db, opportunity, source, event_type=event_type)
    db.commit()

    social_event = social_event_from_db(db, event)

    assert social_event is not None
    assert social_event.event_type == "opportunity.rfp_closing"


def test_social_event_from_db_defaults_to_none_for_an_unmapped_event_type(db: Session) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.unmapped")
    proposal = make_visible_proposal(db, source)
    db.flush()
    event = make_event(db, proposal, source, event_type="field_changed")
    db.commit()

    assert social_event_from_db(db, event) is None


def test_social_event_from_db_ignores_a_non_proposal_opportunity_subject(db: Session) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.other_subject")
    event = Event(
        subject_type="organization",
        subject_id=_uuid.uuid4(),
        event_type="created",
        observed_at=dt.datetime.now(UTC),
        source_id=source.id,
        source_url=source.url,
        retrieved_at=dt.datetime.now(UTC),
        licence_id=source.licence_id,
        idempotency_key="test:other-subject",
    )
    db.add(event)
    db.flush()

    assert social_event_from_db(db, event) is None


def test_social_event_from_db_opportunity_without_issuer_is_not_draftable(db: Session) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.no_issuer")
    opportunity = make_visible_opportunity(db, source)  # issuer_org_id left null
    db.flush()
    event = _make_opportunity_event(db, opportunity, source, event_type="opened")
    db.commit()

    assert social_event_from_db(db, event) is None


def test_social_event_from_db_award_and_funding_events_are_not_draftable_yet(db: Session) -> None:
    """Known schema gap (services/social/README.md): no `awardee`/`award_amount`/
    `funding_program` field exists yet on `opportunity` or in any connector's `event.after`."""
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.funding_gap")
    org = make_org(db)
    opportunity = make_visible_opportunity(db, source)
    opportunity.issuer_org_id = org.id
    db.flush()
    awarded = _make_opportunity_event(db, opportunity, source, event_type="awarded")
    cancelled = _make_opportunity_event(db, opportunity, source, event_type="cancelled")
    reinstated = _make_opportunity_event(db, opportunity, source, event_type="reinstated")
    db.commit()

    assert social_event_from_db(db, awarded) is None
    assert social_event_from_db(db, cancelled) is None
    assert social_event_from_db(db, reinstated) is None


def test_social_event_from_db_raises_for_a_missing_subject(db: Session) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.missing_subject")
    proposal = make_visible_proposal(db, source)
    db.flush()
    event = make_event(db, proposal, source, event_type="created")
    for link in list(proposal.sources):
        db.delete(link)
    db.flush()
    db.delete(proposal)
    db.commit()

    with pytest.raises(SubjectNotFoundError):
        social_event_from_db(db, event)


def test_social_event_from_db_raises_for_a_missing_opportunity_subject(db: Session) -> None:
    licence = make_open_licence(db)
    source = make_public_source(db, licence, id_="us.test.missing_opportunity_subject")
    opportunity = make_visible_opportunity(db, source)
    db.flush()
    event = _make_opportunity_event(db, opportunity, source, event_type="opened")
    for link in list(opportunity.sources):
        db.delete(link)
    db.flush()
    db.delete(opportunity)
    db.commit()

    with pytest.raises(SubjectNotFoundError):
        social_event_from_db(db, event)
