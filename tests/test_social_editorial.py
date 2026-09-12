"""services/social/editorial.py: which events earn a post (docs/32 §3.1), templates (§3.3),
attribution/disclosure (§3.2, docs/13 §6), and the §4.3 validation gates.

The fixture below is a synthetic event for every `POSTABLE_EVENT_TYPES` entry, plus the
never-posted and gated cases docs/32 §3.1 calls out explicitly.
"""

import dataclasses
import datetime as dt
import pathlib
import sys

import pytest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from services.social import editorial
from services.social.editorial import (
    CHANNEL_LIMITS,
    DEFAULT_CONFIG,
    SocialEvent,
    channels_for_event,
)

RETRIEVED_AT = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 9, 10)

_PROPOSAL_BASE = {
    "event_id": "e-proposal",
    "event_date": TODAY,
    "subject_type": "proposal",
    "source_id": "us.iso.caiso.gen_queue",
    "source_name": "CAISO Public Queue Report",
    "source_url": "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx",
    "retrieved_at": RETRIEVED_AT,
    "reuse_class": "attribution",
    "lag_days": 14,
}

_OPPORTUNITY_BASE = {
    "event_id": "e-opportunity",
    "event_date": TODAY,
    "subject_type": "opportunity",
    "source_id": "us.utility_rfps",
    "source_name": "Utility RFP Portal",
    "source_url": "https://example.com/rfps",
    "retrieved_at": RETRIEVED_AT,
    "reuse_class": "open",
}


def proposal_new(**overrides: object) -> SocialEvent:
    fields = {
        **_PROPOSAL_BASE,
        "event_type": "proposal.new",
        "subject_id": "caiso:1",
        "page_url": "https://{{DOMAIN}}/proposals/caiso:1",
        "technology": "solar",
        "capacity_mw": 250.0,
        "county": "Kern",
        "state": "CA",
        "iso_rto": "CAISO",
        "queue_id": "Q1234",
        "developer_org": "Acme Solar LLC",
    }
    fields.update(overrides)
    return SocialEvent(**fields)  # type: ignore[arg-type]


def proposal_status_changed(**overrides: object) -> SocialEvent:
    fields = {
        **_PROPOSAL_BASE,
        "event_type": "proposal.status_changed",
        "subject_id": "caiso:2",
        "page_url": "https://{{DOMAIN}}/proposals/caiso:2",
        "technology": "solar",
        "capacity_mw": 250.0,
        "state": "CA",
        "iso_rto": "CAISO",
        "queue_id": "Q1234",
        "status_from": "permitted",
        "status_to": "contracted",
        "proposal_name": "Acme Solar Farm",
    }
    fields.update(overrides)
    return SocialEvent(**fields)  # type: ignore[arg-type]


def proposal_withdrawn(**overrides: object) -> SocialEvent:
    fields = {
        **_PROPOSAL_BASE,
        "event_type": "proposal.withdrawn",
        "subject_id": "ercot:1",
        "page_url": "https://{{DOMAIN}}/proposals/ercot:1",
        "technology": "storage",
        "capacity_mw": 300.0,
        "county": "Ward",
        "state": "TX",
        "iso_rto": "ERCOT",
        "queue_id": "INR12345",
        "withdrawal_reason_code": "Withdrawn by IC",
    }
    fields.update(overrides)
    return SocialEvent(**fields)  # type: ignore[arg-type]


def rfp_opened(**overrides: object) -> SocialEvent:
    fields = {
        **_OPPORTUNITY_BASE,
        "event_type": "opportunity.rfp_opened",
        "subject_id": "rfp:1",
        "page_url": "https://{{DOMAIN}}/opportunities/rfp:1",
        "issuer_org": "Big Utility Co",
        "solicitation_title": "2027 All-Source RFP",
        "technology": "solar",
        "capacity_mw": 500.0,
        "deadline_date": dt.date(2026, 11, 1),
    }
    fields.update(overrides)
    return SocialEvent(**fields)  # type: ignore[arg-type]


def rfp_closing(days_left: int, **overrides: object) -> SocialEvent:
    fields = {
        **_OPPORTUNITY_BASE,
        "event_type": "opportunity.rfp_closing",
        "subject_id": "rfp:1",
        "page_url": "https://{{DOMAIN}}/opportunities/rfp:1",
        "issuer_org": "Big Utility Co",
        "solicitation_title": "2027 All-Source RFP",
        "deadline_date": TODAY + dt.timedelta(days=days_left),
    }
    fields.update(overrides)
    return SocialEvent(**fields)  # type: ignore[arg-type]


