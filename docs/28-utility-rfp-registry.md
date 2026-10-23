# Utility RFP issuer registry: terms verdicts per issuer (US-303)

Phase 2 data doc. Owner: legal-compliance agent. Status: v1, 2026-10-21. Companion to
`13-legal-data-rights.md` (§3 scraping law, §6 publication matrix), `26-platform-posture.md` (the
`noncommercial` class) and the registry it explains, `data/vendored/rfp_issuers/issuers.yaml`.

**I am not a lawyer and this is not legal advice.** It is a reading of each issuer's own terms, quoted,
with my inferences marked. §7 lists the questions that need counsel.

**Reading convention.** As in docs/13: text in quotation marks or blockquotes is verbatim from the page
named, read on 2026-10-21. Anything marked *Inference* is my reading. The registry holds the full quotes;
the build script checked every one against the captured page text and fails if a quote is not found
word for word.

## 1. Purpose

On 2026-10-21 the owner chose developers first (`docs/00-PLAN.md`, "Matching targets developers first"):
the opportunities a developer or IPP needs are utility, co-op, CCA and state solicitations. The live site
held 0 rows of kind `rfp`. US-303 asks for each curated issuer to be "a row in the source registry with
`source_id`, URL, cadence, `licence`" (`docs/10-prd-mvp.md`). The market-research lane built a candidate
list of 63 US issuers. Before any of them is ingested, each issuer's terms must say what the connector may
do. That is this document:

- read the terms that govern each solicitation page, and its robots.txt;
- give a verdict per issuer, with the clause that decides it;
- recommend the first tranche to build, and what the connector needs per page format.

`CLAUDE.md` is unchanged by any of this: no private aggregator (Energy Adepto, BidNet and the rest) is a
source, and an `unknown` source is treated as restricted.

## 2. Method

- **Scope.** The 63 issuers in the market-research list (29 IOUs, 6 G&T co-ops plus CORE Electric,
  9 public power, 2 joint-action agencies, 6 CCAs, 7 state procurements, the Virginia SCC docket, TVA,
  Puerto Rico). For each, the page that governs is the page in the row's `url`: the listing, or where none
  exists the release, PDF or docket page the RFP was found on.
- **Finding the terms.** The terms link in the listing page's footer; failing that, the homepage footer;
  failing that, one or two conventional paths and a web search. "Not located" means all of these failed.
  A footer link that turned out to be electric-service terms (CPA, SVCE, WestLight, CPS Energy, Santee
  Cooper) is recorded as such, not as website terms.
- **Requests.** 186 GETs to 71 hosts (all HTTPS except Entergy's terms link, fetched over HTTP as the
  page gives it), one at a time, about 1.2 s apart, from this environment's
  datacentre egress, with the repo's identified User-Agent (`Bankable/0.2 (+https://inframation.io/bot;
  contact https://inframation.io/contact)` after a browser-like prefix, as `pipeline/connectors/http.py`
  sends it). Results: 160 × 200, 16 × 403, 10 × 404. No 403, CDN challenge or block was retried, routed
  around or bypassed; those sites are recorded as not retrieved.
- **robots.txt.** Each host's robots.txt was fetched immediately before its first page. The batch did not
  wait to evaluate it before the next request, so one host was read against its robots: one GET of
  `https://www.scc.virginia.gov/` went out before its robots.txt (`User-agent: *` / `Disallow: /`) was
  evaluated. No further request was made to that host. `www.dpuc.state.ct.us` (Connecticut DEEP's
  document store) disallows everything; only its robots.txt was fetched.
- **Challenge flags in my capture tool.** My scratch fetcher scanned whole bodies for Cloudflare markers
  and flagged five 200 pages (MD PSC twice, UES and TEP terms, a Santee Cooper release). Each carries
  Cloudflare's passive detection script (`/cdn-cgi/challenge-platform/scripts/jsd/main.js`) near the end
  of the body, at byte offsets 77,975 to 190,591. They are not blocks: the full text was served. The
  repo's `is_challenge` scans only the first 4,000 bytes, so it does not misfire on these pages. True
  challenges came from TVA, NYSERDA and ACES only.
- **What was not done.** No bid portal was opened or registered on (PowerAdvocate, Accion, Enel X, Zycus,
  Jaggaer, Asite, TEA, rfpmanager, Ariba). No third-party copy of a terms page (archives, ToS trackers) was
  relied on. Raw captures are in the session scratchpad, not committed; re-run the fetches to verify.

## 3. Verdicts and the rules that produce them

### 3.1 The four verdicts

| Verdict | Automated reading | What may be published | Registry `publish` |
|---|---|---|---|
| **cleared** | yes, at a polite cadence | derived fields (issuer, title, dates, MW, technology, contract type, states) with the credit line and a link back; never the documents | `derived_fields_with_attribution` |
| **link-out only** | yes, only to detect new solicitations | title, dates and link; no derived fields beyond those, no copied documents | `title_date_link` |
| **manual only** | no | a human may record title, dates and link | `title_date_link` |
| **excluded** | no | nothing from this site; use another route (regulator docket, written permission) | `none` |

### 3.2 Decision rules (the registry's `basis` field)

Applied in this order; the first that fits decides.

1. `login`: the listing needs registration → **excluded**.
2. `terms_bar_manual_monitoring`, `terms_bar_manual_and_derivative_use`, `terms_bar_linking`: the terms
   bar a human from monitoring or copying the pages, bar any derivative use, or bar links → **excluded**.
   A listing compiled by a person and linked out would itself breach these.
3. `robots_disallow`, `blocked` (403 or challenge to the identified client), `terms_bar_automation` (an
   express robots/scraping/data-mining clause), `terms_not_located`, `terms_silent` → **manual only**.
   Unknown terms follow docs/13's rule for `unknown` (treated as restricted, nothing ingested
   automatically; the Class VI precedent in docs/13 §6). A 403 to a client that names itself is a gate:
   `docs/20` lets egress be escalated "only with legal sign-off", and docs/13 §3.1 says bypassing a gate is
   what turns a contract question into a CFAA one (`docs/20` §12).
