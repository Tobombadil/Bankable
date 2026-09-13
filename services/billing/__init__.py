"""Stripe billing adapter, subscription mirror application, and billing HTTP routes.

Sprint 3, first wave (`docs/00-PLAN.md`, `docs/34-crm-system-of-record.md` §1, §5). This package
sits behind `services.sor.ports.BillingPort` (`docs/20` §9, ADR 0006): `stripe.py` is the real
adapter (form-encoded HTTP via `httpx`, no vendor SDK — CLAUDE.md "no new dependencies"),
`fake.py` is the dry-run stand-in used when no Stripe secret key is configured and in tests,
`entitlement.py` turns a vendor-neutral `EntitlementChange` into a database write, and
`router.py` exposes checkout, the customer portal, the inbound webhook and the admin
subscription list. See `services/billing/README.md` for the full decision log.
"""

from __future__ import annotations
