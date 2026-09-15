"""Attio CRM adapter behind `services.sor.ports.CrmPort` (docs/34-crm-system-of-record.md).

Decisions and deviations from the vendor evidence are recorded in `services/crm/README.md`, not
repeated here; this module states only what the code does. Key structural choices:

- A single synchronous `httpx.Client` with an injectable `transport` (tests use
  `httpx.MockTransport`, never live network) and an injectable `sleep`/`clock` so retry and
  timestamp behaviour is deterministic in tests.
- Every Attio write wraps each attribute value in a list — the vendor's `values`/`entry_values`
  object schemas declare `additionalProperties: {"type": "array"}` (verified against
  `attio-openapi.json`), so single-select attributes are written as one-element arrays exactly
  like multi-select ones.
- `build_crm_port()` is the one factory `services.sor.wiring.get_crm_port` imports: it returns a
  live adapter when `ATTIO_API_KEY` is set, otherwise a process-wide `InMemoryCrm` (dry run),
  logged once at INFO so a misconfigured deployment is loud in logs without failing requests.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from services.crm.fake import InMemoryCrm
from services.sor.ports import (
    ActivityNote,
    Company,
    CompanyRef,
    CompanyUpsert,
    ContactPolicy,
    CrmChange,
    CrmPort,
    DealCreate,
    DealRef,
    LeadSignal,
    LeadSignalRef,
    SorRejected,
    SorUnavailable,
    SubscriptionMirror,
    WebhookRejected,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------------- object slugs
#: docs/34 §2, assumption A-34-1 (see services/crm/README.md): the owner is instructed to use
#: these exact slugs when creating the two custom objects and the "Unmatched signals" list, so the
#: adapter treats them as constants rather than configuration.
COMPANIES = "companies"
DEALS = "deals"
LEAD_SIGNALS = "lead_signals"
SUBSCRIPTIONS = "subscriptions"
UNMATCHED_SIGNALS_LIST = "unmatched_signals"

#: Retry-After is capped in code regardless of what the vendor sends (task spec; also keeps tests
#: fast) and every 429/5xx retry sleeps through the injectable `sleep` callable, never `time.sleep`
#: directly, so tests never actually wait.
_MAX_RETRY_SLEEP_SECONDS = 5.0
_MAX_429_RETRIES = 3

#: docs/34 §5: exactly these `CompanyUpsert` fields are adapter-owned; `name` is deliberately
#: excluded (see README decision D-2) even though the port dataclass carries it.
_ADAPTER_OWNED_COMPANY_FIELDS = (
    "platform_org_id",
    "platform_account_id",
    "lead_score",
    "lead_band",
    "top_event",
    "top_event_url",
    "top_event_at",
)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _get_header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive lookup; the port's `parse_webhook` takes a plain `Mapping[str, str]`, not
    FastAPI's case-insensitive `Headers`, so callers (and tests) may pass either casing."""
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _scalar_from_entry(entry: Mapping[str, Any]) -> Any:
    """One value-history entry (docs from `attio-openapi.json`'s record GET response: each entry
    carries `active_from`/`active_until`/`created_by_actor` plus type-specific fields) reduced to
    the plain scalar the platform reads. Tolerant of an unrecognised `attribute_type` — returns
    whatever is under `value` so a fresh field the owner adds does not crash a read."""
    attribute_type = entry.get("attribute_type")
    if attribute_type in ("select", "status"):
        option = entry.get("option") or entry.get("status") or {}
        return option.get("title")
    if attribute_type == "domain":
        return entry.get("domain")
    if attribute_type == "record-reference":
        return entry.get("target_record_id")
    return entry.get("value")


def _first_active(values: Mapping[str, Any], key: str) -> Any | None:
    """The first entry on `key` whose `active_until` is still null (docs/34 §5 "Reads"); Attio
    keeps a value's full history, but the platform only ever wants the current one."""
    for entry in values.get(key) or []:
        if entry.get("active_until") is None:
            return _scalar_from_entry(entry)
    return None


