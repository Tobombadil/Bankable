# services/billing — Stripe adapter, subscription mirror, entitlement application, billing routes

Sprint 3, first wave (`docs/00-PLAN.md`; `docs/34-crm-system-of-record.md` §1, §2.5, §5). Behind
`services/sor/ports.py`'s `BillingPort`: `stripe.py` (the real adapter, plain `httpx`, no vendor
SDK), `fake.py` (`InMemoryBilling`, the dry-run stand-in), `entitlement.py` (turns an inbound
`EntitlementChange` into a database write), `router.py` (checkout, portal, the inbound webhook,
the admin subscription list), plus `signing.py` (the shared Stripe webhook signature verifier).

## Stripe endpoints used

All under `https://api.stripe.com`, form-encoded (`Content-Type: application/x-www-form-urlencoded`,
Stripe's bracket notation for nested fields), `Authorization: Bearer <STRIPE_SECRET_KEY>`.

| Endpoint | Used for | Docs |
|---|---|---|
| `POST /v1/customers` | Create a Stripe customer on first checkout | https://docs.stripe.com/api/customers/create |
| `POST /v1/checkout/sessions` | Subscription-mode Checkout Session | https://docs.stripe.com/api/checkout/sessions/create |
| `POST /v1/billing_portal/sessions` | Customer portal session | https://docs.stripe.com/api/customer_portal/sessions/create |
| `GET /v1/subscriptions/{id}` | Refresh one subscription's state | https://docs.stripe.com/api/subscriptions/retrieve |
| `GET /v1/invoices?customer=…&limit=24` | `BillingPort.list_invoices` | https://docs.stripe.com/api/invoices/list |
| (inbound) webhook | `customer.subscription.*`, `checkout.session.completed`, `invoice.paid`/`invoice.payment_failed` | https://docs.stripe.com/webhooks, https://docs.stripe.com/api/events/types |

### API version decision

No `Stripe-Version` header is sent. The saved vendor pages
(`stripe-webhooks.html`'s `event_destinations` curl example) only show a literal
`$$API_VERSION_REPLACE_ME$$` placeholder where a real version string would be — there is no way to
extract an actual current version from what was fetched, and guessing one (`"2024-06-20"` or any
other value memorised rather than verified) would risk pinning to a version that has since been
retired. The account's own configured default API version applies instead. This is a documented
gap, not a silent omission: revisit once a real Stripe account (sandbox or live) is available to
read `GET /v1/account` or the dashboard for the true current default.

### Rate limits (from the saved `stripe-rate-limits.html`; quoted, not memorised)

- Global API rate limit: **live mode 100 requests/second**, **sandbox 25 requests/second**, per
  Stripe account.
- Individual API endpoints (unless otherwise noted): **25 requests/second**.
- Files API: 20 read/second, 20 write/second. Payouts API: 15 create/second, 30 concurrent per
  business. (Not directly used by this adapter; quoted for completeness since the page covers the
  whole API.)
- Every account has a minimum allocation of **10,000 read API requests per month**; write
  requests have no allocation limit. Exceeding a limit returns `429` with (when present) an
  `X-Stripe-Rate-Limit-Bucket` reason header (`global-rate` / `endpoint-rate` / `resource-specific`)
  and Stripe's own libraries default to retrying on exponential backoff.
- Not extractable from the saved pages: the exact numeric bucket sizes for
  `/v1/checkout/sessions`, `/v1/billing_portal/sessions` or `/v1/subscriptions` specifically —
  only the "25 requests/second, unless otherwise noted" default is documented for endpoints not
  named in the page's table, and none of these three is named there.
- Webhook delivery retries (`stripe-webhooks.html`, quoted): "Stripe attempts to deliver events to
  your destination for up to three days with an exponential back off in live mode... We retry
  event deliveries created in a sandbox three times over the course of a few hours." This is why
  `services/billing/router.py`'s webhook handler never answers a per-change failure with a 5xx —
  Stripe's own retry loop would just resend the same delivery for up to three days over a problem
  a retry cannot fix (a downstream lookup failure already logged and counted).

This adapter's own retry policy (independent of the numbers above, since none of them cover the
three endpoints it actually calls): `429` → honour `Retry-After` (seconds), up to 3 retries, then
`SorUnavailable`; `5xx` or a transport error → retry once, then `SorUnavailable`; any other `4xx`
→ `SorRejected` (the vendor's error message goes to the log, never the response body). 30s
per-request timeout.

## Webhook verification

`Stripe-Signature: t=<unix ts>,v1=<hex>[,v1=<hex>...]` (multiple `v1=` values during a secret
rotation — the first match wins). Signed payload is `f"{t}.{raw body}"`, HMAC-SHA256 with
`STRIPE_WEBHOOK_SECRET`, compared with `hmac.compare_digest`; default tolerance 300 seconds
between `t` and now (`stripe-webhooks.html`: "Our libraries have a default tolerance of 5 minutes").
No configured secret, a missing/malformed header, a signature mismatch, or a stale timestamp are
all `WebhookRejected` → the router answers `401` and never touches the body. Shared between the
real adapter and `InMemoryBilling` via `services/billing/signing.py`, so router/entitlement tests
exercise the *real* verification code path even against the fake port.

## Entitlement rules (from `services/sor/ports.py`, not re-derived here)

`entitlement_for(plan_tier, status)`: `status` in `{trialing, active, past_due}` **and**
`plan_tier` in `PLAN_ENTITLEMENT` (`pro`→pro, `team`→pro, `api`→api, `enterprise`→api) grants that
entitlement; anything else (canceled, paused, unpaid, an unrecognised plan) resolves to `public`.
`past_due` deliberately keeps entitlement — Stripe's dunning retries a failed card over days, and
cutting access on the first bounce loses paying customers over a transient decline.

`account.entitlement = "admin"` is never touched by a billing webhook (docs/21 §3.13: admin is a
manual grant, never derived from billing) — `apply_entitlement_change` checks this before writing
anything to the account, though the `subscription` mirror row is still written regardless (it is a
read model of the commercial record, independent of what the platform does with it).

### Idempotency scheme

- **Outbound** (every Stripe POST carries an `Idempotency-Key`, derived from the caller's own ids
  rather than a bare random value whenever one is stable): `customer:<account_public_id>` (create
  customer), `checkout:<account_public_id>:<plan>:<YYYY-MM-DD>` (checkout — one key per
  account/plan/day, so a retried click within the same day cannot double-create a session),
  `portal:<billing_ref>:<YYYY-MM-DD>` (portal, same reasoning).
- **Inbound**: `apply_entitlement_change` records an `Event` row with
  `idempotency_key = f"billing:{change.event_ref}"` (the Stripe event id). A replayed webhook
  delivery for the same event is a no-op — the function returns the (already-correct) account
  without writing the mirror row or the account entitlement a second time.

## Decisions and deviations (numbered; cross-referenced from code comments as `decision #N`)

1. **`build_billing_port()` fallback.** `STRIPE_SECRET_KEY` set → real `StripeBillingAdapter` from
   environment variables (`STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_PRO/TEAM/API/ENTERPRISE`); unset →
   a process-wide `InMemoryBilling` singleton (dry run), logged once at `WARNING`. Mirrors
   `services.crm.attio.build_crm_port`'s pattern exactly (same fallback shape, same file-layering
   reason: `services.sor` stays vendor-free and lazily imports the adapter package).
2. **Idempotency-Key derivation.** See "Idempotency scheme" above — never a bare UUID when a
   stable key is available from the caller's own ids; a UUID would make a client-side retry
   indistinguishable from a genuinely new purchase.
3. **`Subscription.plan_tier`'s CHECK vocabulary is `free | pro | team | api`** — the same four
   values as `api/openapi.yaml`'s `PlanTier` schema (verified by reading the schema directly:
   `enum: [free, pro, team, api]`, no `enterprise` member) and docs/21 §3.14, *not* the wider
   vendor-neutral `services.sor.ports.PLAN_TIERS` (`pro | team | api | enterprise` — the
   commercial names a customer can *buy*, one purchasable tier above `api`). An `enterprise`
   purchase is stored with `plan_tier = "api"` (the entitlement `enterprise` grants, per
   `PLAN_ENTITLEMENT`) while `plan_code` keeps the vendor's exact price/plan code — no information
   is lost. This CHECK constraint never needed CLAUDE.md's "extend an existing vocab tuple" escape
   hatch: it is a brand-new tuple for a brand-new table, not an extension of one the API layer
   already relies on elsewhere.
