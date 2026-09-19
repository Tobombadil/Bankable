# Feasibility: an energy & infrastructure proposals platform

**Status:** Phase 0 deliverable · 2026-09-12 · evidence in `data/sources.yaml` and `data/probes/2026-09-12.json`

> **Addendum 2026-09-12 (owner direction):** where this document treats the existing Bankable workflow
> (analyse → certify → route → fund) as the destination or the reason to build, read it as a *later
> consideration*. The platform stands alone; the analysis of the graph, feed, tiers and market is unchanged.

## 1. The question, and a reframing

The brief: build a platform that scrapes the internet and public databases for active energy and related
infrastructure proposals, lists them, syndicates them to social platforms, monetises via API / live-vs-delayed
access, and later grows into a "bankable" site where sponsors submit projects tailored to proposals and
funding opportunities.

Before assessing feasibility I want to challenge the framing, because the feasibility answer depends on it.

**Fact:** bankablehq.com already exists and describes itself as "a deal intelligence platform that analyzes,
scores, and routes infrastructure projects to the right capital partners — powered by AI" (Analyze → Improve →
Certify → Route → Fund). That is the *second* half of the brief. It is live as a Lovable app fronted by a
Cloudflare worker in this repo.

**Inference:** the proposals scraper is not a new business. It is the top of the funnel and the data asset for
the business you have already named. Framed as "scrape and list", it competes head-on with free and cheap
incumbents and has no moat. Framed as "the fused supply/demand graph that feeds Bankable's scoring and
routing", it has a defensible purpose and a clear buyer.

The rest of this document evaluates both framings and recommends the second.

## 2. Lenses used, and why

| Lens | Why it is load-bearing here |
|---|---|
| Competitive / market | "Is anyone already doing this, and for free?" decides whether listing alone is a product |
| Legal / data rights | Redistribution terms on ISO data and the EU database right decide what can be resold |
| Data engineering | The hard part is fusion and entity resolution, not fetching; this sets cost and timeline |
| Unit economics | Price anchors from Cleanview and Halcyon bound what the market will pay |
| Distribution / media | Social APIs changed materially in 2026; this sets the cost of the syndication idea |
| Policy environment | 2025–26 federal changes altered the demand side of the marketplace |

Each lens is stated independently, then synthesised in §4.

## 3. The lenses, independently

### 3.1 Competitive / market lens

**Facts (verified this session):**

- Interconnection.fyi (GridTracker) publishes a free, daily-updated, cleaned queue dataset covering 50+ ISOs and
  non-ISO utilities including the Southeast and Canada, and sells the full dataset by CSV, Snowflake share and API.
- LBNL "Queued Up" 2026 (CC BY 4.0) gives the project-level history: ~8,200 active projects, 1,312 GW generation and
  749 GW storage at end-2025.
- gridstatus (open source) pulls seven ISO queues in one call. Five of seven worked from this container today
  (CAISO 2,278 rows, ERCOT 1,778, SPP 3,074, NYISO 3,164, ISO-NE 1,751). PJM needs a free key; MISO is behind a
  Cloudflare challenge.
- Cleanview sells its power-projects tracker at $9,000/yr for five seats, API add-on $5,000/yr, one-off download
  $4,900. Halcyon sells regulatory-filing intelligence from $99/month to $50k+/yr. Enverus PRISM is the enterprise
  incumbent (Energy Acuity folded into it in 2025). Energy Adepto aggregates utility RFPs. Paces sells site/permit
  intelligence. interconnectionqueue.org and ZEG publish queue trackers.

**Inference:** "scrape ISO queues and list them" is a solved, partly free, commodity. A new entrant listing the
same rows cannot charge for them. What none of the above does is fuse *supply* (queued and permitted projects)
with *demand* (utility RFPs, DOE/USDA/MDB funding, tenders) and with *stage evidence* (FERC filings, NEPA, state
siting) into a single proposal record with a lifecycle, and then act on it (routing to capital). That fusion is
where Bankable's existing thesis lives.