4. `terms_restrict_information`, `terms_restrict_content`: automation is not barred, but the terms bar
   copying or disseminating the site's content (in eight cases, content defined to include "information"
   or "data") → **link-out only**. This is docs/13's treatment of ISO-NE, whose "Content" also includes
   "all information and data" and which is `link-out-only` (§1.6).
5. `terms_permit_open`, `terms_permit_noncommercial`, `terms_permissive_by_omission`: automation not
   barred, robots allows the path, and the terms either grant reuse or, read in full, restrict only marks
   or images → **cleared**. Permissive by omission is docs/13's NYISO reading (§1.5).

### 3.3 How a verdict maps to docs/13 and to `data/sources.yaml`

| Verdict | Registry `reuse` | docs/13 class | Manifest `reuse` / `publication` it supports today |
|---|---|---|---|
| cleared (NJ BPU) | `open` | `permissive` | `open` / `derived_only` |
| cleared (CT DEEP, ODEC) | `attribution` | `attribution-restricted` | `attribution` / `derived_only` |
| cleared (AEP ×4) | `noncommercial` | `noncommercial` | `noncommercial` / `derived_only`, gated under `commercial` (docs/26 §5) |
| link-out only, manual only (terms restrict) | `excluded` | `restricted` | `restricted` / `none` |
| manual only (terms unknown) | `unknown` | `unknown` | `unknown` / `none` |
| excluded | `excluded` | `restricted` | `restricted` / `none` |

The registry's `reuse` uses the vocabulary the US-303 brief asked for (open / attribution / unknown / excluded) plus
`noncommercial`, because AEP's grant is exactly that class and docs/13 §0 has it. `excluded` in `reuse`
means the terms withhold reuse of the content; it is not the verdict.

**Link-out only and manual only publish nothing today.** `docs/21` §8 shows nothing at all from a
`restricted` or `unknown` source: no record, no event. A title-date-link row needs a link-out surface like
the one news items already use (`services/api/documents.py`: "a news item is always link-out"), and
counsel item C-2. Until both exist, those rows are a backlog of known solicitations the operator sees, not
public records.

## 4. What the terms say, and what I infer

**The text.** The utility terms are boilerplate and mostly restrictive. 34 rows rest on a terms page read
in full: 31 distinct pages, since the four AEP companies share one. Two secondary pages (Southern Company,
PSEG Long Island) were read too. The patterns below are each quoted in the registry.

- *Personal or noncommercial use only, no redistribution.* EPE: "The content of the Site is for your
  personal, noncommercial use." Hawaiian Electric: "You may not distribute, publish, transmit, modify,
  create derivative works from, or in any way exploit any of the content, in whole or in part, for any
  purpose." Eight issuers define the protected content to include "information" or "data" (PacifiCorp,
  PGE, APS, PNM, Hawaiian Electric, SDG&E, SMUD, Austin Energy).
- *No dissemination of information.* PGE and SMUD use the same sentence: "Commercial use or broadcasting
  or otherwise disseminating any information in the proprietary materials in any media, without prior
  written approval ..., is strictly prohibited."
- *No robots.* PSE: "Use any data mining, robots or similar data gathering or extraction methods designed
  to scrape or extract data from the Services". Dominion, Consumers Energy, PG&E, NIPSCO and Ava say the
  same in their own words.
- *No manual monitoring either.* UES/TEP: "Use any manual process, including phishing, to monitor or copy
  any Content ... without UES’ prior written consent." Tri-State, LIPA, CenterPoint and Evergy have the
  same clause.
- *No links.* DTE and Evergy "prohibit caching, unauthorized hypertext links". PSEG Long Island's
  Acceptable Use Policy: "Use any deep link to the Site or any portion thereof".
- *Conditional clauses that leave room.* SCE bars bots only "in a manner that interferes with,
  destabilizes, degrades the performance of, or places an unreasonable or disproportionately large load
  on, the Website". APS bars automation only "to attempt to gain unauthorized access" and "encourages you
  instead to tell others about the Site and direct them to the Site". Austin Energy "grants permission for
  websites to link to the home page of austinenergy.com or to any page within."
- *Grants.* New Jersey: "anyone may view, copy or distribute State information found here without
  obligation to the State". AEP: "If attribution to AEP is included, limited quotations from the content
  are hereby permitted. You may not copy or display for redistribution to third parties for commercial
  purposes any portion of the content without the prior written permission of AEP."
- *Assent clauses* ("By using this Site, you agree ...") appear on almost every IOU page. Under docs/13
  §3.2, reading these terms gives Inframation actual knowledge, so the restrictions are operating
  constraints, not suggestions.

**Inferences.**

1. *Facts versus contract.* The facts of a solicitation (issuer, title, dates, MW) are not copyrightable
   (*Feist*, docs/13 §1.4). The restrictions bite as browsewrap contract terms, and actual knowledge makes
   them more enforceable. That is why "information"-inclusive clauses get link-out only rather than
   cleared: the same call docs/13 made for ISO-NE. Confidence: high on what the text says, moderate on
   whether an issuer would enforce it against a factual listing that links back. Utilities run RFPs to
   attract bidders. Counsel item C-1.
2. *Public bodies are mostly silent, not restrictive.* SCPPA, the Illinois Power Agency, the Georgia and
   Maryland PSCs, Santee Cooper, NYPA and three of the CCAs publish no website-use terms at all. Under the
   conservative rule they are manual only, but their solicitations are public contracting notices and
   their records are public by statute. One counsel ruling (C-3) could move most of them to cleared.
   Confidence: moderate.
3. *The verdicts track the site, not the solicitation.* An issuer that is excluded on its own website may
   still be reachable through its regulator's docket: OPUC, IPUC, MPSC, IURC, CO PUC. Each docket system is
   its own source with its own terms (`us.state.puc_dockets` is `unknown` in docs/13 §6). The same
   solicitation found there is a separate terms question, not a loophole.
