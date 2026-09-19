"""Anti-corruption layer for the commercial systems of record (docs/20 §9, ADR 0006).

`ports.py` holds the two ports in the platform's own vocabulary; one adapter per vendor lives in
`services/crm/` (Attio) and `services/billing/` (Stripe). Nothing outside those two packages may
import a vendor SDK or speak a vendor field name (docs/04 E-2).
"""