**Confidence:** high that listing alone fails as a paid product; moderate-high that fusion is unoccupied (I have
not seen every enterprise product's roadmap; Enverus could ship it).

### 3.2 Legal / data-rights lens

**Facts:**

- US federal works are public domain (17 U.S.C. §105): FERC, EIA, DOE, EPA, BLM, BOEM, USACE, grants.gov,
  SAM.gov, USAspending, the Permitting Dashboard. No reuse restriction beyond rate courtesy.
- ISOs are not federal agencies. Terms differ:
  - ERCOT: raw public data "may be used, reproduced, and redistributed in compilations, charts, and analyses". Open.
  - PJM Data Miner: "for internal use only … redistribution … strictly prohibited without an effective PJM-issued
    Redistribution License". Non-members limited to 6 connections/min. Restricted.
  - CAISO: credit required; no republishing "except as authorized". Attribution with residual risk.
  - MISO, SPP, NYISO, ISO-NE: terms pages were not retrievable from this container (403/404/503). Unknown; must be
    read from a browser and recorded before launch.
- Scraping public pages is not a CFAA violation in the Ninth Circuit (hiQ v. LinkedIn, reaffirmed 2022, after Van
  Buren), but breach-of-contract claims on terms of service remain live, which is the claim that ultimately sank hiQ.
- EU: TED is reusable with attribution under the Commission's reuse decision; the sui generis database right
  (Directive 96/9/EC) protects *private* aggregators such as Interconnection.fyi, Cleanview, Energy Adepto, BidNet.
  DSM Article 4 lets rights-holders opt out of commercial text-and-data mining via machine-readable signals.
- Personal data exposure is small (filer names, contact emails on notices) but non-zero: GDPR/CCPA hygiene applies.

**Inference:** a per-source legal register is a launch requirement, not paperwork. Two design rules follow:
publish *derived* records (normalised, enriched, linked) rather than mirroring raw ISO tables, and never scrape a
private aggregator. PJM specifically needs a licence conversation before its rows appear on a public page.

**Confidence:** high on the federal/ERCOT/PJM facts; low on MISO/SPP/NYISO/ISO-NE until the terms are read.

### 3.3 Data-engineering lens

**Facts:**

- Fetching is cheap: 28 of 38 probed endpoints returned useful responses on first contact with a one-second
  pause between calls. Formats are xlsx, csv, JSON, OCDS JSON, CKAN, ArcGIS REST, RSS, and one undocumented
  JSON backend (FERC eLibrary, which its own community client notes returns intermittent 5xx).
- gridstatus already normalises the seven ISO queues to one column set (Queue ID, Project Name, Interconnecting
  Entity, County, State, Interconnection Location, Transmission Owner, Generation Type, Capacity (MW), Queue Date,
  Status, Proposed Completion Date, Withdrawn Date, Actual Completion Date). That is a usable seed schema.
- Status vocabularies differ (ACTIVE/COMPLETED/WITHDRAWN; Active/Completed; blank). 216 SPP rows have no status.
- The same physical project appears in a queue, in EIA-860M (with plant ID), in a state siting docket, in a FERC
  LGIA filing, in a NEPA notice, and in a press release, each with a different name and sponsor spelling.
- Roughly a quarter to a third of US queued capacity sits outside the seven ISOs, on ~40 utility OASIS sites with
  no common format. Blockers observed today: Cloudflare (MISO), WAF on non-browser agents (ferc.gov pages,
  emp.lbl.gov, aemo.com.au), SPA-only apps (BLM ePlanning, ENTSO-E TYNDP map).

**Inference:** the cost centre is entity resolution and status harmonisation, not connectors. Budget the work as
roughly 20% fetch, 30% normalise, 50% resolve/enrich/QA. Headless-browser capacity and a residential egress
option are needed for perhaps 20% of sources. A change-detection layer (snapshot diffs) is what turns static
registers into "news".

**Confidence:** high.

### 3.4 Unit-economics lens

**Facts:** price anchors above. Fixed infrastructure for an MVP at this scale (tens of thousands of records,
daily refresh, search, map, alerts) is in the low hundreds of dollars per month on managed Postgres plus a small
worker fleet. X posting with links costs $0.20 per post; Bluesky is free; LinkedIn organisation posting is free
once Marketing Developer Platform approval (2–8 weeks) is granted.

**Inference:** a three-tier ladder is consistent with the market: free delayed (7–30 day lag, attribution, SEO and
social feed), Pro live (~$100–250/month per seat: alerts, filters, exports), Team/API (~$5–15k/yr). The gross
margin problem is not compute; it is the analyst hours in data QA and the licence fees for PJM-class sources.
Break-even on a one-person operation is plausibly 40–80 Pro seats or 5–8 API customers. The larger revenue is
downstream: Bankable's routing/certification fees, for which the proposals graph is the lead engine.

**Confidence:** moderate. TAM is bounded: on the order of 1,000–2,000 US organisations develop, finance, build or
advise on utility-scale projects; international widens it but each market adds connectors.

### 3.5 Distribution / media lens

**Facts:** X moved new developers to pay-per-use in February 2026 and removed follow/like endpoints in April. LinkedIn
Community Management API needs partner approval. Reddit's commercial API is contract-only (~$0.24 per 1,000 calls;
reports of $12k/month tiers). Bluesky is free with generous limits. Meta requires business verification.

