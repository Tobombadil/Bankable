"""services/social/queue.py: the review queue (docs/32 §4.4-§4.9)."""

import datetime as dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services.social import editorial
from services.social import queue as queue_mod
from services.social.models import PostDraft

RETRIEVED_AT = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)


def make_event(**overrides: object) -> editorial.SocialEvent:
    fields: dict[str, object] = {
        "event_id": "e1",
        "event_type": "proposal.new",
        "event_date": dt.date(2026, 9, 10),
        "subject_type": "proposal",
        "subject_id": "caiso:1",
        "source_id": "us.iso.caiso.gen_queue",
        "source_name": "CAISO Public Queue Report",
        "source_url": "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx",
        "retrieved_at": RETRIEVED_AT,
        "reuse_class": "attribution",
        "page_url": "https://{{DOMAIN}}/proposals/caiso:1",
        "lag_days": 14,
        "technology": "solar",
        "capacity_mw": 250.0,
        "county": "Kern",
        "state": "CA",
        "iso_rto": "CAISO",
        "queue_id": "Q1234",
        "developer_org": "Acme Solar LLC",
    }
    fields.update(overrides)
    return editorial.SocialEvent(**fields)  # type: ignore[arg-type]


@pytest.fixture
def q(tmp_path: pathlib.Path) -> queue_mod.ReviewQueue:
    return queue_mod.ReviewQueue(tmp_path / "queue.json")


class TestAddAndDuplicateSuppression:
    def test_add_draft_persists_and_lists(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "bluesky")
        q.add_draft(draft)
        assert len(q.list_drafts()) == 1
        assert q.get(draft.id).id == draft.id

    def test_same_idempotency_key_is_rejected(self, q: queue_mod.ReviewQueue) -> None:
        event = make_event()
        q.add_draft(editorial.build_draft(event, "bluesky"))
        with pytest.raises(queue_mod.DuplicateDraft, match="idempotency_key"):
            q.add_draft(editorial.build_draft(event, "bluesky"))

    def test_different_channel_is_not_a_duplicate(self, q: queue_mod.ReviewQueue) -> None:
        event = make_event()
        q.add_draft(editorial.build_draft(event, "bluesky"))
        q.add_draft(editorial.build_draft(event, "x"))  # must not raise
        assert len(q.list_drafts()) == 2

    def test_same_subject_within_window_is_suppressed_unless_higher_priority(
        self, q: queue_mod.ReviewQueue
    ) -> None:
        new_event = make_event(event_id="e-new", event_type="proposal.new")
        q.add_draft(editorial.build_draft(new_event, "bluesky"))

        same_event_again = make_event(
            event_id="e-new-2", event_type="proposal.new", event_date=dt.date(2026, 9, 11)
        )
        with pytest.raises(queue_mod.DuplicateDraft, match="already posted"):
            q.add_draft(editorial.build_draft(same_event_again, "bluesky"))

    def test_higher_priority_event_is_not_suppressed(self, q: queue_mod.ReviewQueue) -> None:
        new_event = make_event(event_id="e-new")
        q.add_draft(editorial.build_draft(new_event, "bluesky"))

        withdrawn = make_event(
            event_id="e-withdrawn", event_type="proposal.withdrawn", event_date=dt.date(2026, 9, 11),
            withdrawal_reason_code=None,
        )
        draft = editorial.build_draft(withdrawn, "bluesky")
        q.add_draft(draft)  # must not raise -- withdrawn outranks new
        assert draft.id in {d.id for d in q.list_drafts()}

    def test_content_hash_guard_catches_a_near_identical_rewrite(self, q: queue_mod.ReviewQueue) -> None:
        event_a = make_event(subject_id="caiso:1", event_id="e-a")
        event_b = make_event(subject_id="caiso:2", event_id="e-b", queue_id="Q1234")
        draft_a = editorial.build_draft(event_a, "bluesky")
        q.add_draft(draft_a)
        # different subject (so the cross-event window check doesn't fire), but same rendered
        # text once the URL and date are stripped out -- a copy/paste style hazard.
        draft_b = editorial.build_draft(event_b, "bluesky")
        draft_b.body = draft_a.body.replace(event_a.page_url, event_b.page_url)
        draft_b.dedupe_key = f"bluesky|{event_b.subject_id}"
        draft_b.idempotency_key = "different-key"
        with pytest.raises(queue_mod.DuplicateDraft, match="content hash"):
            q.add_draft(draft_b)


class TestReviewOperations:
    def test_approve_requires_passing_validation(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "bluesky")
        draft.validation = editorial.ValidationResult(passed=False, failures=("length",))  # type: ignore[attr-defined]
        q.add_draft(draft)
        with pytest.raises(ValueError, match="failed validation"):
            q.approve(draft.id, "andrew@example.com")

    def test_approve_then_reject_state_machine(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "bluesky")
        q.add_draft(draft)
        approved = q.approve(draft.id, "andrew@example.com")
        assert approved.status == "approved"
        with pytest.raises(ValueError):
            q.approve(draft.id, "andrew@example.com")  # already approved

    def test_reject_requires_a_known_reason_code(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "bluesky")
        q.add_draft(draft)
        with pytest.raises(ValueError, match="reason_code"):
            q.reject(draft.id, "andrew@example.com", "because")
        rejected = q.reject(draft.id, "andrew@example.com", "wrong_fact")
        assert rejected.status == "rejected"
        assert rejected.review.reject_reason == "wrong_fact"

    def test_edit_stores_a_diff_and_returns_to_draft(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "bluesky")
        q.add_draft(draft)
        edited = q.edit(draft.id, draft.body.replace("250 MW", "260 MW"), "andrew@example.com")
        assert edited.status == "draft"
        assert edited.review.edit_count == 1
        assert "260 MW" in edited.body
        assert edited.review.last_edit_diff is not None
        # re-validated: 260 no longer traces to the event's capacity_mw field
        assert edited.validation is not None
        assert not edited.validation.passed


