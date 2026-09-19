"""FastAPI dependency providers for the two ports.

Imports are lazy so this module never fails to import while an adapter package is still being
built, and so `services.sor` stays free of vendor code (docs/04 E-2). Tests override these with
`app.dependency_overrides[get_crm_port] = lambda: fake` exactly as they override `get_db`.
The `import-not-found` ignores are inert once the adapter packages exist (pyproject sets
`warn_unused_ignores = false`); they only keep the strict gate green in between.
"""

from __future__ import annotations

from services.sor.ports import BillingPort, CrmPort


def get_crm_port() -> CrmPort:
    from services.crm.attio import build_crm_port  # type: ignore[import-not-found]

    port: CrmPort = build_crm_port()
    return port


def get_billing_port() -> BillingPort:
    from services.billing.stripe import build_billing_port  # type: ignore[import-not-found]

    port: BillingPort = build_billing_port()
    return port
