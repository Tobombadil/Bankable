# Market and competition

**Status:** Phase 1 deliverable · 2026-09-12 · extends `01-feasibility.md` §3.1, §3.4, §5 (does not repeat them)
**Method:** live web research and four public APIs queried today. Every fact carries a URL and the retrieval
date 2026-09-12 unless stated. Facts are marked **F**; inferences are marked **I** with a confidence level.
Raw API responses are in the session scratchpad, not committed.

## 1. Competitor teardown

### 1.1 Summary table

| Product | Coverage (F) | Cadence (F) | Pricing | Customers / segments | Funding | Threat to fused supply+demand thesis (I) |
|---|---|---|---|---|---|---|
| Interconnection.fyi / GridTracker | 50+ US queues, 40,000+ projects, 1M+ filings indexed, 1,000+ active data-centre builds; DG: 500,000+ projects across 28 utilities/16 states | Daily | Not public (form). One-time export, monthly subscription, real-time API; "flat subscription" MCP connector | "Top developers, EPCs, and lenders"; research institutions; LBNL | None found (I: self-funded research firm, moderate) | **Medium** — owns supply side; partner or licensor, not a demand-side player |
| Cleanview | Every planned/operating US solar, wind, storage, gas project; data-centre tracker 1,306 operating / 2,091 planned | Continuous ("updates all year"); tracker refreshed monthly | $9,000/yr up to 5 users (either platform); API +$5,000/yr; one-off download $4,900; Research $18,000/yr | Analysts, BD teams, investors, journalists | Bootstrapped | **Medium** — sets the low price anchor; no RFP/funding side |
| Halcyon | All 50 state PUCs, every ISO/RTO, FERC; trackers: 472 gas plants, 264 large-load tariffs, 5,536 substations, BESS, rate cases | Alerts daily; Gas tracker monthly | Free alerts tier (F). $99/mo–$50k+/yr per `01-feasibility` (not re-verifiable today: /pricing is 404) | OpenAI, Quanta, Berkeley Lab, SemiAnalysis, Enchanted Rock, GridLab, DESRI, Acadia, RMI | $21M Series A, 16 Mar 2026, Energize lead | **High on stage-evidence layer**, low on demand side |
| Enverus PRISM (Energy Acuity, Pearl Street) | Energy Acuity retired 30 Jun 2025 into PRISM; Pearl Street SUGAR/Interconnect acquired 13 Mar 2025; "8,000+ companies" (all Enverus) | Continuous | Not public. I: $40–80k/yr typical (moderate; from Cleanview's "legacy platform" claim), higher with modules | Utilities, developers, power traders, financial services | Private (Hellman & Friedman/Genstar) | **High if they ship demand-side fusion; medium otherwise** |
| Paces | Site/permit/zoning/grid data; Power & Permitting reports; AI agent | Not stated | Not public (I: $15–40k/yr enterprise, low) | Qcells, Demeter, OnCore Origination, Third Pillar Solar; data-centre developers | $11M Series A Jul/Aug 2024; ~$13–16M total | **Low** — siting workflow, not opportunity discovery |
| Energy Adepto | Utility & renewable RFPs from 1,200+ sources; researches "all 1,200 load serving entities registered by EIA" | Real-time notifications; "RFP volume up 38% last month" (Dec 2025) | Starter $120/mo (annual) or $150 monthly, 50 RFP views, 1 user; Professional $320/mo (annual) or $400 monthly, unlimited, 3 users; Enterprise custom | Utility vendors, IPPs, renewable sellers, EPCs | None found (I: bootstrapped, moderate) | **High on demand side** — closest to our RFP wedge; no supply side, no funding/tender side |
| interconnectionqueue.org | 7 ISO/RTOs; curated large-load positioning | Manual; last full refresh 9 Jul 2026 | Free | Individual analyst dashboard (Jackson Ramsey) | None | **Nil** (signal that the niche is unserved) |
| ZEG Queue Tracker | Queue windows across ISO/RTOs; 57 entries | "Refreshed periodically"; weekly/monthly email | Free; funnel to paid REST/Sitra software and consulting | Large-load developers, IPPs, utilities | Consultancy | **Nil** as product; competes for attention |
| LevelTen MarketPulse / RFP | PPA offers, RFP bids, project data; 800+ credentialed developers, ~90% NA coverage, 25 countries; 1,300 developers in 35 countries | Within 24h of developer upload; weekly settlement values | Not public (I: $20–50k/yr, low) | Developers, advisors, buyers, utilities | $65M Series D Jul 2024; $125–137M total | **Medium** — owns *corporate* PPA demand; not utility/public funding |
| PVcase Prospect (ex Anderson Optimization) | Parcel screening, interconnection capacity reports, ISO-aligned power flow | Continuous | Quote-only; I: $10–20k/yr + AutoCAD seats (third-party, low-moderate) | Solar developers; data-centre siting | PVcase (Highland Europe); AO acquired Jun 2023 | **Low** — siting tool |
| Wood Mackenzie Lens P&R / S&P / BNEF | Global forecasts, asset databases; Lens P&R launched Mar 2025 | Quarterly research, continuous data | Not public. Bloomberg terminal $24k/yr per seat; I: $40–80k/yr contracts (Cleanview claim) | Enterprise strategy, IB, utilities | Corporate | **Low on workflow, high on budget capture** |
| New Project Media (incumbent, not in brief) | 100,000+ projects US/EU/APAC; NPM Edge AI May 2026; 450+ customers | Real-time news + data | Not public; I: $40–80k/yr (Cleanview claim) | Developers, lenders, infra funds, IB, advisors, EPC, utilities | Private | **Medium-high** — closest to "fused" in spirit, journalist-driven, not API-first |

Entrants 2025–2026 (see §1.3): Piq Energy, Nira Energy, Vela Energy, GridUnity, Orennia, LandGate, Transect,
Grid Status, Kadoa, datacenterHawk.

### 1.2 One paragraph each

**Interconnection.fyi / GridTracker.** F: free public daily-updated queue database; the research firm co-authors
LBNL *Queued Up* 2025 and 2026; sells the full dataset as one-time export, monthly subscription or real-time API,
and (as GridTracker.io) a dashboard, an MCP connector on a "flat subscription", and paid custom reports; claims
40,000+ projects, 50+ queues, 1M+ filings indexed, 1,000+ active data-centre builds, "trusted by top developers,
EPCs, and lenders" (https://www.interconnection.fyi/about-gridtracker, https://www.interconnection.fyi/dg/purchase-data,
https://www.gridtracker.io/). Pricing is not published. I (moderate): a small research firm without
institutional funding; revenue from data licences. Weaknesses: supply-only, US-only, no funding/RFP/tender
layer, no lifecycle beyond queue status. Threat: medium. They are the natural supply-side licensor (open question
5 in `00-PLAN.md`), not a competitor for the demand side.

**Cleanview.** F: $9,000/yr for up to five users on either the Power Projects or the Data Center platform, API
+$5,000/yr, one-off download $4,900, research subscription $18,000/yr, reports from $4,900; bootstrapped team in
Colorado; September 2026 data-centre tracker lists 1,306 operating facilities (61,443 MW) and 2,091 planned
(380,103 MW); founder's newsletter reaches 75,000+ (https://cleanview.co/pricing, https://cleanview.co/about,
https://blog.mean.ceo/data-centers-news-september-2026/, https://newsletter.cleanview.co/about). Their pricing page
explicitly positions against NPM, S&P and Wood Mackenzie at "$40,000–80,000 annual contracts". Weaknesses: no
RFP/funding/tender data, no lifecycle stitching to dockets, no international. Threat: medium; it is the price
ceiling for a Team tier and the model for audience-led distribution.

**Halcyon.** F: $21M Series A announced 16 Mar 2026 led by Energize Capital with Zero Infinity, Congruent,
Obvious, Sabanci; catalogue spans all 50 state PUCs, every ISO/RTO and FERC; products are Search, agentic Alerts
(free account tier) and Data Subscriptions (Gas Power Plant Tracker 472 plants updated monthly, Large Load Tariff
Tracker 264, Substation Tracker 5,536, BESS, Rate Cases); logos include OpenAI, Quanta, Berkeley Lab, SemiAnalysis,
Enchanted Rock, GridLab, DESRI, Acadia, RMI; roadmap includes international dockets, satellite imagery and
Salesforce pipeline scoring (https://www.businesswire.com/news/home/20260316032633/en/, https://halcyon.io/,
https://halcyon.io/alerts, https://halcyon.io/blog/machine-readable/new-data-subscriptions,
https://energizecap.com/insights/why-we-invested-in-halcyon). The $99/mo–$50k+/yr ladder cited in
`01-feasibility.md` could not be re-verified today (halcyon.io/pricing returns 404). Weaknesses: regulatory
documents in, not opportunities out; no RFP/grant/tender coverage; no capital routing. Threat: high on the
stage-evidence layer (they will own "what the docket says"), which argues for buying that layer from them rather
than building it (`01-feasibility.md` §5 non-goal).

**Enverus PRISM (Energy Acuity, Pearl Street).** F: Energy Acuity "will be retired June 30, 2025, to complete the
transition to … Enverus PRISM"; Pearl Street Technologies (SUGAR, Interconnect) acquired 13 Mar 2025; Enverus
released a 2026 Interconnection Queue Outlook on 24 Feb 2026 using "proprietary machine learning" to evaluate
queued projects; Enverus overall claims "8,000+ companies" (https://www.enverus.com/energy-acuity/,
https://www.enverus.com/newsroom/undo-the-queue-enverus-acquires-pearl-street-technologies-to-solve-for-a-more-reliable-resilient-grid/,
https://www.enverus.com/newsroom/enverus-releases2026-interconnection-queue-outlook/). No public price. I
(moderate): $40–80k/yr for a P&R seat bundle; third-party claims of $100k–500k+ (https://energystackhub.com/resources/commercial-energy-management-platform-pricing)
likely describe multi-module enterprise deals (low confidence). Energy Acuity historically carried RFP and PPA
tracking, so Enverus is the one incumbent that *already holds* both supply and demand rows. Weaknesses: price
excludes the long tail; product is a terminal, not a feed; no public/free tier. Threat: high if they productise
fusion; today it is a table of rows behind a $40k+ paywall.

**Paces.** F: $11M Series A (Jul/Aug 2024), ~$12.9–15.9M total; YC-backed; products are an AI agent, siting
software, Power & Permitting reports and services; named clients Qcells, Demeter Land Development, OnCore
Origination, Third Pillar Solar; serves power developers, data-centre developers, utilities
(https://www.paces.com/, https://www.utilitydive.com/news/data-platform-paces-nabs-11m-to-scale-clean-energy-development/722760/,
https://tracxn.com/d/companies/paces/__8EXjwSHllCfw6owjGHVpf3XD75wdonZG9IP5AiIKVd4/funding-and-investors). No public
price. Weaknesses: parcel-first workflow tool; no opportunity discovery. Threat: low; complementary (a Bankable
proposal could link to a Paces report).

**Energy Adepto.** F: Starter $120/mo billed annually ($150 monthly) with 50 RFP views and one user; Professional
$320/mo annually ($400 monthly) unlimited views, three users; Enterprise custom; 60-day money-back; monitors
"1,200+ sources" and "all 1,200 load serving entities registered by EIA"; RFP mix Dec 2025: Renewable 21%,
All-Source 16.7%, Energy Storage 13.3%, Solar 12.2%, Energy 7.8%, Dispatchable 5.6%; geography North America,
concentrated in California, Texas, Northeast (https://energyadepto.com/,
https://energyadepto.com/utility-rfp-database-view-active-energy-rfps/). Weaknesses: RFPs only, no supply side,
no grants/tenders/MDB, no API, thin brand. Threat: high on the RFP wedge specifically; it proves a $1.4–3.8k/yr
price point exists for RFP discovery alone.

**interconnectionqueue.org.** F: "Built and maintained by Jackson Ramsey", a "manually maintained analyst dashboard
for data-center and energy-project development" across the seven ISO/RTOs, last full refresh 9 Jul 2026, free,
no API (https://interconnectionqueue.org/). Threat: nil as a business; useful as evidence that large-load
positioning across ISOs is being hand-curated because no product does it.

**ZEG Queue Tracker.** F: free tracker of upcoming queue windows across ISO/RTOs (57 entries today), Gantt and
table views, weekly/monthly email alerts; a lead-generator for ZEG's REST/Sitra software and consulting
(https://queue-tracker.zeroemissiongrid.com/, https://www.zeroemissiongrid.com/). Threat: nil as a product; queue
windows are one event type our change feed must include.

**LevelTen (MarketPulse / RFP).** F: MarketPulse aggregates PPA price offers, RFP bids and project data from
"800+ credentialed developers" with "approximately 90% market coverage in North America and 60% in Europe" across
25 countries, updated within 24 hours of upload; the 2025 review cites 5.4 GW transacted and "more than 1,300
project developers in 35 countries", with unique corporate offtakers down more than 50% versus 2024; Q2 2026 NA
index: 266 offers from 185 projects; $65M Series D July 2024, $125–137M total
(https://www.leveltenenergy.com/marketpulse, https://www.leveltenenergy.com/post/2025review,
https://www.leveltenenergy.com/post/levelten-north-american-ppa-price-index-q2-2026,
https://www.leveltenenergy.com/post/levelten-series-d-funding-round). Weaknesses: closed marketplace data
(proprietary bids), corporate buyers only, not utility/public/MDB demand. Threat: medium; they own corporate PPA
demand and would be an acquirer or partner rather than a copier of public-register fusion.

**Anderson Optimization / PVcase Prospect.** F: acquired June 2023; logins merged Dec 2024; Prospect generates
Interconnection Capacity Reports per substation and "ISO-aligned power flow studies to identify interconnection
traps"; quote-only pricing, with third-party observation of $10–20k/yr plus AutoCAD seats
(https://pvcase.com/blog/pvcase-acquires-anderson-optimization, https://pvcase.com/site-selection-analysis,
https://www.heavengreenenergy.com/blog/pvcase-review). Threat: low; design-side tool.

**Wood Mackenzie / S&P Global / BNEF (enterprise incumbents).** F: Wood Mackenzie launched Lens Power & Renewables
in March 2025 with editions for utilities/developers and financiers/investors; no public pricing; a Bloomberg
terminal is $24,000/yr per seat (https://www.woodmac.com/lens/power-and-renewables,
https://solarquarter.com/2025/03/10/wood-mackenzie-launches-lens-power-renewables-to-support-the-energy-transition/,
https://qz.com/84961/this-is-how-much-a-bloomberg-terminal-costs). I (moderate): $40–80k/yr per organisation for a
P&R data seat bundle. Weaknesses: forecast-led, not event-led; no public tier; slow to add new source classes.
Threat: low on workflow, high on budget: an enterprise that already pays WoodMac will not add a second $40k line,
but will add a $9k one.

**New Project Media** (not in the brief, but the closest incumbent to the fused idea). F: 450+ development,
investment, finance, advisory and corporate customers across seven segments; 100,000+ projects tracked incl.
interconnection queue analysis; NPM Edge AI launched May 2026; APAC coverage launched 2026; Energy Rev (London)
acquired Jan 2024 (https://newprojectmedia.com/npmclients/,
https://www.prnewswire.com/news-releases/new-project-media-launches-ai-powered-market-intelligence-across-its-global-platform-302761226.html).
Threat: medium-high; a journalist-driven firm with 450 paying organisations is the best available proxy for the
size of the paying market for "who is developing what, financed by whom".

### 1.3 Entrants found (2025–2026)

| Entrant | What (F) | Signal | Threat (I) |
|---|---|---|---|
| Piq Energy | Agentic grid-planning platform; $5M seed 8 Jul 2026 led by Active Impact; founded 2023 (https://www.globenewswire.com/news-release/2026/07/08/3324265/0/en/) | Engineering-study automation, utility + developer buyers | Low: studies, not discovery |
| Nira Energy | Interconnection study replication; "more than 100 of the country's largest solar, storage, and data center developers"; 500+ GW studied; profitable since 2021; Energize strategic investment 29 May 2025 (https://www.prnewswire.com/news-releases/nira-energy-partners-with-energize-capital-to-scale-transmission-automation-software-302467710.html) | Proves developers pay five figures for interconnection risk tools | Low-medium |
| Vela Energy | YC W26; "AI execution agents for large-load energy projects" (https://yespress.io/vela-yc-w26) | Large-load workflow | Low |
| GridUnity | Interconnection lifecycle management for utilities/ISOs; clients Entergy, ISO-NE, SPP, MISO, HECO, SCE, PG&E, Southern, Xcel (https://www.gridunity.com/) | Utility-side system of record; potential data source, not competitor | Nil |
| Orennia | Energy-transition analytics incl. data-centre siting and transmission; Series C 21 Jan 2025 led by Decarbonization Partners (BlackRock/Temasek), Series B $25M Jul 2023 (https://www.globenewswire.com/news-release/2025/01/21/3012665/0/en/) | Enterprise analytics for capital allocators | Medium (investor segment) |
| LandGate | Enterprise AI Data Agent Feb 2026; 5,500 data centres, 65K substations, 19K plants mapped (https://www.landgate.com/energy-markets/data-centers, https://www.prnewswire.com/news-releases/landgate-launches-an-enterprise-ai-data-agent-for-infrastructure--energy-development-302684264.html) | Parcel + power for data centres | Low-medium |
| Transect | Environmental due-diligence and siting; Series A; data-centre siting product (https://www.transect.com/) | Siting | Low |
| Grid Status | Hosted ISO data API; free plan 500,000 rows/month (https://www.gridstatus.io/products/api, https://docs.gridstatus.io/developers/api-reference/api-usage); pricing page blocked to bots today | API pricing reference | Nil |
| Kadoa | Scraped registry: 453 US data-centre operators, 2,297 sites, "updated daily" (https://www.kadoa.com/datacenter/operators) | Shows AI-scraper entrants can build registries fast | Low |
| datacenterHawk | Data-centre real-estate intelligence; quarterly market reports (https://datacenterhawk.com/) | Incumbent for DC real estate | Low |

I (high confidence): no entrant found today fuses public supply registers with public demand (RFP, grant, tender,
large-load) into one lifecycle record. The 2025–26 money went to study automation (Piq, Nira, GridUnity), docket
AI (Halcyon), and siting (Paces, LandGate, Transect, Orennia).

## 2. Bottom-up market sizing (US)

### 2.1 Organisation counts by segment

| Segment | Count (F, with source) | Addressable subset (I) | Seats/org (I) | WTP/org/yr (I) |
|---|---|---|---|---|
| Developers / IPPs | ERCOT active queue: 1,198 rows, **862 unique interconnecting entities** (gridstatus pull today, https://www.ercot.com/); NYISO queue: 1,604 rows with sponsor, **844 unique** (https://www.nyiso.com/documents/20142/1407078/NYISO-Interconnection-Queue.xlsx); LevelTen 800+ credentialed / 1,300 developers in 35 countries; SEIA 1,200 member companies (https://seia.org/about/); NPM 450+ paying customers; Nira 100+ developer customers | ~1,200 organisations with a utility-scale origination or BD function (raw sponsor names 2,500–4,000 nationally collapse to 1,200–2,000 parents after SPV de-duplication; moderate) | 2–5 | $1.5k–9k (Energy Adepto $1.4–3.8k; Cleanview $9k) |
| Lenders, tax equity, infra funds | Banks ≈ 80% of a ~$20–35B tax-equity market (https://acore.org/resources/tax-equity-enabling-clean-energy-and-growing-the-american-economy/, https://www.pv-tech.org/us-clean-energy-tax-credit-monetisation-to-reach-up-to-us60-billion-in-2025-crux/); Preqin: 98 renewable-focused infrastructure fund managers, 124 vehicles (https://www.preqin.com/insights/blogs/renewable-energy-focused-infrastructure-fund-managers/5434); NA energy-transition funds raised $28.8B in 2025 | ~300 (≈40 tax-equity/transfer buyers, ~100 infra GPs, ~150 project-finance lenders and IB desks; moderate) | 2–4 | $5k–20k |
| Utilities incl. co-ops and CCAs | 168 IOUs (EIA, 2017 count, https://www.eia.gov/todayinenergy/detail.php?id=40913); EEI US member list ~140 entities incl. subsidiaries (https://www.eei.org/-/media/Project/EEI/Documents/About/memberlist_print.pdf); NRECA: 830 distribution + 60–64 G&T co-ops (https://www.cooperative.com/programs-services/bts/Documents/Data/Electric-Co-op-Fact-Sheet.pdf); ~2,000 public power communities (https://www.publicpower.org/public-power); CCAs: 25 California programs, 169 MA communities, 500+ IL, 350+ OH, 115 NH, 60 NJ, 7 RI, 11.4M+ accounts (https://www.nationalccea.org/cca-by-state); Energy Adepto: 1,200 EIA-registered LSEs | ~500 (all IOUs, all G&Ts, ~200 procurement-active munis/co-ops, ~40 CCA procurement entities; moderate) | 1–3 | $2k–10k |
| EPCs / OEMs | 153 companies on Solar Power World 2025 Top Solar EPCs (https://www.solarpowerworldonline.com/2025-top-solar-epcs/); OEM count not sourced | ~250 (top-100 EPCs incl. gas/storage/transmission contractors, ~100 OEM/supplier BD teams; low-moderate) | 1–3 | $1.5k–9k |
| Advisory / law | Chambers USA ranks firms in Projects: Renewables & Alternative Energy, Power, and Power & Renewables Transactional (https://chambers.com/legal-rankings/projects-renewables-alternative-energy-usa-nationwide-5:1559:12788:1); count not extracted | ~160 (≈60 ranked law firms, ~100 technical/financial advisors and IB boutiques; low-moderate) | 2–5 | $5k–15k |
| Data-centre operators / hyperscalers | 453 named US operators across 2,297 sites (https://www.kadoa.com/datacenter/operators); Cleanview: 2,091 planned projects; hyperscalers: AWS, Microsoft, Meta, Apple, Google, plus Oracle/OpenAI-Stargate class | ~150 operators/developers with greenfield power siting + 6–10 hyperscaler energy teams (moderate) | 2–10 | $9k–50k (Halcyon's ceiling; Cleanview DC platform $9k) |
| **Total** | | **≈2,600 organisations** | | |

Cross-check (F): NPM sells to 450+ organisations, LevelTen has 800+ credentialed developers, Enverus claims
8,000+ companies across all its lines. I (moderate-high): 2,000–3,000 US organisations with a paid-data budget for
this problem is consistent with all three, and with `01-feasibility.md` §3.4's 1,000–2,000 estimate for
developers/financiers alone.

### 2.2 SAM

SAM = Σ(addressable orgs × mid-point WTP), US only, data tiers only (no Bankable routing fees):

| Segment | Orgs | Mid WTP | $/yr |
|---|---|---|---|
| Developers/IPPs | 1,200 | $4,000 | $4.8M |
| Finance | 300 | $10,000 | $3.0M |
| Utilities/co-ops/CCAs | 500 | $4,000 | $2.0M |
| EPC/OEM | 250 | $3,500 | $0.9M |
| Advisory/law | 160 | $8,000 | $1.3M |
| Data centres | 150 | $15,000 | $2.25M |
| **SAM** | **2,560** | | **≈$14M/yr** (range $9–22M) |

I (moderate): the range reflects seat-count uncertainty (low = 1 seat/org at the Pro price; high = Team tier
across most orgs). Adding EU/UK via TED/FTS/NESO adds perhaps 30–50% but each market needs connectors; not
counted.

### 2.3 Three-year obtainable revenue

Assumptions (explicit): solo founder plus contractors (open question 2); launch of public delayed tier and Pro
alerts within six months; no paid marketing beyond Bluesky/LinkedIn/email; PJM licensed by month 12; annual
churn 20% Pro, 10% Team; ACV Pro $1,500 (single seat), Team $9,000, API $14,000 (Team + API).

| Year | Pro seats | Team/API customers | ARR exit | Confidence |
|---|---|---|---|---|
| 1 | 40–80 | 3–6 | $90k–$200k | moderate |
| 2 | 120–200 | 10–20 | $270k–$580k | moderate-low |
| 3 | 200–350 | 20–40 | $480k–$1.1M | low |

Benchmarks (F): Nira reached "more than 100" developer customers on a profitable, bootstrapped basis by 2025;
Energy Adepto and Cleanview run on sub-$10k ACVs without institutional capital; NPM's 450+ customers set a
practical ceiling for an intelligence product in this sector. I (moderate): a 3-year exit ARR of ~$500k for the
data tiers is the base case; the reason to build is the Bankable routing/certification revenue that the graph
feeds (`01-feasibility.md` §5), which this document does not size.

## 3. Pricing recommendation

| Tier | Price | Includes | Rationale |
|---|---|---|---|
| Free (delayed) | $0 | Public pages, derived records, attribution, RSS/Bluesky/LinkedIn feed; lag per table below; search and map; no export, no alerts | SEO and audience engine (Cleanview's newsletter and Interconnection.fyi prove the model); PJM/MISO rows absent until licensed |
| Pro | $149/mo or $1,490/yr per seat | Live data, saved searches, daily change-feed email/Slack alerts, CSV export (capped), opportunity deadlines calendar | Sits between Energy Adepto Starter ($120–150/mo) and Professional ($320–400/mo) with strictly more data (supply + demand + funding + tenders). `01-feasibility.md` §6 pre-sale test is $150–250/mo; start at the bottom of that band to win the first ten logos |
| Team | $9,000/yr, 5 seats | Everything in Pro, shared watchlists, unlimited export, entity-resolution links to dockets/EIA/permits, proposal-to-opportunity matching | Price-matches Cleanview exactly so procurement cannot say "more expensive than Cleanview"; the buyer compares breadth |
| API / Data | +$5,000/yr on Team (Team+API $14,000), or $25,000/yr standalone enterprise with bulk/Snowflake | Change-event webhooks, bulk pulls, licence pass-through for restricted sources | Matches Cleanview's +$5k add-on; the $25k enterprise point is below any WoodMac/Enverus/NPM line ($40–80k) and above Halcyon's data-subscription level |

Delayed-tier lag by source cadence (the premium exists only where updates are at least weekly, per
`01-feasibility.md` §7):

| Source cadence | Examples | Free-tier lag | Why |
|---|---|---|---|
| Daily or intraday | ISO queues (CAISO/ERCOT/SPP/NYISO/ISO-NE), grants.gov, SAM.gov, TED, FTS, FERC eLibrary, World Bank notices | **7 days for opportunities, 14 days for supply rows** | RFP/tender windows are typically 30–90 days: a 7-day lag keeps free pages useful for SEO while Pro gets day-0 alerts; queue rows change slowly, so 14 days costs free users little and makes Pro's change feed the reason to pay |
| Weekly | Utility RFP pages (curated 50), state siting dockets, Permitting Dashboard | 30 days | One missed cycle is the minimum that makes alerts valuable |
| Monthly | EIA-860M, Halcyon-class trackers, NESO TEC register | One cycle (30–45 days) | Free tier shows last month's snapshot |
| Quarterly/annual | LBNL Queued Up, GEM, EIA-860 | No lag | No freshness premium possible; publish fully with attribution |
| Status changes (cancelled, frozen, reinstated, withdrawn, window opening) | DOE award actions, USDA REAP status, ZEG-style queue windows | Pro only for 30 days, then free | The change event *is* the product (`00-PLAN.md` principle) |

I (moderate): expect 1–2% free-to-Pro conversion on an engaged list; at 5,000 monthly active free users that is
50–100 Pro seats, which is the break-even band in `01-feasibility.md` §3.4.

## 4. Demand-side reality check (September 2026)

Counts obtained today from public APIs (F), then estimates (I).

| Source | Query | Result (F) | Note |
|---|---|---|---|
| grants.gov search2 (https://api.grants.gov/v1/api/search2) | keyword "energy", posted+forecasted | **151** (134 posted, 17 forecasted); 83 of the posted opened in 2026; 95 close in 2026–27 | Keyword matches many non-energy (DoD 42, State 20) |
| grants.gov | funding category EN (Energy), posted | **18** | Includes legacy REAP entries from 2015/2018 still "posted" |
| grants.gov | DOE family agencies (GFO, NETL, ID, ARPA-E, SC), posted+forecasted | **16 posted, 0 forecasted**; ~10 opened in 2026, of which only ~4 are competitive project FOAs (ASPECT DE-FOA-0003647, Advancing Oil & Gas DE-FOA-0003634, Nuclear Licensing Cost-Share DE-FOA-0003339, Genesis Mission DE-FOA-0003612) plus ARPA-E SCALEUP and Office of Science open calls; rest are NOIs, RFIs and staff-support awards | Confirms `01-feasibility.md` §3.6: CMEI (ex-EERE) posts far fewer FOAs than 2023–24 (https://grantedai.com/grants/cmei-funding-opportunities-doe-u-s-department-of-energy-4234daf8) |
| grants.gov | USDA energy | **RUS "Powering Affordable Reliable Technology (PART)" posted 8 Sep 2026, closes 9 Oct 2026**; REAP grants paused pending new rules under EO 14315, guaranteed loans continue (https://www.rd.usda.gov/media/file/download/usda-rd-reap-faq-03312026.pdf) | |
| TED (https://api.ted.europa.eu/v3/notices/search) | `classification-cpv=09* AND publication-date>=20260801` | **2,423 notices** in six weeks (**1,281** are contract notices cn-standard/social/desg); **14,746** CPV-09 notices in 2026 YTD; **10,115** in six weeks for the broader set (45251 power plants, 452313 pipelines, 31 electrical equipment, 713 engineering, 65 utilities) | Note: the API requires a `fields` array; the brief's minimal body returns HTTP 400 |
| UK Find a Tender OCDS (https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages) | updatedFrom 2026-08-12, stages=tender | **189** tender-stage releases in 30 days; 17 energy-adjacent by CPV; 2 strictly energy | Above-threshold notices only; energy-specific volume is low (low confidence in the CPV filter) |
| World Bank procurement (https://search.worldbank.org/api/v2/procnotices) | qterm=energy, newest 200 | 200 notices span **21 Aug–11 Sep 2026** (≈270/month): 126 contract awards, 46 REOI, 27 IFB, 1 GPN; all-time 41,164; "solar" 144 and "transmission" 175 in the same window | ≈100 *open-type* (REOI/IFB) energy notices per month at the World Bank alone |
| SAM.gov | needs an api.data.gov key (`00-PLAN.md` next action) | not counted | |

Estimates for what no API returned today (I):

| Demand class | Live at a typical moment | Confidence | Basis |
|---|---|---|---|
| US utility/co-op/CCA generation and storage RFPs | **30–60 open**, ~150–250 issued per year | moderate | Named 2026 all-source/ESS RFPs verified today: SRP 2,900 MW (issued 23 Feb, closed 29 Apr), PSO 4,000 MW, OG&E (bids 20 May), SWEPCO 3,000 MW, Idaho Power ~350 MW peak + 1,100 MW VER, Georgia Power 500 MW ESS (due Jan 2026) and 2,000–6,000 MW all-source (Q2 2026), Duke SC 400 MW battery (20 Apr 2026), Duke NC solar/storage RFP paused by NCUC order Apr 2026, Entergy Louisiana BESS final docs Mar 2026, Entergy Arkansas 1 GW, Entergy Texas CCCT 2026 (https://www.publicpower.org/periodical/article/srp-issues-2026-all-source-rfp-meet-increased-energy-demand, https://www.psoklahoma.com/lib/docs/business/b2b/rfp/pso/2026_PSO_All_Source_RFP_2-20-26-Final.pdf, https://www.oge.com/documents/d/portal/og-e-2026-all-source-rfp, https://www.swepco.com/business/b2b/energy-rfps/AllSource2026, https://idahopower.com/about-us/doing-business-with-us/request-for-resources/, https://www.energy-storage.news/georgia-power-files-rfp-for-up-to-6000mw-of-new-dispatchable-energy-resources/, https://dms.psc.sc.gov/Attachments/Matter/cdbec8e5-6c18-4105-bf55-d3144ab6ee40, https://cleanenergy.org/news/a-chairmans-order-a-lawsuit-and-a-ticking-clock-inside-the-fight-over-north-carolinas-2026-solar-rfp/, https://rfp.entergy.com/); Energy Adepto's mix implies all-source ≈17% of a larger RFP flow that includes T&D, services and software |
| Utility RFPs incl. T&D, services, consulting, software | 200–400 open | moderate | Energy Adepto tracks these from 1,200 LSEs and reports +38% month-on-month growth (Dec 2025) |
| DOE competitive FOAs relevant to project sponsors | **4–8 open** | high | Counted above |
| Other federal (USDA RUS/REAP loans, EDA, DOI, DOT) energy-relevant | 10–20 | moderate | grants.gov category EN 18 minus stale entries plus RUS PART |
| State programme solicitations (NYSERDA, CEC, MassCEC, NJBPU, IL Power Agency, TX Energy Fund) | 30–60 | low-moderate | Not counted today; curated list needed |
| Large-load interconnection requests (data centres) | ERCOT 438–474 GW queue, ~90% data centres (`01-feasibility.md` §3.6); FERC RM26-4 large-load rulemaking open (https://www.ferc.gov/rm26-4) | high on facts | These are *supply-side load* records, but each one is a demand signal for generation, transmission and gas |
| MDB energy notices (World Bank, ADB, AfDB, EBRD, IDB) | **300–500 open-type per month across MDBs**; ~1,000+ live | moderate | World Bank ≈100/month measured; aggregators list 756+ ADB, 347–806 AfDB, 71–85 EBRD live tenders all sectors (https://www.tenderspedia.com/financier/asian-development-bank-adb-tenders/, https://www.tenderspedia.com/financier/african-development-bank-afdb-tenders/); energy ≈15–25% of MDB procurement |
| EU TED energy contract notices | **≈850/month** (1,281 in six weeks) | high | Measured |
| UK FTS energy | ≈5–15/month above-threshold | low | Measured 2–17 in 30 days depending on CPV breadth |

I (high): the go/no-go threshold in `01-feasibility.md` §6 ("fewer than ~30 live, relevant opportunities at any
time") is comfortably cleared once utility RFPs, MDB and TED notices are included, and is *not* cleared by US
federal grants alone (4–8 relevant DOE FOAs). The demand side of the product is utility procurement, large-load
signals and international tenders; federal funding is a status-change feed, not a listings feed.

## 5. The case against, the case for, and a calibrated view

### 5.1 Strongest case against the fused supply/demand thesis

1. **Each half is already served cheaply.** Supply: Interconnection.fyi is free daily; Cleanview is $9k. Demand:
   Energy Adepto is $1.4–3.8k. A buyer can assemble both for under $13k and a spreadsheet. Fusion has to be
   worth the difference to *the same buyer*, and the RFP-hunting BD analyst and the queue-watching development
   engineer are often different people with different budgets.
2. **The fusion is thinner than it sounds.** A utility RFP does not name which queued project will win; a DOE FOA
   does not map to a queue row. The "match" between a proposal and an opportunity is a screening heuristic
   (technology, MW, state, COD, interconnection status) that Energy Adepto or NPM can add in a quarter.
3. **Enverus already holds both row sets.** Energy Acuity's RFP/PPA tracking now lives inside PRISM next to queue
   and Pearl Street study data. If fusion is valuable, the incumbent with 8,000 accounts ships it first.
4. **Demand is structurally shrinking in the US federal channel** (4–8 live DOE FOAs; REAP grants paused) and
   corporate PPA demand fell (LevelTen: unique corporate offtakers down >50% in 2025). Utility all-source RFPs are
   large but few (~150–250/year), and their issuers publish them loudly; a developer already knows about SRP.
5. **The paying universe is small.** NPM's 450+ customers after a decade is the realistic ceiling for a
   subscription intelligence product here. A $14M SAM with a 3-year base case of ~$500k ARR does not justify a
   venture build; it justifies a bootstrapped feature.
6. **Legal drag on the supply side.** PJM and MISO rows, roughly half of US queued capacity, cannot appear on the
   free tier until licensed; the free-tier audience engine launches with a hole where the largest market is.

### 5.2 Case for it

1. **Nobody has assembled public demand.** Energy Adepto covers US utility RFPs only; nobody covers utility RFPs +
   large-load signals + DOE/USDA status + MDB + TED/FTS in one feed. The measured volumes (≈850 TED energy contract
   notices/month, ≈100 World Bank open-type/month, 30–60 live US utility RFPs) are large enough to be a product and
   too scattered for any single buyer to watch.
2. **Change events are unowned.** Cancellation, freeze, reinstatement, queue-window opening, RFP pause (Duke NC),
   RFP re-issue: none of the competitors sells a status-change feed across sources. That is the freshness premium
   the delayed/live model needs, and it works even where the underlying register is slow.
3. **The buyer for fusion exists and is under-served:** capital allocators (300 finance orgs, 150 data-centre power
   teams) want "which proposals are positioned for which demand", which is precisely Bankable's routing question.
   Halcyon's customer list (OpenAI, SemiAnalysis, DESRI, Acadia) shows that this buyer pays five figures for
   structured public data.
4. **The incumbents are priced out of the long tail.** $40–80k contracts exclude the ~2,000 organisations below
   the top 500; Cleanview and Energy Adepto prove a sub-$10k tier sells to them.
5. **The graph is the moat, not the rows.** Entity resolution across queue, EIA-860M, dockets, permits and
   opportunities is the expensive part (`01-feasibility.md` §3.3) and compounds; rows do not.
6. **The data-tier revenue is not the point.** The graph is the lead engine for Bankable's routing/certification
   fees, which scale with deal value rather than seat count.

### 5.3 Calibrated view

- Probability that a *standalone* fused data subscription reaches $1M ARR within three years: **~25%** (SAM
  ~$14M, strong low-priced incumbents on each half, small paying universe).
- Probability that the fused graph materially improves Bankable's routing conversion and justifies its build cost
  as an internal asset with a public free tier: **~65%** (the demand-side gap is real and measurable; the free
  tier is cheap audience).
- Probability that Enverus, NPM or Halcyon ships an explicit supply/demand matching feature within 24 months:
  **~50%**. This is the main external risk and argues for speed on the demand-side wedge and for partnering (not
  competing) on supply rows (GridTracker) and docket evidence (Halcyon).
- Recommendation unchanged from `01-feasibility.md` §5, sharpened: lead with the **demand-side feed** (utility
  RFPs, large-load, MDB, TED, federal status changes) at Pro $149/mo, price Team at Cleanview parity ($9k), and
  treat supply rows as licensed inputs rather than the product.

## 6. Customer discovery

### 6.1 Ten-question interview guide (45 minutes; record answers verbatim, no leading)

1. Walk me through the last time you found a new opportunity (RFP, tender, funding round, large-load request).
   Where did it come from, how long after it was published, and who else saw it first?
2. What do you pay for today to find or track projects and opportunities (tools, newsletters, consultants,
   memberships)? What is the annual total and who signs it off?
3. Which of these would you open every day: a list of queued projects, a list of open RFPs, a list of status
   changes across both? Why that one?
4. Tell me about a time you missed or were late to an opportunity. What did it cost?
5. When you see an RFP, how do you decide which of your projects (or clients' projects) to bid? What data do you
   pull, from where, and how long does it take?
6. If we told you a specific queued project had just been withdrawn, or a specific RFP paused, within a day, what
   would you do with that? Who would you forward it to?
7. Which sources do you not trust, and why? Which do you trust but find unusable?
8. What would you need to see in a free public version before you asked your firm to pay? What would make you
   stop using a free version?
9. For a live feed plus alerts across supply and demand, is $149 per seat per month clearly cheap, clearly
   expensive, or something you would need to justify? What about $9,000 a year for the team?
10. Who else in your organisation, or at your counterparties, should I talk to about this? (Ask for role and
    company type only.)

Kill signal (from `01-feasibility.md` §6): fewer than 10% of interviewees name a workflow they would pay for.

### 6.2 Twenty target interviewees (role / company type; no personal data)

| # | Role | Company type |
|---|---|---|
| 1 | VP Origination | Utility-scale solar+storage developer, ERCOT/SPP footprint |
| 2 | Director of Development | Gas-fired and hybrid IPP, MISO/SERC |
| 3 | Head of Interconnection | Multi-technology developer with 50+ queue positions |
| 4 | Business Development Manager | Standalone storage developer, CAISO/PJM |
| 5 | Managing Director, Project Finance | Commercial bank tax-equity and construction lending desk |
| 6 | Investment Principal | Energy-transition infrastructure fund (Preqin-listed, $1–5B AUM) |
| 7 | Transferability Buyer / Tax Credit Desk | Insurance company or corporate tax-credit purchaser |
| 8 | Resource Planning Manager | Investor-owned utility that issues all-source RFPs |
| 9 | Power Supply Manager | Generation & transmission cooperative |
| 10 | Procurement Director | California CCA with a long-term RFO cycle |
| 11 | Business Development Lead | Top-20 solar/storage EPC |
| 12 | Utility-Scale Sales Director | Inverter or battery OEM with US BD team |
| 13 | Partner, Projects/Energy | Chambers-ranked law firm, renewables and power |
| 14 | Director, Energy Advisory | Independent engineer / technical advisory firm |
| 15 | Head of Energy Strategy | Hyperscaler or AI-lab energy procurement team |
| 16 | VP Site Selection / Power | Colocation or wholesale data-centre developer |
| 17 | Grid Analyst | Non-profit research group using queue and docket data |
| 18 | Energy Reporter | Trade publication covering interconnection and procurement |
| 19 | Product Lead | Interconnection study or siting software vendor (partnership angle) |
| 20 | Programme Officer | State energy office or green bank that issues solicitations |

## 7. Sources consulted (retrieved 2026-09-12)

Competitors: https://cleanview.co/pricing · https://cleanview.co/about · https://www.interconnection.fyi/about-gridtracker ·
https://www.interconnection.fyi/dg/purchase-data · https://www.gridtracker.io/ · https://halcyon.io/ · https://halcyon.io/alerts ·
https://halcyon.io/blog/machine-readable/new-data-subscriptions · https://www.businesswire.com/news/home/20260316032633/en/ ·
https://energizecap.com/insights/why-we-invested-in-halcyon · https://www.enverus.com/energy-acuity/ ·
https://www.enverus.com/newsroom/undo-the-queue-enverus-acquires-pearl-street-technologies-to-solve-for-a-more-reliable-resilient-grid/ ·
https://www.enverus.com/newsroom/enverus-releases2026-interconnection-queue-outlook/ · https://www.paces.com/ ·
https://www.utilitydive.com/news/data-platform-paces-nabs-11m-to-scale-clean-energy-development/722760/ · https://energyadepto.com/ ·
https://energyadepto.com/utility-rfp-database-view-active-energy-rfps/ · https://interconnectionqueue.org/ ·
https://queue-tracker.zeroemissiongrid.com/ · https://www.leveltenenergy.com/marketpulse · https://www.leveltenenergy.com/post/2025review ·
https://www.leveltenenergy.com/post/levelten-series-d-funding-round · https://pvcase.com/blog/pvcase-acquires-anderson-optimization ·
https://www.heavengreenenergy.com/blog/pvcase-review · https://www.woodmac.com/lens/power-and-renewables ·
https://qz.com/84961/this-is-how-much-a-bloomberg-terminal-costs · https://newprojectmedia.com/npmclients/ ·
https://www.prnewswire.com/news-releases/new-project-media-launches-ai-powered-market-intelligence-across-its-global-platform-302761226.html ·
https://www.globenewswire.com/news-release/2026/07/08/3324265/0/en/ ·
https://www.prnewswire.com/news-releases/nira-energy-partners-with-energize-capital-to-scale-transmission-automation-software-302467710.html ·
https://yespress.io/vela-yc-w26 · https://www.gridunity.com/ · https://www.globenewswire.com/news-release/2025/01/21/3012665/0/en/ ·
https://www.landgate.com/energy-markets/data-centers · https://www.transect.com/ · https://www.gridstatus.io/products/api ·
https://www.kadoa.com/datacenter/operators · https://datacenterhawk.com/ · https://energystackhub.com/resources/commercial-energy-management-platform-pricing

Market sizing: https://seia.org/about/ · https://www.eia.gov/todayinenergy/detail.php?id=40913 ·
https://www.eei.org/-/media/Project/EEI/Documents/About/memberlist_print.pdf ·
https://www.cooperative.com/programs-services/bts/Documents/Data/Electric-Co-op-Fact-Sheet.pdf · https://www.publicpower.org/public-power ·
https://www.nationalccea.org/cca-by-state · https://www.solarpowerworldonline.com/2025-top-solar-epcs/ ·
https://acore.org/resources/tax-equity-enabling-clean-energy-and-growing-the-american-economy/ ·
https://www.pv-tech.org/us-clean-energy-tax-credit-monetisation-to-reach-up-to-us60-billion-in-2025-crux/ ·
https://www.preqin.com/insights/blogs/renewable-energy-focused-infrastructure-fund-managers/5434 ·
https://chambers.com/legal-rankings/projects-renewables-alternative-energy-usa-nationwide-5:1559:12788:1 ·
ERCOT and NYISO queue files via gridstatus 0.x (https://opensource.gridstatus.io/)

Demand side: https://api.grants.gov/v1/api/search2 · https://api.ted.europa.eu/v3/notices/search ·
https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages · https://search.worldbank.org/api/v2/procnotices ·
https://www.rd.usda.gov/media/file/download/usda-rd-reap-faq-03312026.pdf ·
https://grantedai.com/grants/cmei-funding-opportunities-doe-u-s-department-of-energy-4234daf8 · https://www.ferc.gov/rm26-4 ·
utility RFP URLs cited inline in §4 · https://www.tenderspedia.com/financier/asian-development-bank-adb-tenders/ ·
https://www.tenderspedia.com/financier/african-development-bank-afdb-tenders/