def opportunity_awarded(**overrides: object) -> SocialEvent:
    fields = {
        **_OPPORTUNITY_BASE,
        "event_type": "opportunity.awarded",
        "subject_id": "rfp:1",
        "page_url": "https://{{DOMAIN}}/opportunities/rfp:1",
        "issuer_org": "Big Utility Co",
        "solicitation_title": "2027 All-Source RFP",
        "awardee_org": "Acme Solar LLC",
        "award_usd": 250_000_000.0,
    }
    fields.update(overrides)
    return SocialEvent(**fields)  # type: ignore[arg-type]


def funding_change(event_type: str, **overrides: object) -> SocialEvent:
    fields = {
        **_OPPORTUNITY_BASE,
        "event_type": event_type,
        "subject_id": "grant:1",
        "page_url": "https://{{DOMAIN}}/opportunities/grant:1",
        "source_id": "us.grants_gov.search2",
        "source_name": "grants.gov",
        "awardee_org": "Acme Solar LLC",
        "proposal_name": "Acme Solar Project",
        "state": "CA",
        "funding_program": "DOE LPO",
        "award_usd": 8_500_000.0,
        "award_prior_status": "Active",
    }
    fields.update(overrides)
    return SocialEvent(**fields)  # type: ignore[arg-type]


def digest_weekly(**overrides: object) -> SocialEvent:
    fields = {
        **_OPPORTUNITY_BASE,
        "event_type": "digest.weekly",
        "subject_id": "digest:2026-W37",
        "page_url": "https://{{DOMAIN}}/digest/2026-W37",
        "digest_items": (
            {"event_type": "proposal.new", "capacity_mw": 100.0},
            {"event_type": "opportunity.rfp_opened"},
            {"event_type": "opportunity.awarded"},
        ),
    }
    fields.update(overrides)
    return SocialEvent(**fields)  # type: ignore[arg-type]


#: One event per docs/32 §3.1 postable row, used to drive the "every event type" tests.
FIXTURE: dict[str, SocialEvent] = {
    "proposal.new": proposal_new(),
    "proposal.status_changed": proposal_status_changed(),
    "proposal.withdrawn": proposal_withdrawn(),
    "opportunity.rfp_opened": rfp_opened(),
    "opportunity.rfp_closing": rfp_closing(14),
    "opportunity.awarded": opportunity_awarded(),
    "funding.cancelled": funding_change("funding.cancelled"),
    "funding.reinstated": funding_change("funding.reinstated"),
    "digest.weekly": digest_weekly(),
}


# --------------------------------------------------------------------------------- eligibility

