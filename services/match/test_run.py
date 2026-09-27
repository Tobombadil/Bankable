"""`services/match/run.py` against a real (SQLite) store: the three modes, the one-active-row-per-pair
upsert, removal with a reason, the two events per change (one per timeline) and their visibility
stamps, the watermark, and the CLI."""

from __future__ import annotations

import dataclasses
import datetime as dt
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from services.api.conftest import (
    make_attribution_licence,
    make_open_licence,
    make_public_source,
    make_visible_opportunity,
    make_visible_proposal,
)
from services.db.models import Event, Match, Opportunity, Proposal, WorkerWatermark
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from services.match import run as run_mod
from services.match.rules import load_rules
from services.match.run import WATERMARK_NAME, active_match_count, candidate_pairs, main, run_matches

UTC = dt.UTC


@pytest.fixture()
def db() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as session:
        yield session


def _storage_proposal(db: Session, source: object, suffix: str, **kw: object) -> Proposal:
    prop = make_visible_proposal(db, source, public_id_suffix=suffix)  # type: ignore[arg-type]
    prop.technology = str(kw.get("technology", "storage"))
    prop.jurisdiction = str(kw.get("jurisdiction", "US-TX"))
    prop.capacity_mw = kw.get("capacity_mw", 200.0)  # type: ignore[assignment]
    prop.lifecycle_state = str(kw.get("lifecycle_state", "studied"))
    db.flush()
    return prop


def _storage_rfp(db: Session, source: object, suffix: str, **kw: object) -> Opportunity:
    opp = make_visible_opportunity(db, source, public_id_suffix=suffix)  # type: ignore[arg-type]
    opp.technologies = list(kw.get("technologies", ["bess"]))  # type: ignore[call-overload]
    opp.jurisdiction = str(kw.get("jurisdiction", "US-TX"))
    opp.capacity_sought_mw = kw.get("capacity_sought_mw", 500.0)  # type: ignore[assignment]
    opp.due_at = kw.get("due_at", dt.datetime.now(UTC) + dt.timedelta(days=45))  # type: ignore[assignment]
    db.flush()
    return opp


@pytest.fixture()
def world(db: Session) -> dict[str, object]:
    open_lic = make_open_licence(db)
    attr_lic = make_attribution_licence(db)
    queue = make_public_source(db, attr_lic, id_="us.test.queue")
    notices = make_public_source(db, open_lic, id_="us.test.notices")
    matching = _storage_proposal(db, queue, "1")
    wrong_state = _storage_proposal(db, queue, "2", jurisdiction="US-CA")
    solar = _storage_proposal(db, queue, "3", technology="solar")
    rfp = _storage_rfp(db, notices, "1")
    db.commit()
    return {"queue": queue, "notices": notices, "p1": matching, "p2": wrong_state, "p3": solar, "rfp": rfp}


def _active(db: Session) -> list[Match]:
    return list(db.scalars(select(Match).where(Match.status == "active")))


def _events(db: Session, event_type: str) -> list[Event]:
    return list(db.scalars(select(Event).where(Event.event_type == event_type).order_by(Event.seq)))


PUBLISHED = dataclasses.replace(load_rules(), publish=True)


def test_full_run_writes_one_match_and_two_events(db: Session, world: dict[str, object]) -> None:
    report = run_matches(db, full=True, rules=PUBLISHED)
    db.commit()
    assert (report.mode, report.added, report.removed, report.kept) == ("full", 1, 0, 0)
    assert report.pairs_scored == 2  # p1 and p2 share TX/US and the storage family; p3 is blocked out
    assert report.events_written == 2
    assert report.active_total == 1
    assert "added=1" in report.summary()

    (match,) = _active(db)
    p1, rfp = world["p1"], world["rfp"]
    assert (match.proposal_id, match.opportunity_id) == (p1.id, rfp.id)  # type: ignore[attr-defined]
    assert match.created_by == "rule"
    assert match.rule_set_version == "match-rules@v1"
    assert match.rationale_text == "storage, TX, 50–550 MW, due in 45 days"
    assert float(match.score) == 1.0
    assert match.rationale["rules_failed"] == []

    events = _events(db, "match_added")
    assert {(e.subject_type, e.subject_id) for e in events} == {
        ("proposal", p1.id),  # type: ignore[attr-defined]
        ("opportunity", rfp.id),  # type: ignore[attr-defined]
    }
    by_subject = {e.subject_type: e for e in events}
    # Provenance is the counterpart's: the proposal-timeline event discloses the opportunity.
    assert by_subject["proposal"].source_id == "us.test.notices"
    assert by_subject["opportunity"].source_id == "us.test.queue"
    assert by_subject["proposal"].after["counterpart"]["public_id"] == rfp.public_id  # type: ignore[attr-defined]
    assert by_subject["proposal"].after["match_id"] == public_id("mat", match.id)
    for event in events:
        assert event.public_at is not None and event.published_at is not None
        assert event.actor_type == "system"
        assert event.idempotency_key == f"match:{match.id}:match_added:{event.subject_type}"
    assert db.get(WorkerWatermark, WATERMARK_NAME) is not None


