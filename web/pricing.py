"""The public pricing page and its upgrade path (`GET /pricing`, `POST /pricing/checkout`,
`POST /pricing/portal`).

**Every number and inclusion below comes from `docs/41-pricing-page-copy.md`.** Nothing here is
invented: no price, no limit, no feature. Where `docs/41` marks a figure as an anchor rather than
a measured price ("verify before publishing"), the tier carries `price_is_provisional = True` and
the page renders that state next to the figure — an anchor is never silently promoted to a price.

**What is deliberately *not* on this page** (`docs/41`'s own do-not-publish rules, and the same
rule applied to the rest of the tier lists):

* **Export and watchlists.** `docs/41`: "Do not publish an export or watchlist claim on this page
  until those routes ship." They have not shipped — `exports_per_day` / `export_rows_max` are
  quota fields on `GET /v1/me` with no route behind them, and nothing is named watchlist anywhere
  in the codebase. `web/test_pricing.py::test_unbuilt_shapes_are_not_advertised` fails the build
  if either word reaches the rendered page, so the day the copy is added without the routes, CI
  catches it rather than a customer.
* **Licence pass-through** on the API tier — `docs/41`'s legal footer: not until the customer
  terms and the indemnity position exist. That is a counsel item.
* **Anything else `docs/41` marks "not built"**: the opportunity-deadline calendar, shared
  watchlists, proposal-to-opportunity matching. The same reasoning the doc gives for export and
  watchlists applies to them; only built shapes are sold here.
* **Bulk data pulls**, which `docs/41` lists under API/Data without a built/not-built marker:
  `api/openapi.yaml` has `/v1/bulk/proposals` and `/v1/bulk/opportunities` at `x-status: planned`
  and `services/api/` implements neither, so the claim is not published. Webhooks, by contrast,
  are `x-status: live` and implemented in `services/api/pro.py`, so they are.

**No delay, anywhere, and nothing on this page says otherwise.** Records carried no lag from
2026-09-19; the ISO change-event delay — the last one — was dropped on 2026-09-21 (owner,
`docs/00-PLAN.md`), together with its per-source knob (`services/ingest/lag.py`, migration
`0019`). The two sentences that stated that delay have gone with it, along with the
`iso_change_event_lag_days` variable that fed them and the `shows_live_iso_events` flag on the
paid tiers: a page that advertises "live events, unlike the free plan" when both are live is a
false claim about the product, and a dormant `{% if %}` around it would be a claim waiting to be
switched back on. `web/test_pricing.py` fails if any delay wording returns. What the paid plans
sell here is shape — saved searches, alerts, the API — exactly as the owner's decision says.

**The upgrade path.** Checkout is hosted by the payment processor: this site collects no card
details anywhere (that is the entire point of the redirect). A signed-in visitor's POST reaches
`POST /v1/billing/checkout` (`services/billing/router.py`) and this module redirects to the
session URL the port returns. A signed-out visitor is sent to register or sign in with
`?next=/pricing?plan=…`, so they come back to the tier they picked. A user with no email on file
is a real, reachable state, not an edge case — `POST /v1/billing/checkout` refuses with a
`conflict` rather than fabricating an address for the processor (`services/billing/README.md`
decision #7) — so the page shows that user what is wrong and what to do *before* they press a
button, and again if the refusal arrives from the route.

Nothing here is processor-specific: the template names no vendor, and this module talks only to
the two billing routes, which sit behind `services/sor/ports.py`'s `BillingPort`. Whichever
adapter is wired (the real one, or `services/billing/fake.py`), the page behaves the same.

**The `noncommercial` posture suspends the paid tiers honestly rather than hiding a live "Get the
plan" button behind them** (docs/26-platform-posture.md §3 precondition (i); owner, 2026-09-26
decisions log, verbatim: "Mark inactive, then flip — keep the pricing page visible with a 'paid
tiers not currently offered' notice; disable checkout; flip the posture once ready"). Read the
same way `/about` and `/methodology` read it — `web/page.py::get_platform_posture`, which asks
`GET /v1/health`'s `posture`/`posture_statement` fields rather than the environment, so this page
can never claim a posture the API-side gate is not applying. Under `noncommercial`: `GET /pricing`
prints a notice near the top combining the API's `posture_statement` with one page-owned sentence,
every tier card stays visible but is marked inactive with no checkout button or form, and
`POST /pricing/checkout` re-renders the page instead of proceeding (the API's own gate,
`services/billing/router.py`'s `PAID_TIERS_ACTIVE`, is the backstop this route's check mirrors,
exactly as `_is_dry_run_url` mirrors and does not replace the API's own state). Under `commercial`,
the default, none of this fires: `get_platform_posture` returns a `commercial` value, every branch
below behaves exactly as it did before this posture existed. `POST /pricing/portal` is unaffected
by any of it — managing a subscription an account already holds is not selling a new one.

Same page-rendering/`get_api`/CSRF glue as `web/auth.py` and `web/legal.py`, duplicated for the
reason `web/auth.py`'s docstring gives: `web/app.py` mounts this router, so importing back from
it at module level would be circular.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from services.api.common import DOMAIN
from web.api_client import ApiClient, build_client
from web.assets import ASSET_VERSION
from web.page import get_platform_posture
from web.viewmodels import footer_build as vm_footer_build

router = APIRouter()

_WEB_ROOT = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_WEB_ROOT / "templates"))

SESSION_COOKIE_NAME = "session"

#: `infraque.com` is still the placeholder token `services/api/common.py` documents, so these two
#: addresses are placeholders too — rendered through template variables, never written into the
#: markup, so replacing the domain is one edit.
SALES_EMAIL = f"sales@{DOMAIN}"
SUPPORT_EMAIL = f"support@{DOMAIN}"


# --------------------------------------------------------------------------------------- tiers
@dataclass(frozen=True)
class Tier:
    """One column of the table. `price`/`price_period` are the figures from `docs/41` §Free/Pro/
    Team/API; `includes` are only the inclusions whose route exists today (see this module's
    docstring for what is held back and why)."""

    id: str
    name: str
    price: str
    price_period: str
    #: `docs/41` marks this figure "verify before publishing" — an anchor from the market review,
    #: not a measured price. The page says so next to the number.
    price_is_provisional: bool
    summary: str
    includes: tuple[str, ...]
    #: The `PLAN_TIERS` value posted to `POST /v1/billing/checkout`, or `None` for a tier with no
    #: self-serve purchase (Free, which is simply the site; API/Data, which `docs/41` prices two
    #: different ways depending on whether the customer already has Team — a buy button cannot
    #: state one price honestly, so that tier asks for an email instead).
    plan: str | None
    #: Seats sent with the checkout: `docs/41` Team is "5 seats"; Pro is per seat.
    seats: int


#: Order is the order they render in. Prices: `docs/41` §Free ("$0"), §Pro ("$149/mo or $1,490/yr
#: per seat"), §Team ("$9,000/yr, 5 seats", anchor), §API/Data ("+$5,000/yr on top of Team
#: (Team+API = $14,000/yr), or $25,000/yr as a standalone enterprise tier", anchor).
TIERS: tuple[Tier, ...] = (
    Tier(
        id="free",
        name="Free",
        price="$0",
        price_period="no account needed to read",
        price_is_provisional=False,
        summary="Every proposal and every opportunity, published as soon as it is ingested.",
        includes=(
            "Every record on the public pages, with attribution to the register it came from",
            "Search, filters and the map",
            "RSS feeds of new proposals and opportunities",
        ),
        plan=None,
        seats=1,
    ),
    Tier(
        id="pro",
        name="Pro",
        price="$149",
        price_period="per seat, per month — or $1,490 per seat, per year",
        price_is_provisional=False,
        summary="Everything in Free, plus the work you would otherwise do by checking the site.",
        includes=(
            "Saved searches",
            "Daily change-feed alerts, by email or webhook",
        ),
        plan="pro",
        seats=1,
    ),
    Tier(
        id="team",
        name="Team",
        price="$9,000",
        price_period="per year, five seats",
        price_is_provisional=True,
        summary="Pro for a team, bought once.",
        includes=(
            "Five Pro seats on one invoice",
            "Support from the people who run the pipeline",
        ),
        plan="team",
        seats=5,
    ),
    Tier(
        id="api",
        name="API / Data",
        price="+$5,000",
        price_period="per year on top of Team ($14,000 per year together), or $25,000 per year on its own",
        price_is_provisional=True,
        summary="Everything in Team, reachable by software.",
        includes=(
            "API keys with live read access",
            "Change-event webhooks, signed, retried and logged",
            "Higher rate limits",
        ),
        plan=None,
        seats=1,
    ),
)

#: The tiers a visitor can buy here. `enterprise` is a `PLAN_TIERS` value too, but `docs/41`
#: prices it as a negotiated standalone tier, so it is not a button.
PURCHASABLE = {tier.id: tier for tier in TIERS if tier.plan is not None}

#: `docs/41` "Coverage line (shared, above the pricing table)", verbatim but for the doc-internal
#: citations, which are links on this page instead.
COVERAGE_LINE = (
    "Infraque tracks every major US interconnection queue and the open international tender "
    "registers. ERCOT, CAISO and NYISO are published today; PJM, MISO, SPP and ISO-NE are linked "
    "out pending licence clearance."
)

#: Shown when `POST /v1/billing/checkout` refuses for want of an email, and *before* that — on the
#: tier buttons themselves — when `GET /v1/me` already shows no email on file. There is no route
#: anywhere in this codebase (not even an admin one) that sets a user's email after the fact, so
#: the fix really is "write to us"; saying anything else would send the reader in a circle.
NO_EMAIL_TITLE = "We need an email address on your account first"
NO_EMAIL_DETAIL = (
    "Your sign-in has no email address on file, and the payment page has to have one to send "
    f"receipts and payment notices to. Email {SUPPORT_EMAIL} from the address you want on the "
    "account and we will add it, then come back to this page."
)


# ------------------------------------------------------------------- duplicated web/app.py glue
# (Identical to `web/auth.py`'s and `web/legal.py`'s copies — see `web/auth.py`'s docstring for
# why each page-rendering router keeps one rather than importing from `web/app.py`.)
def get_api(request: Request) -> ApiClient:
    client: ApiClient | None = getattr(request.app.state, "api_client", None)
    if client is None:
        client = build_client()
        request.app.state.api_client = client
    return client


def is_preview_active(request: Request) -> bool:
    override = getattr(request.app.state, "preview_active", None)
    if override is not None:
        return bool(override)
    return os.environ.get("WEB_DEV_PREVIEW", "").strip().lower() in ("1", "true", "yes", "on")


def get_lag_days(request: Request) -> dict[str, int]:
    cached: dict[str, int] | None = getattr(request.app.state, "lag_days_default", None)
    if cached is None:
        health = get_api(request).get("/v1/health")
        cached = dict(health["lag_days_default"])
        request.app.state.lag_days_default = cached
    return cached


templates.env.globals["is_preview_active"] = is_preview_active
templates.env.globals["footer_lag_days"] = get_lag_days
templates.env.globals["footer_build"] = lambda request: vm_footer_build(request, get_api(request))
templates.env.globals["asset_version"] = ASSET_VERSION


# ---------------------------------------------------------------------------------------- CSRF
def _is_same_origin(request: Request) -> bool:
    """Identical rule to `web/auth.py`'s `_is_same_origin`."""
    candidate = request.headers.get("origin") or request.headers.get("referer")
    if not candidate:
        return False
    parsed = urlsplit(candidate)
    if not parsed.scheme or not parsed.netloc:
        return False
    return (parsed.scheme, parsed.netloc) == (request.url.scheme, request.url.netloc)


