"""Items 3 and 5 of the 2026-09-18 blockers sprint, alerts side: the suppression store is honoured
by the sender; every digest carries `List-Unsubscribe` / `List-Unsubscribe-Post`, a legal postal
line and the delayed-data notice; a missing legal sender line refuses the send (raise, log,
count) where a real message could leave; the digest body is refused if it renders a bare `None`
(`services/alerts/mail.py`, `services/alerts/suppression.py`, `services/alerts/evaluate.py`)."""

from __future__ import annotations

import datetime as dt
import logging

import pytest

from services.alerts import evaluate as evaluate_module
from services.alerts.evaluate import MatchedItem, render_digest_body, run_alert_cycle
from services.alerts.mail import (
    ONE_CLICK_POST_VALUE,
    MailPortCannotCarryHeaders,
    OutboundEmail,
    ResendAlertMailer,
    SenderIdentityMissing,
    deliver,
    refused_send_count,
    reset_counters,
    sender_identity,
    unsubscribe_headers,
)
from services.alerts.suppression import is_suppressed, suppress
from services.alerts.worker import run_alert_tick
from services.api.audit import hash_identifier
from services.api.auth import ResendEmailAdapter, SentEmail
from services.api.common import API_HOST, DOMAIN, WEB_HOST
from services.api.conftest import make_event, make_open_licence, make_public_source, make_visible_proposal
from services.db.models import Alert, SavedSearch, Suppression
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ids import public_id
from services.social.textgate import BareNoneError
from tests.conftest import make_account, make_user

UTC = dt.UTC
FAKE_NAME = "Test Sender Ltd (not a real entity)"
FAKE_ADDRESS = "1 Test Street, Testville, TS1 1TS (fake)"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("SENDER_LEGAL_NAME", FAKE_NAME)
    monkeypatch.setenv("SENDER_POSTAL_ADDRESS", FAKE_ADDRESS)
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "test-pepper-not-a-secret")
    monkeypatch.delenv("APP_ENV", raising=False)
    reset_counters()


def _seed(db, *, email="ana@example.com"):
    account = make_account(db)
    user = make_user(db, account, email=email)
    lic = make_open_licence(db)
    src = make_public_source(db, lic)
    prop = make_visible_proposal(db, src, public_id_suffix="1")
    make_event(db, prop, src)
    search = SavedSearch(
        public_id="",
        user_id=user.id,
        account_id=account.id,
        name="Texas storage",
        entity="proposal",
        query={},
        query_hash="x",
        channels=["email"],
    )
    db.add(search)
    db.flush()
    search.public_id = public_id("ss", search.id)
    db.commit()
    return account, user, prop, search


class HeaderPort:
    """A real-sender double: `send` takes `headers`, no `dry_run` attribute."""

    def __init__(self) -> None:
        self.sent: list[tuple[SentEmail, dict[str, str]]] = []

    def send(self, *, to, subject, body, headers):
        record = SentEmail(to=to, subject=subject, body=body, provider_message_id="hp_1", dry_run=False)
        self.sent.append((record, dict(headers)))
        return record


# ----------------------------------------------------------------------------------- headers
def test_every_digest_carries_one_click_unsubscribe_headers_and_the_legal_footer(db):
    _account, _user, prop, _search = _seed(db)
    port = HeaderPort()

    created = run_alert_cycle(db, email_port=port)

    assert len(created) == 1 and created[0].status == "sent"
    sent, headers = port.sent[0]
    token = created[0].unsubscribe_token
    assert headers["List-Unsubscribe-Post"] == ONE_CLICK_POST_VALUE
    assert f"<mailto:unsubscribe@{DOMAIN}?subject=unsubscribe%20{token}>" in headers["List-Unsubscribe"]
    assert f"<{API_HOST}/v1/alerts/unsubscribe?token={token}>" in headers["List-Unsubscribe"]
    assert f"{FAKE_NAME}, {FAKE_ADDRESS}" in sent.body
    assert f"Unsubscribe (one click): {WEB_HOST}/unsubscribe?token={token}" in sent.body
    assert "Data notice: this digest is sent at your account's live tier" in sent.body
    assert prop.name_canonical in sent.body
    assert "None" not in sent.body


def test_public_tier_digest_carries_the_delayed_notice(db):
    account, _user, _prop, _search = _seed(db)
    account.entitlement = "public"
    db.commit()
    port = HeaderPort()
    run_alert_cycle(db, email_port=port)
    assert "delayed public tier" in port.sent[0][0].body


def test_unsubscribe_headers_url_encode_the_token():
    headers = unsubscribe_headers("ut_a b&c")
    assert "ut_a%20b%26c" in headers["List-Unsubscribe"]
    assert " b&" not in headers["List-Unsubscribe"]


def test_deliver_refuses_a_message_without_the_headers():
    with pytest.raises(ValueError, match="List-Unsubscribe"):
        deliver(HeaderPort(), OutboundEmail(to="a@example.com", subject="s", body="b", headers={}))


def test_legacy_email_port_is_accepted_only_as_a_dry_run(caplog):
    """`services.api.auth.ResendEmailAdapter.send` has no `headers` parameter (not this lane's
    file): it may still be used while it is a dry run, and is refused the moment it could send."""
    message = OutboundEmail(to="a@example.com", subject="s", body="b", headers=unsubscribe_headers("ut_x"))
    dry = ResendEmailAdapter(api_key="")
    with caplog.at_level(logging.WARNING, logger="services.alerts.mail"):
        sent = deliver(dry, message)
    assert sent.dry_run is True
    assert "legacy_email_port_dry_run" in caplog.text

    live = ResendEmailAdapter(api_key="re_fake_key_not_real")
    with pytest.raises(MailPortCannotCarryHeaders):
        deliver(live, message)