4. *Permission is cheap here.* Unlike a data vendor, an issuer gains from wider notice of its RFP. A short
   written request is the shortest path for the restrictive IOUs (§9).

## 5. Issuers, verdicts and the deciding clause

Sorted by verdict, then developer value. "Terms URL" is the page read, or attempted when blocked. Cells
quote the deciding text in short form; the registry carries it in full.

| # | Issuer (`id`) | Value | Verdict | Reuse | Clause or fact that decides it | Terms URL | Read |
|---|---|---|---|---|---|---|---|
| 1 | Appalachian Power (`us.rfp.appalachian_power`) | high | **cleared** | `noncommercial` | "If attribution to AEP is included, limited quotations from the content are hereby permitted. You may not copy or display for redistribution to third parties for commercial purposes ..." | https://www.aep.com/terms/ | 2026-10-21 |
| 2 | Connecticut DEEP (`us.rfp.ct_deep`) | high | **cleared** | `attribution` | Disclaimer restricts only the state arms/seal and the CT.gov logo; no restriction on reuse of state information or on automated access | https://portal.ct.gov/portal/policies/disclaimer/ | 2026-10-21 |
| 3 | New Jersey Board of Public Utilities (`us.rfp.nj_bpu`) | high | **cleared** | `open` | "anyone may view, copy or distribute State information found here without obligation to the State" | https://www.nj.gov/nj/legal.shtml | 2026-10-21 |
| 4 | Public Service Company of Oklahoma (`us.rfp.pso`) | high | **cleared** | `noncommercial` | "If attribution to AEP is included, limited quotations from the content are hereby permitted. You may not copy or display for redistribution to third parties for commercial purposes ..." | https://www.aep.com/terms/ | 2026-10-21 |
| 5 | Southwestern Electric Power Company (`us.rfp.swepco`) | high | **cleared** | `noncommercial` | "If attribution to AEP is included, limited quotations from the content are hereby permitted. You may not copy or display for redistribution to third parties for commercial purposes ..." | https://www.aep.com/terms/ | 2026-10-21 |
| 6 | Indiana Michigan Power (`us.rfp.indiana_michigan_power`) | medium | **cleared** | `noncommercial` | "If attribution to AEP is included, limited quotations from the content are hereby permitted. You may not copy or display for redistribution to third parties for commercial purposes ..." | https://www.aep.com/terms/ | 2026-10-21 |
| 7 | Old Dominion Electric Cooperative (`us.rfp.odec`) | low | **cleared** | `attribution` | Notice restricts only trademarks ("may not be used without permission"); no restriction on content reuse or automated access | https://www.odec.com/terms/ | 2026-10-21 |
| 8 | Arizona Public Service (`us.rfp.aps`) | high | link-out only | `excluded` | "otherwise disseminating any Content [defined to include all information] ... is strictly prohibited"; "APS encourages you instead to tell others about the Site and direct them to the Site" | https://www.aps.com/en/Utility/Regulatory-and-Legal/Terms-of-Use | 2026-10-21 |
| 9 | Austin Energy (`us.rfp.austin_energy`) | high | link-out only | `excluded` | "no unauthorized use is permitted" of copyrighted "data, information and images"; "grants permission for websites to link to ... any page within" | https://austinenergy.com/about/company-profile/privacy-policy | 2026-10-21 |
| 10 | El Paso Electric (`us.rfp.el_paso_electric`) | high | link-out only | `excluded` | "The content of the Site is for your personal, noncommercial use"; commercial use needs written consent | https://www.epelectric.com/company/terms-of-use | 2026-10-21 |
| 11 | Hawaiian Electric (`us.rfp.hawaiian_electric`) | high | link-out only | `excluded` | "You may not distribute, publish ... or in any way exploit any of the content [incl. all information, data], in whole or in part, for any purpose" | https://www.hawaiianelectric.com/terms-of-use | 2026-10-21 |
| 12 | PacifiCorp (`us.rfp.pacificorp`) | high | link-out only | `excluded` | "The Content on this site may not be used, unless specifically permitted in writing" (Content = "the information included on this site") | https://www.pacificorp.com/terms | 2026-10-21 |
| 13 | Public Service Company of New Mexico (`us.rfp.pnm`) | high | link-out only | `excluded` | "You may not copy, modify or distribute the content [= any information ...] in any form without written permission" | https://www.pnm.com/terms-of-use | 2026-10-21 |
| 14 | Portland General Electric (`us.rfp.portland_general`) | high | link-out only | `excluded` | "otherwise disseminating any information in the proprietary materials in any media, without prior written approval from PGE, is strictly prohibited" | https://portlandgeneral.com/legal-notices | 2026-10-21 |
| 15 | Southern California Edison (`us.rfp.sce`) | high | link-out only | `excluded` | Use limited to "non-commercial personal or internal business use"; bots barred only "in a manner that interferes with ... or places an unreasonable or disproportionately large load" | https://www.sce.com/terms-conditions/website-terms-of-use | 2026-10-21 |
| 16 | San Diego Gas & Electric (`us.rfp.sdge`) | medium | link-out only | `excluded` | "No other use of the Content [incl. information, data], including without limitations any republications thereof, is permitted without prior written permission" | https://www.sdge.com/node/20211 | 2026-10-21 |
| 17 | Colorado Springs Utilities (`us.rfp.colorado_springs_utilities`) | low | link-out only | `excluded` | "content may not be reproduced, downloaded, disseminated, published, or transferred in any form ... except with the prior written permission" | https://www.csu.org/copyright-notice?hsLang=en | 2026-10-21 |
| 18 | Great River Energy (`us.rfp.great_river_energy`) | low | link-out only | `excluded` | "personal, non-commercial transitory viewing only"; may not use the materials "for any public display" | https://greatriverenergy.com/terms/ | 2026-10-21 |
| 19 | Sacramento Municipal Utility District (`us.rfp.smud`) | low | link-out only | `excluded` | "otherwise disseminating any information in the proprietary materials in any media, without prior written approval from SMUD, is strictly prohibited" | https://www.smud.org/Corporate/Privacy-policy/Terms-and-conditions | 2026-10-21 |
| 20 | Clean Power Alliance of Southern California (`us.rfp.clean_power_alliance`) | high | manual only | `unknown` | Footer "Terms and Conditions" is the customer service agreement; no website-use terms located | https://cleanpoweralliance.org/terms-and-conditions/ | 2026-10-21 |
| 21 | Consumers Energy (`us.rfp.consumers_energy`) | high | manual only | `excluded` | "not to use ... spiders, robots ... to navigate or search the Site ... other than generally available third-party web browsers" | https://www.consumersenergy.com/terms-and-conditions | 2026-10-21 |
| 22 | Dominion Energy Virginia (`us.rfp.dominion_virginia`) | high | manual only | `excluded` | "You agree You will not: use spiders, robots, data mining techniques or other automated devices or programs to catalog, download or otherwise reproduce, store or distribute content"; linking permitted | https://www.dominionenergy.com/terms-and-conditions | 2026-10-21 |
| 23 | Duke Energy (`us.rfp.duke_energy`) | high | manual only | `unknown` | Site answers 403 "Access Denied" to an identified client, robots.txt included; terms not retrieved | https://www.duke-energy.com/legal/terms-of-use | 2026-10-21 |
| 24 | Entergy (`us.rfp.entergy`) | high | manual only | `unknown` | Terms page on www.entergy.com answers 403 to an identified client; rfp.entergy.com footer "© 2022 Entergy Corporation, All Rights Reserved." | http://www.entergy.com/privacy_legal/legal.aspx | 2026-10-21 |
| 25 | Georgia Power (`us.rfp.georgia_power`) | high | manual only | `unknown` | No website-use terms located; footer "©2026 Georgia Public Service Commission. All rights reserved." | https://psc.ga.gov/search/facts-docket/?docketId=56298 | 2026-10-21 |
| 26 | Illinois Power Agency (`us.rfp.illinois_ipa`) | high | manual only | `unknown` | No website-use terms located; footer "© 2026 Copyright Illinois Power Agency. All rights reserved." | https://www.ipa-energyrfp.com/indexed-renewables/ | 2026-10-21 |
| 27 | Massachusetts DOER and distribution companies (`us.rfp.massachusetts_clean_energy`) | high | manual only | `unknown` | No website-use terms located (only a WordPress.com footer) | https://macleanenergy.com/ | 2026-10-21 |
| 28 | NV Energy (`us.rfp.nv_energy`) | high | manual only | `unknown` | No website-use terms located (JavaScript-only site) | https://www.nvenergy.com/ | 2026-10-21 |
| 29 | NYSERDA (`us.rfp.nyserda`) | high | manual only | `unknown` | Cloudflare challenge on every request including robots.txt; terms not retrieved | https://www.nyserda.ny.gov/robots.txt | 2026-10-21 |
| 30 | Puget Sound Energy (`us.rfp.puget_sound_energy`) | high | manual only | `excluded` | "you will not: ... Use any data mining, robots or similar data gathering or extraction methods designed to scrape or extract data from the Services" | https://www.pse.com/en/pages/terms-of-use | 2026-10-21 |
| 31 | Southern California Public Power Authority (`us.rfp.scppa`) | high | manual only | `unknown` | No website-use terms located; footer "© 2026 SCPPA . All rights reserved." | https://scppa.org/rfps-bids/rfps-resourceproject/ | 2026-10-21 |
| 32 | San Diego Community Power (`us.rfp.sdcp`) | high | manual only | `unknown` | Site answers 403 to an identified client; terms not retrieved | https://sdcommunitypower.org/ | 2026-10-21 |
| 33 | Salt River Project (`us.rfp.srp`) | high | manual only | `unknown` | Site answers 403 to an identified client although robots.txt allows /; terms not retrieved | https://www.srpnet.com/ | 2026-10-21 |
| 34 | Xcel Energy (`us.rfp.xcel`) | high | manual only | `unknown` | No website-use terms located (only account-holder terms linked) | https://www.xcelenergy.com/company/rates_and_regulations/resource_plans | 2026-10-21 |
| 35 | Ava Community Energy (`us.rfp.ava`) | medium | manual only | `excluded` | "expressly restricted from ... publishing any Website material in any media ... engaging in any data mining, data harvesting, data extracting" | https://avaenergy.org/terms-website/ | 2026-10-21 |
| 36 | Avista Utilities (`us.rfp.avista`) | medium | manual only | `unknown` | No website-use terms located; the policies page is privacy and text-alert terms only | https://www.myavista.com/about-us/policies-and-guidelines | 2026-10-21 |
| 37 | CORE Electric Cooperative (`us.rfp.core_electric`) | medium | manual only | `unknown` | Site answers a Cloudflare challenge (403) to an identified client; terms not retrieved | https://www.acespower.com/ | 2026-10-21 |
| 38 | Maine Public Utilities Commission (`us.rfp.maine_puc`) | medium | manual only | `unknown` | CloudFront 403 "Request blocked." on robots.txt; terms not retrieved | https://www.maine.gov/robots.txt | 2026-10-21 |
| 39 | Maryland Public Service Commission (`us.rfp.maryland_psc`) | medium | manual only | `unknown` | No website-use terms located; footer "© 2026 Maryland Public Service Commission" | https://psc.maryland.gov/ | 2026-10-21 |
| 40 | MCE (`us.rfp.mce`) | medium | manual only | `unknown` | Site answers 403 to an identified client; terms not retrieved | https://www.mcecleanenergy.org/ | 2026-10-21 |
| 41 | Pacific Gas and Electric (`us.rfp.pge`) | medium | manual only | `excluded` | Mining clause: no "software, bots, spiders, or other information-gathering devices ... to monitor, copy, or otherwise 'mine' information displayed on the Website for display on or use with any Website or service" | https://www.pge.com/en/privacy-center/disclosure.html | 2026-10-21 |
| 42 | Santee Cooper (`us.rfp.santee_cooper`) | medium | manual only | `unknown` | Assent sentence points to the retail electric-service terms, which say nothing about website reuse or automated access | https://www.santeecooper.com/about/legal-notices/ | 2026-10-21 |
| 43 | Silicon Valley Clean Energy (`us.rfp.svce`) | medium | manual only | `unknown` | Footer "Terms & Conditions" is the customer service agreement; no website-use terms located | https://www.svcleanenergy.org/conditions/ | 2026-10-21 |
| 44 | Tennessee Valley Authority (`us.rfp.tva`) | medium | manual only | `unknown` | Cloudflare challenge on every request including robots.txt; terms not retrieved | https://www.tva.com/robots.txt | 2026-10-21 |
| 45 | Virginia SCC (`us.rfp.virginia_scc`) | medium | manual only | `unknown` | robots.txt: "User-agent: *" / "Disallow: /" | https://www.scc.virginia.gov/robots.txt | 2026-10-21 |
| 46 | WestLight Energy (`us.rfp.westlight`) | medium | manual only | `unknown` | Footer "Terms & Conditions" is the customer service agreement; no website-use terms located | https://www.westlightenergy.org/terms-and-conditions-of-service/ | 2026-10-21 |
| 47 | Ameren Missouri (`us.rfp.ameren`) | low | manual only | `unknown` | robots.txt and /terms answer 403 to an identified client; terms not retrieved | https://www.ameren.com/terms | 2026-10-21 |
| 48 | California Community Power (`us.rfp.cc_power`) | low | manual only | `unknown` | No website-use terms located; footer "Copyright © 2026, CA Community Power" | https://cacommunitypower.org/ | 2026-10-21 |
| 49 | CPS Energy (`us.rfp.cps_energy`) | low | manual only | `unknown` | Footer "Customer Terms & Conditions" is the electric-service tariff document; no website-use terms located | https://www.cpsenergy.com/content/corporate/en/about-us/terms-and-conditions.html | 2026-10-21 |
| 50 | East Kentucky Power Cooperative (`us.rfp.ekpc`) | low | manual only | `unknown` | Site answers a Cloudflare challenge (403) to an identified client; terms not retrieved | https://www.acespower.com/ | 2026-10-21 |
| 51 | Golden Spread Electric Cooperative (`us.rfp.golden_spread`) | low | manual only | `unknown` | Site answers a Cloudflare challenge (403) to an identified client; terms not retrieved | https://www.acespower.com/ | 2026-10-21 |
| 52 | Hoosier Energy (`us.rfp.hoosier_energy`) | low | manual only | `unknown` | Site answers a Cloudflare challenge (403) to an identified client; terms not retrieved | https://www.acespower.com/ | 2026-10-21 |
| 53 | NIPSCO (`us.rfp.nipsco`) | low | manual only | `excluded` | "not permitted ... accessing or collecting data from our Website using automated or any other unauthorized means" | https://www.nipsco.com/our-site/terms-of-use | 2026-10-21 |
| 54 | New York Power Authority (`us.rfp.nypa`) | low | manual only | `unknown` | Only footer legal page (Disclaimer) concerns the translation widget; terms silent on reuse and automated access | https://www.nypa.gov/Footer/Disclaimer | 2026-10-21 |
| 55 | Platte River Power Authority (`us.rfp.platte_river`) | low | manual only | `unknown` | No website-use terms located; footer "Copyright © 2026 Platte River Power Authority. All rights reserved." | https://prpa.org/linking-policy/ | 2026-10-21 |
| 56 | DTE Electric (`us.rfp.dte`) | high | excluded | `excluded` | "prohibit caching, unauthorized hypertext links to a website"; no use of content "for any commercial or public purpose" | https://www.dteenergy.com/us/en/quicklinks/terms-conditions.html | 2026-10-21 |
| 57 | Idaho Power (`us.rfp.idaho_power`) | high | excluded | `excluded` | "performing manually or in an automated fashion ... scrape the Site ... make any derivative use of this Site or its contents"; no storage "in any information storage and retrieval system" | https://www.idahopower.com/terms-and-conditions/ | 2026-10-21 |
| 58 | CenterPoint Energy Indiana South (`us.rfp.centerpoint_indiana`) | medium | excluded | `excluded` | "deep-link, scraper, robot ... or manual process having similar processes or functionality, to access, acquire, copy, or monitor any portion of the Service" | https://www.centerpointenergy.com/en-us/about-us/legal/terms-of-use | 2026-10-21 |
| 59 | Long Island Power Authority (`us.rfp.lipa`) | medium | excluded | `excluded` | lipower.org: "Use any manual process to monitor or copy any of the material"; psegliny.com (listing host): "Use any deep link to the Site or any portion thereof" | https://www.lipower.org/terms-of-use/ | 2026-10-21 |
| 60 | Puerto Rico Energy Bureau / PREPA renewable and storage tranches (`us.rfp.puerto_rico_preb`) | medium | excluded | `excluded` | Bid portal requires registration (Accion, PREB independent coordinator); energia.pr.gov has no terms page | https://energia.pr.gov/ | 2026-10-21 |
| 61 | Tucson Electric Power and UniSource Energy Services (`us.rfp.tep_uns`) | medium | excluded | `excluded` | "Access, monitor, or copy any part of the Website using any automatic device ... Use any manual process ... to monitor or copy any Content" without written consent | https://www.uesaz.com/terms-of-use/ | 2026-10-21 |
| 62 | Tri-State Generation and Transmission Association (`us.rfp.tri_state`) | medium | excluded | `excluded` | "Use any robot ... to access the Website for any purpose"; "Use any manual process to monitor or copy any of the material" | https://tristate.coop/terms-and-conditions/ | 2026-10-21 |
| 63 | Evergy (`us.rfp.evergy`) | low | excluded | `excluded` | "or any manual process, to monitor or copy any of the Web pages, data or content"; "prohibits ... unauthorized hypertext links" | https://www.evergy.com/terms-and-conditions | 2026-10-21 |