def _csrf_rejection() -> PlainTextResponse:
    return PlainTextResponse("Forbidden: this request did not come from the same site.", status_code=403)


# ------------------------------------------------------------------------------------- helpers
def _me(request: Request) -> dict[str, Any] | None:
    """The signed-in visitor, or `None`. A non-200 from `/v1/me` (no cookie, expired session,
    revoked session) means "not signed in" here, exactly as it does in `web/auth.py::account`."""
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return None
    result = get_api(request).get_result("/v1/me", cookies={SESSION_COOKIE_NAME: cookie})
    if result.status_code != 200:
        return None
    data = result.body.get("data")
    return data if isinstance(data, dict) else None


def _billing_configured(request: Request) -> bool:
    """Whether a real payment processor is wired, read from `GET /v1/health`'s
    `checks.billing_configured`. The web host is a separate deployable from the API and cannot
    see the processor secret, so asking is the only way it can know.

    This exists so the page does not offer "Continue to payment" when nothing can take a payment.
    `POST /pricing/checkout` still refuses a dry-run URL afterwards -- that guard is the backstop
    and is not replaced by this.

    **An unreadable health check assumes payments ARE live, deliberately.** The two failure
    directions are not symmetric: guessing "live" when it is not costs the visitor one press and
    an honest explanation from the backstop, while guessing "not live" when it is would hide
    checkout from a paying customer with nothing to catch it.
    """
    try:
        health = getattr(request.app.state, "billing_configured", None)
        if health is None:
            body = get_api(request).get("/v1/health")
            health = bool(body["checks"]["billing_configured"])
            request.app.state.billing_configured = health
        return bool(health)
    except Exception:
        return True


