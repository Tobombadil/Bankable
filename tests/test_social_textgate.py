"""Item 6 of the 2026-09-18 blockers sprint (docs/50-audit-2026-09-18.md §3.2: "two templates can
publish the literal word None; the automation disclosure names the old company").

The first half renders every `services/social/editorial.py` template with each optional field it
interpolates set to `None` and proves the render path refuses the text (`services/social/
textgate.py`) rather than publishing "None". The two templates the audit counted are
`proposal.status_changed` (`status_from`/`status_to`, and `technology` on LinkedIn) and
`opportunity.rfp_closing` (`days_left` from a missing `deadline_date`); the parametrisation covers
the other templates whose f-strings interpolate nullable fields too, since the gate is generic.
The second half proves the disclosure names the product and follows the environment.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

import pytest

from services.social import editorial
from services.social.editorial import SocialEvent, disclosure_text_for, disclosure_texts
from services.social.textgate import BareNoneError, contains_bare_none, reject_bare_none

RETRIEVED_AT = dt.datetime(2026, 9, 10, 12, 0, tzinfo=dt.UTC)
TODAY = dt.date(2026, 9, 10)

_BASE = {
    "event_id": "e-1",
    "event_date": TODAY,
    "source_id": "us.iso.caiso.gen_queue",
    "source_name": "CAISO Public Queue Report",
    "source_url": "https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx",
    "retrieved_at": RETRIEVED_AT,
    "reuse_class": "attribution",
    "page_url": "https://infraque.com/proposals/caiso:1",
    "subject_id": "caiso:1",
}


def _event(event_type: str, subject_type: str = "proposal", **fields: object) -> SocialEvent:
    return SocialEvent(event_type=event_type, subject_type=subject_type, **_BASE, **fields)  # type: ignore[arg-type]


_FULL_PROPOSAL = {
    "proposal_name": "Kern Solar",
    "technology": "solar",
    "capacity_mw": 250.0,
    "county": "Kern",
    "state": "CA",
    "iso_rto": "CAISO",
    "queue_id": "Q123",
    "developer_org": "Sunrise Power LLC",
    "status_from": "filed",
    "status_to": "studied",
    "lag_days": 14,
}
_FULL_OPPORTUNITY = {
    "issuer_org": "Arizona Public Service",
    "solicitation_title": "2026 All-Source RFP",
    "deadline_date": dt.date(2026, 10, 1),
    "awardee_org": "Desert Wind LLC",
    "award_usd": 12_000_000.0,
    "funding_program": "Grid Resilience",
    "proposal_name": "Desert Wind",
    "state": "AZ",
}


@pytest.mark.parametrize(
    ("event_type", "subject_type", "full", "null_field", "channels"),
    [
        # The two the audit counted.
        ("proposal.status_changed", "proposal", _FULL_PROPOSAL, "status_from", ("bluesky", "x", "linkedin")),
        ("proposal.status_changed", "proposal", _FULL_PROPOSAL, "status_to", ("bluesky", "x", "linkedin")),
        ("proposal.status_changed", "proposal", _FULL_PROPOSAL, "technology", ("linkedin",)),
        (
            "opportunity.rfp_closing",
            "opportunity",
            _FULL_OPPORTUNITY,
            "deadline_date",
            ("bluesky", "x", "linkedin"),
        ),
        (
            "opportunity.rfp_closing",
            "opportunity",
            _FULL_OPPORTUNITY,
            "issuer_org",
            ("bluesky", "x", "linkedin"),
        ),
        # The same class of bug in the other templates.
        (
            "opportunity.rfp_opened",
            "opportunity",
            _FULL_OPPORTUNITY,
            "issuer_org",
            ("bluesky", "x", "linkedin"),
        ),
        (
            "opportunity.rfp_opened",
            "opportunity",
            _FULL_OPPORTUNITY,
            "solicitation_title",
            ("bluesky", "linkedin"),
        ),
        (
            "opportunity.awarded",
            "opportunity",
            _FULL_OPPORTUNITY,
            "awardee_org",
            ("bluesky", "x", "linkedin"),
        ),
        (
            "funding.cancelled",
            "opportunity",
            _FULL_OPPORTUNITY,
            "funding_program",
            ("bluesky", "x", "linkedin"),
        ),
        (
            "funding.cancelled",
            "opportunity",
            _FULL_OPPORTUNITY,
            "proposal_name",
            ("bluesky", "x", "linkedin"),
        ),
    ],
)
def test_render_refuses_a_template_that_interpolates_a_null_field(
    event_type, subject_type, full, null_field, channels
):
    fields = {**full, null_field: None}
    event = _event(event_type, subject_type, **fields)
    for channel in channels:
        with pytest.raises(BareNoneError) as excinfo:
            editorial.render(event, channel)
        assert excinfo.value.template_id == f"{event_type}.{channel}"
        assert "None" in excinfo.value.text  # what would have been published


def test_render_passes_when_every_interpolated_field_is_present():
    event = _event("proposal.status_changed", **_FULL_PROPOSAL)
    for channel in ("bluesky", "x", "linkedin"):
        body, template_id, _version = editorial.render(event, channel)
        assert "None" not in body
        assert template_id == f"proposal.status_changed.{channel}"


def test_draft_events_skips_the_bad_event_and_keeps_the_batch(caplog):
    good = _event("proposal.status_changed", **_FULL_PROPOSAL)
    bad = dataclasses.replace(good, event_id="e-2", subject_id="caiso:2", status_from=None)
    with caplog.at_level("WARNING", logger="services.social.editorial"):
        drafts = editorial.draft_events([bad, good])
    assert {d.event_id for d in drafts} == {"e-1"}
    assert "draft_rejected_bare_none" in caplog.text
    assert "e-2" in caplog.text
    assert "Kern Solar" not in caplog.text, "the rendered text is never logged"


def test_validate_draft_flags_a_bare_none_introduced_after_render():
    event = _event("proposal.status_changed", **_FULL_PROPOSAL)
    body, *_ = editorial.render(event, "bluesky")
    edited = body.replace("filed", "None")
    result = editorial.validate_draft(
        edited,
        event,
        "bluesky",
        attribution_line=editorial.attribution_line_for(event),
        disclosure_text=disclosure_text_for("bluesky"),
        delayed_tier_notice=editorial.delayed_tier_notice_for(event, "bluesky"),
    )
    assert not result.passed
    assert "bare 'None' token" in " ".join(result.failures)


def test_gate_matches_the_bare_token_only():
    assert contains_bare_none("Status None → filed") is True
    assert contains_bare_none("Nonesuch Ridge; Nonell County; NONE of it") is False
    assert reject_bare_none("clean", template_id="t") == "clean"


# ------------------------------------------------------------------------------- disclosure
def test_disclosure_names_the_product_not_the_old_company(monkeypatch):
    monkeypatch.delenv("PRODUCT_NAME", raising=False)
    monkeypatch.delenv("PRODUCT_URL", raising=False)
    monkeypatch.delenv("PRODUCT_CONTACT_EMAIL", raising=False)
    texts = disclosure_texts()
    for channel, text in texts.items():
        assert "Bankable" not in text, channel
        assert "bankablehq" not in text, channel
    assert texts["bluesky"].startswith("Automated feed run by Infraque (infraque.com).")
    assert "contact: hello@infraque.com" in texts["x"]
    assert "Infraque's pipeline" in texts["linkedin"]
    assert editorial.DISCLOSURE_TEXT["bluesky"] == texts["bluesky"]
    assert "x" in editorial.DISCLOSURE_TEXT


def test_disclosure_follows_the_environment(monkeypatch):
    monkeypatch.setenv("PRODUCT_NAME", "Renamed Product")
    monkeypatch.setenv("PRODUCT_URL", "https://renamed.example/")
    monkeypatch.setenv("PRODUCT_CONTACT_EMAIL", "hi@renamed.example")
    text = disclosure_text_for("x")
    assert text.startswith("Automated feed run by Renamed Product (renamed.example).")
    assert text.endswith("contact: hi@renamed.example.")
    event = _event("proposal.status_changed", **_FULL_PROPOSAL)
    draft = editorial.build_draft(event, "x")
    assert draft.disclosure_text == text