Two rows have a second terms source in the registry. Georgia Power's own site is under Southern Company's
terms ("You may not reproduce, republish, transmit, distribute, publicly display ... any of the Contents
of the Web Sites without the prior written consent"), so `georgiapower.com` is not a route either. LIPA's
listing lives on `psegliny.com`, whose Acceptable Use Policy bars deep links.

## 6. Counts

| | Cleared | Link-out only | Manual only | Excluded | Total |
|---|---|---|---|---|---|
| High value | 5 | 8 | 15 | 2 | 30 |
| Medium value | 1 | 1 | 12 | 5 | 19 |
| Low value | 1 | 3 | 9 | 1 | 14 |
| **All** | **7** | **12** | **36** | **8** | **63** |

By basis: terms not located 14; blocked (403 or challenge) 13; terms restrict information 8; terms bar
automation 6; terms bar manual monitoring 5; terms restrict content 4; noncommercial grant 4; permissive by
omission 2; terms silent 2; open grant 1; robots disallow 1; login 1; linking barred 1; manual and
derivative use barred 1.

Summarised three ways:

- **Cleared: 7.** Terms permit automated reading and republishing facts with attribution: Appalachian
  Power, PSO, SWEPCO and Indiana Michigan Power (AEP, `noncommercial`), Connecticut DEEP, New Jersey BPU,
  ODEC.
- **Unclear: 29.** Terms not located (14), not retrieved because the site refused this client (13), or
  silent (2).
- **Prohibited or gated: 27.** The terms restrict reuse or automation, bar monitoring or linking, need a
  login, or robots.txt disallows the path (Virginia SCC).

Of the 17 issuers the market-research lane named strongest for a first connector, 4 are cleared (the AEP
operating companies), 5 are link-out only (PacifiCorp, PGE, El Paso Electric, SCE, Austin Energy), 7 are
manual only (Entergy, SCPPA, PSE, PG&E, Illinois IPA, Massachusetts, Consumers Energy) and 1 is excluded
(LIPA).

## 7. Questions for counsel

Numbered C-1 to C-12 so the registry's notes can cite them. Each names the rows it would move.

- **C-1 Facts against "information" clauses.** Eight issuers define restricted content to include
  "information" or "data" and bar disseminating it. Four more bar copying their content. Does a listing
  Inframation writes itself (issuer, title, dates, MW, technology, link) breach those clauses? It copies
  no text and no documents. If not, those 12 link-out-only rows can move to cleared. This is docs/13 §7
  item 6 (the event without content) in RFP form.
- **C-2 Title, dates and link.** If C-1 is no, is a bare title, dates and a link still permitted? APS
  "encourages you ... to tell others about the Site and direct them to the Site"; Austin Energy permits
  links "to any page within". Is an RFP's title "Content"? The answer decides whether the link-out surface
  (§3.3) is worth building.
- **C-3 Silent public bodies.** SCPPA, Illinois IPA, Georgia PSC, Maryland PSC, Santee Cooper, NYPA, CPA,
  SVCE, WestLight, CC Power, CPS Energy and Platte River publish no reuse terms. May `unknown` become
  `attribution` for a public body whose public contracting notice is a public record (statutes listed per
  row; not retrieved)? Related: may a California public agency restrict reuse of records it must disclose
  (*County of Santa Clara v. Superior Court* (2009) 170 Cal.App.4th 1301, cited from memory, not
  retrieved)? Ava's site, a public joint powers agency's, expressly bars "publishing any Website material
  in any media".
- **C-4 AEP's ceiling.** AEP defines content as "the text and images herein and their arrangement" and
  bars commercial redistribution of "any portion of the content". Are derived facts outside "content", so
  the four AEP rows could be `attribution` and survive a flip to `commercial` (docs/26 §5)? Or is
  `noncommercial` right?
- **C-5 Conditional anti-bot clauses.** SCE bars bots only where they interfere or load the site. APS bars
  automation only for unauthorized access. Is one polite request per listing per poll outside both?
- **C-6 Manual monitoring and links.** UES/TEP, Tri-State, LIPA, CenterPoint and Evergy bar manual
  monitoring. DTE and Evergy bar "unauthorized hypertext links", PSEG Long Island bars deep links, and
  Tri-State conditions links on notifying its Webmaster. Are these enforceable against a non-customer
  reading a public page? Deep-linking case law (*Ticketmaster Corp. v. Tickets.com*, C.D. Cal. 2000, cited
  from memory) is copyright, not contract.
- **C-7 Media licences.** PacifiCorp licenses "Reporters and media companies" to reproduce "Content
  intended for press release". SCE's Newsroom content "may be reproduced solely for editorial purposes in
  news and trade publications". Does Inframation qualify, and does it matter that the platform is
  noncommercial?
- **C-8 TVA.** TVA is a federal corporation. Are its works 17 U.S.C. § 105 public domain? If so, only the
  Cloudflare gate stands between TVA and cleared, and that gate is technical, not legal.
- **C-9 Bid portals.** PowerAdvocate, Accion, Enel X, Zycus, Jaggaer, Asite, TEA, rfpmanager and Ariba
  are clickwrap (docs/13 §3.2, §3.5). Confirm the rule this registry assumes: never register on one to
  collect data.
- **C-10 Permission letters.** Who signs, what scope to ask for, and whether an email reply is enough
  licence evidence. The ask: automated reading at a stated cadence, derived facts, credit, link; free now,
  commercial later. The pattern is the PJM request in `docs/37`.
- **C-11 Docket routes.** For excluded issuers, the regulator's docket is the alternative. Each state
  docket system needs its own terms read before use (`us.state.puc_dockets`, docs/13 §6). Which states
  first?
- **C-12 Browser egress for 403 sites.** SRP's robots.txt says `Allow: /` but its pages answer 403 to an
  identified client. May a browser worker read such a site, or is the 403 the operative answer? My
  recommendation: the 403 governs, and a human reads the terms first.

## 8. First tranche and what the connector needs

### 8.1 What can be built now

Only 6 issuers are cleared and have a listing to read; the seventh, ODEC, has none. That falls short of
the 15 to 20 the brief asked for, and it would be wrong to pad it. The buildable set is two tiers.

**Tranche 1: cleared, automated, published as derived fields** (ranked by developer value):

| Rank | Issuer | Value | Listing | Why it ranks |
|---|---|---|---|---|
| 1 | Public Service Company of Oklahoma (AEP) | high | psoklahoma.com/business/b2b/energy-rfps/ | 2026 all-source RFP up to 4,000 MW SPP accredited capacity |
| 2 | Appalachian Power (AEP) | high | appalachianpower.com/business/b2b/energy-rfps/ | Annual VCEA-driven RFPs; 1,500 MW CC, 800 MW renewables PSA, storage PSA/CPA in 2026 |
| 3 | SWEPCO (AEP) | high | swepco.com/business/b2b/energy-rfps/ | 2026 all-source RFP; joint SPP capacity RFPs |
| 4 | Connecticut DEEP | high | portal.ct.gov DEEP news releases | Annual zero-carbon and multi-state procurements |
| 5 | New Jersey BPU | high | nj.gov/bpu public notices and orders | Storage (GSESP) and offshore wind tranches |
| 6 | Indiana Michigan Power (AEP) | medium | indianamichiganpower.com/business/b2b/energy-rfps/ | Load-growth RFPs up to 4,000 MW (2024) |

The four AEP rows are publishable only while `PLATFORM_POSTURE=noncommercial` (docs/26), which is the
deployed posture.

**Tranche 1b: link-out detectors, built now, published when C-2 and a link-out surface exist.** Automated
reading is not barred and robots allows the path: PacifiCorp, Portland General Electric, APS, PNM, El Paso
Electric, Hawaiian Electric, SCE (all high), Austin Energy (high), SDG&E (medium). With tranche 1 that is
15 issuers under automation. Until publication is allowed, the operator sees their solicitations as an
internal queue.

### 8.2 Connector requirements, by page format

| Format (registry `format`) | Issuers in tranches 1 and 1b | What the connector does |
|---|---|---|
| `html_list_with_year_tabs`, `html_list_plus_per_rfp_pages` (one AEP page template) | APCo, PSO, SWEPCO, I&M | One parser, four host configs. On APCo's page (the one fetched for this review) items are server-rendered: title, "Proposals Due <date>", "Up to N MW of ..." and contract type (PSA, PPA, CPA, REC) are in the text. Key items by title and year tab; follow "Related information" per-RFP links on the same host. The listing needs no login. Confirm the other three hosts render the same way before reusing the parser. |
| `news_post_plus_lotus_notes_filing` | CT DEEP | Read the DEEP news-release index on portal.ct.gov and keep releases naming a request for proposals. Take title, release date and due date from the release text. RFP documents sit on `www.dpuc.state.ct.us`, which robots disallows: store the link, never fetch it. |
| `pdf_orders_and_notices` | NJ BPU | Read the BPU newsroom and public-notice index (`www.nj.gov/bpu/newsroom/`), select solicitation notices, extract title and dates from the PDF text in memory, keep no bytes. The NJ grant has an exception "unless otherwise stated on particular material", so check each PDF for a restriction notice and skip any that has one. |
| `html_list_plus_per_rfp_pages`, `html_index_plus_per_rfp_pages` | PacifiCorp, El Paso Electric, Hawaiian Electric, SCE (1b) | Diff the list by item URL. Store title, first-seen date and any date in the item line. Open per-RFP pages only to read their title and dates, not their documents. |
| `html_single_page_with_pdfs`, `html_single_page_with_pdf`, `html_single_page` | PGE, APS, PNM (1b) | Hash the solicitation block. On change, record title and dates; never fetch the PDFs. |
| `html_list_with_year_tabs`, `html_per_rfp_pages_with_year_tabs` | Austin Energy, SDG&E (1b) | Check whether the year tabs are server-rendered before building. If a tab needs JavaScript, the row stays manual. |

What every issuer connector needs, from docs/13 §3.3 and the registry:

- `pipeline/connectors/http.py`'s polite session: robots honoured per request, identified UA, no challenge
  bypass, circuit-break on 403, no retry of 404. Rate 0.5 rps or slower, and honour a declared
  `Crawl-delay` (10 s on several hosts; see each row's `robots`). Use conditional GETs.
- Poll weekly, and daily while a window the connector has seen is open. Solicitations change on a cadence
  of months, so polling more often adds load and no information.
- A terms-drift check. Store a hash of each row's `terms_url` page and re-read it monthly. If the hash
  changes, pause that issuer (`ingest_only`, docs/13 §6.3 QA-1) until a reviewer re-reads it. Most of
  these pages say they may change "at any time without notice".
- Read only the registry's `automated_reading: allowed` rows. Fail closed on any other value, including a
  missing key.
- Provenance on every record (`source_id` = the row's `id`, `source_url`, `retrieved_at`, `licence`), and
  the row's `attribution` credit line rendered verbatim.
- Never fetch bid portals and never store solicitation documents (docs/10 US-302 AC1: title and link
  only).

**Note, headings on the AEP template (lane rfp-view, 2026-10-23).** An AEP listing item is a bold title
with a description, a page of its own, or both (`pipeline/connectors/us_rfp/extract.py::aep_items`). A
bold line that only names a year ("2024 RFPs", "2025-26 Requests for Proposals") and has no page of its
own, and any bold line with neither a description nor a page ("Current RFPs", a bold "Archive" line), is a
heading. It is left out before classification and counted in the snapshot's `meta.left_out` under
`heading.year` or `heading.section` (`aep_heading_rule`); a bold "Archive" line also starts the archive
section, as an `Archive` heading does. Read as an item, a year heading with a sentence under it took that
sentence's resource words and loaded as an RFP with no deadline, so status unknown. None of the four pages
recorded on 2026-10-21 has such a heading, so the tests insert headings into the recorded PSO and APCo
pages (`test_connector.py`). I&M's "2024 RFPs", the one unknown record in the 2026-10-23 store, is not a
heading: it has its own description ("I&M has issued Requests for Proposals ("2024 RFPs") which seeks to
secure up to 4,000 MW of generation resources") and its own page, which names four RFPs, so it stays a
record. Its status stays unknown because neither AEP page states a deadline. The schedule is on Charles
River Associates' RFP site (https://imallsourcerfp.com/), a host the connector does not read. Showing it
as closed would take either that host cleared in this registry or an admin override of its status
(`PATCH /admin/v1/opportunities/{public_id}`, which records the override); the connector does not guess
(`status_map.yaml`: "without one the state is unknown, never guessed").

## 9. Unlock plan: where the next issuers come from

| Lever | Rows it can move | First targets (by developer value) |
|---|---|---|
| Counsel C-3 on silent public bodies, plus a one-line confirmation email to each agency | up to 12 manual → cleared | SCPPA, Illinois IPA, CPA, Georgia PSC (Georgia Power), Maryland PSC, Santee Cooper |
| Counsel C-1, or written permission | 12 link-out → cleared | PacifiCorp, PGE, APS, PNM, EPE, Hawaiian Electric, SCE, Austin Energy, SDG&E |
| Written permission | 6 manual (clause) and 7 excluded | PSE, PG&E, Dominion, Consumers Energy, Idaho Power, DTE, LIPA, Tri-State, TEP/UNS |
| A human reads the terms in a browser and saves a PDF (docs/13 §1.7 pattern) | 13 blocked and 3 not located | **Entergy first** (top issuer; its terms host refuses this client), then Duke, NYSERDA, SRP, SDCP, MCE, NV Energy, Xcel, TVA, Maine PUC, ACES (4 co-ops) |
| Regulator dockets, each read as its own source | excluded issuers' solicitations | OPUC (PGE, PacifiCorp, Idaho Power), MPSC (DTE, Consumers), IURC (CenterPoint), CO PUC (Tri-State), ACC (TEP/UNS) |

**Permission request, outline** (drafted by an agent, sent by a human; CLAUDE.md):

1. Who we are: Inframation, a free noncommercial platform that lists energy solicitations for developers.
2. What we would do: read the RFP listing page weekly with a named bot (UA and contact URL given). Publish
   the solicitation's title, dates, size, technology and a link to the issuer's page, credited to the
   issuer. Never copy documents. Never touch the bid portal.
3. What we ask: written permission for that use, now and if the platform later charges, or a narrower yes.
4. A named contact and the address for a withdrawal request.

## 10. Wiring notes for the coordinator (nothing here is applied)

- **US-303 AC1 cannot hold as written.** It says each curated issuer carries "`licence: attribution`".
  The terms support `attribution` or better for 3 issuers, `noncommercial` for 4, and nothing for the
  rest. Each row's licence should come from `data/vendored/rfp_issuers/issuers.yaml` (`manifest`).
- **Order of edits.** `scripts/check_manifest_licences.py` lets the manifest be no more permissive than
  docs/13 §6, so each cleared issuer needs a §6 row before its manifest entry. Proposed rows, in §6's
  vocabulary:

| `source_id` | Class | Publication rule | Evidence | Confidence |
|---|---|---|---|---|
| `us.rfp.appalachian_power` | noncommercial | derived-only; credit "Source: Appalachian Power (AEP), Energy RFPs" | docs/28 §5; aep.com/terms quoted in the registry | mod-high |
| `us.rfp.pso` | noncommercial | derived-only; credit "Source: Public Service Company of Oklahoma (AEP), Energy RFPs" | as above | mod-high |
| `us.rfp.swepco` | noncommercial | derived-only; credit "Source: SWEPCO (AEP), Energy RFPs" | as above | mod-high |
| `us.rfp.indiana_michigan_power` | noncommercial | derived-only; credit "Source: Indiana Michigan Power (AEP), Energy RFPs" | as above | mod-high |
| `us.rfp.ct_deep` | attribution-restricted | derived-only; credit "Source: Connecticut Department of Energy and Environmental Protection"; documents on www.dpuc.state.ct.us are robots-disallowed: link only | State of Connecticut Disclaimer, permissive by omission | mod |
| `us.rfp.nj_bpu` | permissive | derived-only; credit "Source: New Jersey Board of Public Utilities" | nj.gov Legal Statement, "anyone may view, copy or distribute State information" | high |
| `us.rfp.odec` | attribution-restricted | derived-only (no listing to read) | odec.com/terms restricts trademarks only | low-mod |

- Every other row is `restricted` or `unknown`, `publication: none`. The umbrella `us.utility_rfps`
  stays `unknown` / `none`; per-issuer ids replace it.
- `CLAUDE.md`'s Energy Adepto prohibition is unaffected. So is the market-research lane's exclusion of
  nationgraph.com and starbridge.ai.

## 11. Limits and re-checks

- No terms could be retrieved for 14 issuers: 13 blocked, plus Virginia SCC, where robots.txt barred the
  read. For 14 more no website-use terms were found where I looked. For 11 of those (public bodies,
  CCAs, the Massachusetts site) the absence is itself the finding. For Avista, NV Energy and Xcel a terms
  page probably exists behind a client-rendered footer. All 28 verdicts are conservative placeholders,
  not findings.
- The statutes in `public_records_law` are named from general knowledge and were not retrieved.
- The verdicts are dated 2026-10-21. Re-read every `terms_url` at least quarterly and before any verdict
  changes. The drift check in §8.2 is the mechanism.
- Developer value and cadence come from the market-research lane's list of the same date. This document
  does not re-assess them.