def _notice(title: str, detail: str | None = None, *, request_id: str | None = None) -> dict[str, Any]:
    """An error the reader can act on: what went wrong, and what to do about it (docs/31 §6 —
    the RFC 9457 `title` and `request_id`, never a raw exception)."""
    return {"title": title, "detail": detail, "request_id": request_id}


def _problem_notice(body: dict[str, Any], fallback_title: str) -> dict[str, Any]:
    return _notice(
        str(body.get("title") or fallback_title),
        str(body["detail"]) if body.get("detail") else None,
        request_id=str(body["request_id"]) if body.get("request_id") else None,
    )


def _is_missing_email_conflict(status_code: int, body: dict[str, Any]) -> bool:
    """`services/billing/router.py` raises `ProblemError("conflict", "An email address is required
    before checkout")`. Matched on the problem `code` plus the word, rather than the exact
    sentence, so a reworded title still lands on the right explanation."""
    return status_code == 409 and "email" in str(body.get("title", "")).lower()


def _selected_plan(raw: str | None) -> str | None:
    """`/pricing?plan=pro` — where a visitor lands after registering from a tier button. Anything
    that is not a tier this page sells is simply ignored."""
    return raw if raw in PURCHASABLE else None


def _register_link(plan: str, *, path: str = "/register") -> str:
    """`?next=/pricing?plan=…` so the round trip through registration returns to the tier the
    visitor picked (`web/auth.py::_safe_next` accepts a path with a query, and rejects anything
    that leaves this site)."""
    return f"{path}?next={quote(f'/pricing?plan={plan}', safe='')}"