class TestSlaExpiry:
    def test_x_and_bluesky_have_different_slas(self) -> None:
        assert queue_mod.SLA_HOURS["x"] == 24
        assert queue_mod.SLA_HOURS["bluesky"] == 48

    def test_stale_draft_expires_to_withdrawn(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "x")
        q.add_draft(draft)
        later = draft.created_at + dt.timedelta(hours=25)
        expired = q.expire_stale(at=later)
        assert len(expired) == 1
        assert q.get(draft.id).status == "withdrawn"

    def test_fresh_draft_does_not_expire(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "x")
        q.add_draft(draft)
        soon = draft.created_at + dt.timedelta(hours=1)
        assert q.expire_stale(at=soon) == []


class TestScheduling:
    def test_schedule_requires_approval_first(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "bluesky")
        q.add_draft(draft)
        with pytest.raises(ValueError, match="cannot schedule"):
            q.schedule(draft.id)

    def test_schedule_assigns_a_slot_and_respects_the_daily_ceiling(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "linkedin")
        q.add_draft(draft)
        approved = q.approve(draft.id, "andrew@example.com")
        scheduled = q.schedule(approved.id, after=dt.datetime(2026, 9, 14, 6, 0, tzinfo=dt.UTC))  # a Monday
        assert scheduled.status == "scheduled"
        assert scheduled.scheduled_for is not None

    def test_linkedin_ceiling_of_3_per_day_is_enforced(self, q: queue_mod.ReviewQueue) -> None:
        day = dt.date(2026, 9, 14)  # Monday
        for i in range(queue_mod.DAILY_CEILING["linkedin"]):
            assert q.check_rate_limit("linkedin", day)
            # queue_id/page_url vary per iteration so the content-hash guard (docs/32 §4.8) sees
            # genuinely distinct posts, not the same proposal posted twice.
            event = make_event(
                event_id=f"e{i}", subject_id=f"caiso:{i}", queue_id=f"Q{i}",
                page_url=f"https://{{{{DOMAIN}}}}/proposals/caiso:{i}",
            )
            draft = editorial.build_draft(event, "linkedin")
            q.add_draft(draft)
            q.approve(draft.id, "andrew@example.com")
            q.schedule(draft.id, after=dt.datetime(2026, 9, 14, 6, 0, tzinfo=dt.UTC))
        assert not q.check_rate_limit("linkedin", day)


class TestGraduation:
    def test_linkedin_never_graduates(self, q: queue_mod.ReviewQueue) -> None:
        result = q.graduation_status("linkedin", "proposal.new")
        assert result.eligible is False
        assert "never auto-publishes" in result.reasons[0]

    def test_empty_queue_is_not_eligible(self, q: queue_mod.ReviewQueue) -> None:
        for channel in ("bluesky", "x"):
            result = q.graduation_status(channel, "proposal.new")
            assert result.eligible is False
            assert result.posts_in_window == 0
            assert any("200" in r for r in result.reasons)


class TestPersistence:
    def test_reload_from_disk_preserves_state(self, tmp_path: pathlib.Path) -> None:
        path = tmp_path / "queue.json"
        q1 = queue_mod.ReviewQueue(path)
        draft = editorial.build_draft(make_event(), "bluesky")
        q1.add_draft(draft)
        q1.approve(draft.id, "andrew@example.com")

        q2 = queue_mod.ReviewQueue(path)
        reloaded = q2.get(draft.id)
        assert reloaded.status == "approved"
        assert reloaded.review.reviewer == "andrew@example.com"
        assert isinstance(reloaded, PostDraft)


class TestCostAndStats:
    def test_cost_summary_only_counts_scheduled_or_published(self, q: queue_mod.ReviewQueue) -> None:
        draft = editorial.build_draft(make_event(), "x")
        q.add_draft(draft)
        assert q.cost_summary()["x"] == 0.0  # still a plain draft
        q.approve(draft.id, "andrew@example.com")
        q.schedule(draft.id, after=dt.datetime(2026, 9, 14, 6, 0, tzinfo=dt.UTC))
        assert q.cost_summary()["x"] == pytest.approx(editorial.cost_estimate_usd("x"))

    def test_review_stats_counts(self, q: queue_mod.ReviewQueue) -> None:
        d1 = editorial.build_draft(
            make_event(event_id="e1", subject_id="a", queue_id="QA", page_url="https://{{DOMAIN}}/proposals/a"),
            "bluesky",
        )
        d2 = editorial.build_draft(
            make_event(event_id="e2", subject_id="b", queue_id="QB", page_url="https://{{DOMAIN}}/proposals/b"),
            "bluesky",
        )
        q.add_draft(d1)
        q.add_draft(d2)
        q.reject(d2.id, "andrew@example.com", "wrong_fact")
        stats = q.review_stats()
        assert stats["drafted"] == 2
        assert stats["rejected"] == 1
        assert stats["wrong_fact"] == 1