class TestEligibility:
    @pytest.mark.parametrize("event_type", sorted(editorial.POSTABLE_EVENT_TYPES))
    def test_every_postable_event_type_earns_at_least_one_channel(self, event_type: str) -> None:
        assert channels_for_event(FIXTURE[event_type]) != ()

    def test_proposal_new_below_threshold_earns_nothing(self) -> None:
        small = proposal_new(capacity_mw=5.0, technology="wind")
        assert channels_for_event(small) == ()

    def test_proposal_new_at_exact_threshold_qualifies(self) -> None:
        exact = proposal_new(capacity_mw=DEFAULT_CONFIG.proposal_new_mw, technology="wind")
        assert set(channels_for_event(exact)) == {"bluesky", "x"}

    def test_high_volume_iso_solar_needs_the_higher_bar(self) -> None:
        below_high_bar = proposal_new(capacity_mw=75.0, technology="solar", iso_rto="CAISO")
        assert channels_for_event(below_high_bar) == ()
        at_high_bar = proposal_new(capacity_mw=100.0, technology="solar", iso_rto="CAISO")
        assert channels_for_event(at_high_bar) != ()

    def test_low_volume_iso_solar_uses_the_ordinary_bar(self) -> None:
        event = proposal_new(capacity_mw=75.0, technology="solar", iso_rto="NYISO")
        assert channels_for_event(event) != ()

    def test_proposal_new_linkedin_needs_the_higher_bar(self) -> None:
        mid = proposal_new(capacity_mw=100.0, technology="wind")
        assert set(channels_for_event(mid)) == {"bluesky", "x"}
        large = proposal_new(capacity_mw=250.0, technology="wind")
        assert set(channels_for_event(large)) == {"bluesky", "x", "linkedin"}

    def test_proposal_new_linkedin_technology_override(self) -> None:
        nuclear = proposal_new(capacity_mw=60.0, technology="nuclear")
        assert set(channels_for_event(nuclear)) == {"bluesky", "x", "linkedin"}

    def test_proposal_new_via_load_mw_or_voltage_or_capex(self) -> None:
        no_capacity = {"capacity_mw": None, "technology": None}
        assert channels_for_event(proposal_new(load_mw=150.0, **no_capacity)) != ()
        assert channels_for_event(proposal_new(voltage_kv=115.0, **no_capacity)) != ()
        assert channels_for_event(proposal_new(capex_usd=150_000_000.0, **no_capacity)) != ()

    def test_proposal_new_with_no_size_field_at_all_never_posts(self) -> None:
        event = proposal_new(capacity_mw=None, load_mw=None, voltage_kv=None, capex_usd=None, technology=None)
        assert channels_for_event(event) == ()

    def test_status_changed_requires_a_canonical_transition(self) -> None:
        non_canonical = proposal_status_changed(status_from="filed", status_to="withdrawn")
        assert channels_for_event(non_canonical) == ()

    def test_status_changed_linkedin_only_for_agreement_construction_operation(self) -> None:
        studied = proposal_status_changed(status_from="filed", status_to="studied", capacity_mw=250.0)
        assert set(channels_for_event(studied)) == {"bluesky", "x"}
        contracted = proposal_status_changed(status_from="permitted", status_to="contracted")
        assert "linkedin" in channels_for_event(contracted)

    def test_status_changed_below_threshold_never_posts(self) -> None:
        event = proposal_status_changed(capacity_mw=5.0)
        assert channels_for_event(event) == ()

    def test_withdrawn_linkedin_needs_200mw(self) -> None:
        # technology="gas" keeps this off the high-volume-ISO 100 MW bar (ERCOT default is a
        # high-volume ISO for solar/storage), isolating the size check this test is about.
        small = proposal_withdrawn(capacity_mw=60.0, technology="gas")
        assert set(channels_for_event(small)) == {"bluesky", "x"}
        large = proposal_withdrawn(capacity_mw=250.0, technology="gas")
        assert set(channels_for_event(large)) == {"bluesky", "x", "linkedin"}

    def test_rfp_opened_has_no_size_threshold(self) -> None:
        tiny = rfp_opened(capacity_mw=1.0)
        assert channels_for_event(tiny) == ("bluesky", "linkedin", "x")

    def test_rfp_closing_only_fires_at_14_or_3_days(self) -> None:
        assert channels_for_event(rfp_closing(14)) == ("bluesky", "linkedin", "x")
        assert channels_for_event(rfp_closing(3)) == ("bluesky", "x")
        assert channels_for_event(rfp_closing(7)) == ()
        assert channels_for_event(rfp_closing(0)) == ()

    def test_awarded_requires_a_named_awardee(self) -> None:
        no_awardee = opportunity_awarded(awardee_org=None)
        assert channels_for_event(no_awardee) == ()
        assert channels_for_event(opportunity_awarded()) == ("bluesky", "linkedin", "x")

    def test_digest_never_reaches_x(self) -> None:
        assert channels_for_event(digest_weekly()) == ("bluesky", "linkedin")

    def test_restricted_source_never_posts(self) -> None:
        event = proposal_new(reuse_class="restricted", source_id="us.iso.pjm.gen_queue")
        assert channels_for_event(event) == ()

    def test_unknown_reuse_class_never_posts(self) -> None:
        event = proposal_new(reuse_class="unknown")
        assert channels_for_event(event) == ()

    @pytest.mark.parametrize(
        "event_type",
        [
            "proposal.capacity_changed",
            "proposal.field_changed",
            "source.health_changed",
            "resolver.merged",
            "made_up_type",
        ],
    )
    def test_never_posted_event_types_are_default_denied(self, event_type: str) -> None:
        event = dataclasses.replace(proposal_new(), event_type=event_type)
        assert channels_for_event(event) == ()


