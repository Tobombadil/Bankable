"""The public pricing page (`web/pricing.py`, `web/templates/pricing.html`; copy from
`docs/41-pricing-page-copy.md`).

Same wiring as `web/test_auth.py`: `web.app.app` driven in-process against the real
`services.api.app.app` on a fresh SQLite database, so the checkout hand-off goes through the real
`POST /v1/billing/checkout` (`services/billing/router.py`) rather than a fake of our own. The one
port that is substituted is the billing port — `services/billing/fake.py`'s `InMemoryBilling`,
overridden exactly as `services/billing/conftest.py` does it — so no test here reaches a network,
and the checkout/portal URLs the page redirects to are the fake's.

The load-bearing test in this file is `test_unbuilt_shapes_are_not_advertised`: `docs/41` forbids
publishing an export or watchlist claim until those routes exist, and they do not
(`exports_per_day`/`export_rows_max` are quota fields on `GET /v1/me` with nothing behind them;
nothing is named watchlist anywhere). It fails the build the day that copy is added without the
routes.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session, sessionmaker

from services.api.app import app as api_app
from services.api.auth import ResendEmailAdapter, create_session
from services.api.auth_routes import get_email_port
from services.api.auth_routes import router as auth_router
from services.api.deps import get_db
from services.api.ratelimit import default_limiter
from services.billing.fake import InMemoryBilling
from services.db.session import get_engine, get_sessionmaker, init_db
from services.sor.ports import CheckoutRequest, CheckoutSession, PortalSession
from services.sor.wiring import get_billing_port
from tests.conftest import make_account, make_user
from web.api_client import ApiClient
from web.app import app as web_app

if not any(getattr(r, "path", None) == "/v1/auth/register" for r in api_app.routes):
    api_app.include_router(auth_router)

ORIGIN = {"origin": "http://testserver"}
PASSWORD = "correct horse battery staple"


@pytest.fixture(autouse=True)
def _reset_rate_limiter() -> None:
    default_limiter.reset()


@pytest.fixture()
def db_sessionmaker() -> sessionmaker[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    return get_sessionmaker(engine)


@pytest.fixture()
def billing_port() -> InMemoryBilling:
    """The dry-run port, which is what a deploy with no processor configured falls back to today
    (`services/billing/README.md` decision #1). Its URLs are hosted at `.invalid`."""
    return InMemoryBilling()


class LiveLikeBilling(InMemoryBilling):
    """The same fake, answering with a resolvable host — a stand-in for a configured processor,
    so the redirect itself can be exercised. Still no network: nothing fetches the URL."""

    #: It stands in for a configured processor, so it reports itself live and the page offers the
    #: buy button (`BillingPort.live`).
    live = True

    def create_checkout(self, request: CheckoutRequest) -> CheckoutSession:
        session = super().create_checkout(request)
        return replace(session, url=f"https://pay.example.com/c/{session.session_ref}")

    def open_portal(self, *, billing_ref: str, return_url: str) -> PortalSession:
        session = super().open_portal(billing_ref=billing_ref, return_url=return_url)
        return replace(session, url="https://pay.example.com/p/1")


@pytest.fixture()
def live_billing_port() -> LiveLikeBilling:
    return LiveLikeBilling()


@pytest.fixture()
def web_client(db_sessionmaker: sessionmaker[Session], billing_port: InMemoryBilling) -> Iterator[TestClient]:
    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    api_app.dependency_overrides[get_email_port] = lambda: ResendEmailAdapter()
    api_app.dependency_overrides[get_billing_port] = lambda: billing_port
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    web_app.state.billing_configured = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


@pytest.fixture()
def live_client(
    db_sessionmaker: sessionmaker[Session], live_billing_port: LiveLikeBilling
) -> Iterator[TestClient]:
    """`web_client`, but with a processor-like billing port wired, so the hand-off redirect is
    exercised rather than the "payments are not switched on yet" answer."""

    def _override_get_db() -> Iterator[Session]:
        session = db_sessionmaker()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    api_app.dependency_overrides[get_db] = _override_get_db
    api_app.dependency_overrides[get_email_port] = lambda: ResendEmailAdapter()
    api_app.dependency_overrides[get_billing_port] = lambda: live_billing_port
    web_app.state.api_client = ApiClient(TestClient(api_app, base_url="http://api-internal"))
    web_app.state.lag_days_default = None
    web_app.state.billing_configured = None
    with TestClient(web_app) as client:
        yield client
    api_app.dependency_overrides.clear()
    del web_app.state.api_client


def _register(client: TestClient, email: str = "buyer@example.com") -> None:
    resp = client.post(
        "/register", data={"email": email, "password": PASSWORD, "next": "/account"}, headers=ORIGIN
    )
    assert resp.status_code == 200


def _sign_in_as(
    client: TestClient,
    db_sessionmaker: sessionmaker[Session],
    *,
    entitlement: str = "public",
    email: str | None = "ana@example.com",
    billing_ref: str | None = None,
) -> None:
    """A session cookie for a user the registration form could not have produced — one with no
    email on file, or one whose account already carries a paid entitlement."""
    with db_sessionmaker() as session:
        account = make_account(session, entitlement=entitlement)
        account.billing_ref = billing_ref
        user = make_user(session, account, password=PASSWORD)
        user.email = email
        session.flush()
        _row, cookie = create_session(session, user)
        session.commit()
    client.cookies.set("session", cookie)


# ------------------------------------------------------------------------------ signed-out page
def test_pricing_page_renders_the_four_tiers_for_a_signed_out_visitor(web_client: TestClient) -> None:
    resp = web_client.get("/pricing")

    assert resp.status_code == 200
    body = resp.text
    for name in ("Free", "Pro", "Team", "API / Data"):
        assert f">{name}</h2>" in body
    # Every figure is docs/41's, and only docs/41's.
    for price in ("$0", "$149", "$1,490", "$9,000", "$5,000", "$14,000", "$25,000"):
        assert price in body
    # Signed out, the control says what it does: account first, then payment.
    assert "Create an account to buy Pro" in body
    assert 'href="/register?next=%2Fpricing%3Fplan%3Dpro"' in body
    assert 'href="/login?next=%2Fpricing%3Fplan%3Dteam"' in body
    assert "Continue to payment" not in body


def test_provisional_prices_are_marked_as_anchors_not_promoted_to_prices(web_client: TestClient) -> None:
    """docs/41 marks the Team and API/Data figures "verify before publishing" — an anchor from the
    market review, not a measured price. The page renders the number and says so."""
    body = web_client.get("/pricing").text

    assert body.count("Indicative price") == 2
    for tier_id in ("team", "api"):
        section = body.split(f'aria-labelledby="tier-{tier_id}"', 1)[1].split("</section>", 1)[0]
        assert "Indicative price" in section
    for tier_id in ("free", "pro"):
        section = body.split(f'aria-labelledby="tier-{tier_id}"', 1)[1].split("</section>", 1)[0]
        assert "Indicative price" not in section


def test_the_page_is_linked_from_the_site_navigation(web_client: TestClient) -> None:
    assert '<a href="/pricing">Pricing</a>' in web_client.get("/login").text


# -------------------------------------------------------------- docs/41 do-not-publish rules
@pytest.mark.parametrize("path", ["/pricing", "/pricing?plan=pro"])
def test_unbuilt_shapes_are_not_advertised(web_client: TestClient, path: str) -> None:
    """`docs/41`: "Do not publish an export or watchlist claim on this page until those routes
    ship." Neither exists in the code — `exports_per_day`/`export_rows_max` are quota fields on
    `GET /v1/me` with no route behind them, and nothing is named watchlist anywhere — and the
    owner's Team decision (seats plus support at the current price) depends on both staying off
    this page. If someone adds the copy before the routes, this fails."""
    body = web_client.get(path).text.lower()

    assert "export" not in body
    assert "watchlist" not in body


def test_unbuilt_shapes_are_not_advertised_to_a_signed_in_visitor(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _register(web_client)
    assert "export" not in web_client.get("/pricing").text.lower()

    _sign_in_as(web_client, db_sessionmaker, entitlement="pro", billing_ref="cus_test")
    signed_in = web_client.get("/pricing").text.lower()
    assert "export" not in signed_in
    assert "watchlist" not in signed_in


# --------------------------------------------------------------------------- no delay, anywhere
def test_the_page_claims_no_delay_on_any_plan(web_client: TestClient) -> None:
    """The ISO change-event delay was dropped on 2026-09-21 (owner, `docs/00-PLAN.md`) and every
    sentence about it went with it. A page that still sold "live events" as a paid upgrade would
    be advertising a difference the product no longer has, so the words are gone rather than
    hidden behind a condition that could be switched back on."""
    body = web_client.get("/pricing").text

    for phrase in (
        "days after it happens",
        "days later",
        "delayed-notice",
        "One delay",
        "ISO queue change events",
        "held back",
    ):
        assert phrase not in body, phrase
    # The footer's "every proposal, opportunity and change event is public as soon as it is
    # ingested" is the opposite claim and is meant to be there, so "change event" alone is not
    # a forbidden string -- only the sentences that assert a delay are.
    assert ">Pro</h2>" in body, "the rest of the page is untouched"
    assert "Saved searches" in body, "what the paid plans actually sell is shape"


def test_no_health_lag_field_can_put_the_delay_copy_back(web_client: TestClient) -> None:
    """Even if a `/v1/health` payload carried the old field, the page has nothing to render from
    it: the template variable and the tier flag are gone, not merely unset."""
    web_app.state.lag_days_default = {"supply": 0, "opportunities": 0, "iso_change_events": 14}
    try:
        body = web_client.get("/pricing").text
    finally:
        web_app.state.lag_days_default = None

    assert "14" not in body.split("<footer")[0] or "days after it happens" not in body
    assert "days after it happens" not in body
    assert "days later" not in body


# -------------------------------------------------------------------------- signed-in, free
def test_signed_in_free_user_gets_a_checkout_button_per_purchasable_tier(
    live_client: TestClient,
) -> None:
    """Driven by `live_client` rather than `web_client` since 2026-09-20: a buy button is only
    correct when the wired port can actually take a payment, and the plain dry-run fake cannot.
    The same visitor against `web_client` gets the "payments are not switched on yet" answer,
    which `test_the_buy_button_is_replaced_when_no_processor_is_configured` asserts."""
    _register(live_client)

    body = live_client.get("/pricing").text

    assert 'action="/pricing/checkout"' in body
    assert 'value="pro"' in body
    assert 'value="team"' in body
    assert "Continue to payment for Pro" in body
    assert "Continue to payment for Team" in body
    # API/Data is priced two ways in docs/41 (add-on or standalone), so it asks for an email
    # rather than claiming one price at a button.
    assert 'value="api"' not in body
    assert "Email us about API access" in body
    assert "Create an account to buy Pro" not in body


def test_a_picked_tier_is_marked_after_the_round_trip_through_registration(
    web_client: TestClient,
) -> None:
    _register(web_client)

    body = web_client.get("/pricing?plan=team").text

    assert "This is the plan you picked." in body
    assert 'class="tier tier--selected" aria-labelledby="tier-team"' in body


# -------------------------------------------------------------------------------- checkout
def test_checkout_post_reaches_the_billing_route_with_the_right_plan_and_seats(
    live_client: TestClient, live_billing_port: LiveLikeBilling
) -> None:
    _register(live_client, email="team-buyer@example.com")

    resp = live_client.post(
        "/pricing/checkout", data={"plan": "team"}, headers=ORIGIN, follow_redirects=False
    )

    assert len(live_billing_port.checkouts) == 1
    recorded = live_billing_port.checkouts[0].request
    assert recorded.plan == "team"
    assert recorded.seats == 5  # docs/41 Team: "$9,000/yr, 5 seats"
    assert recorded.customer_email == "team-buyer@example.com"
    # The hosted checkout page, followed as the port returned it — no card form on this site.
    assert resp.status_code == 303
    assert (
        resp.headers["location"] == f"https://pay.example.com/c/{live_billing_port.checkouts[0].session_ref}"
    )


def test_checkout_against_a_dry_run_billing_port_says_payments_are_not_live(
    web_client: TestClient, billing_port: InMemoryBilling
) -> None:
    """There is no processor account yet, so `build_billing_port()` falls back to the in-memory
    stand-in, whose checkout URL is hosted at the reserved `.invalid` TLD. Following it would put
    a dead browser page where the payment form should be, which reads as a broken checkout. The
    page says what is actually true instead, and the plan still reached the billing route."""
    _register(web_client, email="early@example.com")

    resp = web_client.post("/pricing/checkout", data={"plan": "pro"}, headers=ORIGIN, follow_redirects=False)

    assert resp.status_code == 503
    assert "Payments are not switched on yet" in resp.text
    assert "sales@" in resp.text
    assert billing_port.checkouts[0].request.plan == "pro"


def test_signed_out_checkout_post_sends_the_visitor_to_register_and_back_to_the_tier(
    web_client: TestClient, billing_port: InMemoryBilling
) -> None:
    resp = web_client.post("/pricing/checkout", data={"plan": "pro"}, headers=ORIGIN, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "/register?next=%2Fpricing%3Fplan%3Dpro"
    assert billing_port.checkouts == []


def test_checkout_post_from_another_site_is_refused(web_client: TestClient) -> None:
    assert web_client.post("/pricing/checkout", data={"plan": "pro"}).status_code == 403


def test_an_unknown_plan_is_answered_with_a_page_not_a_stack_trace(web_client: TestClient) -> None:
    resp = web_client.post("/pricing/checkout", data={"plan": "enterprise"}, headers=ORIGIN)

    assert resp.status_code == 400
    assert "That plan is not one you can buy here" in resp.text


# ------------------------------------------------------------------------------ no email yet
def test_a_user_with_no_email_is_told_what_to_do_before_pressing_anything(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    """`POST /v1/billing/checkout` refuses a user with no email rather than inventing one for the
    processor (`services/billing/README.md` decision #7). That is a reachable state, so the page
    explains it up front instead of rendering a button that cannot work."""
    _sign_in_as(web_client, db_sessionmaker, email=None)

    body = web_client.get("/pricing").text

    assert "We need an email address on your account first" in body
    assert "support@" in body
    assert "Continue to payment" not in body


def test_a_checkout_without_an_email_explains_it_rather_than_showing_the_raw_error(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session], billing_port: InMemoryBilling
) -> None:
    _sign_in_as(web_client, db_sessionmaker, email=None)

    resp = web_client.post("/pricing/checkout", data={"plan": "pro"}, headers=ORIGIN)

    assert resp.status_code == 409
    assert "We need an email address on your account first" in resp.text
    assert "and we will add it" in resp.text
    assert billing_port.checkouts == []


# ------------------------------------------------------------------------ existing subscriber
def test_a_subscriber_sees_manage_billing_instead_of_a_buy_button(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    _sign_in_as(web_client, db_sessionmaker, entitlement="pro", billing_ref="cus_existing")

    body = web_client.get("/pricing").text

    assert 'action="/pricing/portal"' in body
    assert "Open the billing page" in body
    assert "Continue to payment" not in body
    assert 'action="/pricing/checkout"' not in body


def test_manage_billing_follows_the_portal_url(
    live_client: TestClient, db_sessionmaker: sessionmaker[Session], live_billing_port: LiveLikeBilling
) -> None:
    _sign_in_as(live_client, db_sessionmaker, entitlement="pro", billing_ref="cus_existing")

    resp = live_client.post("/pricing/portal", headers=ORIGIN, follow_redirects=False)

    assert resp.status_code == 303
    assert resp.headers["location"] == "https://pay.example.com/p/1"
    assert live_billing_port.portals[0].billing_ref == "cus_existing"


def test_manage_billing_without_a_billing_customer_explains_the_conflict(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    """An entitlement granted by hand (docs/21 §3.13 — `admin`/interim grants are never derived
    from billing) has no customer at the processor. The route says so rather than 500ing."""
    _sign_in_as(web_client, db_sessionmaker, entitlement="pro", billing_ref=None)

    resp = web_client.post("/pricing/portal", headers=ORIGIN)

    assert resp.status_code == 409
    assert "No billing customer on file" in resp.text
    assert "Start a checkout before opening the portal." in resp.text


def test_portal_post_from_another_site_is_refused(web_client: TestClient) -> None:
    assert web_client.post("/pricing/portal").status_code == 403


# ------------------------------------------------- payments not switched on (checks.billing_configured)
def test_the_buy_button_is_replaced_when_no_processor_is_configured(
    web_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    """`web_client` runs the plain dry-run fake, which cannot take a payment. A signed-in visitor
    must learn that before pressing, not after. `POST /pricing/checkout`'s dry-run guard still
    catches the press; a button that cannot complete is a lie told at the moment we ask for
    money, and the guard does not excuse telling it."""
    _sign_in_as(web_client, db_sessionmaker)
    body = web_client.get("/pricing").text

    assert "Card payments are not switched on yet" in body
    assert "Email us to subscribe to Pro" in body
    assert "Continue to payment for Pro" not in body


def test_the_buy_button_is_shown_when_a_processor_is_configured(
    live_client: TestClient, db_sessionmaker: sessionmaker[Session]
) -> None:
    """The other branch, driven by a port that reports itself live, so the notice above cannot
    become unconditional without failing here."""
    _sign_in_as(live_client, db_sessionmaker)
    body = live_client.get("/pricing").text

    assert "Continue to payment for Pro" in body
    assert "Card payments are not switched on yet" not in body


def test_an_api_that_does_not_report_the_field_assumes_payments_are_live(
    live_client: TestClient, db_sessionmaker: sessionmaker[Session], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The two failure directions are not symmetric, and the default follows the cheaper one.

    Guessing "live" when it is not costs one press and an honest explanation from the checkout
    guard. Guessing "not live" when it is would hide checkout from a paying customer, with
    nothing to catch it. So a health response missing the field -- an API host older than this
    change -- shows the button.
    """
    _sign_in_as(live_client, db_sessionmaker)
    real_get = ApiClient.get

    def _without_the_field(self: ApiClient, path: str, **kwargs: object) -> dict[str, object]:
        body = real_get(self, path, **kwargs)  # type: ignore[arg-type]
        if path == "/v1/health":
            body = {**body, "checks": {k: v for k, v in body["checks"].items() if k != "billing_configured"}}
        return body

    monkeypatch.setattr(ApiClient, "get", _without_the_field)
    web_app.state.billing_configured = None
    body = live_client.get("/pricing").text

    assert "Continue to payment for Pro" in body


def test_the_dry_run_port_reports_itself_not_live(billing_port: InMemoryBilling) -> None:
    """`BillingPort.live` is a property of the wired adapter, not of the environment. Reading an
    environment variable instead gives the wrong answer wherever the port is injected -- which is
    every test, and every adapter that is not Stripe."""
    assert billing_port.live is False
    assert LiveLikeBilling().live is True
