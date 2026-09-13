# ADR 0006 — CRM/ERP integration pattern and system of record

**Status:** Proposed pending owner · 2026-09-12 · solutions-architect
**Blocked on:** `docs/00-PLAN.md` open question **4** — *which CRM/ERP is the system of record?* Candidates named
there: HubSpot (CRM) + QuickBooks/Xero or Stripe (finance); Odoo or ERPNext as one open-source CRM+ERP; Twenty +
Stripe Billing. Carried as **[A-4]** in `docs/20-architecture.md` §16 and as A-4 in `docs/10-prd-mvp.md` §7.
**Related:** `docs/20-architecture.md` §9 (the pattern and the candidate comparison), §15 (Billing),
`docs/10-prd-mvp.md` US-602, US-701, US-901–903, US-403; `docs/21-data-model.md` §3.13, §3.14.

## Context

`docs/00-PLAN.md` fixes the principle: "the admin panel writes customers/subscriptions to the CRM/ERP system of
record and reads back; the app database is **not** the SSOT for commercial records." The vendor is undecided and
the decision is the owner's, but the build cannot wait: entitlement checks (US-602), API key tiers (US-701 AC3),
admin customer screens (US-901–902) and lead hand-off from a match (US-403) all need an interface in Sprint 1–3.
Two decisions therefore have to be separated: **the pattern** (which the architect owns and which must make the
vendor replaceable) and **the vendor** (which the owner owns).

## Options considered

### Pattern

| Option | For | Against |
|---|---|---|
| **Anti-corruption layer: two narrow ports in the app's vocabulary, one adapter per vendor, plus a read-model mirror that is authoritative for nothing** | Vendor swap is a new adapter plus a migration of `sor_ref` values, not a schema change; the app's domain language stays clean; adapters are testable against an in-memory fake (US-903 AC1) | One more indirection; the mirror can drift and needs reconciliation |
| Call the vendor SDK directly from admin and entitlement code | Fastest to write | Vendor field names leak into the app; every entitlement read is a network call on the request path; swapping vendor becomes a rewrite. Contradicts the standing principle |
| Make the app the system of record and sync *out* to the CRM | Simplest entitlement reads | Directly contradicts `docs/00-PLAN.md`; creates two books of record for money, which is the failure mode the principle exists to prevent |
| Buy an iPaaS (Zapier, Make, n8n) to sync | No code | Business logic in a third-party console, invisible to tests and to the repo; unacceptable for entitlements, which are a security boundary |

### Vendor (list prices as published 2026; verify before signing — `docs/20` §9)

| Option | Covers | Cost at MVP | Main risk |
|---|---|---|---|
| **HubSpot free CRM + Stripe Billing** | Pipeline, sequences, activity (HubSpot); subscriptions, invoices, tax, customer portal (Stripe) | HubSpot free tier; Stripe ~2.9% + 30¢ plus Billing fee | Two systems, so "one system of record" is really two; HubSpot upgrade cliffs (Starter per seat, Professional from ~USD 800/mo) |
| Odoo (Online or CE) | CRM + subscriptions + invoicing + accounting | ~USD 25–30/user/mo, or one VM self-hosted | Heaviest to learn; XML/JSON-RPC API; version churn; self-hosting is another system to run |
| ERPNext (Frappe Cloud or self-hosted) | Same breadth, fully open source | ~USD 25–50/mo | Smaller US ecosystem; dated sales UI; fewer email/social integrations |
| Twenty + Stripe Billing | Modern open-source CRM + Stripe | ~USD 9–20/seat | Young project, API still moving; no email sequences |

## Decision

**Pattern (decided, architect's remit):** an anti-corruption layer in `services/sor`.

- **Ports** in the app's vocabulary: `CrmPort` (`upsert_company`, `upsert_contact`, `create_lead(signal)`,
  `log_activity`, `get_company(ref)`, `list_changes(since)`) and `BillingPort` (`create_checkout(account, plan)`,
  `get_subscription(ref)`, `list_invoices(account)`, `open_portal(account)`,
  `handle_webhook(payload) -> EntitlementChange[]`).
- **One adapter per vendor**, selected by configuration, plus an **in-memory fake** that the whole test suite
  runs against (US-903 AC2).
- **Mirror**: `account` and `subscription` (`docs/21` §3.13–3.14) hold only `sor_kind`, `sor_ref`, `billing_ref`,
  the resolved entitlement and a cache stamp. Authoritative for nothing. Refreshed by webhook and by a nightly
  reconciliation job that reports drift; conflicts resolve system-of-record-wins; entitlement cache TTL ≤ 15 min
  (US-602 AC1); if the adapter is down, entitlements fail **open** for 24 h then closed, and admin refuses writes
  and shows a staleness banner (US-902 AC3, `docs/20` §12).
- **No vendor field outside the adapter.** The app stores exactly three vendor-shaped values —
  `account.sor_ref`, `account.billing_ref`, `match.crm_lead_ref` — and no lead or contact content (US-403 AC1).
- **Stripe Billing is adopted regardless of the CRM answer**, so money is never on the critical path of the CRM
  decision. Entitlements derive from Stripe subscription state through `BillingPort`.

**Vendor (proposed, owner decides):** **HubSpot free CRM + Stripe Billing** for the MVP — fastest route to a
first paying customer, both APIs stable and Python-friendly, and the sales-bd workflows in `docs/33` map directly
onto HubSpot objects. If the owner prefers one open-source system with accounting, **ERPNext over Odoo** on API
quality and licence simplicity.

## Owner question to answer

> Which system is the system of record for customers, subscriptions and invoices: HubSpot + Stripe (assumed), or
> one open-source CRM+ERP (ERPNext or Odoo), or Twenty + Stripe? The answer sets the first adapter built in
> Sprint 3 and the admin customer screens; it does not change the schema.

Second-order questions the answer settles: where leads from US-403 land; which system holds invoices for the
metric M-13 revenue figures; and whether accounting consolidation is worth the heavier admin surface.

## Consequences

- Sprint 1–3 work proceeds against the ports and the fake; the concrete adapter is a bounded task once the owner
  answers, and swapping later is a new adapter plus a `sor_ref` migration (`docs/20` §9).
- Two systems under the assumed answer means "system of record" is really "CRM of record + billing of record".
  The `BillingPort`/`CrmPort` split makes that explicit rather than pretending otherwise.
- The mirror can be stale. Nightly reconciliation with a drift report is mandatory, not optional, and drift is a
  monitored metric (`docs/20` §10, `docs/21` §3.14 `drift_flag`).
- Entitlement checks never call the vendor on the request path; a vendor outage degrades to cached entitlements,
  not to a broken login.
- Personal data in the CRM is governed by the vendor's terms; deletion requests must fan out through the port
  (US-910, `docs/20` §11), which is a required operation on `CrmPort` when the adapter is written.


## Decision update (2026-09-13)

The owner chose **Attio** as the CRM system of record instead of HubSpot; Stripe Billing is unchanged. The adapter pattern in this ADR is unaffected: the first concrete CRM adapter is `services/crm/attio.py`. Attio's public REST API v2 (objects, records, lists, notes, tasks, webhooks; API-key auth) is to be verified against https://docs.attio.com before implementation, with rate limits recorded.
