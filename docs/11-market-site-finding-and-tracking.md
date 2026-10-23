# Market: powered-land site finding and the private tracker

**Status:** Phase 1 extension · 2026-10-23 · extends `11-market-and-competition.md` (does not repeat its §1 teardowns of
Interconnection.fyi, Cleanview, Halcyon, Enverus PRISM's queue side, NPM, LevelTen and Energy Adepto's RFP feed).
It covers the two jobs behind the owner's rows of 2026-10-22 and 2026-10-23 in `00-PLAN.md`: **Site finder Pro**
(powered land for large loads, Texas parcels first, the first paid product) and **My pipeline** (one private tracker of
projects, bids and investments, each with a data room, linked to live public records).

**Method.** Public marketing, pricing, help, licence and press pages only, read 2026-10-23 (the retrieval date for every
URL below unless a page date is given). No private aggregator was scraped; for Regrid, LandGate, Acres, Land id, Enverus
and the rest only their marketing, pricing, docs and licence pages were read. Some vendor pages refused the fetcher
(HTTP 403: Enverus, datacenterHawk, DocSend, Data Center Map, the PVcase help centre). Facts for those come from the
search engine's index of the vendor's own page and are marked *(indexed)*. **F** = fact with URL. **I** = inference
with a confidence level. Vendor comparison pages are marked **claim**: they are marketing, not evidence.

## 0. Bottom line

1. **The owner's three-criteria query is not unique.** The query is: a 69 kV-or-higher line within a mile, a
   substation within 5 miles, outside the floodplain. At least four products already run a parcel search by distance
   to lines and substations: Acres (with "230kV+ voltage thresholds"), PVcase Prospect, Transect and LandGate. A fifth,
   Land id, shows the same layers for a check by hand (§1.2, F). What is scarce is two other things. **Deliverable MW at the substation** is what LandGate, PVcase, Nira,
   Enverus and Paces sell, mostly by quote. **Who owns the parcel, and whether neighbouring parcels can be assembled**,
   is what Acres, Land id, Regrid and LandGate sell, at $7 to $125 a month at the low end.
2. **The incumbents consolidated in 2026.** Wood Mackenzie bought LandGate (announced 24 June 2026), and S&P Global
   bought datacenterHawk (closed 1 September 2026) (F, §1.3). I (moderate): the enterprise bundles get richer and
   dearer. That widens the gap below $300 a month rather than closing it.
3. **The cheap end is crowded, but its grid data is stale.** Land id's substation overlay is HIFLD data "last update
   was 05/21", and the overlay "has been removed from the HFLID [sic] and can no longer be updated" (F). Transect's lines and
   substations and DC Hub's lines also come from HIFLD. Inframation's OpenStreetMap network is current and openly
   licensed. That is a real edge, but a narrow one, and it is unmeasured (§6, A3).
4. **Texas demand is real, but frozen at the grid step.** The governor ordered an audit on 3 August 2026. ERCOT says
   the Batch Zero study will not finish by 9 April 2027. The large-load queue is about 474 GW against a record peak of
   91 GW (F, §1.5). In Texas, proximity to the grid is not the bottleneck; verified power and site control are.
5. **No product links a private bid to its public record (Job 2).** No product found links a private bid or investment
   to the live public record it came from, so that deadlines and statuses update themselves (I, moderate-high, §2.3).
   The other tools each hold one piece:
   - CRMs hold the deal, not the record.
   - RFP-response tools hold answers, not status.
   - Data rooms hold documents.
   - Siting tools' trackers (Paces, LandGate PowerCRM, Acres, Land id lease tracking) track parcels, not RFPs or
     funding calls.
6. **Recommendation.** Sell Site finder Pro at the Founding Pro price ($150 a month, `docs/33` §6.4) as the cheapest tool
   that does four things: runs the corridor search statewide on a current open grid; joins it with queue, large-load,
   retirement and price records; adds Texas parcels by acreage and same-owner holdings; and hands the shortlist to the
   tracker. Link out for capacity, owner contact, environmental due diligence and formal data rooms. Integrate Attio
   (the CRM of record) rather than build a CRM.

## 1. Job 1: finding powered land for large loads

### 1.1 Summary table

Threat is to Inframation's Site finder Pro at about $150 a month for developers, land teams and small funds (I).

| Product | What it does (F) | Parcels and owners (F) | Grid and capacity (F) | Public pricing (F) | Who buys (F) | Threat (I) | Why (I) |
|---|---|---|---|---|---|---|---|
| **LandGate** (Wood Mackenzie since June 2026) | Parcel and grid platform: data-centre due-diligence reports, DC dashboard, map-based CRM, off-market listings | Yes: 150M+ parcels; buyer, seller and price in reports; landowner contact export (PowerParcels with CoreLogic, 2022) | ">84K mapped substations"; "offtake capacity data for every substation in the US"; T&D lines; 6,300+ DCs; fibre; Pnodes | Quote only; `/pricing` is 404 | DC and energy developers, funds, lenders | **High** | Does Job 1 end to end, plus listings and a CRM, now with WoodMac's sales force |
| **PVcase Prospect** | Grid-first parcel screening, capacity and upgrade costs, DC layers, sentiment and opposition layers | Yes: parcel and zoning; landowner info; landowner phone numbers on Professional and Premium | Injection and offtake capacity, upgrade costs (Premium); fibre with carrier; LMP; First Street flood | Quote only (Essentials, Professional, Premium named, no prices) | Solar and DC developers, brokers; G2 4.7 from 479+ reviews | **High** | One seat has the two things Inframation lacks: capacity and owner phone numbers |
| **Paces** | AI agent, software, Power & Permitting reports, services | "Millions of parcels"; owner data not stated | Power-flow reports (sample: Hansford County, TX); "grid potential" | Quote only | Power and DC developers (Qcells, Demeter, onCORE) | **Medium-high** | Reports checked by people; "Track every project in one place" overlaps Job 2 |
| **Acres** | Parcel platform; Data Center Index; LLC unmasking; AI agent (Acres Intelligence) | Yes: owner names and addresses on every tier; 150M+ parcels; skip tracing on Enterprise | Substation and line filter with "230kV+" thresholds; "Utilities and infrastructure" is Enterprise-only; FEMA flood on all tiers | Plus $9.99/mo ($7.49 annual); Pro $124.99/mo ($94.99 annual); Enterprise custom | Land teams, DC developers, brokers | **Medium-high** | Same price band as ours; parcel-first, owners included; grid gated to Enterprise |
| **Enverus PRISM** (generation and DC siting) | Withdrawal and injection capability per substation; suitable-land analysis; DC siting workflows | Yes *(indexed)*: "156 million parcels" with ownership | Historical and planning-case withdrawal capability; ATC; LMP | Quote only (`docs/11` I: $40–80k/yr) | Utilities, IPPs, traders, finance | **Medium** | Best grid data; priced out of the long tail |
| **Land id** | Parcel and owner map; lease tracking for land teams | Yes: parcel and owner nationwide; owner contact on Pro | HIFLD lines and substations (substations May 2021, frozen); FEMA floodplain; on every plan | $7, $12, $33.33 or $66.67 per user/mo, billed annually | Brokers, landowners, developers' land agents | **Medium** | The owner's three criteria can be checked by hand for $12/mo; stale substations, no corridor automation |
| **Nira Energy** | Interconnection capacity and study replication; Prospecting Tool at every ≥69 kV substation | No | Substation capacity and upgrade cost at ≥69 kV; section for load developers | Quote only; `/pricing` is 404 | "100+ developers" | **Medium** for "where is the capacity"; **low** for land | The capacity answer Inframation will not compute; a complement |
| **Latapult** | GIS land intelligence for acquisition teams | Yes: ownership, value, sales; a 5+ acre large-parcel ownership layer on States | Energy layers on State and USA plans only; flood zones | Metros $149/mo; States $299/mo; USA $999/mo (homepage). Regions from $149, Regions Plus from $186 (pricing page) | "300+ businesses" | **Medium-low** | Public ladder near ours; generic GIS; energy layers not specified |
| **Regrid** | Nationwide parcels: app, county and state files, API | Yes: owner name 98%, mailing address 92% (Ward County, TX) | None found | App: Starter free, Pro $10/mo, Team $20/user/mo. County file prices not shown today (2023 schedule: $300–$800 a county; state $8–20k; nationwide $50–135k; API $0.10–0.28 a record) | Everyone who needs parcels | **Low** as a competitor; **supplier risk** | Store licence bars "a website, product, or service that may compete" |
| **Transect** | Environmental due diligence and siting; community sentiment; reports marketplace | Parcel Filter by distance to lines and substations; owners not stated | HIFLD substations ≥69 kV, lines 69–765 kV | Quote only; free mini-report from a KML upload | NextEra, Engie, RWE, Arevon (logos) | **Low-medium** | Diligence, not land finding; same HIFLD vintage |
| **Pivvot** (Terracon) | Routing and siting: Aware, Route, Siting, Reporting | Land ownership records; parcel characteristics | "Nearby interconnects"; distance to infrastructure | Quote; free trial | Xcel, AES, Brookfield Renewable, Avangrid, Grid United, Black & Veatch | **Low-medium** | Enterprise engineering tool; DC page is generic |
| **Orennia** | Energy-transition analytics incl. DC siting, injection and withdrawal capacity | "Buildable areas"; no owners | Queue data; injection and withdrawal capacity | Quote | Capital allocators | **Low-medium** | Competes for the investor persona, not the land search |
| **datacenterHawk** (S&P Global Energy since Sept 2026) | DC supply, demand, pricing, pipelines, site selection; FiberLocator | No parcel product stated | Power from S&P Global Energy | Quote; a rival claims "$15K+/yr" | DC providers, investors, brokers | **Low** for land, **medium** for budget | Owns colocation market data, now bundled with 451 Research |
| **DC Byte** | Global DC market, hyperscaler and supplier analytics; Site Selector | Not described | Not described | Free Map Search (limited); paid by demo | Operators, investors, suppliers, utilities | **Low** | Facility intelligence, not land |
| **DC Hub Land & Power Map** | Site-selection map, 40+ layers, AI site score | No parcels | "134,000+" substations; HIFLD lines; FEMA flood; 33+ fibre hubs | Free preview; Pro price not listed | DC site teams | **Low-medium** | A free corridor-style map; the closest shape to our free tier |
| **GridCARE** | Finds unused grid headroom for AI data centres, with utilities | No | Utility-partnered capacity | Service; $13.5M seed (May 2025) | Hyperscalers, large DC developers | **Low** | A service for the top buyers |
| Covered in `docs/11` §1 | Cleanview DC platform ($9k/yr), Halcyon Substation Tracker (5,536), Interconnection.fyi (1,000+ DC builds), Kadoa (2,297 sites) | — | — | — | — | **Low-medium** | Data-centre demand signals, not land |