# --------------------------------------------------------------------------------- templates

class TestTemplates:
    @pytest.mark.parametrize("event_type", sorted(editorial.POSTABLE_EVENT_TYPES))
    def test_every_channel_fits_its_limit(self, event_type: str) -> None:
        event = FIXTURE[event_type]
        for channel in channels_for_event(event):
            draft = editorial.build_draft(event, channel)
            assert len(draft.body) <= CHANNEL_LIMITS[channel], draft.body

    @pytest.mark.parametrize("event_type", sorted(editorial.POSTABLE_EVENT_TYPES))
    def test_every_draft_carries_attribution_link_and_disclosure(self, event_type: str) -> None:
        event = FIXTURE[event_type]
        for channel in channels_for_event(event):
            draft = editorial.build_draft(event, channel)
            assert draft.attribution_line
            assert draft.link_url == event.page_url
            assert draft.body.count(draft.link_url) == 1
            assert draft.disclosure_text  # present as metadata on every draft, per task brief

    @pytest.mark.parametrize("event_type", sorted(editorial.POSTABLE_EVENT_TYPES))
    def test_every_draft_passes_validation(self, event_type: str) -> None:
        event = FIXTURE[event_type]
        for channel in channels_for_event(event):
            draft = editorial.build_draft(event, channel)
            assert draft.validation is not None
            assert draft.validation.passed, draft.validation.failures

    def test_proposal_events_carry_the_lag_notice(self) -> None:
        for event_type in ("proposal.new", "proposal.status_changed", "proposal.withdrawn"):
            event = FIXTURE[event_type]
            for channel in channels_for_event(event):
                draft = editorial.build_draft(event, channel)
                assert draft.delayed_tier_notice is not None
                assert draft.delayed_tier_notice in draft.body

    def test_live_events_carry_no_lag_notice(self) -> None:
        for event_type in ("opportunity.rfp_opened", "opportunity.awarded", "funding.cancelled"):
            event = FIXTURE[event_type]
            for channel in channels_for_event(event):
                draft = editorial.build_draft(event, channel)
                assert draft.delayed_tier_notice is None

    def test_linkedin_never_gets_the_disclosure_body_line_bluesky_x_do(self) -> None:
        assert editorial.DISCLOSURE_TEXT["bluesky"] == editorial.DISCLOSURE_TEXT["x"]
        assert editorial.DISCLOSURE_TEXT["linkedin"] != editorial.DISCLOSURE_TEXT["bluesky"]

    def test_null_fields_are_omitted_not_invented(self) -> None:
        event = proposal_new(developer_org=None, queue_id=None)
        draft = editorial.build_draft(event, "bluesky")
        assert "None" not in draft.body

    def test_withdrawn_reason_quoted_verbatim_when_present(self) -> None:
        draft = editorial.build_draft(proposal_withdrawn(), "bluesky")
        assert "Withdrawn by IC" in draft.body

    def test_withdrawn_reason_omitted_when_absent(self) -> None:
        event = proposal_withdrawn(withdrawal_reason_code=None)
        draft = editorial.build_draft(event, "bluesky")
        assert "Reason per record" not in draft.body

    def test_overflow_drops_optional_clauses_before_truncating(self) -> None:
        event = proposal_new(
            technology="solar photovoltaic plus four-hour battery energy storage system",
            developer_org="A Very Long Renewable Energy Development Company Holdings International LLC",
            county="San Luis Obispo",
        )
        for channel in ("bluesky", "x"):
            draft = editorial.build_draft(event, channel)
            assert len(draft.body) <= CHANNEL_LIMITS[channel]
            assert draft.body.count(event.page_url) == 1
            assert draft.attribution_line in draft.body

    def test_extreme_overflow_falls_back_to_truncation_and_still_fits(self) -> None:
        event = rfp_opened(
            issuer_org="A Very Long Regional Municipal Utility Cooperative Authority Board",
            solicitation_title="Request for Proposals " * 30,
        )
        draft = editorial.build_draft(event, "x")
        assert len(draft.body) <= CHANNEL_LIMITS["x"]
        assert draft.body.count(event.page_url) == 1

    def test_banned_words_fail_validation(self) -> None:
        event = proposal_new()
        body, _template_id, _version = editorial.render(event, "bluesky")
        body = body.replace("New in", "Massive, game-changing new in")
        result = editorial.validate_draft(
            body,
            event,
            "bluesky",
            attribution_line=editorial.attribution_line_for(event),
            disclosure_text=editorial.DISCLOSURE_TEXT["bluesky"],
            delayed_tier_notice=editorial.delayed_tier_notice_for(event, "bluesky"),
        )
        assert not result.passed
        assert any("banned words" in f for f in result.failures)

    def test_unsolicited_mention_fails_validation_on_x_and_bluesky(self) -> None:
        event = proposal_new()
        for channel in ("bluesky", "x"):
            body, *_ = editorial.render(event, channel)
            body = body + " cc @someone"
            result = editorial.validate_draft(
                body, event, channel,
                attribution_line=editorial.attribution_line_for(event),
                disclosure_text=editorial.DISCLOSURE_TEXT[channel],
                delayed_tier_notice=editorial.delayed_tier_notice_for(event, channel),
            )
            assert not result.passed
            assert any("mention" in f for f in result.failures)

    def test_stray_url_fails_validation(self) -> None:
        event = proposal_new()
        body, *_ = editorial.render(event, "bluesky")
        body = body + " see also https://evil.example/"
        result = editorial.validate_draft(
            body, event, "bluesky",
            attribution_line=editorial.attribution_line_for(event),
            disclosure_text=editorial.DISCLOSURE_TEXT["bluesky"],
            delayed_tier_notice=editorial.delayed_tier_notice_for(event, "bluesky"),
        )
        assert not result.passed
        assert any("URL other than" in f for f in result.failures)

    def test_invented_number_fails_validation(self) -> None:
        event = proposal_new()
        body, *_ = editorial.render(event, "bluesky")
        body = body.replace("250 MW", "999 MW")
        result = editorial.validate_draft(
            body, event, "bluesky",
            attribution_line=editorial.attribution_line_for(event),
            disclosure_text=editorial.DISCLOSURE_TEXT["bluesky"],
            delayed_tier_notice=editorial.delayed_tier_notice_for(event, "bluesky"),
        )
        assert not result.passed
        assert any("numbers not traceable" in f for f in result.failures)

    def test_restricted_reuse_class_fails_validation_even_if_somehow_rendered(self) -> None:
        event = proposal_new(reuse_class="restricted")
        body, *_ = editorial.render(event, "bluesky")
        result = editorial.validate_draft(
            body, event, "bluesky",
            attribution_line=editorial.attribution_line_for(event),
            disclosure_text=editorial.DISCLOSURE_TEXT["bluesky"],
            delayed_tier_notice=editorial.delayed_tier_notice_for(event, "bluesky"),
        )
        assert not result.passed
        assert any("reuse_class" in f for f in result.failures)

    def test_missing_disclosure_fails_validation(self) -> None:
        event = proposal_new()
        body, *_ = editorial.render(event, "bluesky")
        result = editorial.validate_draft(
            body, event, "bluesky",
            attribution_line=editorial.attribution_line_for(event),
            disclosure_text="",
            delayed_tier_notice=editorial.delayed_tier_notice_for(event, "bluesky"),
        )
        assert not result.passed
        assert "disclosure text missing" in result.failures


