"""In-memory `CrmPort` (docs/34-crm-system-of-record.md) used by tests directly and, through
`services.crm.attio.build_crm_port`, as the process-wide dry-run adapter when `ATTIO_API_KEY` is
unset. Unlike a mock, every method has real read-back semantics: `find_company`/`get_company`
return what `upsert_company` actually wrote, `create_lead_signal` is genuinely idempotent on
`signal_id`, and `parse_webhook` runs the same HMAC verification `AttioCrmAdapter` does (against
`webhook_secret`, default `"test-webhook-secret"` — a low-entropy fake per CLAUDE.md, never a real
secret) so router tests exercise the real signature path rather than a stub.

`sor_kind = "attio_fake"`, not `"attio"` (README decision D-1): this is a stand-in, and a
`match.crm_lead_ref`/`account.sor_kind` written while it is active should never be mistaken for a
real Attio reference once the workspace is wired up.
"""

from __future__ import annotations

import hashlib
import hmac
import itertools
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from services.sor.ports import (
    ActivityNote,
    Company,
    CompanyRef,
    CompanyUpsert,
    ContactPolicy,
    CrmChange,
    DealCreate,
    DealRef,
    LeadSignal,
    LeadSignalRef,
    SubscriptionMirror,
    WebhookRejected,
)


@dataclass
class _CompanyRow:
    record_id: str
    domain: str
    name: str | None = None
    do_not_contact: bool = False
    consent_basis: str | None = None
    stage: str | None = None
    platform_org_id: str | None = None
    platform_account_id: str | None = None
    lead_score: int | None = None
    lead_band: str | None = None
    top_event: str | None = None
    top_event_url: str | None = None
    top_event_at: datetime | None = None


@dataclass
class _SignalRow:
    record_id: str
    signal: LeadSignal
    company_ref: CompanyRef | None


@dataclass
class _DealRow:
    record_id: str
    deal: DealCreate
    company_ref: CompanyRef | None
    signal_record_id: str | None


@dataclass
class _NoteRow:
    record_id: str
    parent_kind: str  # "company" | "deal"
    parent_ref: str
    title: str
    body: str


@dataclass
class _SubscriptionRow:
    record_id: str
    subscription: SubscriptionMirror
    company_ref: CompanyRef | None


@dataclass
class _TaskRow:
    record_id: str
    email: str
    reason: str


@dataclass
class _UnmatchedEntry:
    entry_id: str
    signal_record_id: str


