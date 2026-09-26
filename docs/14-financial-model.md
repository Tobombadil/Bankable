# 24-month financial model

**Status:** Phase 1 deliverable (the "24-month financial model" the plan lists as outstanding) · v1 2026-09-26 ·
market-researcher (lane E8a)
**Inputs:** `00-PLAN.md` (phases table; decisions log 2026-09-18, 2026-09-25, 2026-09-26), `01-feasibility.md`
§3.4, `11-market-and-competition.md` §2.3 and §3, `26-platform-posture.md`, `33-gtm-and-sales-playbook.md`
§6, `41-pricing-page-copy.md`, `60-deployment.md` §4, `32-social-operating-playbook.md` §4.6–4.7 and §7,
`13-legal-data-rights.md` §6–7, `20-architecture.md` §14, `04-standards.md` O-8.
**Reproduce:** `.venv/bin/python scripts/financial_model.py` prints every table in §4 (`../scripts/financial_model.py`).
The tables are pasted from that run, not typed; if a number here disagrees with the script, the script is right.
**Convention:** facts are marked **F** with a source and retrieval date; inferences and assumptions are marked
**I** with a confidence level. Assumptions carry an `A-n` id and appear in §2 with what they depend on and what
happens if they are wrong.

## 0. Headline

The platform runs `PLATFORM_POSTURE=noncommercial` today (owner, 2026-09-25 and 2026-09-26), so paid tiers are
marked inactive and `POST /v1/billing/checkout` refuses with `403 paid_tiers_inactive` (`docs/26` §3(i)). While
that holds, subscription revenue is zero by construction and the model is a cost model. This document models
that period explicitly, then a switch-back month as a parameter, then three adoption scenarios after the switch.

| # | Number | Value | Confidence (I) |
|---|---|---|---|
| H1 | Monthly cost stack under `noncommercial` | **$352/mo** before the model gateway ships, **$457/mo** after (Table 1) | Moderate-high on shape, moderate on level: the Neon line is a usage estimate (`docs/60` [B-1]), the LinkedIn bridge is unverified, X is a measured price schedule at an assumed volume |
| H2 | Cash burned while noncommercial, base switch at M7 (2027-04) | **$2,424** over six months; **$10,643** over 24 months if the posture never switches (Table 6a, last row) | High arithmetic; conditional on A-1, A-3, A-12 (no counsel spend, no owner draw) |
| H3 | Operating break-even month | **M7 (low: M8)** — the switch month itself, in every scenario, **because labour is not a cash cost in the base parameters** (Table 2) | High arithmetic, low usefulness: with a $492/mo cost base, four Pro seats cover it. The meaningful statement is H4 |
| H4 | Break-even with labour priced in | Every **$1,500/mo of owner draw pushes operating break-even back 1–2 months and cash-positive back 3–4 months** in the base scenario; at $6,000/mo it is M13 / M21 with a $42.6k trough (Table 6b). In seat terms: a $6,000/mo draw needs ~45 Pro seats, matching `docs/01` §3.4's "40–80 Pro seats" | Moderate: the draw is an owner input, the seat arithmetic is exact |
| H5 | Cumulative cash at M24 | **low $67k · base $186k · high $273k**, before any draw, counsel or licence fee (Table 2) | **Low**: rests on `docs/11` §2.3's unmeasured adoption bands, and the plan's 2026-09-20 row records that the free-to-Pro conversion argument behind them is now unsupported. The pre-sale (`docs/33` §6.4) is the instrument that replaces this assumption |
| H6 | ARR run-rate 12 months after the switch (M18) | low $57k · base $143k · high $205k (Tables 3a–3c) | Cross-check, not a forecast: `docs/11` §2.3 year 1 is $90k–200k; base sits inside it, low below it by construction, high at its ceiling. The model is not more optimistic than the market doc |
| H7 | The three inputs that move M24 cash most (Table 5) | **switch-back month** ($157k swing across M4→M13), **owner draw** ($144k across $0→6k), **Pro gross adds** ($111k across 2.5→7.5/mo). Infrastructure and model-call costs together swing under $4.1k | High: measured by the tornado over every input with a cited range |

The one-sentence version: infrastructure is not the constraint (`docs/01` §3.4 said so; Table 5 confirms it at
under 3% of the total swing); the model is decided by when the posture switches back, whether the owner's time is
paid, and whether the pre-sale delivers the adoption the market doc assumed.

## 1. Inputs and their sources

### 1.1 Facts (F)