def _company_values(company: CompanyUpsert) -> dict[str, list[Any]]:
    """docs/34 §5: only the adapter-owned attributes that are not `None`, plus `domains` always
    (the match key). See `_ADAPTER_OWNED_COMPANY_FIELDS` for exactly which fields those are —
    `CompanyUpsert.name` is a human-owned field on Attio's Company object and is never sent
    (README decision D-2)."""
    values: dict[str, list[Any]] = {"domains": [{"domain": company.domain}]}
    for field_name in _ADAPTER_OWNED_COMPANY_FIELDS:
        value = getattr(company, field_name)
        if value is None:
            continue
        if isinstance(value, datetime):
            values[field_name] = [_iso(value)]
        else:
            values[field_name] = [value]
    return values


def _company_from_record(record: Mapping[str, Any]) -> Company:
    record_id: str = record["id"]["record_id"]
    values = record.get("values", {})
    return Company(
        ref=CompanyRef(sor_kind="attio", sor_ref=record_id),
        domain=_first_active(values, "domains"),
        name=_first_active(values, "name"),
        do_not_contact=bool(_first_active(values, "do_not_contact") or False),
        consent_basis=_first_active(values, "consent_basis"),
        stage=_first_active(values, "stage"),
        platform_org_id=_first_active(values, "platform_org_id"),
        platform_account_id=_first_active(values, "platform_account_id"),
    )