def _context(
    request: Request,
    *,
    selected_plan: str | None = None,
    notice: dict[str, Any] | None = None,
    no_email: bool = False,
) -> dict[str, Any]:
    me = _me(request)
    user = me.get("user") if me else None
    tier = str(me.get("tier")) if me else None
    email = (user or {}).get("email") if isinstance(user, dict) else None
    posture = get_platform_posture(request)
    return {
        "tiers": TIERS,
        "coverage_line": COVERAGE_LINE,
        # Say payments are off before the button rather than after it (`_billing_configured`).
        "billing_configured": _billing_configured(request),
        # docs/26 §3 precondition (i): the paid tiers stay visible but inactive, and no checkout
        # button/form renders, while the platform posture is `noncommercial`. `posture` carries
        # the API's own sentence (`GET /v1/health`'s `posture_statement`) for the template to
        # print verbatim, the same rule `/about` and `/methodology` follow.
        "posture": posture,
        "paid_tiers_inactive": posture is not None and posture["value"] == "noncommercial",
        "signed_in": me is not None,
        "tier": tier,
        # `admin` is a manual grant, never derived from billing (docs/21 §3.13), so it is not a
        # subscription to manage; `pro` and `api` are what a paid plan resolves to.
        "subscribed": tier in ("pro", "api"),
        "email": email,
        # A signed-in user with no email cannot check out and the button would lie; the page says
        # so up front instead (`services/billing/README.md` decision #7).
        "no_email": no_email or (me is not None and not email),
        "no_email_title": NO_EMAIL_TITLE,
        "no_email_detail": NO_EMAIL_DETAIL,
        "selected_plan": selected_plan,
        "notice": notice,
        "sales_email": SALES_EMAIL,
        "support_email": SUPPORT_EMAIL,
        "register_links": {plan: _register_link(plan) for plan in PURCHASABLE},
        "login_links": {plan: _register_link(plan, path="/login") for plan in PURCHASABLE},
    }


