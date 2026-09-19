"""services/social/publishers/: dry-run-only adapters (docs/32 §4.1, §4.5)."""

import datetime as dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services.social import editorial
from services.social.publishers import PUBLISHERS, BlueskyPublisher, LinkedInPublisher, XPublisher
from services.social.publishers.base import CredentialsMissing

RETRIEVED_AT = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)


def make_draft(channel: str, *, status: str = "approved"):
    event = editorial.SocialEvent(
        event_id="e1",
        event_type="proposal.new",
        event_date=dt.date(2026, 9, 10),
        subject_type="proposal",
        subject_id="caiso:1",
        source_id="us.iso.caiso.gen_queue",
        source_name="CAISO Public Queue Report",
        source_url="https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx",
        retrieved_at=RETRIEVED_AT,
        reuse_class="attribution",
        page_url="https://infraque.com/proposals/caiso:1",
        lag_days=14,
        technology="solar",
        capacity_mw=250.0,
        county="Kern",
        state="CA",
        iso_rto="CAISO",
        queue_id="Q1234",
        developer_org="Acme Solar LLC",
    )
    draft = editorial.build_draft(event, channel)
    draft.status = status
    return draft


class TestPublisherRegistry:
    def test_all_three_channels_registered(self) -> None:
        assert set(PUBLISHERS) == {"bluesky", "linkedin", "x"}


class TestBluesky:
    def test_dry_run_never_calls_network_and_reports_would_send(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("BSKY_HANDLE", raising=False)
        monkeypatch.delenv("BSKY_APP_PASSWORD", raising=False)
        publisher = BlueskyPublisher()
        draft = make_draft("bluesky")
        result = publisher.publish(draft, dry_run=True)
        assert result.dry_run is True
        assert result.platform_id is None
        assert result.would_send["text"] == draft.body
        assert result.would_send["validation"]["passed"] is True

    def test_default_construction_never_goes_live_even_with_dry_run_false(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BSKY_HANDLE", "feed.example.com")
        monkeypatch.setenv("BSKY_APP_PASSWORD", "app-password")
        publisher = BlueskyPublisher()  # feature_flag_live defaults to False
        result = publisher.publish(make_draft("bluesky"), dry_run=False)
        assert result.dry_run is True  # refuses to go live without the flag

    def test_live_flag_without_credentials_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("BSKY_HANDLE", raising=False)
        monkeypatch.delenv("BSKY_APP_PASSWORD", raising=False)
        publisher = BlueskyPublisher(feature_flag_live=True)
        with pytest.raises(CredentialsMissing):
            publisher.publish(make_draft("bluesky"), dry_run=False)

    def test_live_flag_with_only_one_credential_still_refuses(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BSKY_HANDLE", "feed.example.com")
        monkeypatch.delenv("BSKY_APP_PASSWORD", raising=False)
        publisher = BlueskyPublisher(feature_flag_live=True)
        with pytest.raises(CredentialsMissing):
            publisher.publish(make_draft("bluesky"), dry_run=False)

    def test_live_flag_with_credentials_is_not_implemented_not_silently_sent(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("BSKY_HANDLE", "feed.example.com")
        monkeypatch.setenv("BSKY_APP_PASSWORD", "app-password")
        publisher = BlueskyPublisher(feature_flag_live=True)
        with pytest.raises(NotImplementedError):
            publisher.publish(make_draft("bluesky"), dry_run=False)


class TestLinkedIn:
    def test_dry_run(self) -> None:
        publisher = LinkedInPublisher()
        result = publisher.publish(make_draft("linkedin"), dry_run=True)
        assert result.dry_run is True
        assert result.would_send["commentary"] == make_draft("linkedin").body

    def test_never_goes_live(self) -> None:
        publisher = LinkedInPublisher()
        with pytest.raises(NotImplementedError):
            publisher.publish(make_draft("linkedin"), dry_run=False)


class TestX:
    def test_dry_run_reports_cost_estimate(self) -> None:
        publisher = XPublisher()
        result = publisher.publish(make_draft("x"), dry_run=True)
        assert result.dry_run is True
        assert result.would_send["estimated_cost_usd"] == pytest.approx(editorial.cost_estimate_usd("x"))

    def test_never_goes_live(self) -> None:
        publisher = XPublisher()
        with pytest.raises(NotImplementedError):
            publisher.publish(make_draft("x"), dry_run=False)


class TestValidateGate:
    def test_wrong_channel_draft_fails_validate(self) -> None:
        publisher = BlueskyPublisher()
        draft = make_draft("x")  # an X draft handed to the Bluesky publisher
        result = publisher.validate(draft)
        assert not result.passed
        assert any("channel" in f for f in result.failures)

    def test_unapproved_draft_fails_validate(self) -> None:
        publisher = BlueskyPublisher()
        draft = make_draft("bluesky", status="draft")
        result = publisher.validate(draft)
        assert not result.passed
        assert any("not ready to publish" in f for f in result.failures)

    def test_approved_draft_passes_validate(self) -> None:
        publisher = BlueskyPublisher()
        draft = make_draft("bluesky", status="approved")
        result = publisher.validate(draft)
        assert result.passed, result.failures