4. **`incomplete` Stripe subscription status** is handled two different ways in two different
   contracts, both documented where they happen (`services/billing/stripe.py`'s
   `_RAW_TO_PLATFORM_STATUS` docstring): at the **webhook layer**
   (`customer.subscription.created|updated|paused|resumed`), an `incomplete` subscription produces
   *no* `EntitlementChange` at all (`handle_webhook` returns `[]` for that event) — the task brief
   explicitly rejected mapping it to `past_due` (would grant unpaid access) or `canceled` (would
   revoke access from a subscription that might succeed seconds later), and "no change" is a real
   third option a webhook handler can take that a single-subscription lookup cannot. At the
   **`get_subscription` layer**, the caller asked about one specific subscription and needs an
   answer; `incomplete` (or any status this table does not yet know) falls back to `canceled`
   (fail-closed: no entitlement) with a `WARNING` log, rather than raising or returning `None`
   (which would read as "not found", a different and wrong signal).
5. **The entitlement-change audit event.** `Event.event_type = "entitlement_changed"` and
   `actor_type = "system"`: `services/db/models.py`'s `Event.__table_args__` has no CHECK
   constraint on `event_type` (only `actor_type` is constrained, to `ACTOR_TYPES = ("pipeline",
   "model", "user", "system")`, which already includes `"system"`), so this is a freely-chosen new
   value, not an extension of a constrained vocabulary. `published_at = public_at = now` mirrors
   `services.api.audit.record_audit_event`'s convention for a non-pipeline, non-ingest event
   (docs/21 §6.1's "one code path, one predicate") — it does not make the row appear on any public
   proposal/opportunity timeline (every public-feed endpoint filters `subject_type IN (proposal,
   opportunity)`), it only means the timestamp is honestly set rather than nulled for no reason.
6. **`current_period_start`/`current_period_end` missing from a subscription payload.** No known
   Stripe API shape omits these on a real subscription object, so this should never happen; if it
   does, `_subscription_state_from_object` logs a `WARNING` and falls back to "now" for the
   missing bound rather than raising `KeyError` and dropping the whole event — a stale-but-present
   mirror row beats an unprocessed webhook.
7. **Checkout requires a real email; never fabricated.** `POST /v1/billing/checkout` raises
   `409 conflict` ("An email address is required before checkout") when `user.email` is `None`,
   rather than inventing a placeholder address. Stripe emails receipts and dunning notices to the
   Customer's address it is given — a made-up one means the customer never sees a failed-payment
   notice, and it plants a fake identity in a third-party system. This sprint's auth
   (`services/api/auth.py`) has no email-verification story yet (`User.email_verified_at` can be
   null); until one exists, a user with no email on file simply cannot check out. (Coordinator
   review, 2026-09-13: this replaces an earlier draft that synthesised a placeholder address —
   caught before merge.)
8. **`Subscription.public_id` prefix is `subn`, not `sub`.** Verified directly against
   `api/openapi.yaml`'s `SubscriptionPublicId` schema (`pattern: '^subn_[0-9A-HJKMNP-TV-Z]{10,26}$'`)
   rather than assumed from the table name — `sub` would have collided with nothing else in this
   codebase, but it also would not have matched the spec, and `tests/test_api_contract.py`'s
   jsonschema check caught the mismatch immediately during development.
9. **The Stripe error message never reaches the response body.** `create_checkout`/`open_portal`
   log the caught `SorUnavailable`/`SorRejected` text at `WARNING` with the account's public id,
   and answer with a fixed, generic `detail` ("The billing provider could not be reached; try
   again shortly." / "The billing provider rejected the request.") instead of `str(exc)` — a
   vendor error string can carry account-specific or otherwise sensitive detail that has no
   business leaving the platform in an API response. (Coordinator review, 2026-09-13.)

## Deferred (not built this wave)

- **`POST /admin/v1/subscriptions`** (create-through-adapter) stays unimplemented — `api/openapi.yaml`
  already marks it `x-status: planned`, and `api/fragments/billing.yaml` leaves it that way too. It
  needs the admin-console write path (choosing a plan/seats for a customer who did not self-serve
  checkout) that the admin wave, not this one, owns. `GET /admin/v1/subscriptions` (the read-only
  mirror list) is fully implemented and flipped to `x-status: live` in the fragment.
- **Mounting `services.billing.router.router` onto `services.api.app.app`.** This package writes
  only under `services/billing/**` (CLAUDE.md "one agent per file area"); `services/api/app.py` is
  explicitly out of bounds. `router = APIRouter()` is exported for the coordinator's one
  `include_router` call. `services/billing/test_router.py` exercises the router against a
  standalone test-local FastAPI app (`services/billing/conftest.py`) that mounts this router
  plus a read-only import of `services.api.pro.router` (needed for the one test that checks
  `GET /v1/me` reflects a webhook's entitlement change end-to-end) — never against the shared
  production `app` object, so this package's tests cannot interfere with the two other agents
  working concurrently in `services/crm/` and `web/`.
- **Wiring `subscriptions_for_account` into `GET /v1/account`'s `subscriptions` field.**
  `services/api/pro.py:get_account` currently hardcodes `"subscriptions": []`; this package exports
  `services.billing.router.subscriptions_for_account(db, account) -> list[dict]` (same serializer
  the admin list uses) for the coordinator to call from there — `services/api/pro.py` is not in
  this package's write scope either.
- **`BillingPort.list_invoices`** is implemented and tested but has no HTTP route yet — no invoice
  history endpoint exists in `api/openapi.yaml` for this sprint's scope.
- **Reverse pagination** (`page.prev_cursor`) on `GET /admin/v1/subscriptions` is always `null`,
  inherited from `services/api/pagination.py`'s existing "forward pagination only" scope (already
  an open decision in `services/README.md`, not repeated here as a new one).
- **A real Stripe API version pin** — see "API version decision" above.

## Verbatim tails

### `pytest services/billing services/db tests/test_api_contract.py services/api -v` (last lines)

```
  /home/user/Bankable/tests/test_api_contract.py:43: DeprecationWarning: jsonschema.RefResolver is deprecated as of v4.18.0, in favor of the https://github.com/python-jsonschema/referencing library, which provides more compliant referencing behavior as well as more flexible APIs for customization. A future release will remove RefResolver. Please file a feature request (on referencing) if you are missing an API for the kind of customization you need.
    resolver = jsonschema.validators.RefResolver.from_schema(spec)

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
====================== 131 passed, 28 warnings in 10.46s =======================
```

### `ruff check services/billing services/db api/fragments`

```
All checks passed!
```

### `ruff format --check services/billing services/db`

```
22 files already formatted
```

### `mypy --cache-dir /tmp/mypy-billing services/billing services/db`

```
Success: no issues found in 13 source files
```

### `coverage run --branch -m pytest services/billing -q && coverage report --include="services/billing/*"`

```
Name                                   Stmts   Miss Branch BrPart  Cover
------------------------------------------------------------------------
services/billing/__init__.py               1      0      0      0   100%
services/billing/conftest.py              62      0      0      0   100%
services/billing/entitlement.py           81      0     18      2    98%
services/billing/fake.py                  66      3      4      1    94%
services/billing/router.py               101      3     22      2    96%
services/billing/signing.py               37      3     16      2    91%
services/billing/stripe.py               219     21     74      9    88%
services/billing/test_entitlement.py     164      0      0      0   100%
services/billing/test_models.py           94      1      6      1    98%
services/billing/test_router.py          186      0      0      0   100%
services/billing/test_stripe.py          320      2      8      1    99%
------------------------------------------------------------------------
TOTAL                                   1331     33    148     18    96%
```

### `DATABASE_URL="sqlite+pysqlite:///:memory:" alembic -c services/db/migrations/alembic.ini history`

```
0003 -> 0004 (head), Subscription mirror (Sprint 3, first wave: `services/billing/`).
0002 -> 0003, Pro tier and alerts: auth, entitlement mirror, saved searches/alerts, webhooks.
0001 -> 0002, Add `licence.quote_text`, and the indexes the visibility predicate was missing.
<base> -> 0001, Initial schema: the public-tier entities of docs/21-data-model.md.
```