class InMemoryCrm:
    """Dict-backed fake. Every store is a public attribute (task item 2: "expose the stores as
    public attributes so other suites ... can assert on them") — `companies`, `signals`, `deals`,
    `notes`, `subscriptions`, `tasks`, `unmatched`."""

    sor_kind = "attio_fake"

    def __init__(self, *, webhook_secret: str = "test-webhook-secret") -> None:  # noqa: S107
        self._webhook_secret = webhook_secret
        self.companies: dict[str, _CompanyRow] = {}  # domain -> row
        self._companies_by_id: dict[str, _CompanyRow] = {}
        self.signals: dict[str, _SignalRow] = {}  # signal_id -> row
        self.deals: dict[str, _DealRow] = {}  # record_id -> row
        self.notes: dict[str, _NoteRow] = {}  # record_id -> row
        self.subscriptions: dict[str, _SubscriptionRow] = {}  # stripe_subscription_id -> row
        self.tasks: dict[str, _TaskRow] = {}  # record_id -> row
        self.unmatched: list[_UnmatchedEntry] = []
        self._ids = itertools.count(1)

    def _next_id(self, prefix: str) -> str:
        return f"{prefix}_{next(self._ids)}"

    # ------------------------------------------------------------------------- company lookups
    def _find_company_row(
        self, *, domain: str | None = None, platform_org_id: str | None = None
    ) -> _CompanyRow | None:
        if domain and domain in self.companies:
            return self.companies[domain]
        if platform_org_id:
            for row in self.companies.values():
                if row.platform_org_id == platform_org_id:
                    return row
        return None

    def _find_company_by_account(self, platform_account_id: str) -> _CompanyRow | None:
        for row in self.companies.values():
            if row.platform_account_id == platform_account_id:
                return row
        return None

    @staticmethod
    def _row_to_company(row: _CompanyRow) -> Company:
        return Company(
            ref=CompanyRef(sor_kind="attio_fake", sor_ref=row.record_id),
            domain=row.domain,
            name=row.name,
            do_not_contact=row.do_not_contact,
            consent_basis=row.consent_basis,
            stage=row.stage,
            platform_org_id=row.platform_org_id,
            platform_account_id=row.platform_account_id,
        )

    # ---------------------------------------------------------------------------------- CrmPort
    def upsert_company(self, company: CompanyUpsert) -> CompanyRef:
        """Mirrors `AttioCrmAdapter.upsert_company`: only the adapter-owned fields are written,
        `CompanyUpsert.name` is ignored (docs/34 §5, README decision D-2), and the row is created
        on first sight of the domain or patched in place on a later call."""
        row = self.companies.get(company.domain)
        if row is None:
            row = _CompanyRow(record_id=self._next_id("cmp"), domain=company.domain)
            self.companies[company.domain] = row
            self._companies_by_id[row.record_id] = row
        for field_name in (
            "platform_org_id",
            "platform_account_id",
            "lead_score",
            "lead_band",
            "top_event",
            "top_event_url",
            "top_event_at",
        ):
            value = getattr(company, field_name)
            if value is not None:
                setattr(row, field_name, value)
        return CompanyRef(sor_kind="attio_fake", sor_ref=row.record_id)

    def get_company(self, ref: CompanyRef) -> Company | None:
        row = self._companies_by_id.get(ref.sor_ref)
        return self._row_to_company(row) if row is not None else None

    def find_company(
        self, *, domain: str | None = None, platform_org_id: str | None = None
    ) -> Company | None:
        row = self._find_company_row(domain=domain, platform_org_id=platform_org_id)
        return self._row_to_company(row) if row is not None else None

    def contact_policy(self, *, domain: str) -> ContactPolicy:
        row = self.companies.get(domain)
        if row is None:
            return ContactPolicy(do_not_contact=False, consent_basis=None)
        return ContactPolicy(do_not_contact=row.do_not_contact, consent_basis=row.consent_basis)

    def create_lead_signal(self, signal: LeadSignal) -> LeadSignalRef:
        company_row = None
        if signal.company_domain:
            company_row = self._find_company_row(domain=signal.company_domain)
        if company_row is None and signal.platform_org_id:
            company_row = self._find_company_row(platform_org_id=signal.platform_org_id)
        company_ref = (
            CompanyRef(sor_kind="attio_fake", sor_ref=company_row.record_id)
            if company_row is not None
            else None
        )

        existing = self.signals.get(signal.signal_id)
        record_id = existing.record_id if existing is not None else self._next_id("sig")
        self.signals[signal.signal_id] = _SignalRow(
            record_id=record_id, signal=signal, company_ref=company_ref
        )

        if company_ref is not None:
            return LeadSignalRef(
                sor_kind="attio_fake", sor_ref=record_id, company=company_ref, unmatched=False
            )

        if not any(e.signal_record_id == record_id for e in self.unmatched):
            self.unmatched.append(
                _UnmatchedEntry(entry_id=self._next_id("entry"), signal_record_id=record_id)
            )
        return LeadSignalRef(sor_kind="attio_fake", sor_ref=record_id, company=None, unmatched=True)

    def create_deal(self, deal: DealCreate) -> DealRef:
        company_row = None
        if deal.company_domain:
            company_row = self._find_company_row(domain=deal.company_domain)
        if company_row is None and deal.platform_org_id:
            company_row = self._find_company_row(platform_org_id=deal.platform_org_id)
        company_ref = (
            CompanyRef(sor_kind="attio_fake", sor_ref=company_row.record_id)
            if company_row is not None
            else None
        )

        signal_record_id = None
        if deal.originating_signal_id:
            signal_row = self.signals.get(deal.originating_signal_id)
            if signal_row is not None:
                signal_record_id = signal_row.record_id

        record_id = self._next_id("deal")
        self.deals[record_id] = _DealRow(
            record_id=record_id, deal=deal, company_ref=company_ref, signal_record_id=signal_record_id
        )
        note_id = self._next_id("note")
        self.notes[note_id] = _NoteRow(
            record_id=note_id,
            parent_kind="deal",
            parent_ref=record_id,
            title="Lead hand-off from platform",
            body=(
                f"Proposal: {deal.proposal_public_id}\nOpportunity: {deal.opportunity_public_id}\n"
                f"Score: {deal.score}\nRationale: {deal.rationale}\nLink: {deal.link_url}"
            ),
        )
        return DealRef(sor_kind="attio_fake", sor_ref=record_id)

    def log_activity(self, company: CompanyRef, note: ActivityNote) -> str:
        note_id = self._next_id("note")
        self.notes[note_id] = _NoteRow(
            record_id=note_id,
            parent_kind="company",
            parent_ref=company.sor_ref,
            title=note.kind,
            body=note.body,
        )
        return note_id

    def upsert_subscription(self, subscription: SubscriptionMirror) -> str:
        company_row = None
        if subscription.platform_account_id:
            company_row = self._find_company_by_account(subscription.platform_account_id)
        if company_row is None and subscription.company_domain:
            company_row = self._find_company_row(domain=subscription.company_domain)
        company_ref = (
            CompanyRef(sor_kind="attio_fake", sor_ref=company_row.record_id)
            if company_row is not None
            else None
        )
        existing = self.subscriptions.get(subscription.stripe_subscription_id)
        record_id = existing.record_id if existing is not None else self._next_id("sub")
        self.subscriptions[subscription.stripe_subscription_id] = _SubscriptionRow(
            record_id=record_id, subscription=subscription, company_ref=company_ref
        )
        return record_id

    def request_personal_data_deletion(self, *, email: str, reason: str) -> str:
        """Never deletes anything (docs/34 §5 "Never") — opens a task for a human, same as the
        real adapter. There is deliberately no `delete_*` method anywhere on this class
        (`services/crm/test_fake.py` asserts the port surface has none)."""
        task_id = self._next_id("task")
        self.tasks[task_id] = _TaskRow(record_id=task_id, email=email, reason=reason)
        return task_id

    def parse_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> list[CrmChange]:
        """Same verification scheme as `AttioCrmAdapter.parse_webhook` (HMAC-SHA256 hex digest of
        the raw body, header `Attio-Signature` falling back to `X-Attio-Signature`), so router
        tests exercise real signature checking against a known fake secret rather than a stub
        that always accepts. Event shape: `{"events": [...]}` or a single event dict, each
        `{"event_type", "id": {"object_id", "record_id"}}` — `object_id` is treated directly as
        the object slug (`"companies"` / `"lead_signals"`) since the fake has no vendor UUIDs to
        map through `ATTIO_OBJECT_IDS`."""
        signature = None
        for key, value in headers.items():
            if key.lower() in ("attio-signature", "x-attio-signature"):
                signature = value
                break
        if not signature:
            raise WebhookRejected("missing Attio-Signature header")
        expected = hmac.new(self._webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.strip().lower(), expected):
            raise WebhookRejected("signature verification failed")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebhookRejected("invalid JSON body") from exc

        raw_events: list[Any]
        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            raw_events = payload["events"]
        else:
            raw_events = [payload]

        changes: list[CrmChange] = []
        for raw_event in raw_events:
            if not isinstance(raw_event, dict):
                continue
            event_type = raw_event.get("event_type")
            id_obj = raw_event.get("id") or {}
            object_id = id_obj.get("object_id")
            record_id = id_obj.get("record_id")
            if not isinstance(object_id, str) or not isinstance(record_id, str):
                continue
            if object_id == "companies" and event_type == "record.updated":
                kind = "company.updated"
            elif object_id == "lead_signals" and event_type in ("record.updated", "record.created"):
                kind = "lead_signal.updated"
            else:
                continue
            changes.append(
                CrmChange(
                    kind=kind,
                    sor_ref=record_id,
                    occurred_at=datetime.now(UTC),
                    fields={"actor": raw_event.get("actor"), "event_type": event_type},
                )
            )
        return changes


__all__ = ["InMemoryCrm"]
