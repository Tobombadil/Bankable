# Pricing page copy

**Status:** Sprint 3 item 6, v2 · 2026-09-20 · backend-developer (paywall-by-shape amendment) · v1
2026-09-13 product-manager, reviewed by owner (`docs/04` R-2)

> **Amended 2026-09-19 — the paywall is by shape, not by time (owner).** Verbatim: "alerts, exports, API and
> watchlists are paid; free users see every record; the delay is kept only on ISO change events. Supersedes the
> time-delay model in docs/10 and docs/41". The delay schedule below is struck through rather than deleted so
> the change is visible to whoever reads this next. **The free tier is a marketing surface; what Pro sells is
> workflow.** Two of the four paid shapes — export and watchlists — **do not exist in the code yet**: there are
> `exports_per_day` / `export_rows_max` quota fields on the entitlement payload in `services/api/pro.py` and no
> routes behind them, and nothing named watchlist anywhere. That is why the owner's decision on Team was "sold
> as seats plus support at the current price until export and watchlists exist" (`docs/00-PLAN.md`,
> 2026-09-18 row, item 3). **Do not publish an export or watchlist claim on this page until those routes ship.**

**Inputs:** `docs/11-market-and-competition.md` §3 (pricing recommendation and delay schedule) only, for every
number and inclusion below; `docs/13-legal-data-rights.md` §6, §7 (coverage and licence-pass-through caveats);
`docs/00-PLAN.md` S3-4 (coverage statement).
**Scope:** draft copy for the public pricing page. No claim here goes beyond what `docs/11` §3 states; every
number cites its row. Anchor figures are marked "verify before publishing" exactly where `docs/11` marks them
as an anchor rather than a measured price.

---

## Coverage line (shared, above the pricing table)

> Infraque tracks every major US interconnection queue and the open international tender registers.
> ERCOT, CAISO and NYISO are published today; PJM, MISO, SPP and ISO-NE are linked out pending licence
> clearance (`docs/13-legal-data-rights.md` §6; `docs/00-PLAN.md` decision S3-4).

---

## Free

**Price:** $0 (`docs/11` §3, row "Free (delayed)" — the price is unchanged; the name "delayed" is not).

Every record, published as soon as it is ingested: public pages, derived records, full attribution, search, map
and an RSS / Bluesky / LinkedIn feed. No alerts, no export, no API, no watchlists.

~~**Delay:** free-tier lag is set per source cadence (`docs/11` §3, "Delayed-tier lag by source cadence" table):~~
~~- Daily/intraday sources (ISO queues, grants.gov, SAM.gov, TED, FTS, FERC eLibrary, World Bank notices):~~
  ~~**7-day lag for opportunities, 14-day lag for supply rows**.~~
~~- Weekly sources (utility RFP pages, state siting dockets, Permitting Dashboard): **30-day lag**.~~
~~- Monthly sources (EIA-860M, NESO TEC register): **one cycle (30–45 days)**.~~
~~- Quarterly/annual sources (LBNL Queued Up, GEM, EIA-860): **no lag**.~~
~~- Status-change events: **Pro only for 30 days, then free**.~~

**The one delay that remains** (owner, 2026-09-19): a **change event from an ISO interconnection queue** is held
**14 days** on the free tier. It is declared per source in `data/sources.yaml` (`change_event_lag_days`) and
today applies to the eight `us.iso.*` queue registers. Nothing else is delayed — not records, not events from
any other source.

> ~~Why the delay: the change *is* the product. A free list is useful for research; a live change feed is what
> Pro pays for.~~
>
> Why the shape: a free reader can already see today's record, and can see that it changed today
> (`last_changed` is a public field and the default list sort). What Pro sells is not knowing sooner — it is not
> having to look: an alert that finds you, a saved search, an export, an API. **Be careful with the copy here.**
> A free reader who polls the public list daily can reconstruct an ISO status change from two consecutive reads
> before the 14-day event is released, so this page must not imply the free tier cannot see the change at all.
> See `docs/21` §5.4 and the finding recorded in `docs/CHANGELOG.md` for 2026-09-20.

---

## Pro

**Price:** $149/mo or $1,490/yr per seat (`docs/11` §3, row "Pro").

Everything in Free, plus the workflow shapes — and ISO change events live rather than at 14 days. Adds:
- Saved searches — **built** (`services/api/pro.py`, entitlement-gated)
- Daily change-feed alerts by email or webhook — **built** (`services/alerts/`, entitlement-gated)
- CSV export (capped) — **not built**: quota fields only, no route. Do not publish.
- Opportunity-deadline calendar — not built

> Positioned between Energy Adepto Starter ($120–150/mo) and Professional ($320–400/mo), with strictly more
> data — supply, demand, and funding/tender coverage together (`docs/11` §3 rationale column).

---

## Team

**Price:** $9,000/yr, 5 seats (`docs/11` §3, row "Team"). *Price-matched to Cleanview — verify before
publishing that this remains accurate at launch (`docs/11` §3 rationale: "so procurement cannot say 'more
expensive than Cleanview'").*

**Sold as seats plus support at this price until export and watchlists exist** (owner, 2026-09-19). The
differentiators below are the plan, not the product:
- Shared watchlists — **not built** (no model, no route)
- Unlimited export — **not built** (see Pro)
- Entity-resolution links across dockets, EIA and permits — built (`services/resolve/`)
- Proposal-to-opportunity matching — not built

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
> customer terms that bind subscribers to each upstream source's restrictions. See infraque.com/terms and the
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
| Tier entitlement (who has alerts, saved searches, API access) | `services/sor/ports.py` (entitlement port) and `require_entitlement(...)` on every route in `services/api/pro.py` |
| ISO change events held 14 days on the free tier | `services/ingest/lag.py` + `data/sources.yaml` `change_event_lag_days` → `source.lag_days`; read back by `services/api/visibility.py` from the stored `event.public_at` |
| Records undelayed on the free tier | `services/ingest/lag.py::record_public_at`; migration `0016` for rows loaded before 2026-09-20 |
| Per-tier rate limits | `services/api/ratelimit.py` |
| Export, watchlists | **Nothing. Neither exists.** `exports_per_day` / `export_rows_max` appear in the `GET /v1/me` entitlement payload and no route reads them |

No other promise on this page (seats, saved-search count, export cap size, Snowflake delivery, licence
pass-through) has a cited enforcement point — do not imply otherwise in the published copy.