# --------------------------------------------------------------------------------- formatting

class TestFormatting:
    def test_fmt_mw_no_decimal_at_or_above_10(self) -> None:
        assert editorial.fmt_mw(250.0) == "250 MW"
        assert editorial.fmt_mw(10.0) == "10 MW"

    def test_fmt_mw_one_decimal_below_10(self) -> None:
        assert editorial.fmt_mw(8.5) == "8.5 MW"

    def test_fmt_usd_bn_m_k(self) -> None:
        assert editorial.fmt_usd(1_200_000_000) == "$1.2bn"
        assert editorial.fmt_usd(450_000_000) == "$450m"
        assert editorial.fmt_usd(8_500_000) == "$8.5m"

    def test_fmt_date(self) -> None:
        assert editorial.fmt_date(dt.date(2026, 9, 12)) == "12 Sep 2026"

    def test_fmt_state_short_and_full(self) -> None:
        assert editorial.fmt_state("ca", full=False) == "CA"
        assert editorial.fmt_state("ca", full=True) == "California"
        assert editorial.fmt_state("zz", full=True) == "zz"  # unmapped falls back, never guesses


# --------------------------------------------------------------------------------- dedupe

class TestDedupeAndPriority:
    def test_idempotency_key_stable_for_identical_inputs(self) -> None:
        event = proposal_new()
        assert editorial.idempotency_key(event, "bluesky") == editorial.idempotency_key(event, "bluesky")

    def test_idempotency_key_differs_by_channel(self) -> None:
        event = proposal_new()
        assert editorial.idempotency_key(event, "bluesky") != editorial.idempotency_key(event, "x")

    def test_idempotency_key_differs_by_status_to(self) -> None:
        a = proposal_status_changed(status_to="contracted")
        b = proposal_status_changed(status_to="built")
        assert editorial.idempotency_key(a, "bluesky") != editorial.idempotency_key(b, "bluesky")

    def test_dedupe_key_ignores_event_type(self) -> None:
        new_event = proposal_new(subject_id="caiso:9")
        withdrawn_event = proposal_withdrawn(subject_id="caiso:9")
        assert editorial.dedupe_key(new_event, "bluesky") == editorial.dedupe_key(withdrawn_event, "bluesky")

    def test_priority_order_withdrawn_over_status_changed_over_new(self) -> None:
        assert editorial.outranks("proposal.withdrawn", "proposal.status_changed")
        assert editorial.outranks("proposal.status_changed", "proposal.new")
        assert not editorial.outranks("proposal.new", "proposal.status_changed")

    def test_rfp_closing_always_outranks(self) -> None:
        assert editorial.outranks("opportunity.rfp_closing", "proposal.withdrawn")