def test_rerun_keeps_the_row_and_writes_nothing_new(db: Session, world: dict[str, object]) -> None:
    run_matches(db, full=True)
    db.commit()
    (first,) = _active(db)
    first_matched_at, first_id = first.first_matched_at, first.id
    report = run_matches(db, full=True)
    db.commit()
    assert (report.added, report.kept, report.removed, report.events_written) == (0, 1, 0, 0)
    (again,) = _active(db)
    assert again.id == first_id
    assert again.first_matched_at == first_matched_at


def test_incremental_removes_then_readds_with_a_new_row(db: Session, world: dict[str, object]) -> None:
    run_matches(db, full=True)
    db.commit()
    rfp = world["rfp"]
    rfp.status = "closed"  # type: ignore[attr-defined]
    db.commit()

    report = run_matches(db)
    db.commit()
    assert (report.mode, report.removed, report.added) == ("incremental", 1, 0)
    assert report.opportunities_considered == 1
    removed = db.scalars(select(Match).where(Match.status == "removed")).one()
    assert removed.removed_at is not None
    removal_events = _events(db, "match_removed")
    assert len(removal_events) == 2
    assert removal_events[0].after["reason"] == "rules failed: timing"
    assert removal_events[0].before == {"match": "active"}

    rfp.status = "open"  # type: ignore[attr-defined]
    db.commit()
    report = run_matches(db)
    db.commit()
    assert report.added == 1
    assert len(db.scalars(select(Match)).all()) == 2  # the removed row is kept; a new one is active


def test_nothing_changed_is_a_no_op(db: Session, world: dict[str, object]) -> None:
    run_matches(db, full=True)
    db.commit()
    report = run_matches(db)
    assert (report.mode, report.pairs_scored, report.added, report.removed) == ("incremental", 0, 0, 0)


def test_first_run_without_a_watermark_is_full(db: Session, world: dict[str, object]) -> None:
    assert run_matches(db).mode == "full"


def test_a_new_rule_set_version_forces_a_full_run(db: Session, world: dict[str, object]) -> None:
    run_matches(db, full=True)
    db.commit()
    v2 = dataclasses.replace(load_rules(), version="match-rules@v2")
    report = run_matches(db, rules=v2)
    assert report.mode == "full"
    (match,) = _active(db)
    assert match.rule_set_version == "match-rules@v2"


def test_a_deadline_expires_without_any_row_changing(db: Session, world: dict[str, object]) -> None:
    run_matches(db, full=True)
    db.commit()
    later = dt.datetime.now(UTC) + dt.timedelta(days=60)
    report = run_matches(db, now=later)
    assert (report.mode, report.removed) == ("incremental", 1)
    assert _events(db, "match_removed")[0].after["reason"] == "rules failed: timing"


def test_scoped_run_touches_only_its_records_and_not_the_watermark(
    db: Session, world: dict[str, object]
) -> None:
    run_matches(db, full=True)
    db.commit()
    mark = db.get(WorkerWatermark, WATERMARK_NAME)
    assert mark is not None
    before = mark.updated_at
    p2 = world["p2"]
    p2.jurisdiction = "US-TX"  # type: ignore[attr-defined]
    db.commit()
    report = run_matches(db, proposal_ids=[p2.id])  # type: ignore[attr-defined]
    db.commit()
    assert (report.mode, report.added, report.proposals_considered) == ("scoped", 1, 1)
    assert active_match_count(db, proposal_id=p2.id) == 1  # type: ignore[attr-defined]
    assert active_match_count(db, opportunity_id=world["rfp"].id) == 2  # type: ignore[attr-defined]
    assert db.get(WorkerWatermark, WATERMARK_NAME).updated_at == before  # type: ignore[union-attr]

    report = run_matches(db, opportunity_ids=[world["rfp"].id])  # type: ignore[attr-defined]
    assert (report.kept, report.opportunities_considered) == (2, 1)