**Inference:** automated syndication is feasible and cheap on Bluesky and LinkedIn (once approved); X is a budget
line (~$300/month at 50 link posts/day); Reddit and Meta are manual or skipped. The real distribution asset is an
owned channel: a per-topic RSS/email alert that every social post links back to. Social is acquisition, not the product.

**Confidence:** high.

### 3.6 Policy-environment lens (demand side)

**Facts:** the One Big Beautiful Bill Act (July 2025) accelerated the 45Y/48E phase-out for wind and solar
(begin construction after 5 July 2026 → placed in service by end-2027) while leaving storage, nuclear, geothermal
and others with a longer runway, and added FEOC restrictions. DOE cancelled 223 awards (~$7.6B) in October 2025;
litigation continues and some cancellations have been overturned. EERE (now CMEI) posts far fewer FOAs than 2023–24.
LPO is reoriented to nuclear, geothermal, critical minerals and transmission (~$50B new loans anticipated FY26).
USDA REAP grants are paused pending new rules; guaranteed loans remain. ERCOT's large-load queue is 438–474 GW,
~90% data centres; the PUCT approved "Batch Zero" in June 2026.

**Inference:** the funding-opportunity side is smaller and more volatile than in 2023–24, which cuts two ways. It
reduces the volume of "opportunities" to list, but raises the value of a service that tracks status *changes*
(cancellation, freeze, reinstatement, new FOA) and of non-federal demand: utility all-source RFPs, state programmes,
MDB tenders, and the data-centre/large-load wave. Storage, gas, nuclear, geothermal and transmission proposals are
where the growth is; a clean-energy-only scope would be aimed at the shrinking segment.

**Confidence:** high on facts; moderate on how long the 2026 posture persists.

## 4. Where the lenses converge and diverge

**Converge:**

- Data supply is abundant, mostly free, and mostly legal to *use*. Nobody is blocked at "can we get the data".
- Listing alone has no pricing power. All lenses point at fusion + change detection + action as the product.
- Legal exposure concentrates in a handful of named sources (PJM, private aggregators, four ISOs with unread terms).
- Scope should be technology-agnostic and include load (data centres), transmission, gas, nuclear and storage.

**Diverge:**

- The market lens says international breadth differentiates; the engineering lens says every market is a new
  connector set and QA burden. Resolution: US-first with three cheap international feeds that are already APIs
  (TED, Find a Tender, NESO TEC) to prove the model, then expand by customer pull.
- The media lens says social drives awareness; the economics lens says social is a cost with no direct revenue.
  Resolution: syndicate only to Bluesky and LinkedIn at launch, with X on a capped budget, and measure alert sign-ups.

## 5. Steelman, counter-argument, recommendation

**Steelman of the original plan.** Timing is good: queues are at record size, data centres have made grid access
front-page news, federal funding chaos makes tracking valuable, and public data has never been more accessible.
A well-designed, fast, opinionated public site with daily social output could become the "Interconnection.fyi of
the whole pipeline", win SEO and audience cheaply, and monetise the audience through API tiers and Bankable's
routing fees. Your origination background is the right operator profile for the curation layer.

**Strongest counter-argument.** GridTracker already did the free-public-site-plus-paid-data play for the largest
single source class and has a multi-year head start; Cleanview did the low-priced tracker; Halcyon raised $21M
(March 2026) for AI over regulatory filings; Enverus has the enterprise budget. A generalist entrant that lists
the union of their coverage with less depth in each is a worse product in every individual comparison. Social
syndication of proposal listings is low-engagement content that others can copy in a week.

**Recommendation.** Proceed, but as Bankable's data layer rather than as a standalone listings site:

1. **Product:** a *proposal graph*. One entity per real-world project or opportunity, stitched across queue,
   registry, permit, docket, funding and news sources, with a lifecycle state machine and a change feed.
2. **Wedge:** the demand side nobody has assembled cleanly: utility/co-op/CCA RFPs, DOE/USDA/MDB funding with
   status (open, frozen, cancelled, reinstated), tenders (TED, FTS, World Bank), and large-load interconnection.
   Match these to supply-side proposals. This is precisely what makes a project "bankable".
3. **Distribution:** the free delayed tier plus Bluesky/LinkedIn feed is the audience engine for Bankable's paid
   routing/certification. Treat it as marketing spend with a measured CAC.
4. **Non-goals for MVP:** MISO and PJM raw rows on public pages until terms are settled; anything scraped from a
   private aggregator; Reddit/Meta automation; state PUC dockets (partner with or buy from Halcyon later).

## 6. Go / no-go criteria and the validation plan (weeks 1–8)

