# Pricing page copy

**Status:** Sprint 3 item 6, v1 · 2026-09-13 · product-manager · reviewed by: owner (`docs/04` R-2)
**Inputs:** `docs/11-market-and-competition.md` §3 (pricing recommendation and delay schedule) only, for every
number and inclusion below; `docs/13-legal-data-rights.md` §6, §7 (coverage and licence-pass-through caveats);
`docs/00-PLAN.md` S3-4 (coverage statement).
**Scope:** draft copy for the public pricing page. No claim here goes beyond what `docs/11` §3 states; every
number cites its row. Anchor figures are marked "verify before publishing" exactly where `docs/11` marks them
as an anchor rather than a measured price.

---

## Coverage line (shared, above the pricing table)

> {{PRODUCT}} tracks every major US interconnection queue and the open international tender registers.
> ERCOT, CAISO and NYISO are published today; PJM, MISO, SPP and ISO-NE are linked out pending licence
> clearance (`docs/13-legal-data-rights.md` §6; `docs/00-PLAN.md` decision S3-4).

---

## Free (delayed)

**Price:** $0 (`docs/11` §3, row "Free (delayed)").

Public pages, derived records, full attribution, and an RSS / Bluesky / LinkedIn feed. Search and map
included. No export, no alerts.

**Delay:** free-tier lag is set per source cadence (`docs/11` §3, "Delayed-tier lag by source cadence" table):
- Daily/intraday sources (ISO queues, grants.gov, SAM.gov, TED, FTS, FERC eLibrary, World Bank notices):
  **7-day lag for opportunities, 14-day lag for supply rows**.
- Weekly sources (utility RFP pages, state siting dockets, Permitting Dashboard): **30-day lag**.
- Monthly sources (EIA-860M, NESO TEC register): **one cycle (30–45 days)**.
- Quarterly/annual sources (LBNL Queued Up, GEM, EIA-860): **no lag** — published in full with attribution.
- Status-change events (cancelled, frozen, reinstated, withdrawn, window opening): **Pro only for 30 days,
  then free**.

> Why the delay: the change *is* the product. A free list is useful for research; a live change feed is what
> Pro pays for.

---

## Pro

**Price:** $149/mo or $1,490/yr per seat (`docs/11` §3, row "Pro").

Everything in Free, live (no delay). Adds:
- Saved searches
- Daily change-feed alerts by email or Slack
- CSV export (capped)
- Opportunity-deadline calendar

> Positioned between Energy Adepto Starter ($120–150/mo) and Professional ($320–400/mo), with strictly more
> data — supply, demand, and funding/tender coverage together (`docs/11` §3 rationale column).

---

## Team

**Price:** $9,000/yr, 5 seats (`docs/11` §3, row "Team"). *Price-matched to Cleanview — verify before
publishing that this remains accurate at launch (`docs/11` §3 rationale: "so procurement cannot say 'more
expensive than Cleanview'").*

Everything in Pro, plus:
- Shared watchlists
- Unlimited export
- Entity-resolution links across dockets, EIA and permits
- Proposal-to-opportunity matching

---

## API / Data

**Price:** +$5,000/yr on top of Team (Team+API = $14,000/yr), or $25,000/yr as a standalone enterprise tier
with bulk pulls and Snowflake delivery (`docs/11` §3, row "API / Data"). *The $25,000 enterprise figure is
below the WoodMac/Enverus/NPM range ($40,000–80,000/yr) and above Halcyon's data-subscription level — a market
positioning, not a measured price; verify before publishing.*

Adds:
- Change-event webhooks
- Bulk data pulls
- Licence pass-through for restricted sources (subject to the customer-terms and indemnity work still
  outstanding — see the legal footer below)

---

## Legal footer

> Access to restricted-source data under an API/Data plan is governed by a per-record licence field and
> customer terms that bind subscribers to each upstream source's restrictions. See {{DOMAIN}}/terms and the
> API licence summary in every API response envelope.

**Not yet drafted — do not publish the API/Data tier's "licence pass-through" claim until these exist:**
customer terms binding subscribers to upstream restrictions, and the indemnity position noted in
`docs/13-legal-data-rights.md` §7 item 8 (PJM's clause 9 indemnifies PJM for claims arising from *our
customers'* use of derivatives). This is a counsel item, not a copy decision.

---

## What the engineering actually enforces today

Cite by path only; this table does not describe the code, it maps each promise above to where it is enforced.

| Promise | Enforced by |
|---|---|
| Tier entitlement (who sees live vs delayed data, who can export, who has API access) | `services/sor/ports.py` (entitlement port) and its callers in `services/api/pro.py` |
| Free-tier lag by source cadence | `services/ingest/lag.py` |
| Per-tier rate limits | `services/api/ratelimit.py` |

No other promise on this page (seats, saved-search count, export cap size, Snowflake delivery, licence
pass-through) has a cited enforcement point in this sprint's read scope — do not imply otherwise in the
published copy.