def test_hidden_side_writes_events_that_never_surface(db: Session, world: dict[str, object]) -> None:
    rfp = world["rfp"]
    rfp.publish_state = "pending_review"  # type: ignore[attr-defined]
    db.commit()
    report = run_matches(db, full=True)
    assert report.added == 1  # matched: pending records are eligible, invisible until published
    for event in _events(db, "match_added"):
        assert event.public_at is None and event.published_at is None


def test_ineligible_records_lose_their_matches(db: Session, world: dict[str, object]) -> None:
    run_matches(db, full=True)
    db.commit()
    p1 = world["p1"]
    p1.publish_state = "unpublished"  # type: ignore[attr-defined]
    db.commit()
    run_matches(db, full=True)
    assert _events(db, "match_removed")[0].after["reason"].startswith("proposal no longer eligible")

    p1.publish_state = "public"  # type: ignore[attr-defined]
    db.commit()
    run_matches(db, full=True)
    rfp = world["rfp"]
    rfp.publish_state = "unpublished"  # type: ignore[attr-defined]
    db.commit()
    run_matches(db, full=True)
    assert _events(db, "match_removed")[-1].after["reason"].startswith("opportunity no longer eligible")


def test_below_threshold_removal_names_the_score(db: Session, world: dict[str, object]) -> None:
    run_matches(db, full=True)
    db.commit()
    rfp = world["rfp"]
    rfp.due_at = None  # type: ignore[attr-defined]
    db.commit()
    strict = dataclasses.replace(load_rules(), threshold=0.95)
    run_matches(db, rules=strict, full=True)
    assert _events(db, "match_removed")[0].after["reason"] == "score 0.900 below threshold 0.95"


def test_a_side_with_no_active_source_gets_an_unsourced_event(db: Session, world: dict[str, object]) -> None:
    for link in world["rfp"].sources:  # type: ignore[attr-defined]
        link.active = False
    db.commit()
    run_matches(db, full=True)
    by_subject = {e.subject_type: e for e in _events(db, "match_added")}
    assert by_subject["proposal"].source_id is None  # fails closed on every non-admin read
    assert by_subject["opportunity"].source_id == "us.test.queue"


def test_candidate_pairs_scoping(db: Session, world: dict[str, object]) -> None:
    rules = load_rules()
    proposals = run_mod._eligible_proposals(db)
    opportunities = run_mod._eligible_opportunities(db)
    rfp_id = world["rfp"].id  # type: ignore[attr-defined]
    everything = candidate_pairs(proposals, opportunities, rules)
    assert candidate_pairs(proposals, opportunities, rules, opportunity_scope={rfp_id}) == everything
    p3 = world["p3"].id  # type: ignore[attr-defined]
    assert (
        candidate_pairs(proposals, opportunities, rules, proposal_scope={p3}, opportunity_scope=set())
        == set()
    )


def test_all_source_opportunity_blocks_on_country_only(db: Session, world: dict[str, object]) -> None:
    _storage_rfp(db, world["notices"], "9", technologies=["all_source"], jurisdiction="US")
    db.commit()
    report = run_matches(db, full=True)
    assert report.added == 4  # p1 x rfp, and all three proposals x the all-source notice


def test_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    url = f"sqlite+pysqlite:///{tmp_path / 'cli.db'}"
    engine = get_engine(url)
    init_db(engine)
    with get_sessionmaker(engine)() as db:
        lic = make_open_licence(db)
        src = make_public_source(db, lic)
        prop = _storage_proposal(db, src, "1")
        _storage_rfp(db, src, "1")
        db.commit()
        prop_public_id = prop.public_id
    assert main(["--all", "--database-url", url]) == 0
    assert "mode=full" in capsys.readouterr().out
    assert main(["--proposal", prop_public_id, "--database-url", url]) == 0
    assert "mode=scoped" in capsys.readouterr().out
    assert main(["--proposal", "prop_NOPE000000", "--database-url", url]) == 2
    assert "not found" in capsys.readouterr().err


def test_the_committed_gate_keeps_every_event_off_every_tier(db: Session, world: dict[str, object]) -> None:
    """`match-rules@v1` has `publish: false` (data/match_rules.yaml): matches are still computed and
    stored, and their events are written with no public or published stamp, adds and removals alike."""
    assert load_rules().publish is False
    report = run_matches(db, full=True)
    db.commit()
    assert report.added == 1 and len(_active(db)) == 1
    world["rfp"].status = "closed"  # type: ignore[attr-defined]
    db.commit()
    assert run_matches(db).removed == 1
    events = _events(db, "match_added") + _events(db, "match_removed")
    assert len(events) == 4
    assert all(e.public_at is None and e.published_at is None for e in events)