def _render(
    request: Request,
    context: dict[str, Any],
    *,
    status_code: int = 200,
) -> Response:
    return templates.TemplateResponse(request, "pricing.html", context, status_code=status_code)


def _external_redirect(url: str) -> Response | None:
    """The processor's hosted page. Only an absolute `https://` URL is followed — the billing port
    is trusted, but a redirect target is exactly the place not to take a string on trust."""
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.netloc:
        return None
    return RedirectResponse(url=url, status_code=303)


def _is_dry_run_url(url: str) -> bool:
    """Whether the billing port that answered is a stand-in rather than a live processor.

    `build_billing_port()` falls back to `services/billing/fake.py`'s `InMemoryBilling` whenever
    no processor secret is configured (`services/billing/README.md` decision #1), and there is no
    real account yet, so this is the state a deploy is in today. The fake's URLs are hosted at
    `.invalid`, the TLD RFC 2606 reserves precisely so that it can never resolve — no live
    processor can ever be behind one. Sending a person to it would hand them a dead browser page
    where a payment form should be, which reads as a broken checkout rather than as a product
    that has not switched payments on yet. So this is checked, and the page says the true thing
    instead. It keys on the reserved TLD, not on any vendor name or adapter class, so the check
    stays correct whichever stand-in is wired.
    """
    host = urlsplit(url).hostname or ""
    return host == "invalid" or host.endswith(".invalid")


def _not_live_notice(action: str) -> dict[str, Any]:
    return _notice(
        "Payments are not switched on yet",
        f"We cannot take a payment on this site yet, so nothing was charged and nothing changed. "
        f"Email {SALES_EMAIL} and we will {action} by hand.",
    )


# ------------------------------------------------------------------------------------- pricing
@router.get("/pricing", response_class=HTMLResponse)
def pricing(request: Request) -> Response:
    return _render(request, _context(request, selected_plan=_selected_plan(request.query_params.get("plan"))))


