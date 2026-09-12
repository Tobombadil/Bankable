"""services/social/models.py: PostDraft, Post, ReviewMetadata, ValidationResult round-trips."""

import datetime as dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services.social.models import (
    CHANNELS,
    POST_STATUSES,
    REJECT_REASONS,
    Post,
    PostDraft,
    ReviewMetadata,
    ValidationResult,
)


def make_draft(**overrides: object) -> PostDraft:
    fields = {
        "channel": "bluesky",
        "event_id": "e1",
        "event_type": "proposal.new",
        "subject_type": "proposal",
        "subject_id": "caiso:1",
        "template_id": "proposal.new.bluesky",
        "template_version": "v1",
        "body": "New in CAISO queue: 250 MW solar. https://x/y Source: CAISO, 12 Sep 2026.",
        "link_url": "https://x/y",
        "attribution_line": "Source: CAISO",
        "disclosure_text": "Automated feed run by Bankable.",
        "fields_snapshot": {"capacity_mw": 250.0},
        "idempotency_key": "deadbeef",
        "dedupe_key": "bluesky|caiso:1",
    }
    fields.update(overrides)
    return PostDraft(**fields)  # type: ignore[arg-type]


class TestPostDraft:
    def test_rejects_unknown_channel(self) -> None:
        with pytest.raises(ValueError, match="unknown channel"):
            make_draft(channel="mastodon")

    def test_rejects_unknown_status(self) -> None:
        with pytest.raises(ValueError, match="unknown status"):
            make_draft(status="posted")

    def test_every_channel_and_status_constructs(self) -> None:
        for channel in CHANNELS:
            for status in POST_STATUSES:
                make_draft(channel=channel, status=status)

    def test_to_dict_from_dict_round_trip(self) -> None:
        draft = make_draft()
        draft.review.reviewer = "andrew@example.com"
        draft.review.reject_reason = "wrong_fact"
        draft.validation = ValidationResult(passed=False, failures=("length exceeded",))
        draft.scheduled_for = dt.datetime(2026, 9, 15, 12, 0, tzinfo=dt.UTC)

        restored = PostDraft.from_dict(draft.to_dict())

        assert restored.id == draft.id
        assert restored.channel == draft.channel
        assert restored.body == draft.body
        assert restored.review.reviewer == "andrew@example.com"
        assert restored.review.reject_reason == "wrong_fact"
        assert restored.validation is not None
        assert restored.validation.passed is False
        assert restored.validation.failures == ("length exceeded",)
        assert restored.scheduled_for == draft.scheduled_for
        assert restored.fields_snapshot == draft.fields_snapshot

    def test_to_dict_is_json_serialisable(self) -> None:
        import json

        draft = make_draft()
        json.dumps(draft.to_dict())  # must not raise

    def test_to_post_freezes_content(self) -> None:
        draft = make_draft(status="approved")
        draft.review.reviewer = "andrew@example.com"
        post = draft.to_post(external_post_id="at://abc", published_at=dt.datetime.now(dt.UTC), dry_run=True)
        assert post.draft_id == draft.id
        assert post.body == draft.body
        assert post.external_post_id == "at://abc"
        assert post.dry_run is True


class TestPost:
    def test_round_trip(self) -> None:
        post = Post(
            draft_id="d1", channel="x", event_id="e1", subject_type="proposal", subject_id="caiso:1",
            template_id="t", template_version="v1", body="hello https://x Source: CAISO",
            link_url="https://x", attribution_line="Source: CAISO", disclosure_text="disclosed",
            state="published", cost_usd=0.207,
        )
        restored = Post.from_dict(post.to_dict())
        assert restored.channel == "x"
        assert restored.cost_usd == pytest.approx(0.207)
        assert restored.metrics["clicks"] == 0

    def test_reject_reasons_are_the_docs_32_vocabulary(self) -> None:
        assert REJECT_REASONS == (
            "wrong_fact",
            "not_newsworthy",
            "source_doubt",
            "style",
            "duplicate",
            "other",
        )


class TestReviewMetadata:
    def test_defaults_are_json_safe(self) -> None:
        meta = ReviewMetadata()
        data = meta.to_dict()
        assert data["reviewer"] is None
        assert ReviewMetadata.from_dict(data).drafted_at == meta.drafted_at
