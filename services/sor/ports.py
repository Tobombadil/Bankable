"""The CRM and billing ports (docs/20 §9, ADR 0006) in the platform's vocabulary.

Owned by the coordinator; adapters implement these Protocols, everything else depends on them.

Why the port surface differs from the ADR's one-line sketch (`list_changes(since)`,
`upsert_contact`), recorded here rather than silently:

- `docs/34-crm-system-of-record.md` §5 fixes what the CRM adapter may do: it never creates People,
  so there is no `upsert_contact`; it receives changes by inbound webhook, so `list_changes(since)`
  becomes `parse_webhook(...) -> list[CrmChange]`.
- Deletion requests (US-910, ADR 0006 "fan out through the port") meet docs/34's "never deletes
  anything": the port *requests* deletion by opening a task for a human (`request_personal_data_
  deletion`), it does not delete.
- Entitlement is derived in one place (`entitlement_for`) so the billing router, the fake and the
  admin panel cannot disagree about what a Stripe status means (docs/34 §1: entitlement comes
  from Stripe, never from a human-editable CRM field).

Every value here is vendor-neutral. The only vendor-shaped values the app stores are
`account.sor_ref`, `account.billing_ref`, `subscription.sor_ref` and `match.crm_lead_ref`
(ADR 0006 "no vendor field outside the adapter").
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

# ------------------------------------------------------------------------------------ vocabularies
#: docs/34 §2.1 `lead_band`.
LEAD_BANDS = ("hot", "warm", "watch")
#: docs/34 §2.4 `event_type` on a Lead Signal.
SIGNAL_EVENT_TYPES = (
    "proposal.new",
    "proposal.status_changed",
    "proposal.withdrawn",
    "opportunity.rfp_opened",
    "opportunity.rfp_closing",
    "opportunity.awarded",
    "funding.cancelled",
    "funding.reinstated",
    "match.new",
)
#: docs/34 §2.4 `subject_kind`.
SIGNAL_SUBJECT_KINDS = ("proposal", "opportunity", "match")
#: docs/34 §2.3 / §2.5 plan vocabulary (the commercial name of what was bought).
PLAN_TIERS = ("pro", "team", "api", "enterprise")
#: docs/21 §3.14 `subscription.status`.
SUBSCRIPTION_STATUSES = ("trialing", "active", "past_due", "paused", "canceled")
#: docs/21 §3.13 `account.entitlement`; `admin` is never derived from billing.
ENTITLEMENTS = ("public", "pro", "api")

#: Which entitlement each purchased plan grants while the subscription is in good standing.
PLAN_ENTITLEMENT: Mapping[str, str] = {"pro": "pro", "team": "pro", "api": "api", "enterprise": "api"}
#: Subscription statuses that keep the entitlement. `past_due` keeps it: Stripe retries the card
#: for days and the customer portal is the fix; cutting access on the first failed charge loses
#: paying customers over a bounced card (docs/11 §3 dunning default). `paused`/`canceled` drop it.
STATUSES_IN_GOOD_STANDING = frozenset({"trialing", "active", "past_due"})


def entitlement_for(plan_tier: str, status: str) -> str:
    """The one rule that turns a billing state into `account.entitlement`."""
    if status in STATUSES_IN_GOOD_STANDING and plan_tier in PLAN_ENTITLEMENT:
        return PLAN_ENTITLEMENT[plan_tier]
    return "public"


# ----------------------------------------------------------------------------------------- errors
class SorError(Exception):
    """Base class for anything a system of record did that the platform must surface."""


class SorUnavailable(SorError):
    """Network failure, 5xx, or a rate limit that outlived the retry budget → `503 sor_unavailable`
    (api/openapi.yaml `SorUnavailable`; docs/20 §12 fail-open rules apply to cached entitlements)."""


class SorRejected(SorError):
    """The vendor understood the call and refused it (4xx other than 429): a validation or
    configuration error on our side, never retried blindly. Surfaces as `409 conflict` or a 500,
    at the router's discretion, with the vendor message in the log, not in the response body."""


class WebhookRejected(SorError):
    """Inbound webhook failed signature or timestamp verification. Routers answer 401 and never
    process the body."""


# -------------------------------------------------------------------------------------------- CRM
@dataclass(frozen=True)
class CompanyRef:
    sor_kind: str  #: `attio`
    sor_ref: str  #: vendor record id, stored on `account.sor_ref`


@dataclass(frozen=True)
class CompanyUpsert:
    """docs/34 §5: upsert by primary `domain`; only the adapter-owned fields are set. Human-owned
    fields (segment, stage, do_not_contact, ...) are never written from here."""

    domain: str
    name: str | None = None
    platform_org_id: str | None = None
    platform_account_id: str | None = None
    lead_score: int | None = None
    lead_band: str | None = None
    top_event: str | None = None
    top_event_url: str | None = None
    top_event_at: dt.datetime | None = None


@dataclass(frozen=True)
class Company:
    """What the platform is allowed to read back (docs/34 §5 "Reads")."""

    ref: CompanyRef
    domain: str | None
    name: str | None
    do_not_contact: bool
    consent_basis: str | None
    stage: str | None
    platform_org_id: str | None = None
    platform_account_id: str | None = None


@dataclass(frozen=True)
class ContactPolicy:
    """Read before any draft is generated for a company (docs/34 §5). `allowed` is the only field
    callers should branch on; the rest is for the audit line."""

    do_not_contact: bool
    consent_basis: str | None

    @property
    def allowed(self) -> bool:
        return not self.do_not_contact and self.consent_basis not in (None, "none")