# --------------------------------------------------------------------------------- diff.py adapter

class TestFromDiffRow:
    def test_capacity_change_and_cod_change_are_dropped(self) -> None:
        for diff_type in ("capacity_change", "cod_change", "removed"):
            row = {
                "event_type": diff_type, "record_id": "caiso:1", "source_id": "us.iso.caiso.gen_queue",
                "field": "capacity_mw", "before": "100.0", "after": "120.0",
                "observed_at": "2026-09-10T00:00:00+00:00",
            }
            assert editorial.from_diff_row(
                row, record=None, source_name="CAISO", source_url="https://x", reuse_class="attribution",
                page_url="https://{{DOMAIN}}/proposals/caiso:1", lag_days=14,
            ) is None

    def test_new_row_joins_the_canonical_record(self) -> None:
        row = {
            "event_type": "new", "record_id": "caiso:1", "source_id": "us.iso.caiso.gen_queue",
            "field": "lifecycle_state", "before": None, "after": "filed",
            "observed_at": "2026-09-10T00:00:00+00:00",
        }
        record = {
            "name_canonical": "Solar One", "technology": "solar", "capacity_mw": 250.0,
            "county": "Kern", "state": "CA", "iso": "CAISO", "queue_id": "Q1234",
            "sponsor_name": "Acme Solar LLC", "retrieved_at": "2026-09-10T12:00:00+00:00",
        }
        event = editorial.from_diff_row(
            row, record=record, source_name="CAISO", source_url="https://x", reuse_class="attribution",
            page_url="https://{{DOMAIN}}/proposals/caiso:1", lag_days=14,
        )
        assert event is not None
        assert event.event_type == "proposal.new"
        assert event.capacity_mw == 250.0
        assert event.developer_org == "Acme Solar LLC"

    def test_new_row_without_a_record_has_no_size_fields_and_never_posts(self) -> None:
        row = {
            "event_type": "new", "record_id": "caiso:1", "source_id": "us.iso.caiso.gen_queue",
            "field": "lifecycle_state", "before": None, "after": "filed",
            "observed_at": "2026-09-10T00:00:00+00:00",
        }
        event = editorial.from_diff_row(
            row, record=None, source_name="CAISO", source_url="https://x", reuse_class="attribution",
            page_url="https://{{DOMAIN}}/proposals/caiso:1", lag_days=14,
        )
        assert event is not None
        assert event.capacity_mw is None
        assert channels_for_event(event) == ()  # never invent a value to clear the threshold

    def test_infer_subject_type(self) -> None:
        assert editorial.infer_subject_type("us.iso.caiso.gen_queue") == "proposal"
        assert editorial.infer_subject_type("us.grants_gov.search2") == "opportunity"