| Week | Experiment | Kill / continue signal |
|---|---|---|
| 1–2 | Ingest Tier-1 sources into one Postgres schema; run entity resolution on CAISO+ERCOT+EIA-860M | <60% of queue rows link to an EIA-860M or docket record → resolution cost is higher than modelled; continue only with a narrower scope |
| 2–3 | Read and record terms for MISO, SPP, NYISO, ISO-NE; open PJM licence conversation | Any ISO forbids derived-data publication → drop that ISO from public pages, keep for paid API only |
| 3–4 | Hand-curate 50 recurring RFP issuers + grants.gov + TED + FTS into an "opportunities" table | Fewer than ~30 live, relevant opportunities at any time → demand side is too thin for a marketplace; pivot to supply-side alerts |
| 4–6 | Publish delayed public pages + Bluesky/LinkedIn feed; 20 customer conversations from your network | <10% of conversations name a workflow they would pay for → stay a Bankable internal tool, do not sell data |
| 6–8 | Pre-sell Pro alerts to 10 developers/lenders at $150–250/month | <3 paid commitments → revisit pricing or scope |

## 7. Assumptions I am flagging

- **"Scrape the internet"** — the useful universe is a few hundred structured sources, not the open web. Broad
  crawling adds cost and legal exposure for little signal; news APIs (GDELT, RSS) cover the discovery long tail.
- **"Live vs delayed access"** — works only for sources that update at least weekly. Annual (LBNL) and quarterly
  (GEM) sources cannot carry a freshness premium; the premium is in the change feed and the fusion, not the row.
- **"Share across social platforms"** — X is now metered; LinkedIn needs partner approval; treat this as a
  three-channel plan, not "all platforms".
- **"Complete business plan / ERD / CRM+ERP"** — deferred to Phases 1–3 by your instruction. Nothing here
  pre-empts them, but the CRM/ERP "single source of truth" choice should be made before the admin panel is
  designed, because it determines where customer and subscription records live. Candidates and trade-offs are
  logged in `00-PLAN.md`.

## 8. Sources consulted

Registry entries in `data/sources.yaml` carry the primary URLs. Secondary references used for facts above:

- LBNL, Queued Up 2026 Edition — https://emp.lbl.gov/queues
- gridstatus interconnection queue docs — https://opensource.gridstatus.io/en/latest/interconnection_queues.html
- Interconnection.fyi / GridTracker — https://www.interconnection.fyi/ and https://www.interconnection.fyi/dg/purchase-data
- Cleanview pricing — https://cleanview.co/pricing
- Halcyon Series A / pricing — https://www.businesswire.com/news/home/20260316032633/en/ ; https://halcyon.io/about
- Enverus 2026 Interconnection Queue Outlook — https://www.enverus.com/newsroom/enverus-releases2026-interconnection-queue-outlook/
- PJM Data Miner terms — https://apiportal.pjm.com/ ; https://learn.pjm.com/three-priorities/keeping-the-lights-on/data-miner-faqs/are-there-any-limitations
- ERCOT Terms of Use — https://www.ercot.com/help/terms
- CAISO Privacy & Terms — https://www.caiso.com/privacy-terms-of-use
- hiQ v. LinkedIn analysis — https://www.jenner.com/en/news-insights/publications/client-alert-data-scraping-in-hiq-v-linkedin-the-ninth-circuit-reaffirms-narrow-interpretation-of-cfaa
- EU database protection — https://eur-lex.europa.eu/EN/legal-content/summary/legal-protection-databases.html
- TED API docs — https://docs.ted.europa.eu/api/latest/index.html
- Grants.gov API guide — https://grants.gov/api/api-guide ; SAM.gov opportunities API — https://open.gsa.gov/api/get-opportunities-public-api/
- Permitting Dashboard data portal — https://data.permits.performance.gov/
- NRC ADAMS API — https://adams-api-developer.nrc.gov/
- NESO TEC register — https://www.neso.energy/data-portal/transmission-entry-capacity-tec-register
- Global Energy Monitor licence — https://globalenergymonitor.org/creative-commons-public-license/
- OBBBA energy provisions — https://www.kirkland.com/publications/kirkland-alert/2025/08/one-big-beautiful-bill-act-brings-big-changes-to-green-energy-tax-credits
- DOE award cancellations — https://www.utilitydive.com/news/backlash-trumps-vindictive-grant-cancellations-doe-energy/826226/
- ERCOT large-load queue — https://www.utilitydive.com/news/texas-facing-438-gw-queue-approves-initial-large-load-interconnection-pro/823367/
- X API pricing 2026 — https://postproxy.dev/blog/x-api-pricing-2026/ ; LinkedIn posting — https://postproxy.dev/blog/linkedin-api-automate-company-page-publishing/ ; Reddit — https://www.techloy.com/reddit-api-pricing-in-2026-complete-guide-for-developers-and-businesses/ ; Bluesky limits — https://publishq.com/blog/bluesky-api-post-limits