# ------------------------------------------------------------------------------------ checkout
@router.post("/pricing/checkout", response_class=HTMLResponse)
def checkout(request: Request, plan: Annotated[str, Form()] = "") -> Response:
    """Signed in: hand off to `POST /v1/billing/checkout` and follow the session URL it returns.
    Signed out: register or sign in first, and come back to this tier.

    Refuses while the platform posture is `noncommercial` (docs/26 §3 precondition (i)): the page
    is simply re-rendered, `200`, with `paid_tiers_inactive` true so the persistent notice this
    module's docstring describes is what the visitor sees -- there is no separate error box to
    show and dismiss for a request that was never going to be honoured this way, and `200` fits a
    request the server understood and fully handled (nothing was wrong with it, and nothing about
    it will succeed later without a restart) better than a client error (`400`) or a redirect to
    a prerequisite step (`303`) would. This is a courtesy backstop: the button that would post
    here is already gone from the page under this posture (`web/templates/pricing.html`); the
    actual gate is `POST /v1/billing/checkout` (`services/billing/router.py`'s
    `PAID_TIERS_ACTIVE`), same as `_is_dry_run_url` above is a courtesy check ahead of a real one.
    """
    if not _is_same_origin(request):
        return _csrf_rejection()

    context = _context(request, selected_plan=_selected_plan(plan))
    if context["paid_tiers_inactive"]:
        return _render(request, context, status_code=200)

    if plan not in PURCHASABLE:
        return _render(
            request,
            _context(
                request,
                notice=_notice(
                    "That plan is not one you can buy here",
                    "Pick one of the plans below, or write to us about API and enterprise access.",
                ),
            ),
            status_code=400,
        )

    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url=_register_link(plan), status_code=303)

    tier = PURCHASABLE[plan]
    result = get_api(request).post(
        "/v1/billing/checkout",
        json={"plan": tier.plan, "seats": tier.seats},
        cookies={SESSION_COOKIE_NAME: cookie},
    )
    if result.status_code == 201:
        url = str((result.body.get("data") or {}).get("url") or "")
        if _is_dry_run_url(url):
            return _render(
                request,
                _context(request, selected_plan=plan, notice=_not_live_notice("set the subscription up")),
                status_code=503,
            )
        redirect = _external_redirect(url)
        if redirect is not None:
            return redirect
        return _render(
            request,
            _context(
                request,
                selected_plan=plan,
                notice=_notice(
                    "The payment page could not be opened",
                    "We got an answer we could not use from the payment provider. Nothing has been "
                    f"charged. Try again, or write to {SUPPORT_EMAIL} and we will set the "
                    "subscription up for you.",
                ),
            ),
            status_code=502,
        )

    if result.status_code in (401, 403):
        # The cookie went stale between rendering the page and pressing the button.
        return RedirectResponse(url=_register_link(plan, path="/login"), status_code=303)

    if _is_missing_email_conflict(result.status_code, result.body):
        return _render(
            request,
            _context(request, selected_plan=plan, no_email=True),
            status_code=409,
        )

    return _render(
        request,
        _context(
            request,
            selected_plan=plan,
            notice=_problem_notice(result.body, "The payment page could not be opened"),
        ),
        status_code=result.status_code if result.status_code >= 400 else 502,
    )


# -------------------------------------------------------------------------------------- portal
@router.post("/pricing/portal", response_class=HTMLResponse)
def portal(request: Request) -> Response:
    """Manage an existing subscription: `POST /v1/billing/portal` and follow the URL. Shown
    instead of a buy button once the account carries a paid entitlement.

    Unaffected by the platform posture: unlike `checkout` above, this route carries no
    `paid_tiers_inactive` gate and never will, on purpose. Managing a subscription an account
    already holds is not selling a new one, and the owner's "mark inactive, then flip" decision
    (docs/26-platform-posture.md §3 precondition (i); `docs/00-PLAN.md` 2026-09-26) was to stop
    *offering* paid tiers, not to strand anyone already on one — the same reasoning
    `services/billing/router.py::open_portal`'s docstring gives for the API route this hands off
    to."""
    if not _is_same_origin(request):
        return _csrf_rejection()
    cookie = request.cookies.get(SESSION_COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url="/login?next=/pricing", status_code=303)

    result = get_api(request).post("/v1/billing/portal", cookies={SESSION_COOKIE_NAME: cookie})
    if result.status_code == 200:
        url = str((result.body.get("data") or {}).get("url") or "")
        if _is_dry_run_url(url):
            return _render(
                request,
                _context(request, notice=_not_live_notice("make the change")),
                status_code=503,
            )
        redirect = _external_redirect(url)
        if redirect is not None:
            return redirect
        return _render(
            request,
            _context(
                request,
                notice=_notice(
                    "The billing page could not be opened",
                    "We got an answer we could not use from the payment provider. Nothing has "
                    f"changed on your subscription. Try again, or write to {SUPPORT_EMAIL}.",
                ),
            ),
            status_code=502,
        )

    if result.status_code in (401, 403):
        return RedirectResponse(url="/login?next=/pricing", status_code=303)

    return _render(
        request,
        _context(
            request,
            notice=_problem_notice(result.body, "The billing page could not be opened"),
        ),
        status_code=result.status_code if result.status_code >= 400 else 502,
    )