### 1.2 One paragraph each

**LandGate (Wood Mackenzie).** F: the data-centre page claims ">84K mapped substations", "offtake capacity data for every
substation in the US" with 98% coverage, 6,300+ US data centres, 18K power plants and 97% coverage of pricing nodes.
It also offers due-diligence reports, a Data Center Dashboard, "a modern map-based CRM" and a PowerLeads
marketplace, with no prices, only "Schedule a Demo"
(https://www.landgate.com/energy-markets/data-centers). Its fibre figure conflicts on the same site (1.8M vs 1.2M
miles). `docs/11` recorded 65K substations and 5,500 DCs on 2026-09-12, so the claimed counts are growing fast.
- **Acquisition.** Wood Mackenzie announced the purchase on 24 June 2026, citing "more than 150 million" US parcels and
  data centres as roughly 68% of US load growth to 2030
  (https://www.woodmac.com/press-releases/wood-mackenzie-acquires-landgate-to-deliver-connected-intelligence-for-the-new-era-of-us-power-markets/).
- **The 2022 suite.** PowerCRM tracks land leads; PowerParcels, built with CoreLogic, "allows users to export complete
  ownership contact information"; PowerLeads engages listings (Business Wire release, republished 5 July 2022:
  https://www.mannpublications.com/mannreport/2022/07/05/landgate-unveils-powertools-suite/).
- **Off-market listings.** These come from landowners who have claimed their parcels, and from government agencies.
  The examples carry power facts, such as 266 acres "0.01 miles from a substation with an adjacent 138kV transmission
  line" (https://www.landgate.com/news/the-fastest-path-to-data-center-land-off-market-listings-direct-from-landowners).
- **Pricing.** `https://www.landgate.com/pricing` returns 404.

I (moderate): LandGate is the product a buyer would pick instead of Site finder Pro. Its capacity figures are its own
model. A rival calls that model an 8-bus "theoretical teaching model" (claim, §1.3), so they are contestable, but
buyers see them as an answer. **Threat: high.**

**PVcase Prospect.** F: the data-centre page lists:
- parcel and zoning data and "landowner information";
- "offtake capacity (mirroring grid operator)";
- fibre paths with carrier, route type and status;
- First Street flood analysis and historical LMPs;
- one named customer (CME Energy) and a G2 rating of 4.7 from 479+ reviews
  (https://pvcase.com/data-center-siting).

The pricing page names Essentials, Professional and Premium with no prices. Landowner phone numbers start at
Professional. Injection capacity, offtake capacity and upgrade costs are included only in Premium and are "Custom
quote" on Professional (https://www.pvcase.com/prospect/pricing-plans). The automated parcel search takes a parcel
size and an "acceptable distance from substations or power lines" (https://pvcase.com/site-selection-analysis). The
January 2026 release added a "Data Center Community Sentiment" layer and an "Opposition Layer" *(indexed:
https://help.pvcase.com/hc/en-us/articles/49064052340115-January-2026)*.

Price evidence is third-party only: `docs/11` cites $10–20k/yr plus AutoCAD; one review estimates about $2,990 per user
per year for the suite (https://www.surgepv.com/reviews/pvcase; low confidence).

I (moderate): the most complete single seat for a developer that already does solar. **Threat: high.**

**Paces.** F: the products are an AI agent, software, "Power and permitting reports … delivered at 5x the speed" and
services. Two solution pages exist: power developers and data-centre developers. The sample report is a power-flow
study for Hansford County, Texas, and the testimonials come from Qcells, Demeter and onCORE (https://www.paces.com/).
The software page:
- searches "millions of parcels" with automated screening;
- offers "Track every project in one place", from origination to site control, with projects ranked by risk and
  priority;
- does not mention owner data, flood layers, CRM integration or data rooms (https://www.paces.com/products/software).

There is no public price, and no 2025–2026 funding round was found beyond `docs/11`'s 2024 Series A (search,
2026-10-23). I (moderate): Paces sells software plus reports checked by people; its tracker is the one most likely to
meet Job 2 head-on for developers. **Threat: medium-high.**

**Acres.** F: three tiers:

| Tier | Price | What it adds |
|---|---|---|
| Plus | $9.99 a month ($7.49 annual) | "Owner names and addresses"; "Elevation, flood, and wetlands" |
| Pro | $124.99 a month ($94.99 annual) | KML and CSV export; taxes and assessment details |
| Enterprise | Custom | "Utilities and infrastructure", national water and sewer lines, skip tracing, collaborative maps |

Source: https://www.acres.com/pricing. The data-centre page offers:
- a filter "by proximity to substations and transmission lines with 230kV+ voltage thresholds";
- proprietary fibre and gas pipeline layers;
- "LLC Unmasking";
- a shared portfolio of inbound parcels;
- political-risk data on 3,000+ municipalities from a Hamlet partnership.

It claims to be the "only AI system that unifies parcel data" and related data (claim; https://www.acres.com/data-center).
The Data Center Index adds verified land sales tied to data-centre activity and "Entity unmasking to reveal real buyers
behind entities", by demo only (https://www.acres.com/data-center-index). A 2025 post claims 150M+ parcels and FEMA
zones at the parcel level
(https://www.acres.com/blog/find-powered-land-data-centers-using-parcel-nationwide-power-energy-data, 8 July 2025).

I (moderate): Acres is in Site finder Pro's price band, but its grid layers sit in Enterprise. Its LLC unmasking
answers "who is buying near me", which Inframation should not try to match. **Threat: medium-high.**

**Enverus PRISM (generation and DC siting).** Enverus pages refuse the fetcher; facts below are *(indexed)* from its own
pages.
- A data-centre workflow for 1,000 MW and 600 MW projects around Cleveland left "fewer than 20 high-value sites",
  99.998% disqualified, on available withdrawal capability
  (https://enverus.com/blog/efficient-data-center-siting-smart-locations-for-peak-performance).
- A Nebraska study compares three SPP sites on withdrawal capacity, LMP and buildable acreage
  (https://www.enverus.com/ebooks/comparing-three-nebraska-data-center-sites-enverus/).
- A 2025 brochure states "156 million parcels" with ownership and suitable-land analysis
  (https://edp.com/sites/default/files/document/2025-04/Enverus%2520Power%2520%2526%2520Renewables.pdf).
- A solution page is titled "Validate Deliverable Power for Data Center Sites"
  (https://www.enverus.com/solutions/validate-deliverable-power-for-data-center-sites/).

I (moderate): the deepest grid data in the set, at enterprise prices (`docs/11`: $40–80k/yr, inference). It is a
threat to the owner's enterprise persona, not to a $150 seat. **Threat: medium.**

**Land id.** F: per user per month, billed annually: Land id $7, Premium $12, Pro $33.33 (three maps) or $66.67
(unlimited). Every plan has the Transmission Lines, Substations, Power Plants and FEMA Floodplain layers. Pro adds
owner contact information. CSV mailing lists cost extra. An Organizations plan (SSO, SOC 2) is in early access
(https://id.land/pricing). The energy page offers lease tracking ("leased, in negotiation, open") and owner contact
across multi-parcel project areas (https://id.land/use-cases/energy-renewables). The substations overlay is HIFLD data:
"Last update was 05/21", and "This overlay has been removed from the HFLID [sic] and can no longer be updated"
(https://support.id.land/substations-overlay). The OpenStreetMap community dates HIFLD's withdrawal of public
substations to 2022 (https://community.openstreetmap.org/t/integration-of-substations-in-hifld-into-osm/141961).

I (high): the owner can test his three criteria by hand in Land id today for $12 a month. I (moderate): the stale grid
and the lack of a statewide corridor search are the gap. **Threat: medium.**

**Nira Energy.** F: three tools:
- a **Prospecting Tool** that shows grid capacity "at every ≥69 kV substation" with "real-time substation capacity and
  upgrade cost data";
- an **In-Queue Tool**;
- a **Local Studies Tool** that "Re-runs full ISO study methodology".

Nira claims "100+ Developers", 700+ GW modelled and ">97%" accuracy in replicating ISO studies (claims). It has a Load
Developers section ("Find build-ready sites with the fastest path to power"), no parcel or owner data, and no public
price (https://www.niraenergy.com/; https://www.niraenergy.com/pricing is 404). I (moderate): Nira uses the same 69 kV
floor and answers the question Inframation's prospect score explicitly cannot (spare capacity). **Threat: medium as an
alternative, but best treated as the hand-off partner for "where to ask first".**

**Latapult.** F: the homepage lists Metros $149 a month (+$49 per extra metro), States $299 (+$199 per extra state, "gaining
energy layers and large parcel ownership data (5+ acres layer)") and USA $999. It cites 250+ layers, "300+
businesses" and a 7-day trial (https://www.latapult.com/). The same page says "geothinQ is now Latapult". The pricing page instead shows Regions from $149 a month and
Regions Plus from $186 a month, says energy data is "available at the state and USA subscription levels", and offers
team packages of 3, 5 or 10 (https://www.latapult.com/pricing). The data-centre page lists flood hazard zones and an
"electric infrastructure" proximity search, but not lines by voltage or substations
(https://latapult.com/gis-for-data-center-site-selection). I (moderate): the closest public price ladder to Site
finder Pro, with weaker energy specificity. **Threat: medium-low.**

**Regrid.** F: Property App plans are Starter free (25 lookups a day), Pro "$10/mo (or $100/year)" (1,000 a day,
5,000 export rows a month) and Team "$20/month per user" (page updated 2026-04-27:
https://support.regrid.com/docs/plan-types-property-app).
- **Data store.** It lists Texas counties with parcel counts and refresh dates, for example Harris 1,547,291 parcels
  refreshed 2026-08-19 and Ward 15,635 refreshed 2026-07-08, but shows no prices (https://app.regrid.com/store/us/tx).
  Ward County's page reports Owner Name at 98% and Mailing Address at 92% coverage
  (https://app.regrid.com/store/us/tx/ward).
- **Last published price schedule** (posted 1 December 2022): $300, $500 or $800 for a county shapefile; $8,000 to
  $20,000 a state; $50,000 to $135,000 nationwide; $0.10 to $0.28 per API record
  (https://regrid.com/blog/2023-parcel-data-price-change).
- **Store licence.** It runs one year. It allows display of parcels to customers if that "reasonably prevents the
  customers from obtaining a copy of a substantial portion". It bars resale and sublicensing, and "creating, offering,
  or selling a website, product, or service that may compete". It requires credit to Loveland Technologies
  (https://app.regrid.com/store/license).

I (moderate): Regrid is a supplier, not a rival. The competing-product clause probably excludes its files from Site
finder Pro without a separate licence. The legal lane (`docs/25-siting-parcels.md`) should read it. **Threat: low;
supplier risk real.**

**Transect.** F: products are Site Assessment, Community Sentiment, Transect Reports, Local Solar Regulations and a
Services Marketplace. Logos include NextEra, Engie, RWE and Arevon. A free mini-report comes from a KML upload, and
there is no public pricing (https://www.transect.com/). Its lines and substations are HIFLD (substations ≥69 kV;
lines "69 kV to 765 KV"); a Parcel Filter finds properties near a line or substation
(https://support.transect.com/transmission-lines-and-substations). Its data-centre buyer's guide names no rivals
(https://www.transect.com/data-center-siting-software-buyers-guide). Third-party: Series A in 2022, $7.59M raised in
total (https://www.caplight.com/company/transect; unverified). **Threat: low-medium.**

**Pivvot.** F: modules Aware, Route, Siting and Reporting & Analytics; land ownership records, parcel
characteristics and permitting jurisdictions; clients Xcel Energy, AES, Brookfield Renewable, Avangrid, Grid United
and Black & Veatch; "a wholly owned subsidiary of Terracon"; a free trial and no price (https://pivvot.com/). The
data-centre page filters on "acreage, land ownership, and proximity to infrastructure, water, and energy sources"
(https://pivvot.com/?p=6433). I (moderate): an engineering-firm platform aimed at linear routing and utilities.
**Threat: low-medium.**

**Orennia.** F: the platform covers data centres, transmission and utilities. It lists "Interconnection injection and
withdrawal capacity", "Buildable areas", "Data center development siting" and a US Behind-the-Meter Playbook, with no
pricing (https://www.orennia.com/). Funding is in `docs/11`. I (moderate): it competes for the infrastructure-investor
persona (decision row 2026-10-10), not for a land search. **Threat: low-medium.**

**Data-centre market intelligence.** These products sell facility and market data, not land.
- **datacenterHawk.** S&P Global agreed to buy it on 28 July 2026 and closed on 1 September 2026; it is now part of S&P
  Global Energy, with asset-level data on "supply, demand, pricing, pipelines and site selection" and its FiberLocator
  platform (https://press.spglobal.com/2026-07-28-S-P-Global-to-Acquire-datacenterHawk-Connecting-Global-Data-Center,-Power-and-Infrastructure-Markets,
  https://press.spglobal.com/2026-09-01-S-P-Global-Completes-Acquisition-of-datacenterHawk).
- **DC Byte.** Claims 8,500+ assets, 136+ countries and 540+ GW of IT capacity tracked; sells Market, Hyperscaler and
  Supplier Analytics and a Site Selector by demo (https://www.dcbyte.com/us/).
- **Baxtel.** Sells site-selection listings and infrastructure maps by quote (https://baxtel.com/).
- **DC Hub's Land & Power Map.** Claims 134,000+ substations, HIFLD lines, FEMA flood zones and 33+ fibre hubs, but no
  parcels. A free preview shows three layers; Pro is unpriced. Its comparison table claims datacenterHawk starts at
  "$15K+/yr" and FiberLocator at "$4,500/yr" (claim; https://dchub.cloud/land-power).

I (moderate): these compete for a data-centre buyer's research budget, not for the land workflow. DC Hub's free map is
the main reason corridors alone cannot be the paid product.

**2025–2026 entrants and powered-land buyers.**
- **GridCARE.** Raised a $13.5M seed led by Xora (27 May 2025). It works with hyperscalers and with Portland General
  Electric and PG&E to find "pockets with geographic and temporal capacity on the existing grid"
  (https://www.gridcare.ai/post/silicon-valley-ai-and-energy-pioneers-launch-gridcare).
- **Acres Intelligence and the Data Center Index.** Acres' new AI agent and index (see the Acres paragraph above).
- **LandGate's government and off-market listings.** See the LandGate paragraph above.
- **Powered-land developers (not software).** These buyers assemble powered land themselves:
  - Silver Lake launched a $400M platform for "powered land sites" in the US, Canada and the UK on 8 August 2025
    (https://silverlake.com/silver-lake-launches-400-million-digital-infrastructure-platform-with-adam-fisher-and-peter-rumbold).
  - Coatue's Next Frontier was reported in May 2026, with a 430 MW Indiana campus
    (https://techcrunch.com/2026/05/01/coatue-has-a-plan-to-buy-up-land-for-data-centers-possibly-for-anthropic/, as
    reported).
  - Submer's Rubix claims an 8 GW powered-land portfolio
    (https://bebeez.eu/2026/06/02/submer-launches-rubix-data-centers-claims-platform-has-8gw-powered-land-portfolio/,
    secondary).

I (moderate): these firms compete with Inframation's users for the same parcels and are themselves potential Site
finder Pro customers. They are also the strongest sign that the money is in owning powered land, not in mapping it.

### 1.3 Vendor comparison pages (claims, not evidence)

| Page | What it claims | What it tells us (I) |
|---|---|---|
| PVcase, "LandGate vs. PVcase Prospect" (9 Feb 2026; https://pvcase.com/blog/landgate-vs-pvcase-prospect-data-center-interconnection) | LandGate uses a "theoretical teaching model" covering the nearest 8 buses; PVcase runs full-grid N-1 and ERIS/NRIS on CEII-verified data and "Tracks over 100,000 nodes via Yes Energy" | Capacity method is where the top vendors fight. Any capacity number Inframation showed would be judged against these, so show none |
| PVcase vs Paces (https://pvcase.com/pvcase-vs-paces) | Paces lacks power-flow studies, CAD, DC design and portfolio management; the rival pitch is "transactional consulting costs" | A rival positions Paces as reports and services |
| LandGate vs Paces (https://www.landgate.com/landgate-vs-paces) | Only LandGate has ATC, hosting capacity, landowner lead scoring, mineral ownership and IRR economics | LandGate sells economics and leads, not maps |
| DC Hub table (https://dchub.cloud/land-power) | DC Hub "Free"; datacenterHawk "$15K+/yr"; FiberLocator "$4,500/yr" | The only public price signal for datacenterHawk, and it comes from a rival |
| Transect buyer's guide (link above) | No rivals named; six criteria: fibre and DC locations, interconnection viability, environmental, reports, workflow, vendor trust | The checklist buyers are being taught; Site finder Pro's page should answer each |

### 1.4 Pricing anchors for Site finder Pro

| Price point (F) | Product | What it includes for this job |
|---|---|---|
| Free | DC Hub preview; Open Infrastructure Map; FEMA; TxGIO | Corridor-style maps, flood, Texas parcels |
| $7–$67/user/mo (annual) | Land id | HIFLD lines and substations (frozen 2021), FEMA, owner contact on Pro |
| $10–$20/mo | Regrid app | Parcels and owners; no grid |
| $95–$125/mo | Acres Pro | Owners, flood, export; grid layers only on Enterprise |
| **$150/mo** | **Inframation Founding Pro (`docs/33` §6.4)** | **Corridors on the current OSM grid, plus Texas parcels, fusion with public records, and the tracker** |
| $149–$999/mo | Latapult | Ownership; energy layers on State and USA plans |
| $750/mo ($9k/yr) | Cleanview DC platform (`docs/11`) | DC tracker, no parcels |
| Quote ($15k+ to $80k+/yr, I) | LandGate, PVcase, Paces, Nira, Enverus, datacenterHawk | Capacity, owners, reports |

I (moderate): $150 a month is defensible only with parcels in the product. Without them it competes with free maps.
With them it sits above the parcel viewers (no automated grid screen, or stale grid data) and below every capacity
product. No public price above $1k a month was found for this job, so the ceiling is set by quote-only sellers, and
this document cannot see those prices.

### 1.5 Policy shifts that change Job 1 demand

- **Texas audit (F).** On 3 August 2026 the governor asked the PUCT and ERCOT to audit every data centre in the
  large-load queue, and said non-compliant projects "must be denied connection". The queue then stood at 474 GW, of
  which 420.8 GW (90.2%) was data centres, against a record peak of 91,089 MW (22 July 2026). About 205 GW was eligible
  for Batch Zero, which required $50,000/MW of security by 10 July
  (https://www.powermag.com/abbott-orders-full-audit-of-texas-data-center-interconnection-queue-threatens-to-deny-grid-access/).
- **What followed (F).** ERCOT plans to finish the audit by 10 December. It will also review community impacts for
  data-centre and crypto loads of 25 MW and above. About 300 data centres of 75 MW or more sit in Batch Zero. An ERCOT
  official told the commission, "We will not have the study done by April 9, 2027" (Utility Dive, 21 August 2026,
  https://www.utilitydive.com/news/ercot-texas-puc-data-center-audit/828472/).
- **Status in October (F).** ERCOT's Large Load Integration page posted Batch Zero verification RFIs dated as late as
  14 October 2026 (https://www.ercot.com/services/rq/large-load-integration).
- **Oncor (F).** Load requests reached 298 GW, of which 44 GW qualified for Batch Zero (Utility Dive, 7 August 2026,
  https://www.utilitydive.com/news/fate-of-oncors-nearly-300-gw-load-pipeline-unclear-following-texas-data-ce/827303/).
- **Not a moratorium (F, as indexed).** Law firms stress the directive is not a moratorium on development
  (https://www.troutman.com/insights/texas-hits-pause-on-data-center-grid-connections-amid-growing-oversight-push/).
- **Federal (F).** On 18 June 2026 FERC issued show-cause orders to PJM, MISO, SPP, CAISO, ISO-NE and NYISO (not
  ERCOT). They define a large load as "50 MW or greater" at one site, interconnecting above 69 kV; responses were due
  17 August (https://www.bracewell.com/resources/ferc-show-cause-orders-data-center-interconnection/). The PUCT's
  interconnection-standards rulemaking (Project 58481) was still in scoping in August (PowerMag, above).

I (moderate): in Texas the near-term question moves from "land near lines" to "land whose power claim survives the
audit". That favours a product that shows the public large-load and queue context next to each corridor, and it
punishes one that implies deliverable MW. FERC's "above 69 kV" threshold supports the owner's voltage floor outside
ERCOT. I (low-moderate): Texas data-centre land transactions slow for 6 to 12 months. Texas-first parcels are still
right, because TxGIO is free, but the pre-sale pitch should not lean on 2027 energisation.

## 2. Job 2: one private tracker

### 2.1 Summary table

The owner's spec (row of 2026-10-23) has four parts:
- three item types: project, bid (Considering → Preparing → Submitted → Shortlisted → Won / Lost / Withdrawn) and
  investment;
- a data room on any item;
- links to live public records;
- "changed since your last visit".

| Product | What it does (F) | Live link to public records? (I) | Bid and investment stages (F/I) | Data room (F) | Public pricing (F) | Who buys (F/I) | Threat (I) | Why (I) |
|---|---|---|---|---|---|---|---|---|
| Spreadsheets, Airtable | The status quo | No | Whatever the user builds | Folder links | Airtable Team $20, Business $45 per user/mo (annual) | Everyone | **High** as the default | Free and flexible; loses only on self-updating facts |
| **Attio** (CRM of record) | Configurable CRM with objects, lists, workflows, API, webhooks, MCP | No (custom build) | Any pipeline (configurable) | 50 GB file storage on Free and Plus | Free (3 seats); Plus $44/mo ($35 annual); Pro $99 ($79); Enterprise custom | Startups, VC, small teams | **Medium** | Can model all three types; integrate, don't fight |
| **Salesforce Sales Cloud** | CRM, opportunity and pipeline management | No | Configurable opportunities | Via add-ons | Starter $25; Pro Suite $100; Core $195; Advanced $395; Max $550 per user/mo (annual) | Larger IPPs, utilities | **Medium-low** | Already in large developers; heavy for small ones |
| **Sitetracker** | Asset-lifecycle project management (built on Salesforce, per a third party) | No | Development to operations | Document and agreement management | Quote (a third party estimates $150–300/user/mo, unverified) | Arevon, RWE, Engie, Southern Company, Enfinity | **Medium** after site control | Owns construction and asset tracking, not early bids |
| **Intapp DealCloud** | Deal and relationship platform for private capital and real assets | No (enrichment from Preqin, PitchBook, FactSet) | Configurable deal stages | Not stated | Quote; Vendr shows Intapp $61k–$418k/yr, median $125k | PE, IB, real-assets firms | **Medium-high** for the investor persona | Large infrastructure investors already run it (I) |
| **Affinity** | Relationship-intelligence CRM | No | "Deal pipeline tracking from first contact to close" | No | $2,000, $2,300 or $2,700 per user/yr | VC, PE | **Low-medium** | Investor CRM; no energy records |
| **Dealpath** | Real-estate deal management | No | Pipeline, due diligence, IC approvals | Task and document collaboration | Quote | 300+ firms (Blackstone, Nuveen, LaSalle logos) | **Low-medium** | Model for the investment flow; data centres as real estate |
| Siting tools' trackers (Paces, LandGate PowerCRM, Acres, Land id, PVcase) | Track parcels and projects inside the siting tool | Partly (their own parcels and grid) | Origination → site control; lease status | Not stated (Latapult Enterprise: "Spatial Document Management") | Bundled in §1 prices | Developers' land teams | **Medium-high** | Where Job 1 and Job 2 meet; no RFPs or funding calls |
| **Energy Adepto** | Utility RFP discovery | Its own RFP records | "Pipeline tracking" on Starter | No | Starter $120/$150 a month; Professional $320/$400; Enterprise (CRM sync) custom | Utility vendors, IPPs, EPCs | **Medium** for bids | RFP discovery plus a light pipeline |
| **Deltek GovWin IQ** | Government opportunity intelligence | Its own notices | Opportunity Management; "Flex Fields (Basic CRM)" | No | Quote | Federal and SLED contractors | **Low-medium** | Bid pipeline pattern; not energy |
| **Instrumentl** | Grant discovery and tracking | Its own funder records | "Track your pipeline from prospecting through award" | Library on Enterprise | Discover $299/mo; Pre-Award $499; Full Lifecycle $999 (annual) | Nonprofits | **Low** | Best design reference for funding-call pipelines |
| **Loopio** | RFP response content and workflow | No | Response projects | No | Quote (Foundations includes 10 seats) | 1,700+ companies | **Low** | Writes answers; does not find or track bids |
| **Responsive** | RFP and questionnaire response platform | No | Response projects | No | Emerging "Starting from $10,000"; others by quote | Enterprise proposal teams | **Low** | Same as Loopio |
| **Datasite** | M&A virtual data room | No | Deal process | Yes | "customized for every transaction" | Banks, PE, corporates | **Low** | Later-stage, per-transaction |
| **SS&C Intralinks** | M&A virtual data room | No | Deal process | Yes | Quote (competitor-authored estimates only) | Banks, PE | **Low** | Same |
| **Ansarada** | Virtual data room with AI redaction and Q&A | No | Deal process | Yes | Quote builder; "free to set up"; pay when live or after 90 days | Mid-market M&A | **Low** | Same |
| **DocSend** (Dropbox) | Tracked document sharing and data rooms | No | No | Yes (Advanced Data Rooms) | *(indexed)* Personal $10, Standard $45 per user/mo; Advanced $150/mo, Advanced Data Rooms $180/mo (3 users) | Founders, developers raising money (I) | **Medium** for the data room | Cheap, familiar early-stage room |

### 2.2 One paragraph each

**Spreadsheets and Airtable.** F: Airtable Team is $20 and Business $45 per user per month, billed annually
(https://airtable.com/pricing). I (moderate-high): most small developers track bids and projects in a spreadsheet and
keep documents in a shared drive. The tracker beats this only where items update themselves, which is the
coordinator's own reasoning in the 2026-10-23 row.

**Attio.** F: Free ($0, up to 3 seats, 50,000 records); Plus $44 per user per month billed monthly or $35 annually (up
to 10 seats); Pro $99 or $79 (unlimited seats); Enterprise custom. The comparison lists "API and webhook access", "App
SDK", "MCP server", workflows and sequences, and 50 GB of file storage on Free and Plus (https://attio.com/pricing).
I (high): Attio is the owner's CRM of record (`CLAUDE.md`; `docs/34`). The tracker should push tracker items to Attio
as deals and read nothing private back into public paths. It should not rebuild contacts, email or sequences.
**Threat: medium**, because a user could model bids in Attio by hand.

**Salesforce Sales Cloud.** F: Starter Suite $25 per user per month; Pro Suite $100; then Core $195, Advanced $395 and
Max $550 per user per month, billed annually. The Web Services API is listed at an "Additional $25
USD/user/month" (https://www.salesforce.com/sales/pricing/). I (moderate): the incumbent at large IPPs and utilities;
too heavy for the owner's first buyers. **Threat: medium-low.**

**Sitetracker.** F: an "asset lifecycle management platform" covering energy transition, utilities, data centres and
digital infrastructure. Logos include Arevon, RWE, Engie, Southern Company and Enfinity; there is no public price
(https://www.sitetracker.com/). Third-party: built on Salesforce (https://apis.io/providers/sitetracker/); an estimated
$150–300 per user per month (https://fieldservicesoftware.io/software/sitetracker/, unverified). I (moderate): owns
the post-site-control, construction and asset phases; weak on bids and funding calls. **Threat: medium.**

**Intapp DealCloud.** F: "the deal and relationship intelligence platform for professional firms". Segments are
private capital, investment banking and real assets. Features include pipeline and deal management, relationship
scores, and enrichment from Intapp Data, Preqin, PitchBook and FactSet; pricing is by demo
(https://www.intapp.com/dealcloud/). Vendr reports Intapp contracts of $61,251 to $418,327 a year, median $125,369
(undated buyer data; https://vendr.com/marketplace/dealcloud). I (moderate): the owner's named persona
(infrastructure deal teams, decision row 2026-10-10) mostly runs DealCloud or Salesforce already. For them,
Inframation's investment tracker is a feed into their CRM, not a replacement. **Threat: medium-high** for the investor
type; low for developers.

**Affinity.** F: Essential $2,000, Scale $2,300 and Advanced $2,700 per user per year, with "Deal pipeline tracking from
first contact to close" (https://www.affinity.co/pricing). **Threat: low-medium.**

**Dealpath.** F: deal management for real-estate investors: pipeline, underwriting comparison, due-diligence and IC
approval workflows. It claims "300+" firms and "$10T+" in transactions, with logos including Blackstone, Nuveen and
LaSalle; no public price (https://www.dealpath.com/). I (low-moderate): the closest model for the "Investment:
Screening → Diligence → Term sheet" flow. **Threat: low-medium.**

**Siting tools' built-in trackers.** These are where Job 1 and Job 2 meet. F:
- Paces: "Track every project in one place", from origination to site control (§1.2).
- LandGate: "a modern map-based CRM" and PowerCRM (§1.2).
- Acres: a shared portfolio of inbound parcels with notes and notifications (§1.2).
- Land id: lease tracking ("leased, in negotiation, open") (§1.2).
- PVcase: lists "Project portfolio management & data storage" on its Paces comparison (claim).
- Latapult Enterprise: "Spatial Document Management" (https://www.latapult.com/pricing).

I (moderate-high): none of these tracks a bid on a utility RFP or a funding call. **Threat: medium-high**, because a
buyer who already has the siting tool gets its tracker free.

**Energy Adepto.** F: Starter $120 or $150 a month with "Pipeline tracking"; Professional $320 or $400; Enterprise
custom with "CRM sync (Salesforce, Hubspot)" (https://energyadepto.com/; marketing page only, private aggregator).
I (moderate): the only energy product found that pairs RFP discovery with a pipeline. **Threat: medium** for the bid
type.

**Deltek GovWin IQ.** F: the Essentials tier lists Opportunity Management, real-time notifications and "Flex Fields
(Basic CRM)"; pricing is by quote
(https://www.deltek.com/en/government-contracting/govwin/federal; https://www.capterra.com/p/154858/GovWin-IQ). I
(moderate): proves the discovery-plus-pipeline pattern sells to bidders; not an energy product. **Threat: low-medium.**

**Instrumentl.** F: Discover $299 a month billed annually ($349 monthly, up to 3 users); Pre-Award $499 ($579, up to 5
users), "Track your pipeline from prospecting through award"; Full Lifecycle $999 ($1,159, up to 15 users); deadline
tracking and extraction from award letters (https://www.instrumentl.com/pricing). I (moderate): nonprofit grants only.
It is the best design reference for funding-call stages and deadline handling, and its prices show that bid trackers
for funding sell at $300 to $1,000 a month. **Threat: low.**

**Loopio and Responsive.**
- Loopio offers Foundations (includes 10 seats), Enhanced and Enterprise, all by quote; claims 1,700+ companies; offers
  an MCP connection to AI assistants (https://loopio.com/pricing/).
- Responsive's Emerging Edition is "Starting from $10,000"; Growth and Enterprise are by quote; the platform fee is an
  annual subscription (https://www.responsive.io/pricing).

I (high): both automate writing answers from a content library. Neither finds an RFP or tracks its public status.
**Threat: low.** Do not build answer libraries.

**Data rooms.**
- Datasite says "Datasite pricing is customized for every transaction"
  (https://www.datasite.com/en/resources/faqs/how-much-does-datasite-cost).
- Intralinks publishes no price. Estimates of $0.40–$0.85 a page come from a competitor
  (https://www.peony.ink/blog/intralinks-pricing, claim).
- Ansarada's room is "free to set up and prepare"; payment starts "once your data room goes live or 90 days after it
  was created" (https://www.ansarada.com/pricing).
- DocSend *(indexed: https://www.docsend.com/pricing/)*: Advanced Data Rooms at $180 a month with 3 users. Its help
  centre describes due-diligence tracking, an audit log and analytics
  (https://help.docsend.com/hc/en-us/articles/19257753466139-What-s-the-DocSend-Advanced-Data-Rooms-plan).

I (moderate): the M&A rooms are a later, per-transaction purchase; DocSend is what early-stage developers use. Build
the early-stage room the owner specified (`docs/19` §3 security bar); do not build Q&A, redaction or per-page billing.
**Threat: low** (M&A rooms), **medium** (DocSend).

### 2.3 What nobody does

I (moderate-high; based on the products in §2.1, not an exhaustive market scan): no tracker found links a private
item to a live public record so that it updates itself. The record could be a queue position, an RFP, a funding
notice, a large-load filing or a retiring plant; the update could be a deadline moved, a notice reinstated or
awarded, or a queue status changed. The closest are:
- **Energy Adepto, GovWin and Instrumentl**: their pipelines sit on their own notice records, but each covers one
  notice class, and none covers supply-side records.
- **The siting tools' trackers**: they sit on their own parcel and grid data, but not on RFPs or funding.

This is the only part of Job 2 worth building. Everything else in it (contacts, email, documents, answer libraries) is
done better by Attio, DocSend or the M&A rooms. It also carries the sensitivity flagged in the 2026-10-23 row: which
RFPs a developer bids on is among its most private facts.

## 3. Complements: tools to use alongside Inframation

| Tool | Job | Cost (F) | What it gives (F) | Inframation should (I) |
|---|---|---|---|---|
| TxGIO StratMap Land Parcels (https://www.geographic.texas.gov/stratmap/land-parcels.html) | 1 | **Free** | Appraisal-district parcels with "property owner, land use, value"; shapefile and geodatabase; refreshed "annually" where possible; "Not all counties"; "not survey grade" | **Ingest** once the legal lane clears terms (only a site copyright notice was found today) |
| County appraisal district, via the Comptroller directory (https://comptroller.texas.gov/taxes/property-tax/county-directory/) | 1 | **Free** | Contacts for every appraisal district and county tax office | **Link** from each parcel to the official record (minimum personal data) |
| FEMA NFHL Viewer (https://msc.fema.gov/nfhl) and Map Service Center (https://msc.fema.gov/portal/home) | 1 | **Free** | Effective flood data covering over 90% of the population; county and state shapefiles; FIRMettes; a KMZ for Google Earth (https://www.fema.gov/flood-maps/national-flood-hazard-layer) | **Link** per corridor and parcel; keep "flood data unavailable" where there is no digital map |
| Open Infrastructure Map (https://openinframap.org/) | 1 | **Free** | OSM power network view, "© OpenStreetMap contributors, ODbL" (https://openinframap.org/about) | **Link** for spot checks of the same OSM lines; do not rebuild a power-map viewer |
| Google Earth Pro desktop (https://www.google.com/earth/about/versions/) | 1 | **Free**; new downloads end 25 June 2027 (same page) | Historical imagery; GIS import and export | **Export KML** of corridors and parcels |
| QGIS (https://qgis.org/download/) | 1 | **Free** (GPLv2+) | Desktop GIS for parcel filtering and same-owner grouping | **Export GeoPackage, GeoJSON, CSV** |
| Felt (https://felt.com/pricing) | 1 | **Free** (no uploads); Team $199/mo annual ($249 monthly) | Shared web maps with comments | **Export**; do not build public map sharing beyond named collaborators |
| ERCOT Large Load Integration (https://www.ercot.com/services/rq/large-load-integration) and GIS Report (https://www.ercot.com/mp/data-products/data-product-details?id=PG7-200-ER) | 1 | **Free** | Batch Zero verification forms and RFIs; monthly generation interconnection status | **Link** the first; the second is already a connector |
| PUCT Interchange (https://interchange.puc.texas.gov/) | 1 | **Free** | SB 6 dockets (58481, 58482) | **Link**; watch for the final rule |
| EIA U.S. Energy Atlas (https://atlas.eia.gov/) | 1 | **Free** | Federal energy layers | Link only |
| Texas RRC GIS Viewer (https://gis.rrc.texas.gov/GISViewer/) | 1 | **Free** | Pipelines and wells | Link; the RRC notice applies (row of 2026-10-22) |
| Regrid (https://support.regrid.com/docs/plan-types-property-app; https://app.regrid.com/store/us/tx) | 1 | Pro $10/mo; county files priced in the store | Parcels and owners where TxGIO has gaps | **Buy for internal use only**; licence bars a competing product; never scrape |
| Land id Pro (https://id.land/pricing) or Acres Pro (https://www.acres.com/pricing) | 1 | $33.33 or $94.99 a month (annual) | Owner contact (Land id Pro); owners, flood and export (Acres) | **Use by hand**; never scrape or embed; at most a "view in" link by coordinates if their URL scheme allows |
| Nira, PVcase Prospect, Paces reports | 1 | Quote | Substation capacity and upgrade cost | **Link and partner**: the "where to ask the utility first" hand-off |
| Transect mini-report (https://www.transect.com/) | 1 | **Free** | Environmental screen from a KML upload | **Export KML** the user uploads |
| Attio (https://attio.com/pricing) | 2 | **Free** up to 3 seats; Plus $35–$44 | CRM with API, webhooks and MCP | **Integrate** behind the `docs/20` §9 port: tracker item to Attio deal, with the Inframation link |
| DocSend *(indexed)* or a shared drive | 2 | $180/mo Advanced Data Rooms; drive free | Tracked external sharing | **Link** from a tracker item until Inframation's data rooms pass the `docs/19` §3 bar |
| Calendar (ICS export) | 2 | **Free** | Deadlines in the user's calendar | **Export** each bid's deadline (cheap; no new data) |
| PeeringDB (https://www.peeringdb.com/about) | 1 | **Free**, but its acceptable-use policy "prevents the database being used for commercial purposes" | Interconnection facility locations | **Do not ingest** |

**Integrate, link or rebuild (I, moderate).** In priority order:
1. **Ingest** TxGIO parcels (the product needs them).
2. **Export** KML, GeoPackage and CSV (one feature serves Google Earth, QGIS, Felt and Transect).
3. **Link** to the appraisal district, FEMA, ERCOT and PUCT records on every parcel and corridor.
4. **Integrate** Attio for tracker items.
5. **Export** deadlines as ICS.
6. **Partner or link** for capacity (Nira, PVcase, Paces).

Rebuild none of: owner contact or skip tracing, capacity studies, environmental reports, M&A data rooms, CRM.

## 4. Where Inframation can win, and where it should not compete

### 4.1 The strongest case that the owner should just use LandGate or Paces

1. **LandGate already does Job 1 and much of Job 2.** Parcels, owners (with a CoreLogic contact export), substations
   with claimed capacity, a DC dashboard, off-market listings with power facts and a map-based CRM: all are in one
   product, now owned by Wood Mackenzie (F, §1.2). Inframation would spend months approaching a subset.
2. **Paces adds people checking the work.** Its power and permitting reports are validated by humans and its software
   tracks projects to site control (F). A buyer who wants an answer, not a map, buys Paces.
3. **The deciding fact is capacity, and Inframation will show none.** The prospect score is a screening rank by design
   (row of 2026-10-22). The rivals' own fight is over whose capacity number is right (§1.3); Inframation is not in it.
4. **The three criteria are a commodity filter.** Four products run them as a search (§0). A buyer can do it by hand
   in Land id for $12 a month.
5. **The first geography is paused.** ERCOT's audit and the Batch Zero delay mean Texas data-centre power claims will
   not be settled before 2027 (F, §1.5).
6. **If the goal is deals, buy the tool.** The owner wants "a tool for finding and discovering powered land deals". If
   the goal is to close powered-land deals himself, one LandGate or Paces seat (price unknown; `docs/11` estimated
   $15–40k a year for Paces, low confidence) costs less than the months of build time parcels, scoring and the tracker
   will take a mostly solo team (I, moderate). This is the strongest point, and the owner should answer it before the
   pre-sale.
7. **Grid data quality is unproven.** OSM's 69 kV+ completeness in Texas is unmeasured; the LBNL line layer covers about
   a fifth of US mileage (row of 2026-10-22).

### 4.2 The case for building it anyway

1. **Price gap.** No public price for automated grid screening on current data was found below Latapult's $299 State
   plan, whose energy layers are unspecified. Acres puts its grid layers in Enterprise; Land id's grid is frozen at 2021
   (F, §1.4).
2. **Current open grid.** OSM, with attribution and share-alike, versus a HIFLD layer that "can no longer be updated"
   (F).
3. **Fusion.** None of the parcel-priced tools joins queue rows, large-load signals, retiring plants (81.1 GW of planned
   retirements in the 2026-10-06 row), node prices and RFPs on one record. LandGate and Enverus do, at enterprise
   prices (I, moderate).
4. **The self-updating tracker is unowned** (§2.3).
5. **Transparency is a position.** A rank whose parts are shown, with "where to ask the utility first", stands apart in
   a market where vendors attack each other's black-box capacity (I, moderate).
6. **The free tier is the audience engine,** and the delayed-tier logic no longer applies (`docs/11` §3 flag).

### 4.3 Where Inframation can win (blunt)

- **One flow from corridor to tracker at $150 a month**: statewide corridor search, then Texas parcels filtered by
  acreage and same-owner assemblage, then a shortlist, then a tracker item. Nobody sells that flow at that price (I,
  moderate).
- **Retired and retiring plant connections as prospects.** It is in the store already, and it is the one powered-land
  signal that is fully public.
- **Bids and funding calls in the same tracker as projects,** updating from the public record.
- **Exports into the tools people already use** (§3), instead of pretending to replace them.
- **Honest absence.** "Flood data unavailable", "capacity not public, ask {TSP}", "parcel not in TxGIO".

### 4.4 Where it should not compete

- Capacity, power flow and upgrade-cost numbers (Nira, PVcase, Paces, Enverus, LandGate).
- Nationwide parcels, owner contact, skip tracing and LLC unmasking (Regrid, Land id, Acres, LandGate with CoreLogic).
- Environmental due diligence and Phase I reports (Transect).
- Colocation market data (datacenterHawk at S&P, DC Byte, Cleanview).
- M&A-grade data rooms (Datasite, Intralinks, Ansarada): no Q&A modules, redaction suites or per-page pricing.
- CRM features (Attio is the system of record) and RFP answer libraries (Loopio, Responsive).
- A land-listings marketplace in the LandGate style. It is a different business, and it sits close to the
  deal-offering line `docs/18` §4 keeps for counsel (I, moderate).

### 4.5 Calibrated view

| Statement | Probability | Confidence |
|---|---|---|
| At least half of the first 20 Site finder Pro prospects already pay for LandGate, PVcase Prospect, Paces, Acres or Enverus | 40% | low; untested |
| Site finder Pro with Texas parcels reaches 10 paying seats at $150/mo within six months of launch | 35% | low |
| The same without parcels | 10% | low |
| LandGate under Wood Mackenzie launches a self-serve tier below $300/mo within 18 months | 20% | low |
| In 10 discovery interviews, at least 3 name the self-updating tracker unprompted as a reason to pay | 30% | low |

### 4.6 What would change this view

- **Discovery questions** to add to `docs/11` §6.1:
  - "Which siting tool do you pay for, what does it cost, and what does it get wrong?"
  - "Where do you track RFP bids and funding applications today, and what goes stale?"
  - "Would you pay for corridors without parcels?"
- **A measurement:** OSM vs HIFLD vs utility one-line diagrams for 69 kV+ lines and substations in three Texas counties
  (assumption A3).
- **Licence reads:** a legal read of TxGIO's terms and of Regrid's competing-product clause before parcels are promised
  in the pre-sale.

## 5. Owner's starter toolkit: use these this week

| # | Tool | Link | Cost | Use it for |
|---|---|---|---|---|
| 1 | Open Infrastructure Map | https://openinframap.org/ | Free | Eyeball 69 kV+ lines and substations (OSM, same source as the finder) |
| 2 | FEMA NFHL Viewer | https://msc.fema.gov/nfhl | Free | Check each candidate against the Special Flood Hazard Area; print a FIRMette |
| 3 | TxGIO StratMap Land Parcels | https://www.geographic.texas.gov/stratmap/land-parcels.html | Free | Download the county's parcels (owner, land use, value) |
| 4 | QGIS | https://qgis.org/download/ | Free | Filter parcels by acreage; dissolve by owner name to find contiguous holdings |
| 5 | County appraisal district (via the Comptroller directory) | https://comptroller.texas.gov/taxes/property-tax/county-directory/ | Free | Confirm owner, legal description and value on the official record |
| 6 | Google Earth Pro desktop | https://www.google.com/earth/about/versions/ | Free (download before 25 June 2027) | Historical imagery of the site and line; measure distances |
| 7 | Land id Pro or Acres Pro | https://id.land/pricing · https://www.acres.com/pricing | $33.33 or $94.99 a month (annual) | Owner contact (Land id Pro) or owners plus LLC context (Acres) for the shortlist only |
| 8 | ERCOT Large Load Integration | https://www.ercot.com/services/rq/large-load-integration | Free | Batch Zero forms and verification status before calling a TSP |
| 9 | PUCT Interchange | https://interchange.puc.texas.gov/ | Free | Follow Projects 58481 and 58482 (SB 6) |
| 10 | Attio | https://attio.com/pricing | Free up to 3 seats | Log each site, bid and investor conversation as a deal with the Inframation link, until My pipeline ships |
| 11 | Regrid Pro | https://support.regrid.com/docs/plan-types-property-app | $10 a month | Parcel lookups in counties TxGIO lacks (internal use only) |
| 12 | Felt (optional) | https://felt.com/pricing | Free viewing; $199/mo to upload | Share a shortlist map with a partner |

**A week's workflow (I).** This runs the decision of 2026-10-22 by hand and shows where the product saves time.
1. Pick one county with 138 kV or higher lines (Open Infrastructure Map) and drop the floodplain (NFHL).
2. Load the TxGIO parcels into QGIS. Keep parcels within 1 mile of a line and 5 miles of a substation, at 100 acres or
   more. Group them by owner.
3. Confirm the top ten on the appraisal district site and in Google Earth Pro.
4. Look up owner contact for the top three in Land id or Acres.
5. Check ERCOT's large-load pages for the area's TSP.
6. Log each in Attio.

Time each step: the steps that take longest are what Site finder Pro has to remove.

## 6. Assumptions and open questions

| # | Assumption | Basis |
|---|---|---|
| A1 | Public prices are as shown on 2026-10-23. DocSend's are as indexed, and its URL carries an A/B test parameter | Pages cited |
| A2 | Quote-only prices are unknown; ranges here are third-party or `docs/11` inferences | Labelled where used |
| A3 | OSM's 69 kV+ completeness in Texas is good enough for corridor search; unmeasured | Test in §4.6 |
| A4 | Regrid's store licence excludes use in Site finder Pro without a separate licence; coordinator-level reading, not counsel's | https://app.regrid.com/store/license |
| A5 | The Texas pause lasts at least to the 10 December audit and the Batch Zero study runs past April 2027 | §1.5 sources |
| A6 | The tracker's self-updating link is unique; based on the products in §2.1, not an exhaustive scan | §2.3 |

**Open question for the owner (I, important):** is the goal to sell a site-finding tool, or to close powered-land
deals yourself? If it is the second, buy a LandGate or Paces seat now and build only the fusion and the tracker (§4.1,
point 6).

## 7. Sources consulted (retrieved 2026-10-23)

Job 1 vendors: https://www.landgate.com/energy-markets/data-centers · https://www.landgate.com/pricing (404) ·
https://www.landgate.com/news/the-fastest-path-to-data-center-land-off-market-listings-direct-from-landowners ·
https://www.landgate.com/landgate-vs-paces ·
https://www.woodmac.com/press-releases/wood-mackenzie-acquires-landgate-to-deliver-connected-intelligence-for-the-new-era-of-us-power-markets/ ·
https://www.mannpublications.com/mannreport/2022/07/05/landgate-unveils-powertools-suite/ · https://www.paces.com/ ·
https://www.paces.com/products/software · https://pvcase.com/data-center-siting · https://www.pvcase.com/prospect/pricing-plans ·
https://pvcase.com/site-selection-analysis · https://pvcase.com/blog/landgate-vs-pvcase-prospect-data-center-interconnection ·
https://pvcase.com/pvcase-vs-paces · https://help.pvcase.com/hc/en-us/articles/49064052340115-January-2026 (indexed) ·
https://www.surgepv.com/reviews/pvcase · https://www.acres.com/pricing · https://www.acres.com/data-center ·
https://www.acres.com/data-center-index ·
https://www.acres.com/blog/find-powered-land-data-centers-using-parcel-nationwide-power-energy-data · https://id.land/pricing ·
https://id.land/use-cases/energy-renewables · https://support.id.land/substations-overlay ·
https://community.openstreetmap.org/t/integration-of-substations-in-hifld-into-osm/141961 ·
https://support.regrid.com/docs/plan-types-property-app · https://app.regrid.com/store/us/tx · https://app.regrid.com/store/us/tx/ward ·
https://app.regrid.com/store/license · https://regrid.com/blog/2023-parcel-data-price-change · https://regrid.com/api ·
https://www.latapult.com/ · https://www.latapult.com/pricing · https://latapult.com/gis-for-data-center-site-selection ·
https://pivvot.com/ · https://pivvot.com/?p=6433 · https://www.orennia.com/ · https://www.niraenergy.com/ ·
https://www.transect.com/ · https://support.transect.com/transmission-lines-and-substations ·
https://www.transect.com/data-center-siting-software-buyers-guide · https://www.caplight.com/company/transect ·
https://enverus.com/blog/efficient-data-center-siting-smart-locations-for-peak-performance (indexed) ·
https://www.enverus.com/ebooks/comparing-three-nebraska-data-center-sites-enverus/ (indexed) ·
https://www.enverus.com/solutions/validate-deliverable-power-for-data-center-sites/ (indexed) ·
https://edp.com/sites/default/files/document/2025-04/Enverus%2520Power%2520%2526%2520Renewables.pdf (indexed) ·
https://press.spglobal.com/2026-07-28-S-P-Global-to-Acquire-datacenterHawk-Connecting-Global-Data-Center,-Power-and-Infrastructure-Markets ·
https://press.spglobal.com/2026-09-01-S-P-Global-Completes-Acquisition-of-datacenterHawk · https://www.dcbyte.com/us/ ·
https://baxtel.com/ · https://dchub.cloud/land-power · https://www.gridcare.ai/post/silicon-valley-ai-and-energy-pioneers-launch-gridcare ·
https://silverlake.com/silver-lake-launches-400-million-digital-infrastructure-platform-with-adam-fisher-and-peter-rumbold ·
https://techcrunch.com/2026/05/01/coatue-has-a-plan-to-buy-up-land-for-data-centers-possibly-for-anthropic/ ·
https://bebeez.eu/2026/06/02/submer-launches-rubix-data-centers-claims-platform-has-8gw-powered-land-portfolio/

Policy: https://www.powermag.com/abbott-orders-full-audit-of-texas-data-center-interconnection-queue-threatens-to-deny-grid-access/ ·
https://www.utilitydive.com/news/ercot-texas-puc-data-center-audit/828472/ ·
https://www.utilitydive.com/news/fate-of-oncors-nearly-300-gw-load-pipeline-unclear-following-texas-data-ce/827303/ ·
https://www.ercot.com/services/rq/large-load-integration ·
https://www.ercot.com/files/docs/2026/08/20/Batch-Zero-Verification-and-Audit-PUCT-Presentation.pdf ·
https://www.troutman.com/insights/texas-hits-pause-on-data-center-grid-connections-amid-growing-oversight-push/ (indexed) ·
https://www.bracewell.com/resources/ferc-show-cause-orders-data-center-interconnection/

Job 2 vendors: https://attio.com/pricing · https://www.salesforce.com/sales/pricing/ · https://www.sitetracker.com/ ·
https://apis.io/providers/sitetracker/ · https://fieldservicesoftware.io/software/sitetracker/ · https://www.intapp.com/dealcloud/ ·
https://vendr.com/marketplace/dealcloud · https://www.affinity.co/pricing · https://www.dealpath.com/ · https://energyadepto.com/ ·
https://www.deltek.com/en/government-contracting/govwin/federal · https://www.capterra.com/p/154858/GovWin-IQ ·
https://www.instrumentl.com/pricing · https://loopio.com/pricing/ · https://www.responsive.io/pricing ·
https://www.datasite.com/en/resources/faqs/how-much-does-datasite-cost · https://www.peony.ink/blog/intralinks-pricing ·
https://www.ansarada.com/pricing · https://www.docsend.com/pricing/ (indexed) ·
https://help.docsend.com/hc/en-us/articles/19257753466139-What-s-the-DocSend-Advanced-Data-Rooms-plan · https://airtable.com/pricing

Complements: https://www.geographic.texas.gov/stratmap/land-parcels.html · https://data.geographic.texas.gov/ ·
https://comptroller.texas.gov/taxes/property-tax/county-directory/ · https://www.fema.gov/flood-maps/national-flood-hazard-layer ·
https://msc.fema.gov/nfhl · https://msc.fema.gov/portal/home · https://openinframap.org/about · https://www.google.com/earth/about/versions/ ·
https://qgis.org/ · https://felt.com/pricing · https://www.ercot.com/mp/data-products/data-product-details?id=PG7-200-ER ·
https://interchange.puc.texas.gov/ · https://atlas.eia.gov/ · https://gis.rrc.texas.gov/GISViewer/ · https://www.peeringdb.com/about
