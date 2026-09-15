# Stripe webhook/API fixtures

Hand-written JSON in Stripe's documented object shape (`docs.stripe.com/api/subscriptions`,
`.../invoices`, `.../events/types`, the saved pages under the coordinator's vendor scratchpad) —
**not** recorded from a live Stripe account; no such account exists in this environment
(`services/billing/README.md`). Every id (`sub_…`, `cus_…`, `evt_…`, `price_test_…`) is a
made-up, obviously-fake value, not a real Stripe object.

Used by `services/billing/test_stripe.py` (adapter unit tests) and `services/billing/
test_router.py` / `services/billing/test_entitlement.py` where a real Stripe-shaped payload
(rather than `InMemoryBilling`'s reduced shape) is useful.