@dataclass(frozen=True)
class LeadSignal:
    """One platform event that makes an account timely (docs/34 §2.4). `signal_id` is the platform
    event public id and the idempotency key."""

    signal_id: str
    event_type: str
    subject_kind: str
    subject_name: str
    subject_url: str
    observed_at: dt.datetime
    score: int
    rationale: str
    company_domain: str | None = None
    platform_org_id: str | None = None
    jurisdiction: str | None = None
    technology: str | None = None
    capacity_mw: float | None = None


@dataclass(frozen=True)
class LeadSignalRef:
    sor_kind: str
    sor_ref: str
    company: CompanyRef | None  #: None when the signal landed on the "Unmatched signals" list
    unmatched: bool


@dataclass(frozen=True)
class DealCreate:
    """The US-403 hand-off. The CRM gets ids, a score, a one-line rationale and a link back;
    never a copy of the records (docs/34 §1)."""

    name: str
    proposal_public_id: str
    opportunity_public_id: str
    score: float
    rationale: str
    link_url: str
    company_domain: str | None = None
    platform_org_id: str | None = None
    originating_signal_id: str | None = None
    tier: str | None = None


@dataclass(frozen=True)
class DealRef:
    sor_kind: str
    sor_ref: str  #: stored on `match.crm_lead_ref`


@dataclass(frozen=True)
class ActivityNote:
    """An activity note on a company (docs/34 §5). `kind` is `alert_delivered` when an alert email
    was sent and `alert_open` only when a tracking link fired; engagement is never inferred."""

    kind: str
    title: str
    body: str
    occurred_at: dt.datetime


@dataclass(frozen=True)
class SubscriptionMirror:
    """docs/34 §2.5: the read-only Subscription record the CRM shows humans, written from Stripe."""

    stripe_subscription_id: str
    stripe_customer_id: str
    platform_account_id: str
    plan: str
    seats: int
    status: str
    current_period_end: dt.datetime | None
    mrr: float | None
    company_domain: str | None = None


@dataclass(frozen=True)
class CrmChange:
    """One inbound change from the CRM (docs/34 §5 "Inbound webhooks")."""

    kind: str  #: `company.updated` | `lead_signal.updated`
    sor_ref: str
    occurred_at: dt.datetime
    fields: Mapping[str, Any] = field(default_factory=dict)


class CrmPort(Protocol):
    sor_kind: str

    def upsert_company(self, company: CompanyUpsert) -> CompanyRef: ...

    def get_company(self, ref: CompanyRef) -> Company | None: ...

    def find_company(
        self, *, domain: str | None = None, platform_org_id: str | None = None
    ) -> Company | None: ...

    def contact_policy(self, *, domain: str) -> ContactPolicy: ...

    def create_lead_signal(self, signal: LeadSignal) -> LeadSignalRef: ...

    def create_deal(self, deal: DealCreate) -> DealRef: ...

    def log_activity(self, company: CompanyRef, note: ActivityNote) -> str: ...

    def upsert_subscription(self, subscription: SubscriptionMirror) -> str: ...

    def request_personal_data_deletion(self, *, email: str, reason: str) -> str: ...

    def parse_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> list[CrmChange]: ...


# ---------------------------------------------------------------------------------------- billing
@dataclass(frozen=True)
class CheckoutRequest:
    account_public_id: str
    customer_email: str
    plan: str
    seats: int
    success_url: str
    cancel_url: str
    billing_ref: str | None = None  #: existing customer id; the adapter creates one when None


@dataclass(frozen=True)
class CheckoutSession:
    url: str
    session_ref: str
    billing_ref: str  #: customer id to persist on `account.billing_ref` (created if it was None)


@dataclass(frozen=True)
class SubscriptionCreate:
    """An operator-created subscription (`POST /admin/v1/subscriptions`, US-902 AC2): the vendor
    invoices the customer rather than taking a card at checkout. The mirror is refreshed from the
    adapter's read-back (`SubscriptionState`), never from this request."""

    billing_ref: str
    plan: str
    seats: int
    trial_days: int | None = None
    account_public_id: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class PortalSession:
    url: str


@dataclass(frozen=True)
class SubscriptionState:
    """docs/21 §3.14 minus the mirror bookkeeping the store adds (`mirrored_at`, `drift_flag`)."""

    sor_kind: str
    sor_ref: str
    billing_ref: str
    plan_code: str
    plan_tier: str
    status: str
    seats: int
    current_period_start: dt.datetime
    current_period_end: dt.datetime
    currency: str
    cancel_at: dt.datetime | None = None
    mrr_amount: float | None = None


@dataclass(frozen=True)
class Invoice:
    ref: str
    status: str
    amount_due: float
    currency: str
    created_at: dt.datetime
    hosted_url: str | None = None


@dataclass(frozen=True)
class EntitlementChange:
    """What a billing webhook means for one account. `event_ref` is the vendor event id and the
    idempotency key; `entitlement` is already resolved through `entitlement_for`."""

    event_ref: str
    occurred_at: dt.datetime
    billing_ref: str
    subscription: SubscriptionState
    entitlement: str
    account_public_id: str | None = None  #: when the vendor echoed our `client_reference_id`


class BillingPort(Protocol):
    sor_kind: str

    def create_checkout(self, request: CheckoutRequest) -> CheckoutSession: ...

    def open_portal(self, *, billing_ref: str, return_url: str) -> PortalSession: ...

    def create_subscription(self, request: SubscriptionCreate) -> SubscriptionState: ...

    def get_subscription(self, ref: str) -> SubscriptionState | None: ...

    def list_invoices(self, *, billing_ref: str) -> list[Invoice]: ...

    def handle_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> list[EntitlementChange]: ...