class AttioCrmAdapter:
    """`services.sor.ports.CrmPort` implementation for Attio (docs/34). Every write is idempotent
    on the key documented in docs/34 §2 (`PUT .../records?matching_attribute=...`); every read
    goes through `_request`'s retry/error handling so callers never see an `httpx` exception."""

    sor_kind = "attio"

    def __init__(
        self,
        api_key: str,
        *,
        base_url: str = "https://api.attio.com",
        transport: httpx.BaseTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], datetime] | None = None,
        webhook_secret: str | None = None,
        object_ids: Mapping[str, str] | None = None,
    ) -> None:
        self._client = httpx.Client(
            base_url=base_url,
            transport=transport,
            timeout=30.0,
            headers={"Authorization": f"Bearer {api_key}"},
        )
        self._sleep = sleep
        self._clock: Callable[[], datetime] = clock or (lambda: datetime.now(UTC))
        self._webhook_secret = webhook_secret
        #: slug -> Attio object UUID (`ATTIO_OBJECT_IDS`, docs/34 §2 "exact slugs" — the webhook
        #: payload identifies objects by UUID, never by slug; see README A-34-1).
        self._object_ids: dict[str, str] = dict(object_ids or {})

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------------------- HTTP core
    def _retry_after_seconds(self, header_value: str | None) -> float:
        if not header_value:
            return 1.0
        try:
            return max(0.0, float(header_value))
        except ValueError:
            pass
        try:
            when = parsedate_to_datetime(header_value)
        except (TypeError, ValueError):
            return 1.0
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        delta = (when - self._clock()).total_seconds()
        return max(0.0, delta)

    def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: Mapping[str, str] | None = None,
        treat_404_as_none: bool = False,
    ) -> dict[str, Any] | None:
        retries_429 = 0
        retried_5xx = False
        retried_transport = False
        while True:
            try:
                resp = self._client.request(method, path, json=json_body, params=params)
            except httpx.TransportError as exc:
                if retried_transport:
                    logger.warning("attio_transport_error method=%s path=%s error=%s", method, path, exc)
                    raise SorUnavailable(f"transport error calling attio {method} {path}") from exc
                retried_transport = True
                logger.warning("attio_transport_retry method=%s path=%s error=%s", method, path, exc)
                continue

            if resp.status_code == 429:
                if retries_429 >= _MAX_429_RETRIES:
                    logger.warning("attio_rate_limited_exhausted method=%s path=%s", method, path)
                    raise SorUnavailable(f"attio rate limited after retries: {method} {path}")
                wait = min(
                    self._retry_after_seconds(resp.headers.get("Retry-After")), _MAX_RETRY_SLEEP_SECONDS
                )
                logger.info("attio_rate_limited method=%s path=%s wait=%.2f", method, path, wait)
                self._sleep(wait)
                retries_429 += 1
                continue

            if resp.status_code >= 500:
                if retried_5xx:
                    logger.warning(
                        "attio_server_error method=%s path=%s status=%s", method, path, resp.status_code
                    )
                    raise SorUnavailable(f"attio server error {resp.status_code}: {method} {path}")
                retried_5xx = True
                logger.warning(
                    "attio_server_error_retry method=%s path=%s status=%s", method, path, resp.status_code
                )
                continue

            if resp.status_code == 404 and treat_404_as_none:
                return None

            if resp.status_code >= 400:
                logger.warning("attio_rejected method=%s path=%s status=%s", method, path, resp.status_code)
                raise SorRejected(
                    f"attio rejected {method} {path} status={resp.status_code} body={resp.text[:500]!r}"
                )

            if not resp.content:
                return {}
            result: dict[str, Any] = resp.json()
            return result

    def _query_company(self, attribute: str, value: str) -> dict[str, Any] | None:
        """Internal helper shared by `find_company`, `create_lead_signal`, `create_deal` and
        `upsert_subscription`: `platform_account_id` is not part of the public `find_company`
        signature (docs/34 §5 only reads by domain/`platform_org_id` there) but the Subscription
        mirror needs it too, so this is kept private rather than widening the `CrmPort` Protocol.
        Verbose filter grammar, verified against Attio's filtering-and-sorting guide
        (README decision D-10): an explicit `$eq` on the attribute (or, for `domains`, on its
        `domain` property) rather than the shorthand form, which the guide shows only for a bare
        top-level value with no property nesting."""
        filter_value: Any = {"domain": {"$eq": value}} if attribute == "domains" else {"$eq": value}
        body = self._request(
            "POST",
            f"/v2/objects/{COMPANIES}/records/query",
            json_body={"filter": {attribute: filter_value}, "limit": 1},
        )
        rows = (body or {}).get("data") or []
        return rows[0] if rows else None

    # ------------------------------------------------------------------------------- CrmPort
    def upsert_company(self, company: CompanyUpsert) -> CompanyRef:
        body = self._request(
            "PUT",
            f"/v2/objects/{COMPANIES}/records",
            params={"matching_attribute": "domains"},
            json_body={"data": {"values": _company_values(company)}},
        )
        assert body is not None  # noqa: S101 - `treat_404_as_none` was not requested
        record_id: str = body["data"]["id"]["record_id"]
        return CompanyRef(sor_kind="attio", sor_ref=record_id)

    def get_company(self, ref: CompanyRef) -> Company | None:
        body = self._request("GET", f"/v2/objects/{COMPANIES}/records/{ref.sor_ref}", treat_404_as_none=True)
        if body is None:
            return None
        return _company_from_record(body["data"])

    def find_company(
        self, *, domain: str | None = None, platform_org_id: str | None = None
    ) -> Company | None:
        record = None
        if domain:
            record = self._query_company("domains", domain)
        if record is None and platform_org_id:
            record = self._query_company("platform_org_id", platform_org_id)
        return _company_from_record(record) if record is not None else None

    def contact_policy(self, *, domain: str) -> ContactPolicy:
        company = self.find_company(domain=domain)
        if company is None:
            return ContactPolicy(do_not_contact=False, consent_basis=None)
        return ContactPolicy(do_not_contact=company.do_not_contact, consent_basis=company.consent_basis)

    def create_lead_signal(self, signal: LeadSignal) -> LeadSignalRef:
        company_record = None
        if signal.company_domain:
            company_record = self._query_company("domains", signal.company_domain)
        if company_record is None and signal.platform_org_id:
            company_record = self._query_company("platform_org_id", signal.platform_org_id)

        values: dict[str, list[Any]] = {
            "signal_id": [signal.signal_id],
            "event_type": [signal.event_type],
            "subject_kind": [signal.subject_kind],
            "subject_name": [signal.subject_name],
            "subject_url": [signal.subject_url],
            "observed_at": [_iso(signal.observed_at)],
            "score": [signal.score],
            "rationale": [signal.rationale],
        }
        if signal.jurisdiction is not None:
            values["jurisdiction"] = [signal.jurisdiction]
        if signal.technology is not None:
            values["technology"] = [signal.technology]
        if signal.capacity_mw is not None:
            values["capacity_mw"] = [signal.capacity_mw]

        company_ref = None
        if company_record is not None:
            company_ref = CompanyRef(sor_kind="attio", sor_ref=company_record["id"]["record_id"])
            values["company"] = [{"target_object": COMPANIES, "target_record_id": company_ref.sor_ref}]

        body = self._request(
            "PUT",
            f"/v2/objects/{LEAD_SIGNALS}/records",
            params={"matching_attribute": "signal_id"},
            json_body={"data": {"values": values}},
        )
        assert body is not None  # noqa: S101
        record_id: str = body["data"]["id"]["record_id"]

        if company_ref is not None:
            return LeadSignalRef(sor_kind="attio", sor_ref=record_id, company=company_ref, unmatched=False)

        self._request(
            "PUT",
            f"/v2/lists/{UNMATCHED_SIGNALS_LIST}/entries",
            json_body={
                "data": {
                    "parent_record_id": record_id,
                    "parent_object": LEAD_SIGNALS,
                    "entry_values": {},
                }
            },
        )
        return LeadSignalRef(sor_kind="attio", sor_ref=record_id, company=None, unmatched=True)

    def create_deal(self, deal: DealCreate) -> DealRef:
        company_record = None
        if deal.company_domain:
            company_record = self._query_company("domains", deal.company_domain)
        if company_record is None and deal.platform_org_id:
            company_record = self._query_company("platform_org_id", deal.platform_org_id)

        signal_record_id = None
        if deal.originating_signal_id:
            body = self._request(
                "POST",
                f"/v2/objects/{LEAD_SIGNALS}/records/query",
                json_body={"filter": {"signal_id": {"$eq": deal.originating_signal_id}}, "limit": 1},
            )
            rows = (body or {}).get("data") or []
            if rows:
                signal_record_id = rows[0]["id"]["record_id"]

        values: dict[str, list[Any]] = {"name": [deal.name]}
        if company_record is not None:
            values["associated_company"] = [
                {"target_object": COMPANIES, "target_record_id": company_record["id"]["record_id"]}
            ]
        if signal_record_id is not None:
            values["originating_signal"] = [
                {"target_object": LEAD_SIGNALS, "target_record_id": signal_record_id}
            ]
        if deal.tier is not None:
            values["tier"] = [deal.tier]

        body = self._request("POST", f"/v2/objects/{DEALS}/records", json_body={"data": {"values": values}})
        assert body is not None  # noqa: S101
        record_id: str = body["data"]["id"]["record_id"]

        note_content = (
            f"Proposal: {deal.proposal_public_id}\n"
            f"Opportunity: {deal.opportunity_public_id}\n"
            f"Score: {deal.score}\n"
            f"Rationale: {deal.rationale}\n"
            f"Link: {deal.link_url}"
        )
        self._request(
            "POST",
            "/v2/notes",
            json_body={
                "data": {
                    "parent_object": DEALS,
                    "parent_record_id": record_id,
                    "title": "Lead hand-off from platform",
                    "format": "plaintext",
                    "content": note_content,
                }
            },
        )
        return DealRef(sor_kind="attio", sor_ref=record_id)

    def log_activity(self, company: CompanyRef, note: ActivityNote) -> str:
        body = self._request(
            "POST",
            "/v2/notes",
            json_body={
                "data": {
                    "parent_object": COMPANIES,
                    "parent_record_id": company.sor_ref,
                    "title": note.kind,
                    "format": "plaintext",
                    "content": note.body,
                }
            },
        )
        assert body is not None  # noqa: S101
        note_id: str = body["data"]["id"]["note_id"]
        return note_id

    def upsert_subscription(self, subscription: SubscriptionMirror) -> str:
        company_record = None
        if subscription.platform_account_id:
            company_record = self._query_company("platform_account_id", subscription.platform_account_id)
        if company_record is None and subscription.company_domain:
            company_record = self._query_company("domains", subscription.company_domain)

        values: dict[str, list[Any]] = {
            "stripe_subscription_id": [subscription.stripe_subscription_id],
            "plan": [subscription.plan],
            "seats": [subscription.seats],
            "status": [subscription.status],
        }
        if subscription.current_period_end is not None:
            values["current_period_end"] = [_iso(subscription.current_period_end)]
        if subscription.mrr is not None:
            values["mrr"] = [subscription.mrr]
        if company_record is not None:
            values["company"] = [
                {"target_object": COMPANIES, "target_record_id": company_record["id"]["record_id"]}
            ]

        body = self._request(
            "PUT",
            f"/v2/objects/{SUBSCRIPTIONS}/records",
            params={"matching_attribute": "stripe_subscription_id"},
            json_body={"data": {"values": values}},
        )
        assert body is not None  # noqa: S101
        record_id: str = body["data"]["id"]["record_id"]
        return record_id

    def request_personal_data_deletion(self, *, email: str, reason: str) -> str:
        deadline = self._clock() + timedelta(days=30)
        body = self._request(
            "POST",
            "/v2/tasks",
            json_body={
                "data": {
                    "content": f"Delete personal data for {email}. Reason: {reason}",
                    "format": "plaintext",
                    "deadline_at": _iso(deadline),
                    "is_completed": False,
                    "linked_records": [email],
                }
            },
        )
        assert body is not None  # noqa: S101
        task_id: str = body["data"]["id"]["task_id"]
        return task_id

    def parse_webhook(self, *, body: bytes, headers: Mapping[str, str]) -> list[CrmChange]:
        if not self._webhook_secret:
            raise WebhookRejected("no webhook secret configured; refusing to accept unsigned webhooks")
        signature = _get_header(headers, "Attio-Signature") or _get_header(headers, "X-Attio-Signature")
        if not signature:
            raise WebhookRejected("missing Attio-Signature header")
        expected = hmac.new(self._webhook_secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature.strip().lower(), expected):
            raise WebhookRejected("signature verification failed")
        try:
            payload = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise WebhookRejected("invalid JSON body") from exc

        if isinstance(payload, dict) and isinstance(payload.get("events"), list):
            raw_events: list[Any] = payload["events"]
        else:
            raw_events = [payload]

        changes: list[CrmChange] = []
        for raw_event in raw_events:
            change = self._change_from_event(raw_event)
            if change is not None:
                changes.append(change)
        return changes

    def _change_from_event(self, raw_event: Any) -> CrmChange | None:
        if not isinstance(raw_event, dict):
            return None
        event_type = raw_event.get("event_type")
        id_obj = raw_event.get("id") or {}
        object_id = id_obj.get("object_id")
        record_id = id_obj.get("record_id")
        if not isinstance(object_id, str) or not isinstance(record_id, str):
            return None
        slug = self._slug_for_object_id(object_id)
        if slug == COMPANIES and event_type == "record.updated":
            kind = "company.updated"
        elif slug == LEAD_SIGNALS and event_type in ("record.updated", "record.created"):
            kind = "lead_signal.updated"
        else:
            return None
        return CrmChange(
            kind=kind,
            sor_ref=record_id,
            occurred_at=self._clock(),
            fields={"actor": raw_event.get("actor"), "event_type": event_type},
        )

    def _slug_for_object_id(self, object_id: str) -> str:
        for slug, mapped_id in self._object_ids.items():
            if mapped_id == object_id:
                return slug
        return "unknown"


_dry_run_singleton: InMemoryCrm | None = None


def build_crm_port() -> CrmPort:
    """`services.sor.wiring.get_crm_port` imports this. Live adapter when `ATTIO_API_KEY` is set
    (CLAUDE.md: secrets from environment only); otherwise a process-wide `InMemoryCrm` so the app
    still runs end-to-end in an environment with no Attio workspace yet (dry run), logged once."""
    api_key = os.environ.get("ATTIO_API_KEY")
    if not api_key:
        global _dry_run_singleton
        if _dry_run_singleton is None:
            logger.info("crm_port_dry_run reason=ATTIO_API_KEY_unset")
            _dry_run_singleton = InMemoryCrm()
        return _dry_run_singleton

    webhook_secret = os.environ.get("ATTIO_WEBHOOK_SECRET")
    object_ids_raw = os.environ.get("ATTIO_OBJECT_IDS")
    object_ids: Mapping[str, str] | None = json.loads(object_ids_raw) if object_ids_raw else None
    return AttioCrmAdapter(api_key, webhook_secret=webhook_secret, object_ids=object_ids)


__all__ = ["AttioCrmAdapter", "build_crm_port"]