def test_resend_alert_mailer_records_headers_in_dry_run():
    mailer = ResendAlertMailer(api_key="")
    assert mailer.dry_run is True
    mailer.send(to="a@example.com", subject="s", body="b", headers={"List-Unsubscribe": "<x>"})
    assert mailer.sent_headers == [{"List-Unsubscribe": "<x>"}]


# --------------------------------------------------------------------------- sender identity
@pytest.mark.parametrize("missing", ["SENDER_LEGAL_NAME", "SENDER_POSTAL_ADDRESS"])
def test_missing_sender_identity_refuses_a_real_send_and_counts(db, monkeypatch, caplog, missing):
    monkeypatch.delenv(missing)
    _seed(db)
    port = HeaderPort()  # not a dry run, so strict even without APP_ENV

    with caplog.at_level(logging.ERROR, logger="services.alerts.mail"):
        with pytest.raises(SenderIdentityMissing, match=missing):
            run_alert_cycle(db, email_port=port)

    assert port.sent == []
    assert refused_send_count() == 1
    assert "alert_send_refused" in caplog.text
    assert missing in caplog.text
    assert db.query(Alert).count() == 0, "nothing was created, nothing was sent"


def test_missing_sender_identity_refuses_in_production_even_for_a_dry_run(monkeypatch):
    monkeypatch.delenv("SENDER_POSTAL_ADDRESS")
    monkeypatch.setenv("APP_ENV", "production")
    with pytest.raises(SenderIdentityMissing):
        sender_identity(strict=True)
    assert refused_send_count() == 1


def test_missing_sender_identity_renders_a_visible_placeholder_in_development(db, monkeypatch):
    monkeypatch.delenv("SENDER_LEGAL_NAME")
    _seed(db)
    mailer = ResendAlertMailer(api_key="")  # dry run, APP_ENV unset: non-strict
    created = run_alert_cycle(db, email_port=mailer)
    assert created[0].status == "sent"
    assert "[SENDER_LEGAL_NAME unset]" in mailer.sent[0].body
    assert refused_send_count() == 0


def test_worker_tick_records_the_refusal_as_an_error_and_counts_it(monkeypatch):
    monkeypatch.delenv("SENDER_LEGAL_NAME")
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    factory = get_sessionmaker(engine)
    with factory() as session:
        _seed(session)

    report = run_alert_tick(factory, email_port=HeaderPort(), transport=None, now=dt.datetime.now(UTC))

    assert report.emails_sent == 0
    assert report.emails_refused == 1
    assert any("SenderIdentityMissing" in e for e in report.errors)


# ------------------------------------------------------------------------------ suppression
def test_suppressed_recipient_never_gets_a_digest(db):
    _account, user, _prop, _search = _seed(db)
    suppress(db, user.email, "unsubscribe")
    db.commit()
    port = HeaderPort()

    created = run_alert_cycle(db, email_port=port)

    assert port.sent == []
    assert len(created) == 1
    assert created[0].status == "suppressed"
    assert created[0].error == "recipient is on the suppression list"
    assert user.email not in (created[0].error or "")


def test_suppression_is_case_insensitive_idempotent_and_holds_no_address(db):
    first = suppress(db, "Ana@Example.com ", "erasure")
    again = suppress(db, "ana@example.com", "erasure")
    assert first is again
    assert db.query(Suppression).count() == 1
    row = db.query(Suppression).one()
    assert row.email_hash == hash_identifier("ana@example.com")
    assert "example.com" not in row.email_hash
    assert is_suppressed(db, "ANA@EXAMPLE.COM") is True
    assert is_suppressed(db, "someone-else@example.com") is False
    assert is_suppressed(db, None) is True, "a blank address has nothing lawful to send to"
    assert suppress(db, "", "erasure") is None
    with pytest.raises(ValueError):
        suppress(db, "x@example.com", "because")


def test_hash_identifier_depends_on_the_pepper(monkeypatch):
    a = hash_identifier("ana@example.com")
    monkeypatch.setenv("AUDIT_HASH_PEPPER", "another-fake-pepper")
    b = hash_identifier("ana@example.com")
    assert a != b
    assert hash_identifier(None) is None and hash_identifier("  ") is None


# --------------------------------------------------------------------------------- None gate
def test_digest_body_with_a_none_field_is_refused(db):
    _account, _user, _prop, search = _seed(db)
    items = [
        MatchedItem(
            kind="proposal",
            name=f"{None}",  # a template bug: a null field interpolated
            url=f"{WEB_HOST}/proposals/x",
            source_name=None,
            attribution_text=None,
            event_seq=1,
        )
    ]
    with pytest.raises(BareNoneError) as excinfo:
        render_digest_body(search, items, unsubscribe_token="ut_x")
    assert excinfo.value.template_id == evaluate_module.DIGEST_TEMPLATE_ID


def test_digest_body_allows_names_that_merely_contain_the_word(db):
    _account, _user, _prop, search = _seed(db)
    items = [
        MatchedItem(
            kind="proposal",
            name="Nonesuch Ridge Solar",
            url=f"{WEB_HOST}/proposals/nonesuch",
            source_name="Nonell County Clerk",
            attribution_text=None,
            event_seq=1,
        )
    ]
    body = render_digest_body(search, items, unsubscribe_token="ut_x")
    assert "Nonesuch Ridge Solar" in body