| Input | Value used | Source | Retrieved |
|---|---|---|---|
| Compute, 3 × Hetzner `cx32` (app, worker, browser worker) | $7.69 each, $23.07/mo | `docs/60` §4 → https://www.vpsbenchmarks.com/hosters/hetzner/plans/cx32 | 2026-09-13 |
| Managed Postgres, Neon Launch, usage-based | $35–55/mo; midpoint $45 | `docs/60` §4 → https://neon.com/blog/new-usage-based-pricing | 2026-09-13 |
| Object storage, Cloudflare R2, ~200 GB | $3–6/mo; midpoint $4.50 | `docs/60` §4 → https://egresscost.com/cloudflare/ | 2026-09-13 |
| Edge (Cloudflare), email (Resend), observability (Sentry + Grafana free tiers), domain/secrets | $0–20, $20–40, $0–30, $5; midpoints $10, $30, $15, $5 | `docs/60` §4 carries them "unchanged from `docs/20` §14 (not re-verified)" | 2026-09-12 |
| Model calls once `services/modelgw` ships | $60–150/mo (≈5,000 assisted records at ≈$0.02); midpoint $105; **$0 today** because the gateway does not exist | `docs/60` §4, `docs/20` §14 | 2026-09-13 |
| Core-infrastructure ceiling | ≤ $415/mo (O-8) | `docs/04` §O-8 | — |
| X API pricing | $0.20 per post containing a URL; ≈$189/mo at 30 link posts/day incl. metrics reads; hard cap $250/mo | `docs/32` §4.7 → https://docs.x.com/x-api/getting-started/pricing; cap per `docs/04` §10 item 8 | 2026-09-12 |
| Bluesky API | $0 | `docs/32` §4.4 | 2026-09-12 |
| LinkedIn scheduler bridge until Community Management approval | "assume ≤ $30/month", **not verified** | `docs/32` §4.6 | 2026-09-12 |
| Attio CRM plans | Free up to 3 seats with no API/webhook access; Plus $35/seat/mo annual ($44 monthly), up to 10 seats, API and webhooks | https://attio.com/pricing | 2026-09-26 |
| Stripe card processing | 2.9% + 30¢ per successful domestic card charge; +1.5% international; +1% currency conversion | https://stripe.com/pricing | 2026-09-26 |
| Stripe Billing (subscriptions) | 0.7% of Billing volume, pay as you go, on top of processing | https://stripe.com/billing/pricing | 2026-09-26 |
| Pro price | $149/mo or $1,490/yr per seat | `docs/11` §3; `docs/41` "Pro" | 2026-09-12 |
| Founding Pro pre-sale price | $150/mo locked 24 months, first invoice at launch | `docs/33` §6.4 | 2026-09-12 |
| Team price | $9,000/yr, 5 seats (Cleanview parity, "verify before publishing") | `docs/11` §3; `docs/41` "Team" | 2026-09-12 |
| API add-on price | +$5,000/yr on Team; $25,000/yr standalone enterprise (not modelled: nothing behind it is built, `docs/41`) | `docs/11` §3 | 2026-09-12 |
| Paid tiers inactive while `noncommercial`; portal for existing subscribers unaffected | `PAID_TIERS_ACTIVE = platform_posture() != "noncommercial"`; `403 paid_tiers_inactive` | `docs/26` §3(i); `services/billing/router.py` | 2026-09-26 |
| Manifest sources priced at zero | none of the 113 manifest sources carries a fee; `docs/13` §6 prices no source | `docs/13` §6; `scripts/check_manifest_licences.py` "RESULT: PASS — 113 sources" | 2026-09-26 |
| PJM Redistribution License | "price unknown; owner enquiry" | `docs/20` §14; `docs/13` §1.3 | 2026-09-12 |
| Counsel items before first sale | privacy/erasure, PJM, customer terms are launch blockers; **no fee estimate anywhere in the repo** | `docs/00-PLAN.md` 2026-09-18 row (4); `docs/13` §7 | 2026-09-18 |
| Not yet deployed | "CI's preview-deploy job is a stub awaiting an environment secret; launch runbook written, not executed" | `docs/00-PLAN.md` phases table, Phase 4 | 2026-09-26 |
| Adoption bands, year 1 after launch | 40–80 Pro seats, 3–6 Team/API customers, ARR exit $90k–200k (moderate confidence there) | `docs/11` §2.3 | 2026-09-12 |
| Churn assumed by the market doc | 20%/yr Pro, 10%/yr Team | `docs/11` §2.3 (an assumption there, not a measurement) | 2026-09-12 |
| Conversion estimate status | "`docs/11` §3's 1–2% free-to-Pro conversion estimate rests on a superseded mechanism and has not been re-derived … now unsupported" | `docs/00-PLAN.md` decisions log 2026-09-20 | 2026-09-20 |

### 1.2 What is modelled, and how (I)

- **Clock.** Month 1 = 2026-10, the first production deploy (A-3). Costs before deploy are zero and are not modelled.
- **Posture.** Months 1 to `switch_month − 1` are `noncommercial`: revenue 0, Stripe fees 0, CRM on Attio Free
  (manual notes for discovery calls need no API). From `switch_month` the tiers of `docs/11` §3 are offered and
  Attio Plus (one seat, $35/mo) is added because the Attio adapter (`docs/34`) needs API access.
- **Pro** is billed monthly at $149 (Founding Pro's $150 is within rounding); cash equals recognised revenue.
  Churn 20%/yr applied monthly (1.84%/mo) to the opening balance, then the scenario's gross adds.
- **Team and API add-on** are billed annually in advance: cash lands at signing and at each renewal, recognition
  is spread over twelve months. 90% renew at each anniversary. The API add-on attaches to a fixed share of Team
  accounts (scenario axis).
- **Accounts are fractional expected values** (0.33 Team accounts per month in base). Real cash arrives in
  $9,000 lumps, so the early commercial months in Tables 3–4 are smoother than reality will be; the cumulative
  column is unaffected.
- **Stripe fees** = 3.6% of cash receipts (2.9% + 0.7% Billing) + $0.30 per charge, domestic cards only (A-13).
- **Break-even** has two definitions, both reported: *operating* = first month where recognised revenue net of
  fees ≥ that month's costs; *cash back to zero* = first month where cumulative cash is ≥ 0 and stays there.
- **Unpriced items** (owner draw, PJM licence, counsel) are parameters defaulting to zero, listed in the cost
  stack at $0 so their absence is visible, and swept in Table 5 with illustrative values labelled as such.

### 1.3 Scenarios (I)

The three scenarios differ **only** in gross adds and API attach; every other input is shared. Counts are
assumptions, not forecasts; they are pegged to `docs/11` §2.3's bands so the model cannot quietly exceed the
market doc.

| Scenario | Pro gross adds/mo | Team gross adds/yr | API attach | Why this level |
|---|---|---|---|---|
| low | 2 | 2 | 0% | The pre-sale returns 3–6 commitments ("continue and extend two weeks", `docs/33` §6.4) and the ramp stays at that pace: ~24 gross seats in the first 12 months, **below** the 40–80 band; Team below the 3–6 band; no API buyer because nothing behind the add-on beyond webhooks is built (`docs/41`) |
| base | 5 | 4 | 50% | ~60 gross seats in 12 months, the midpoint of 40–80; Team at the band's midpoint; half of Team desks take the +$5k add-on |
| high | 7 | 6 | 50% | ~84 gross seats, the top of the band; Team at the top of 3–6. Nothing above `docs/11`'s year-1 range is modelled |

## 2. Assumptions

| Id | Assumption | Depends on | Effect if wrong |
|---|---|---|---|
| A-1 | The posture switches back to `commercial` at **M7 (2027-04)** | The owner's "decide how to proceed once we're done" (2026-09-25); the counsel items of `docs/00-PLAN.md` 2026-09-18 (4) being paid for and closed before first sale | Table 6a: each three months later costs ≈ $50k of M24 cash and ≈ $30k of M24 ARR; never switching makes the whole 24 months a $10,643 cost. Earlier is symmetric. Base and high scenarios do not model any product benefit from the extra noncommercial data access, which is the stated reason for the posture |
| A-2 | `services/modelgw` ships at M4 and bills $105/mo from then | `docs/60` §4 (does not exist today); `docs/20` §4.5 | ±$105/mo; immaterial (Table 5 swing $1,890). If the county-permit pilot runs, its cap is $2 per accepted permit event (`docs/00-PLAN.md` 2026-09-15) and is not in this model |
| A-3 | Month 1 is the first production deploy, 2026-10 | The CI preview-deploy secret and `docs/40` being executed | Everything shifts by the slip; pre-deploy months cost $0 (the stack is not running) |
| A-4 | X runs from M1 at 30 link posts/day (≈$189/mo), never above the $250 cap | `docs/32` §7 launch calendar; the budget config `SOCIAL_BUDGET_X_MONTHLY_USD` | Deferring X to the switch month saves ≈ $1.1k over six months; running at the cap costs $61/mo more. Either way Table 5 ranks it ninth |
| A-5 | Pro is billed monthly; no annual prepay | `docs/33` §6.1 "annual prepay = two months free" | If half of seats prepay annually, Pro revenue per seat falls ≈ 8% ($1,490/12 = $124/mo) and cash arrives earlier; M24 cash falls by roughly $4–5k in base |
| A-6 | Team and API renew at 90% on each anniversary | `docs/11` §2.3 churn assumption | At 20% Team churn, M19–24 renewal cash falls 11%; the swing is bounded because only the first six Team cohorts renew inside the window |
| A-7 | Gross adds are constant from the switch month onward | The pre-sale result (`docs/33` §6.4), the 2% page-view-to-registration gate (`docs/00-PLAN.md` 2026-09-18 (8)) | Real ramps are S-shaped. Three zero-add months at the start remove ~15 seats and ~$30k of M24 cash in base; a later acceleration is not modelled and would be upside |
| A-8 | 50% of Team accounts take the API add-on (0% in low) | `docs/11` §3; no measurement; the add-on's bulk routes are `x-status: planned` (`docs/41`) | Table 5: ±$18.8k of M24 cash across 0–100% |
| A-9 | Fractional accounts are expected values | The modelling choice in §1.2 | Cash timing in single months is smoother than reality; cumulative totals unaffected |
| A-10 | **Owner labour is not a cash cost** ($0 draw) | `docs/00-PLAN.md` open question 2 (solo founder plus contractors, or a funded build), unanswered | This is the assumption that makes H3 near-vacuous. Table 6b prices it: $3,000/mo moves operating break-even to M10 and cash-positive to M13; $6,000/mo to M13 and M21. `docs/01` §3.4's warning stands: the margin problem is analyst hours, not compute |
| A-11 | No data-licence fees | `docs/13` §6 (no priced source); PJM unpriced (`docs/20` §14) | A hypothetical $1,000/mo PJM licence from the switch month costs $18k of M24 cash (Table 5). The real figure needs the owner's enquiry; PJM rows are link-out-only until then, so the revenue this model books does not depend on PJM |
| A-12 | Counsel spend is $0 in the model | `docs/13` §7 and the 2026-09-18 counsel order name privacy/erasure, PJM and customer terms as blockers before first sale, with no fee estimate | Any fee paid before the switch deepens the trough dollar-for-dollar (base trough is only $2,424, so counsel will dominate it) and reduces M24 cash by the same amount. Owner input; the parameter `counsel_one_off` exists for it |
| A-13 | All customers pay by US domestic card | Stripe pricing (§1.1) | UK/EU buyers add 1.5% (+1% conversion) on their share; under 1% of revenue |
| A-14 | Excluded: taxes, accounting, insurance, LLC filings, contractors, paid marketing, the $25k standalone enterprise API tier | `docs/11` §2.3 assumes no paid marketing; the enterprise tier has nothing built behind it | Each is a cost or revenue this model does not carry; contractors in particular belong with A-10 |
| A-15 | Prices hold at `docs/11` §3 through M24 | The Cleanview parity check `docs/41` asks for before publishing; the pre-sale reaction | Table 5: Pro at $199 (the `docs/33` list price) adds $37k of M24 cash; Team below $9,000 is not swept because the playbook forbids discounting Pro below $150 and only discounts Team for group licences |

## 3. The posture, in the model

- `noncommercial` months carry **only costs**: Table 1's first two columns. Nothing on the revenue side can
  leak in because checkout is refused at the API (`docs/26` §3(i)), not merely hidden on the page.
- The switch month is one parameter (`switch_month`), swept in Table 6a from M4 to never. The switch itself is
  the `docs/26` §5 runbook: flip, restart, every `noncommercial` row invisible, then purge-or-relicence per
  source. The model books no cost for that work (it is owner time, A-10) and no revenue loss from the rows that
  become invisible (Texas RRC Class VI and the GEM rows are not what any tier is priced on).
- What the posture buys is not in the model: the extra sources it admits (`docs/26` §2, §6) improve the product
  and therefore, plausibly, the adoption bands after the switch. That is an argument for a later switch that
  Table 6a cannot see, and the honest reading of Table 6a is "the cash cost of waiting, holding the product
  constant", not "switch as early as possible".

## 4. Tables (measured output of `scripts/financial_model.py`, run 2026-09-26)

<!-- generated by scripts/financial_model.py; do not edit the tables by hand -->

### Table 1. Monthly cost stack (USD), base parameters

| Line | USD/mo | Source |
|---|---|---|
| Compute: 3 x Hetzner cx32 (app, worker, browser worker) | 23.07 | docs/60 §4, list price |
| Managed Postgres (Neon, ~0.5 CU avg + PITR) | 45.00 | docs/60 §4, midpoint of 35–55 |
| Object storage ~200 GB (Cloudflare R2) | 4.50 | docs/60 §4, midpoint of 3–6 |
| Edge: DNS, CDN, WAF (Cloudflare) | 10.00 | docs/60 §4, midpoint of 0–20 |
| Transactional email (Resend) | 30.00 | docs/60 §4, midpoint of 20–40 |
| Error tracking, logs, metrics (Sentry + Grafana free tiers) | 15.00 | docs/60 §4, midpoint of 0–30 |
| Domain, certificates, secrets tooling | 5.00 | docs/60 §4 |
| **Infrastructure core subtotal** | **132.57** | docs/60 §4 range 80–170 |

| Line | noncommercial, before model gateway (M1–M3) | noncommercial, model gateway live (M4–M6) | commercial (from switch month M7) |
|---|---|---|---|
| Infrastructure core (docs/60 §4) | 132.57 | 132.57 | 132.57 |
| Model calls (docs/60 §4, from gateway ship month) | 0.00 | 105.00 | 105.00 |
| X posting (docs/32 §4.7) | 189.00 | 189.00 | 189.00 |
| LinkedIn scheduler bridge (docs/32 §4.6) | 30.00 | 30.00 | 30.00 |
| CRM seat, Attio Plus (from switch month) | 0.00 | 0.00 | 35.00 |
| PJM licence (unpriced, parameter) | 0.00 | 0.00 | 0.00 |
| Owner draw (unpriced, parameter) | 0.00 | 0.00 | 0.00 |
| Counsel one-off (unpriced, parameter) | 0.00 | 0.00 | 0.00 |
| **Total per month** | **351.57** | **456.57** | **491.57** |

### Table 2. Scenario summary

| Scenario | Pro adds/mo / Team adds/yr / API attach | Operating break-even | Cash back to zero | Cash trough | Cumulative cash M24 | Seats / Team / API at M24 | Recognised revenue M24 | ARR run-rate M24 |
|---|---|---|---|---|---|---|---|---|
| low | 2 / 2 / 0% | M8 (2027-05) | M8 (2027-05) | -$2,424 at M6 | $66,823 | 31 / 2.9 / 0.0 | $6,776 | $81,313 |
| base | 5 / 4 / 50% | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $185,845 | 77 / 5.8 / 2.9 | $17,061 | $204,734 |
| high | 7 / 6 / 50% | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $273,339 | 108 / 8.7 / 4.3 | $24,441 | $293,297 |

### Table 3a. Monthly model, low scenario

| M | Month | Posture | Pro seats | Team | API | Recognised rev | Cash in | Stripe fees | Costs | Net cash | Cumulative cash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| M1 | 2026-10 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$352 |
| M2 | 2026-11 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$703 |
| M3 | 2026-12 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$1,055 |
| M4 | 2027-01 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$1,511 |
| M5 | 2027-02 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$1,968 |
| M6 | 2027-03 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$2,424 |
| M7 | 2027-04 | commercial | 2.0 | 0.17 | 0.00 | $423 | $1,798 | $65 | $492 | $1,241 | -$1,183 |
| M8 | 2027-05 | commercial | 4.0 | 0.33 | 0.00 | $841 | $2,091 | $76 | $492 | $1,522 | $339 |
| M9 | 2027-06 | commercial | 5.9 | 0.50 | 0.00 | $1,253 | $2,378 | $87 | $492 | $1,799 | $2,138 |
| M10 | 2027-07 | commercial | 7.8 | 0.67 | 0.00 | $1,659 | $2,659 | $98 | $492 | $2,070 | $4,207 |
| M11 | 2027-08 | commercial | 9.6 | 0.83 | 0.00 | $2,061 | $2,936 | $109 | $492 | $2,336 | $6,543 |
| M12 | 2027-09 | commercial | 11.5 | 1.00 | 0.00 | $2,458 | $3,208 | $119 | $492 | $2,597 | $9,140 |
| M13 | 2027-10 | commercial | 13.2 | 1.17 | 0.00 | $2,849 | $3,474 | $129 | $492 | $2,854 | $11,994 |
| M14 | 2027-11 | commercial | 15.0 | 1.33 | 0.00 | $3,236 | $3,736 | $139 | $492 | $3,105 | $15,099 |
| M15 | 2027-12 | commercial | 16.7 | 1.50 | 0.00 | $3,618 | $3,993 | $149 | $492 | $3,352 | $18,451 |
| M16 | 2028-01 | commercial | 18.4 | 1.67 | 0.00 | $3,995 | $4,245 | $158 | $492 | $3,595 | $22,046 |
| M17 | 2028-02 | commercial | 20.1 | 1.83 | 0.00 | $4,367 | $4,492 | $168 | $492 | $3,833 | $25,879 |
| M18 | 2028-03 | commercial | 21.7 | 2.00 | 0.00 | $4,735 | $4,735 | $177 | $492 | $4,066 | $29,945 |
| M19 | 2028-04 | commercial | 23.3 | 2.15 | 0.00 | $5,086 | $6,323 | $235 | $492 | $5,597 | $35,542 |
| M20 | 2028-05 | commercial | 24.9 | 2.30 | 0.00 | $5,432 | $6,557 | $244 | $492 | $5,822 | $41,365 |
| M21 | 2028-06 | commercial | 26.4 | 2.45 | 0.00 | $5,775 | $6,787 | $252 | $492 | $6,043 | $47,408 |
| M22 | 2028-07 | commercial | 27.9 | 2.60 | 0.00 | $6,113 | $7,013 | $261 | $492 | $6,260 | $53,668 |
| M23 | 2028-08 | commercial | 29.4 | 2.75 | 0.00 | $6,446 | $7,234 | $269 | $492 | $6,473 | $60,141 |
| M24 | 2028-09 | commercial | 30.9 | 2.90 | 0.00 | $6,776 | $7,451 | $278 | $492 | $6,682 | $66,823 |

### Table 3b. Monthly model, base scenario

| M | Month | Posture | Pro seats | Team | API | Recognised rev | Cash in | Stripe fees | Costs | Net cash | Cumulative cash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| M1 | 2026-10 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$352 |
| M2 | 2026-11 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$703 |
| M3 | 2026-12 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$1,055 |
| M4 | 2027-01 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$1,511 |
| M5 | 2027-02 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$1,968 |
| M6 | 2027-03 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$2,424 |
| M7 | 2027-04 | commercial | 5.0 | 0.33 | 0.17 | $1,064 | $4,578 | $166 | $492 | $3,920 | $1,496 |
| M8 | 2027-05 | commercial | 9.9 | 0.67 | 0.33 | $2,115 | $5,310 | $194 | $492 | $4,624 | $6,120 |
| M9 | 2027-06 | commercial | 14.7 | 1.00 | 0.50 | $3,152 | $6,027 | $222 | $492 | $5,314 | $11,434 |
| M10 | 2027-07 | commercial | 19.5 | 1.33 | 0.67 | $4,176 | $6,732 | $248 | $492 | $5,992 | $17,426 |
| M11 | 2027-08 | commercial | 24.1 | 1.67 | 0.83 | $5,187 | $7,424 | $275 | $492 | $6,657 | $24,083 |
| M12 | 2027-09 | commercial | 28.7 | 2.00 | 1.00 | $6,186 | $8,102 | $300 | $492 | $7,310 | $31,394 |
| M13 | 2027-10 | commercial | 33.1 | 2.33 | 1.17 | $7,172 | $8,769 | $326 | $492 | $7,951 | $39,345 |
| M14 | 2027-11 | commercial | 37.5 | 2.67 | 1.33 | $8,145 | $9,423 | $351 | $492 | $8,581 | $47,926 |
| M15 | 2027-12 | commercial | 41.8 | 3.00 | 1.50 | $9,107 | $10,065 | $375 | $492 | $9,198 | $57,124 |
| M16 | 2028-01 | commercial | 46.1 | 3.33 | 1.67 | $10,056 | $10,695 | $399 | $492 | $9,805 | $66,929 |
| M17 | 2028-02 | commercial | 50.2 | 3.67 | 1.83 | $10,994 | $11,314 | $423 | $492 | $10,400 | $77,328 |
| M18 | 2028-03 | commercial | 54.3 | 4.00 | 2.00 | $11,921 | $11,921 | $446 | $492 | $10,984 | $88,312 |
| M19 | 2028-04 | commercial | 58.3 | 4.30 | 2.15 | $12,804 | $15,967 | $593 | $492 | $14,883 | $103,195 |
| M20 | 2028-05 | commercial | 62.2 | 4.60 | 2.30 | $13,677 | $16,552 | $615 | $492 | $15,445 | $118,640 |
| M21 | 2028-06 | commercial | 66.1 | 4.90 | 2.45 | $14,539 | $17,126 | $637 | $492 | $15,998 | $134,638 |
| M22 | 2028-07 | commercial | 69.8 | 5.20 | 2.60 | $15,390 | $17,690 | $658 | $492 | $16,540 | $151,178 |
| M23 | 2028-08 | commercial | 73.6 | 5.50 | 2.75 | $16,231 | $18,243 | $679 | $492 | $17,072 | $168,251 |
| M24 | 2028-09 | commercial | 77.2 | 5.80 | 2.90 | $17,061 | $18,786 | $700 | $492 | $17,595 | $185,845 |

### Table 3c. Monthly model, high scenario

| M | Month | Posture | Pro seats | Team | API | Recognised rev | Cash in | Stripe fees | Costs | Net cash | Cumulative cash |
|---|---|---|---|---|---|---|---|---|---|---|---|
| M1 | 2026-10 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$352 |
| M2 | 2026-11 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$703 |
| M3 | 2026-12 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $352 | -$352 | -$1,055 |
| M4 | 2027-01 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$1,511 |
| M5 | 2027-02 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$1,968 |
| M6 | 2027-03 | noncommercial | 0.0 | 0.00 | 0.00 | $0 | $0 | $0 | $457 | -$457 | -$2,424 |
| M7 | 2027-04 | commercial | 7.0 | 0.50 | 0.25 | $1,522 | $6,793 | $247 | $492 | $6,055 | $3,630 |
| M8 | 2027-05 | commercial | 13.9 | 1.00 | 0.50 | $3,025 | $7,817 | $286 | $492 | $7,039 | $10,670 |
| M9 | 2027-06 | commercial | 20.6 | 1.50 | 0.75 | $4,509 | $8,822 | $324 | $492 | $8,006 | $18,676 |
| M10 | 2027-07 | commercial | 27.2 | 2.00 | 1.00 | $5,975 | $9,808 | $361 | $492 | $8,955 | $27,631 |
| M11 | 2027-08 | commercial | 33.7 | 2.50 | 1.25 | $7,422 | $10,776 | $398 | $492 | $9,886 | $37,517 |
| M12 | 2027-09 | commercial | 40.1 | 3.00 | 1.50 | $8,852 | $11,727 | $434 | $492 | $10,801 | $48,318 |
| M13 | 2027-10 | commercial | 46.4 | 3.50 | 1.75 | $10,264 | $12,660 | $470 | $492 | $11,698 | $60,016 |
| M14 | 2027-11 | commercial | 52.5 | 4.00 | 2.00 | $11,659 | $13,575 | $505 | $492 | $12,579 | $72,595 |
| M15 | 2027-12 | commercial | 58.6 | 4.50 | 2.25 | $13,037 | $14,474 | $539 | $492 | $13,444 | $86,039 |
| M16 | 2028-01 | commercial | 64.5 | 5.00 | 2.50 | $14,398 | $15,356 | $572 | $492 | $14,292 | $100,331 |
| M17 | 2028-02 | commercial | 70.3 | 5.50 | 2.75 | $15,743 | $16,222 | $605 | $492 | $15,126 | $115,457 |
| M18 | 2028-03 | commercial | 76.0 | 6.00 | 3.00 | $17,073 | $17,073 | $638 | $492 | $15,943 | $131,400 |
| M19 | 2028-04 | commercial | 81.6 | 6.45 | 3.23 | $18,338 | $23,082 | $856 | $492 | $21,734 | $153,135 |
| M20 | 2028-05 | commercial | 87.1 | 6.90 | 3.45 | $19,588 | $23,901 | $887 | $492 | $22,522 | $175,657 |
| M21 | 2028-06 | commercial | 92.5 | 7.35 | 3.67 | $20,824 | $24,705 | $918 | $492 | $23,296 | $198,953 |
| M22 | 2028-07 | commercial | 97.8 | 7.80 | 3.90 | $22,044 | $25,494 | $948 | $492 | $24,055 | $223,008 |
| M23 | 2028-08 | commercial | 103.0 | 8.25 | 4.12 | $23,250 | $26,269 | $977 | $492 | $24,800 | $247,808 |
| M24 | 2028-09 | commercial | 108.1 | 8.70 | 4.35 | $24,441 | $27,029 | $1,006 | $492 | $25,531 | $273,339 |

### Table 4. Revenue by tier after the switch, base scenario (recognised / cash)

| M | Month | Pro | Team | API add-on | Total |
|---|---|---|---|---|---|
| M7 | 2027-04 | $745 / $745 | $250 / $3,000 | $69 / $833 | $1,064 / $4,578 |
| M8 | 2027-05 | $1,476 / $1,476 | $500 / $3,000 | $139 / $833 | $2,115 / $5,310 |
| M9 | 2027-06 | $2,194 / $2,194 | $750 / $3,000 | $208 / $833 | $3,152 / $6,027 |
| M10 | 2027-07 | $2,899 / $2,899 | $1,000 / $3,000 | $278 / $833 | $4,176 / $6,732 |
| M11 | 2027-08 | $3,590 / $3,590 | $1,250 / $3,000 | $347 / $833 | $5,187 / $7,424 |
| M12 | 2027-09 | $4,269 / $4,269 | $1,500 / $3,000 | $417 / $833 | $6,186 / $8,102 |
| M13 | 2027-10 | $4,935 / $4,935 | $1,750 / $3,000 | $486 / $833 | $7,172 / $8,769 |
| M14 | 2027-11 | $5,590 / $5,590 | $2,000 / $3,000 | $556 / $833 | $8,145 / $9,423 |
| M15 | 2027-12 | $6,232 / $6,232 | $2,250 / $3,000 | $625 / $833 | $9,107 / $10,065 |
| M16 | 2028-01 | $6,862 / $6,862 | $2,500 / $3,000 | $694 / $833 | $10,056 / $10,695 |
| M17 | 2028-02 | $7,480 / $7,480 | $2,750 / $3,000 | $764 / $833 | $10,994 / $11,314 |
| M18 | 2028-03 | $8,088 / $8,088 | $3,000 / $3,000 | $833 / $833 | $11,921 / $11,921 |
| M19 | 2028-04 | $8,684 / $8,684 | $3,225 / $5,700 | $896 / $1,583 | $12,804 / $15,967 |
| M20 | 2028-05 | $9,269 / $9,269 | $3,450 / $5,700 | $958 / $1,583 | $13,677 / $16,552 |
| M21 | 2028-06 | $9,843 / $9,843 | $3,675 / $5,700 | $1,021 / $1,583 | $14,539 / $17,126 |
| M22 | 2028-07 | $10,406 / $10,406 | $3,900 / $5,700 | $1,083 / $1,583 | $15,390 / $17,690 |
| M23 | 2028-08 | $10,960 / $10,960 | $4,125 / $5,700 | $1,146 / $1,583 | $16,231 / $18,243 |
| M24 | 2028-09 | $11,503 / $11,503 | $4,350 / $5,700 | $1,208 / $1,583 | $17,061 / $18,786 |

### Table 5. Sensitivity ranking (tornado), base scenario, effect on cumulative cash at M24

| Input | Low → high | Δ cash M24 at low / at high | Swing | Operating break-even at low / high | Range source |
|---|---|---|---|---|---|
| Switch-back month | 4 → 13 | $57,193 / -$100,273 | $157,466 | M4 (2027-01) / M13 (2027-10) | A-1; owner has not set a date |
| Owner draw per month (illustrative) | 0 → 6000 | $0 / -$144,000 | $144,000 | M7 (2027-04) / M13 (2027-10) | unpriced; owner input |
| Pro gross adds per month | 2.5 → 7.5 | -$55,325 / $55,325 | $110,651 | M7 (2027-04) / M7 (2027-04) | docs/11 §2.3 year-1 band 40–80 seats |
| Team gross adds per month | 0.166667 → 0.5 | -$43,234 / $43,234 | $86,467 | M7 (2027-04) / M7 (2027-04) | docs/11 §2.3 band 3–6 per year |
| API attach rate on Team | 0 → 1 | -$18,797 / $18,797 | $37,594 | M7 (2027-04) / M7 (2027-04) | docs/11 §3; no measurement |
| Pro price per seat-month | 149 → 199 | $0 / $37,209 | $37,209 | M7 (2027-04) / M7 (2027-04) | docs/33 §6.1: pre-sale $150, list $200–250 |
| PJM licence per month (illustrative) | 0 → 1000 | $0 / -$18,000 | $18,000 | M7 (2027-04) / M8 (2027-05) | unpriced; docs/20 §14 |
| Pro annual churn | 0.1 → 0.35 | $6,038 / -$9,470 | $15,507 | M7 (2027-04) / M7 (2027-04) | docs/11 §2.3 assumes 20%; no measurement yet |
| X posting per month | 0 → 250 | $4,536 / -$1,464 | $6,000 | M7 (2027-04) / M7 (2027-04) | docs/32 §4.7: off, or the $250 cap |
| Infrastructure core | 80 → 170 | $1,262 / -$898 | $2,160 | M7 (2027-04) / M7 (2027-04) | docs/60 §4 range |
| Model calls per month | 60 → 150 | $945 / -$945 | $1,890 | M7 (2027-04) / M7 (2027-04) | docs/60 §4 range once gateway ships |

Reference (base): cumulative cash at M24 $185,845, operating break-even M7 (2027-04).

### Table 6a. Grid: Switch-back month

| Switch-back month | Operating break-even | Cash back to zero | Cash trough | Cumulative cash M24 | ARR M24 |
|---|---|---|---|---|---|
| 4 (2027-01) | M4 (2027-01) | M4 (2027-01) | -$1,055 at M3 | $243,039 | $233,923 |
| 7 (2027-04) | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $185,845 | $204,734 |
| 10 (2027-07) | M10 (2027-07) | M10 (2027-07) | -$3,794 at M9 | $133,268 | $174,463 |
| 13 (2027-10) | M13 (2027-10) | M14 (2027-11) | -$5,164 at M12 | $85,573 | $143,050 |
| never (cost only) | not within 24 months | not within 24 months | -$10,643 at M24 | -$10,643 | $0 |

### Table 6b. Grid: Owner draw per month (illustrative)

| Owner draw per month (illustrative) | Operating break-even | Cash back to zero | Cash trough | Cumulative cash M24 | ARR M24 |
|---|---|---|---|---|---|
| 0 | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $185,845 | $204,734 |
| 1500 | M9 (2027-06) | M10 (2027-07) | -$11,424 at M6 | $149,845 | $204,734 |
| 3000 | M10 (2027-07) | M13 (2027-10) | -$20,424 at M6 | $113,845 | $204,734 |
| 4500 | M12 (2027-09) | M17 (2028-02) | -$30,004 at M7 | $77,845 | $204,734 |
| 6000 | M13 (2027-10) | M21 (2028-06) | -$42,574 at M10 | $41,845 | $204,734 |

### Table 6c. Grid: Pro gross adds per month

| Pro gross adds per month | Operating break-even | Cash back to zero | Cash trough | Cumulative cash M24 | ARR M24 |
|---|---|---|---|---|---|
| 2.5 | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $130,520 | $135,717 |
| 3.75 | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $158,183 | $170,225 |
| 5 | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $185,845 | $204,734 |
| 6.25 | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $213,508 | $239,242 |
| 7.5 | M7 (2027-04) | M7 (2027-04) | -$2,424 at M6 | $241,171 | $273,750 |


## 5. The case against these numbers, and the case for

### 5.1 Strongest case against (I)

1. **The adoption bands have no measured basis.** The plan's 2026-09-20 row says the free-to-Pro conversion
   argument in `docs/11` §3 "is now unsupported". Every scenario here inherits that gap; the low scenario is
   not a floor, it is the lowest *modelled* case. The genuine floor is the cost-only row of Table 6a:
   **−$10,643 over 24 months** with zero customers. That floor is small, which is the strongest thing that can
   be said for the plan, and it says nothing about revenue.
2. **Labour is the real burn and the model prices it at zero.** `docs/01` §3.4: "the gross margin problem is
   not compute; it is the analyst hours in data QA". Data QA, the docs/13 counsel items, the switch-back
   runbook, the discovery calls and the pre-sale are all hours. At any realistic draw (Table 6b) the break-even
   month moves from "the switch month" to two to four quarters later, and the trough from $2.4k to $20–43k.
   Whoever reads H3 without H4 is being misled by the arithmetic.
3. **The switch may not happen in the window.** The owner's decision is to decide later. If "later" is
   past M13, base-scenario M24 cash is under $86k and ARR under $143k (Table 6a); if it never happens, the
   platform is a $5.3k/yr cost centre with a free audience and no revenue line. That is a legitimate outcome
   under the stated vision (data access first), but it is not a business plan, and this model cannot make it one.
4. **Team is being sold as "seats plus support" until export and watchlists exist** (`docs/00-PLAN.md`
   2026-09-18 (3); `docs/41`). Two of the four paid shapes are unbuilt and the API add-on's bulk routes are
   `planned`. The Team and API lines in Table 4 assume buyers pay `docs/11` §3 prices for what is built today;
   Cleanview at the same $9,000 ships a complete tracker. Team adds may lag the 3–6 band until the shop sprint ships.
5. **Competitor response.** `docs/11` §5.3 puts ~50% on Enverus, NPM or Halcyon shipping explicit supply/demand
   matching within 24 months. Nothing in the adoption bands discounts for it.
6. **Counsel and PJM are unpriced, and both precede first sale.** They are the two inputs most likely to turn
   the $2.4k trough into a five-figure one, and the repo has no estimate for either.

### 5.2 Case for (I)

1. **The downside is bounded and known.** Table 1 is the whole cash exposure absent labour: about $5.5k/yr, well
   inside the O-8 ceiling (core infrastructure is $238/mo against $415), and independent of adoption.
2. **Annual Team billing front-loads cash.** In base, Team and API deliver $3,833 of cash in M7 against
   $319 of recognised revenue that month (Table 4), so the cash position turns positive the month the posture switches
   even before Pro has ten seats. That holds only if the first Team desks sign in the first commercial quarter.
3. **Break-even in seats is consistent across three documents.** `docs/01` §3.4 said 40–80 Pro seats for a
   one-person operation; Table 6b implies ~45 seats for a $6,000/mo draw; `docs/11` §2.3's year-1 midpoint is 60.
   Three independent framings land in the same place, which is weak evidence the shape is right even though the
   level is unmeasured.
4. **Infrastructure and model costs do not matter to the outcome.** Table 5 puts their combined swing under
   $4.1k. The [B-1] Neon re-measurement and the model-gateway price can be wrong by their whole range without
   changing any headline. Attention belongs on A-1, A-10 and A-7.

### 5.3 Calibrated view (I)

- Probability that the posture switches back inside the 24-month window: **~60%** (the owner has twice chosen
  data access over revenue in the last two days, and the counsel items are unfunded; the pricing surfaces were
  deliberately kept rather than removed, which cuts the other way).
- Probability that, given a switch by M7, ARR at M18 lands inside `docs/11` §2.3's year-1 band ($90k–200k):
  **~40%** (unmeasured conversion; unbuilt Team shapes; competitor response).
- Probability that the 24-month cash position is negative at M24 with a $3,000/mo draw and a switch by M7:
  **~30%** (Table 6b says +$114k in base; the low scenario with the same draw is roughly −$5k, so the outcome
  turns on whether the pre-sale returns more than ~3 seats a month).

## 6. Measurements that replace assumptions

| Assumption | Measurement | Where it comes from | When |
|---|---|---|---|
| A-7, A-15 (adoption, price) | Founding Pro commitments by week 8: <3 kill, 3–6 continue, ≥7 open Team conversations | `docs/33` §6.4; the weekly pipeline report §7.3 | Weeks 6–8 after discovery starts |
| A-7 | Company-or-asset page views → registrations ≥ 2% | The Engagement page with the page-view denominator (`docs/00-PLAN.md` 2026-09-18 (8)) | First 30 days after deploy |
| H1 Neon line | Actual CU-hours and PITR GB-month on the first bill | `docs/60` [B-1] | 30 days after `staging` |
| A-4 | X console spend against the $250 cap; cost per sign-up | `docs/32` §5 metrics; `SOCIAL_BUDGET_X_MONTHLY_USD` | Day 15 and day 30 of the launch calendar |
| A-11, A-12 | PJM licence quote; counsel fee quotes for the three blockers | Owner enquiry (`docs/20` §14); counsel order (`docs/00-PLAN.md` 2026-09-18 (4)) | Before the switch |
| A-10 | The owner's answer to open question 2 | `docs/00-PLAN.md` open questions | Owner |
| A-1 | The switch date | Owner decision, recorded in the decisions log | Owner |

When any of these lands, change the parameter in `scripts/financial_model.py`, re-run, and paste the new §4 over
the old one; do not edit the tables by hand.

## 7. Reproduction and what the script does not do

```
.venv/bin/python scripts/financial_model.py
```

Pure Python, no dependencies, deterministic; passes `ruff check`, `ruff format --check` and `mypy --strict`.
It does not read the database, the manifest or any live price page: every input is a literal in `Params` with
its source in a comment, so a change to a source must be made in the script and cited in §1.1 here. It does not
model taxes, accounting, insurance, contractors, paid marketing, the standalone enterprise API tier, or any
revenue from the Bankable deal workflow (a later consideration, not a foundation — `CLAUDE.md`).
