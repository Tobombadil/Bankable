# 06 — Reuse terms: Global Energy Monitor trackers, and statewide parcels for VA, OH, NC, WI and FL

Two readings the 2026-10-23 decisions log left open, done before anything is built (CLAUDE.md: respect per-source
terms; read and record them before publishing):

1. **Global Energy Monitor (GEM).** The "global figures" row of 2026-10-23 says GEM's trackers "state CC BY 4.0
   'unless otherwise noted' with a commercial-exemption process; terms are read and recorded before any use". This
   is that reading, for the Global Integrated Power Tracker (GIPT) and the eight power trackers it is built from.
2. **Statewide parcels** for the next Site finder states after Texas (`docs/25-siting-parcels.md` §2 and §5.2):
   Virginia, Ohio, North Carolina, Wisconsin and Florida, ranked by readiness. Read against the 2026-10-23 rule
   that Inframation stores no owner identity (owner names are read in memory only to join same-owner land, then
   dropped; care-of, mailing, situs and legal-description fields are never read). Builders read an explicit
   allowlist of columns per state (§2.1).

**I am not a lawyer and this is not legal advice.** §3 lists what goes to counsel.

**Numbering.** Drafted as `docs/27-terms-gem-and-parcels.md` and renumbered in the review round: `docs/27` is the
powered-land document (`docs/27-section-R1.md`, which code and the plan cite as `docs/27` section R1). The
precedent is `docs/05-international-queues.md`, a terms reading done before a build and numbered with the
feasibility notes.

**Reading convention** (as in `docs/13-legal-data-rights.md` and `docs/25`). Blockquotes are verbatim source text.
Text marked *Inference* is my reading and carries a stated confidence. Where a page could not be read I say so.

**Retrieval.** Everything below was retrieved on **2026-10-23** (first reading and review round) through the
session proxy with the user-agent `Mozilla/5.0 (research; terms review)`: tracker pages, licence, policy and contact
pages, the GEM download component's JavaScript, ArcGIS catalogue records (`/sharing/rest/content/items/<id>?f=json`),
service descriptions (`?f=json`, which give field names and types and no record), one hub's site configuration,
statutes and one court opinion. Captures are in the session scratchpad, not committed.

**What was not done.** No GEM download form was submitted, no email address was given to anyone, and no licence
box was ticked. No parcel record was downloaded, and no owner, address or legal-description field was requested
from any service. Disclosed: to measure coverage, each state's parcel service answered aggregate statistics queries
(a count of parcels per county and, for Virginia and North Carolina, the latest update date per county) and
`returnCountOnly` queries. Two single-row Ohio requests for `County` and `CurrentTo` were refused by the service and
returned nothing. In the review round one Ohio `sum` statistic on `LandArea`, asked to learn its unit, was refused
too. Field lists come from each service's schema, which carries no record.

---

## 0. The answer

**GEM (confidence moderate-high on the reading; the commercial question needs GEM's written answer).**

- Today no tracker page says "unless otherwise noted", and there is no commercial-exemption page.
  `https://globalenergymonitor.org/commercial-exemption/` answers **301** to the CC BY 4.0 licence text, and the text
  served there is identical to the licence page. The "unless otherwise noted … commercial exemption" sentences
  survive only in search-engine snippets of the retired `/download-data/` pages, which all redirect now (§1.2).
- Of the nine pages, only the **Solar** tracker states CC BY 4.0 in words, and conditionally: "When the data set is
  being shared or adapted and our Creative Commons CC BY 4.0 International license applies … Necessary attribution
  elements are included in our data download files." It carves out its TransitionZero rows as **CC BY-NC 4.0**. The
  other eight, the GIPT included, give only a recommended citation (§1.1).
- The download form names the licence for every one of the nine. It requires the box "I have reviewed and understand
  Global Energy Monitor's Creative Commons Public License" and records "Creative Commons Attribution 4.0
  International (CC BY 4.0)" against each submission (§1.3).
- **What Inframation may do, on that evidence** (§1.6). One gate comes first: **nothing from GEM is loaded or shown
  until the owner has downloaded a file and its in-file notice is recorded in the register (§1.7).** The page's
  licence sentence is conditional and points at the file, so the file settles what applies. After that gate:
  - derived figures (counts, GW by country, technology and status) on public pages, with the release citation, a
    CC BY 4.0 link and a statement of changes (CC BY 4.0 §3(a)(1)(B));
  - unit rows, map points and API rows on the same conditions, if the notice confirms CC BY 4.0 covers the rows;
  - paid tiers after GEM answers in writing whether its old "commercial exemption" still means anything (§1.8).
- **GEM's Google-Sheets summary tables are not a licensed source.** They need no form and carry a credit but no
  licence statement. GEM says "all available data points were included in the status aggregates presented in the
  summary sheets", so their solar and all-technology totals include TransitionZero capacity that cannot be taken
  out. They are not used, on the front page or anywhere else, until GEM confirms in writing (§1.1, §1.8 question 5).

  CC BY 4.0 itself has no non-commercial term and is irrevocable for a copy already received. Three conditions apply
  in every case. **TransitionZero rows (CC BY-NC 4.0) are excluded from every use**: public pages, paid tiers, the
  internal tier and the owner's own deals. Where they cannot be identified, GIPT solar is left out, never shown as
  derived figures (§1.5). gem.wiki prose is never ingested. Our API terms must not restrict what CC BY lets recipients
  do with GEM's own material (§1.6).

**Parcels, ranked by readiness (detail §2.2–2.7):**

| Rank | State | Why it ranks here | Confidence |
|---|---|---|---|
| 1 | **North Carolina** | "Free and unrestricted use policy"; written release agreements "are not required and will not be issued". All 100 counties (5,938,900 parcels). Statewide file geodatabase and GeoPackage each quarter. Owner name (memory only), use code and value present; a subsurface-owner name column is never read | high |
| 2 | **Wisconsin** | "This data free for public consumption", which is permissive but silent on commercial use. All 72 counties (3,574,646 parcels). Full attributes. Annual (V12 June 2026, V13 June 2027). Kenosha withholds every owner name. The download page's own terms are unread | moderate |
| 3 | **Florida** | Disclaimer only, no licence. Florida's public-records law and *Microdecisions v. Skinner* (a property appraiser may not license public-record GIS maps for commercial use) favour reuse. But the state GIO had the University of Florida's GeoPlan Center (UF) remove every parcel layer it hosted, "until … guidance on what may be permissible". 10,950,670 parcels, all 67 counties | moderate |
| 4 | **Ohio** | "Available for public use … for informational purposes only". The public view has **no owner name** (it is hidden; the mailing address is shown, and our rule never reads it), so acreage (from geometry) and land-use filters work and same-owner holdings do not. No prepared download | moderate on terms; low on usefulness |
| 5 | **Virginia** | The statewide layer is geometry plus locality and parcel id. No licence, only disclaimers. The current Appropriation Act (2026, Item 403 B) says distribution of VBMP data "for commercial or private use … shall require payment of a license fee to be determined by VGIN"; whether parcels are covered is unstated. Ask VGIN in writing first | moderate that a written answer is required |

Every parcel builder reads the state's allowlist of columns (§2.1), never "every column except" a list.

This replaces `docs/25` §5.2's order (Virginia, Ohio, North Carolina), which ranked by data-centre market. Readiness
is a different question: Virginia is the largest market and the least ready.

---

## 1. Global Energy Monitor

What `docs/13` already settles and is not re-derived here: the CC BY 4.0 licence text and its Section 3 attribution
elements (§2.2, §2.18); the TransitionZero carve-out on the Solar tracker (§2.2); the gem.wiki
CC BY-NC-SA licence (§2.2); the Ownership Tracker's separate, stricter reading (§2.18). This section reads the nine
power trackers as they stand on 2026-10-23.

### 1.1 What each tracker page says today

Every page renders its download block from one custom element, `<gem-download-form … slugs="…">`, whose
`<subtitle>` is free text written per page. None of the nine sets the element's `license-url` or `license-text`
attribute, so the form's defaults apply to all nine (§1.3). Every page footer reads "© 2026 Global Energy Monitor
All Rights Reserved".

| Tracker (URL, HTTP 200) | Licence prose on the page | Recommended citation (verbatim) | Other |
|---|---|---|---|
| Integrated Power, https://globalenergymonitor.org/projects/global-integrated-power-tracker | **none** | "Global Integrated Power Tracker, Global Energy Monitor, September 2026 release." | built from the eight below (quoted after this table) |
| Solar, https://globalenergymonitor.org/projects/global-solar-power-tracker | **CC BY 4.0, conditional, with a CC BY-NC 4.0 carve-out** (quoted below) | "Global Solar Power Tracker, Global Energy Monitor, February 2026 release." | |
| Wind, https://globalenergymonitor.org/projects/global-wind-power-tracker | none | "Global Wind Power Tracker, Global Energy Monitor, February 2026 release." | |
| Coal plants, https://globalenergymonitor.org/projects/global-coal-plant-tracker | none | "Global Coal Plant Tracker, Global Energy Monitor, July 2026 release." | offers an ownership-chain supplement (`energy-ownership-tracker`, read in `docs/13` §2.18) |
| Oil and gas plants, https://globalenergymonitor.org/projects/global-oil-gas-plant-tracker | none | "Global Oil and Gas Plant Tracker, Global Energy Monitor, August 2026 release." | the subtitle also says "You will receive an .xls file."; ownership supplement offered |
| Hydropower, https://globalenergymonitor.org/projects/global-hydropower-tracker | none | "Global Hydropower Tracker, Global Energy Monitor, March 2026 release." | |
| Nuclear, https://globalenergymonitor.org/projects/global-nuclear-power-tracker | none | "Global Nuclear Power Tracker, Global Energy Monitor, August 2026 release." | |
| Bioenergy, https://globalenergymonitor.org/projects/global-bioenergy-power-tracker | none | "Global Bioenergy Power Tracker, Global Energy Monitor, September 2026 release." | ownership supplement offered |
| Geothermal, https://globalenergymonitor.org/projects/global-geothermal-power-tracker | none | "Global Geothermal Power Tracker, Global Energy Monitor, March 2026 release." | |

The Solar subtitle, verbatim:

> The recommended citation is "Global Solar Power Tracker, Global Energy Monitor, February 2026 release."
> When the data set is being shared or adapted and our Creative Commons CC BY 4.0 International license applies,
> please read the license for attribution requirements. Necessary attribution elements are included in our data
> download files.
> Please note that records with a "TZ ID" in the Other IDs (unit/phase) column were partially or fully sourced from
> the TransitionZero, Solar Asset Mapper, August 2025. Copyright © TransitionZero, Solar Asset Mapper, August 2025.
> Distributed under a Creative Commons Attribution-Non-Commercial 4.0 International License (CC BY-NC 4.0).

*Inference, moderate-high:* the CC BY sentence is **conditional**. It does not say the licence applies; it says what
to do "when" it applies, and that the "necessary attribution elements are included in our data download files".
That echoes the retired "unless otherwise noted" (§1.2) and puts the operative statement in the file. It supports
gating every use on the in-file notice, derived figures included (§1.6, §1.7).

The GIPT's own page, on what it contains:

> The Global Integrated Power Tracker is built on Global Energy Monitor's eight power sector trackers: the Global
> Coal Plant Tracker, Global Oil and Gas Plant Tracker, Global Solar Power Tracker, Global Wind Power Tracker,
> Global Hydropower Tracker, Global Geothermal Power Tracker, Global Bioenergy Power Tracker, Global Nuclear Power
> Tracker.

> The GIPT dataset is updated on a rolling basis after each new release of its underlying power trackers.

The Solar page, on where the TransitionZero rows come in:

> In the absence of exhaustive datasets, records from TransitionZero's Solar Asset Mapper are sourced to improve the
> coverage after being de-duplicated from the existing projects in the dataset.

*Inference, moderate-high:* the Solar tracker's TransitionZero rows flow into the GIPT, but the GIPT page carries no
TransitionZero notice. A row-level CC BY-NC carve-out can therefore arrive through the GIPT with no warning on its
page. Whether the GIPT file keeps the `TZ ID` marker is unknown until a file is read. This is question 2 of the
email (§1.8), and §1.5 says what happens meanwhile.

**GEM's published summary tables.** The GIPT page links eight "Summary tables" hosted as Google Sheets, and each
tracker page links its own. One was read as CSV
(https://docs.google.com/spreadsheets/d/1A-qKwfrjelR8v7zJhZgEFqeYD0Uedqj-zn3iwHpOY0c/export?format=csv&gid=845422713,
1,226 bytes, the contents tab only; re-read in the review round, unchanged). It carries a credit and no licence
statement:

> Power capacity by technology and status, grouped by subnational unit (MW)
> Global Integrated Power Tracker, Global Energy Monitor
> Compiled September 2026, see details in Contents tab for constituent Power Tracker release dates

It also lists each tracker's latest release and threshold: coal July 2026 (≥30 MW), oil and gas August 2026
(≥50 MW; ≥20 MW in the EU and UK), solar February 2026 (≥1 MW operating; ≥20 MW other statuses), wind February 2026
(≥10 MW), nuclear August 2026 (no threshold), hydropower March 2026 (≥30 MW, ≥10 MW where data available), bioenergy
September 2026 (≥20 MW), geothermal March 2026 (≥1 MW).

The Solar page, on what the summary sheets contain:

> The summary sheets include all data for utility-scale solar farms exceeding 1 MW, irrespective of capacity. The
> operating solar farm data is comprehensive down to 1 MW. For other status categories, comprehensive data is
> available for installations of 20 MW and above. Please note that all available data points were included in the
> status aggregates presented in the summary sheets.

**What the summary tables' silence means for use: they are not a licensed source.**

- *What the text says.* The sheet states no licence. The CC BY 4.0 string the download form records attaches to a
  form submission (§1.3), and a sheet is read without one. The pages that link the sheets end "© 2026 Global Energy
  Monitor All Rights Reserved".
- *Inference, moderate-high.*
  1. Labelling a figure from a sheet "CC BY 4.0" would state a licence GEM has not stated for that material. Silence
     is not a grant, and no register row could cite one.
  2. The sheets aggregate "all available data points", and TransitionZero rows are among them. So any solar or
     all-technology total in a sheet includes CC BY-NC capacity that cannot be subtracted, because no `TZ ID`
     survives aggregation. Shown, it would carry neither the "Copyright © TransitionZero, Solar Asset Mapper,
     August 2025" credit nor the CC BY-NC notice the Solar page requires. The site serves the owner's own deals, and
     Site finder Pro is the first paid product (2026-10-23 rows), so the material would reach commercial use against
     CLAUDE.md ("`noncommercial` rows never flow to any commercial use").
  3. A single total is a fact, and citing it in prose with GEM's credit carries little copyright risk. The problem is
     the licence label and the TransitionZero share, not copyright in a number.
- **Use: none, on the front page or anywhere else, until GEM confirms in writing which licence covers the sheets and
  how TransitionZero capacity in them is credited (§1.8 question 5).** No register row is proposed for them; figures
  come from the downloaded file (§1.7).

**Third-party material named on the pages.** Seven of the nine pages carry the sentence "Global Energy Monitor
researchers perform data validation by comparing [GEM’s / our] dataset against proprietary and public data such as
…". The sets named, verbatim per page:

| Page | Comparison sets named |
|---|---|
| Integrated Power, Geothermal | "S&P Global’s World Electric Power Plant Database (WEPP) and the World Resource Institute’s Global Power Plant Database" |
| Solar | "S&P Global’s World Energy Power Plant database, the World Resource Institute’s Global Power Plant Database, Wiki-Solar , CSP Guru" |
| Wind | "S&P Global’s World Energy Power Plant database and the World Resource Institute’s Global Power Plant Database" |
| Oil and gas | "Platts World Energy Power Plant database and the World Resource Institute’s Global Power Plant Database" |
| Hydropower; Nuclear | "the World Resource Institute’s Global Power Plant Database" |

Hydropower and Nuclear name no proprietary source, only "proprietary and public data such as the World Resource
Institute’s Global Power Plant Database". The Bioenergy page has no validation sentence. The coal page describes
its research process instead:

> Preliminary lists of plants in each country were gathered from public and private data sources including Global
> Energy Observatory, CARMA, BankTrack's "Dirty Deals" list, Wikipedia, Enipedia, WRI's "Global Coal Risk Assessment"
> report (2012), Platts World Energy Power Plant database, … as well as various company and government sources. The
> data was then vetted against additional sources of information, listed below.

*Inference, moderate:* comparing is not copying. CC BY 4.0 covers only rights GEM "has authority to license"
(licence §1). If a coal-plant row still carried an identifier or value copied from the Platts database, GEM could not
license it. Nothing on the pages says one does. Unit facts (capacity, location, status) are facts, not expression.
Low risk; note it, and do not ingest any column named for a proprietary source (same mechanism as the S&P Capital IQ
drop in `docs/13` §2.18).

### 1.2 "Unless otherwise noted" and the commercial-exemption page

**What the decisions log row quotes is not on GEM's site today.** None of the nine pages above contains "unless
otherwise noted", "exemption" or, apart from Solar, "Creative Commons". Nor do the sixteen other tracker pages read
(methane, gas infrastructure, ownership, coal mines, extraction, steel, cement, Latin America, energy transition, oil
infrastructure, coal terminals, iron ore, chemicals, private equity, gas finance, coal finance). Two more, the Asia
and Europe gas trackers, answered 403 and were not read. The hub page https://globalenergymonitor.org/download-data
says only "Open-access data on global energy infrastructure, resources, and uses, available for public download."

**Where the sentences come from.** A web search on 2026-10-23 returned these as snippets of the retired per-tracker
`/download-data/` pages, for example https://globalenergymonitor.org/projects/global-integrated-power-tracker/download-data/:

> All Global Energy Monitor tracker data are freely available under a Creative Commons Attribution 4.0 International
> Public License unless otherwise noted.

> If you need to request a commercial exemption to Global Energy Monitor's Creative Commons Public license, please
> visit this page.

These are **search-index text, not a first-hand read.** Every `/download-data/` URL now answers 301 to its project
page (re-checked today for the GIPT and Asia gas tracker). The Internet Archive holds a capture of the GIPT page
dated 2025-12-27 (`archive.org/wayback/available` says so), but `web.archive.org` reset the connection over HTTPS and
answered 403 over HTTP from this sandbox, and the fetch tool refuses the host.

**The exemption page now.** `https://globalenergymonitor.org/commercial-exemption/` (and the same path without the
slash) answers **301 → https://globalenergymonitor.org/creative-commons-license**, title "Creative Commons License |
Global Energy Monitor". The text served is identical to the licence page (the extracted text of both responses
differs by nothing). It holds the CC BY 4.0 legal code, which is not a request form or a price, and nothing on it
mentions an exemption.

The phrase "unless otherwise noted" does survive on GEM's wiki footer, about the wiki
(https://www.gem.wiki/Global_Energy_Monitor:Copyrights):

> Content is available under Creative Commons Attribution-NonCommercial-ShareAlike unless otherwise noted.

*Inference.*
1. A "commercial exemption" to CC BY 4.0 is a contradiction in terms: CC BY 4.0 has no non-commercial condition to be
   exempted from. Two readings fit the old wording. (a) It was carried over from a time when some trackers or rows
   were CC BY-NC (the TransitionZero rows still are), and an exemption was a separate commercial licence for those.
   (b) GEM regards commercial use as something it may charge for whatever the licence says. Either way, GEM has
   since redirected the page to plain CC BY 4.0. **Confidence: moderate.**
2. It does not change what a recipient may do with a copy received under CC BY 4.0. The grant is "irrevocable", and
   the licence says the Licensor "may also offer the Licensed Material under separate terms or conditions or stop
   distributing the Licensed Material at any time; however, doing so will not terminate this Public License". **High.**
3. It does matter commercially and reputationally. A paid product built on a nonprofit's data, against wording that
   once invited a commercial exemption request, should have GEM's written answer first. That is cheap to get and is
   §1.8's question 1. **Recommendation, not a legal requirement.**

### 1.3 The terms shown with the download form

All nine pages load `https://api.globalenergymonitor.org/static/gem-download-form.bundle.js` (HTTP 200, 58,733
bytes, sha256 `e5ecbeab1f6e7509556a67145010aeb5a9167b28362b22fc7db72d797c905e5b`; the same size `docs/13` §2.18
recorded on 2026-09-20). It renders, verbatim:

> `Please review Global Energy Monitor's <a href="${…this._licenseUrl}" …>Creative Commons Public License</a>`

> `<input type="checkbox" name="license" required />` `I have reviewed and understand Global Energy Monitor's Creative Commons Public License*`

> `get _licenseUrl(){return this.getAttribute("license-url")||"/creative-commons-license"}`

> `_licenseText(){return this._hasDownloadSurface?this.getAttribute("license-text")||"Creative Commons Attribution 4.0 International (CC BY 4.0) — "+this._licenseUrl:null}`

The submission sends `license_text` (that default string), `email_optin`, `useragent` and `page_url` with the
fields below to `https://auxunjnrktkmeqyoyngm.supabase.co/rest/v1/rpc/mint_submission`. A presigned file URL comes
back. The form fields, verbatim labels (* = required):

| Field | Label | Notes |
|---|---|---|
| name | "Full Name*" | |
| email | "Email Address*" | |
| organization | "Organization*" | |
| sector | "Sector*" | one of "Academic / Research", "Civil Society / NGO", "Financial / Investor", "Government", "Industry", "Journalism / Media", "Other" |
| country | "Country" | optional |
| use | "How do you plan to use the data? (Please be specific.)*" | at least 100 characters: "Please describe how you plan to use the data (100+ characters)." |
| license | "I have reviewed and understand Global Energy Monitor's Creative Commons Public License*" | the form refuses to submit unticked |
| email_optin | "Send me email updates on projects associated with my download choices" | optional |
| newsletters | "Subscribe to CoalWire, a weekly roundup of coal industry developments worldwide"; "Subscribe to Inside Gas, a weekly roundup of gas industry developments worldwide"; "GEM Signal" | optional |
| remember_me | "Remember me on this device (saves name, email, organization, sector, country)" | shown only where the page sets a storage key |

The submit button reads "Submit and Download".

**GEM's privacy policy**, https://globalenergymonitor.org/privacy-policy (effective 2020-12-22), on what happens to
those fields:

> We will not sell or otherwise share your personal information with third parties for marketing purposes. We will
> only share your information when: Required or permitted by law Necessary for reporting to our donors and partners
> Required by a third party service provider or contractor who we authorize to use your information to help improve
> our Services

*Inference:* the use-case text may reach GEM's donors in aggregate reporting. Write it to be true and specific about
the kinds of use, paid and internal use included (the form asks "Please be specific"), and name no deal or
counterparty.

### 1.4 The licence clauses that decide our uses

From https://globalenergymonitor.org/creative-commons-license (the CC BY 4.0 legal code; GEM's rendering numbers
subsections as "2(1)(a)" where Creative Commons' canonical text says "2(a)(1)").

Grant (Section 2):

> Subject to the terms and conditions of this Public License, the Licensor hereby grants You a worldwide,
> royalty-free, non-sublicensable, non-exclusive, irrevocable license to exercise the Licensed Rights in the Licensed
> Material to: reproduce and Share the Licensed Material, in whole or in part; and produce, reproduce, and Share
> Adapted Material.

"Share" (Section 1):

> to provide material to the public by any means or process that requires permission under the Licensed Rights, such
> as reproduction, public display, public performance, distribution, dissemination, communication, or importation,
> and to make material available to the public including in ways that members of the public may access the material
> from a place and at a time individually chosen by them.

Downstream recipients (Section 2):

> Every recipient of the Licensed Material automatically receives an offer from the Licensor to exercise the Licensed
> Rights under the terms and conditions of this Public License. No downstream restrictions . You may not offer or
> impose any additional or different terms or conditions on, or apply any Effective Technological Measures to, the
> Licensed Material if doing so restricts exercise of the Licensed Rights by any recipient of the Licensed Material.
> No endorsement . Nothing in this Public License constitutes or may be construed as permission to assert or imply
> that You are, or that Your use of the Licensed Material is, connected with, or sponsored, endorsed, or granted
> official status by, the Licensor or others designated to receive attribution …

Scope (Section 1): Licensed Rights are "limited to all Copyright and Similar Rights that apply to Your use of the
Licensed Material and that the Licensor has authority to license." Trademarks (Section 2): "Patent and trademark
rights are not licensed under this Public License."

Termination (Section 6):

> if You fail to comply with this Public License, then Your rights under this Public License terminate automatically.
> Where Your right to use the Licensed Material has terminated under Section 6(1), it reinstates: automatically as of
> the date the violation is cured, provided it is cured within 30 days of Your discovery of the violation; or upon
> express reinstatement by the Licensor.

Attribution (Section 3), already quoted in full at `docs/13` §2.18: creator, copyright notice, licence notice,
warranty-disclaimer notice and link "if it is supplied by the Licensor"; indicate modifications; link the licence;
"in any reasonable manner based on the medium, means, and context".

### 1.5 Carve-outs that apply whatever GEM says

1. **TransitionZero rows (CC BY-NC 4.0) are excluded from every use.** CC BY-NC 4.0 grants its rights "for
   NonCommercial purposes only". The owner's own deals are commercial, Site finder Pro is the first paid product
   (2026-10-23 rows), and CLAUDE.md says "`noncommercial` rows never flow to any commercial use". So TZ rows go into
   no public figure, paid tier, internal-tier view or owner's deal, not just into no published row. In order:
   1. **Marker present.** At ingest, drop every row whose `Other IDs (unit/phase)` carries a `TZ` id: in the Solar
      tracker, and in the GIPT if the marker survives there (`docs/13` §2.2 already requires it for Solar).
   2. **No marker in the GIPT file.** Join GIPT solar rows to the Solar tracker file of the same release by GEM's
      unit or phase id, and drop every GIPT row that matches a TZ-marked Solar row. Record the counts: GIPT solar
      rows, matched, dropped, and unmatched or matched more than once.
   3. **Matching not reliable** (any GIPT solar row unmatched or ambiguous, or the two releases differ): **leave GIPT
      solar out.** Either take solar from the Solar tracker file with its TZ rows dropped, credited with the Solar
      tracker's own citation, or show no solar figure, until GEM answers §1.8 question 2. No GIPT total that includes
      solar (an all-technology GW figure) is shown either.

   **Never `derived_only` as a fallback.** A derived figure still carries the TZ capacity. It would lack the
   "Copyright © TransitionZero, Solar Asset Mapper, August 2025" credit and CC BY-NC notice the Solar page requires,
   and it would make the statement of changes ("TransitionZero-sourced rows removed") untrue.
2. **gem.wiki prose** is CC BY-NC-SA: never ingested. The wiki URL column may be stored and shown as a link.
3. **No column named for a proprietary source** (an S&P, Platts, WEPP or Capital IQ id), on the same reasoning as
   `docs/13` §2.18's SPCIQ drop.
4. **GEM's name and logo** are not licensed. Use the name in the credit only, with no logo and no wording that implies
   GEM endorses Inframation.

### 1.6 What Inframation may do

Assumes CC BY 4.0 governs the file received. That is evidenced for all nine by the form's recorded licence string
and for Solar by its page. The Solar sentence is **conditional** ("When the data set is being shared or adapted and
our Creative Commons CC BY 4.0 International license applies … Necessary attribution elements are included in our
data download files"), so the in-file notice (§1.7) gates every use below, derived figures included.

| Use | Verdict | Conditions | Confidence |
|---|---|---|---|
| **Derived figures on public pages** (operating / construction / pre-construction GW and unit counts by country, region, technology, status; "since last release" deltas) | **Yes, once the owner has downloaded a file and its in-file notice is recorded** (§1.7) | credit with the release-specific citation, "CC BY 4.0" linked, and a statement of changes (CC BY 4.0 §3(a)(1)(B)); TransitionZero rows excluded (§1.5); label as GEM's pipeline, never mixed with Ember's operating figures as one measure (2026-10-23 row) | high on the reading; moderate-high until the notice is read |
| **Unit rows and map points on public pages** | **Yes, on the same gate**, if the notice confirms CC BY 4.0 covers the rows | as above | moderate-high now; high after the notice |
| **Rows in the API** (free or paid keys) | **Yes, on the same gate** | every response carries the credit and licence link (it does: `attribution_text`, `licence_url`); our API licence must not restrict what recipients may do with GEM's own material (see the note after this table) | high on the reading |
| **Paid tiers** (a commercial product showing GEM data) | **Permitted by CC BY 4.0; ask GEM first** | the retired "commercial exemption" wording (§1.2) is the reason to ask. Under today's `noncommercial` posture this does not arise until the flip (`docs/26`) | high that the licence permits; moderate that GEM agrees |
| **Internal tier and the owner's own deals** | **Yes**, for GEM's own material, once a file is downloaded | internal use is not "Share", so no attribution condition bites until data leaves the company; TransitionZero rows are excluded here too (§1.5) | high |
| **Bulk downloads offered to our users** | **Yes**, on the same gate, with the credit inside the file | same as the API | high |
| **GEM's summary tables** (Google Sheets) | **No**, until GEM confirms in writing (§1.8 question 5) | terms silent; TransitionZero capacity inside the totals (§1.1) | moderate-high |
| **Automated refresh** (a connector fetching each new release) | **Not by replaying the form** | the form collects a named person's details per download. Ask GEM for a programmatic route (question 4); until then a person downloads each release into an inbox folder, as for the Global Wind Atlas (2026-10-23 row) | n/a (practice, not licence) |

**The API terms point is concrete.** `docs/13-legal-customer-terms.md` §2.4 says: "You may not resell or republish
bulk extracts of the data as a data product of your own". For GEM's own material (and every other CC BY source:
Ember, LBNL), that sentence restricts what the licence lets a recipient do. The licence's "No downstream
restrictions" clause forbids exactly that. *Inference, moderate-high:* add to §2.4 "This does not limit what a
source's open licence lets you do with that source's own material; it applies to Inframation's additions (derived
fields, identifiers, links, scores)". §2.8 already keeps open-licence data usable after termination. Recorded in
caveats for the legal-compliance owner of `docs/13-legal-customer-terms.md`.

### 1.7 What the owner would submit (not submitted by any agent)

One submission covers the first build: **the GIPT** (all eight technologies). Add the Solar tracker only if the GIPT
file lacks the `TZ ID` marker (§1.5). Submit in a browser at the GIPT page's "Download data" block:

| Field | Suggested value |
|---|---|
| Full Name | the owner's name |
| Email Address | a role address the company controls (for example data@inframation.io), so the record is not tied to a person who leaves |
| Organization | the legal name of the owner's company, then "(Inframation, inframation.io)" |
| Sector | "Industry" (or "Financial / Investor" if that describes the company better; pick what is true) |
| Country | United States |
| How do you plan to use the data? | "Inframation (inframation.io) is an energy market-intelligence website. We plan to show summary figures from the Global Integrated Power Tracker (capacity and unit counts by country, technology and status) with GEM's citation, the CC BY 4.0 licence and a note of our changes, and may show unit records on a map and through an API, including in paid subscriptions and a paid API, and use the data in our own company's energy-project work. Records sourced from TransitionZero will be excluded." |
| Licence box | tick, after reading https://globalenergymonitor.org/creative-commons-license |
| Email updates, newsletters, remember me | leave unticked |

After download, record these for the register. Nothing is loaded, derived figures included, until they are recorded:

1. the file name, release and sha256;
2. the full text of any notice, licence or attribution sheet inside the workbook (verbatim);
3. whether an `Other IDs` column with `TZ` values exists in the GIPT file, and how many rows carry it;
4. if it does not: the §1.5 match counts against the Solar tracker file of the same release, and whether §1.5's
   third step (leave GIPT solar out) applies;
5. any column that names a proprietary source.

The coordinator then updates the row (§1.9): `publication: none` becomes `derived_only`, or `raw_ok` if the notice
confirms CC BY 4.0 for the rows.

### 1.8 Draft email to GEM (for the owner to send; never sent by an agent)

To: info@globalenergymonitor.org. GEM's contact page, https://globalenergymonitor.org/contact-us (retrieved
2026-10-23), shows the address through Cloudflare's email obfuscation; decoded, it reads as given. Each tracker page
also names a project manager for dataset questions.
Subject: Licence questions: Global Integrated Power Tracker in a data service

> Hello,
>
> I run Inframation (inframation.io), an energy market-intelligence site. We would like to use the Global Integrated
> Power Tracker to show global power-pipeline figures, credited as "Global Integrated Power Tracker, Global Energy
> Monitor, [month year] release" with a link to CC BY 4.0. Before we do, could you confirm five points?
>
> 1. Your tracker download pages used to say that data are CC BY 4.0 "unless otherwise noted" and linked a
>    "commercial exemption" request. That link now leads to the CC BY 4.0 licence. Is any exemption or separate
>    licence still needed to show GIPT data in a paid subscription or a paid API, or does CC BY 4.0 cover it?
> 2. The Solar Power Tracker marks rows sourced from TransitionZero as CC BY-NC 4.0. Do those rows appear in the
>    GIPT, and if so, how can we identify them? Are rows in any other tracker under different terms?
> 3. Is the recommended citation plus a link to the licence enough credit on a map label or an API record, or do you
>    want other wording?
> 4. Is there a programmatic way to fetch each new release (an API or a stable file URL), so we do not have to
>    resubmit the download form?
> 5. Your project pages link summary tables as Google Sheets, which carry a credit but no licence statement. May we
>    show figures from them, and under which licence? The Solar page says all available data points are included in
>    their status aggregates, so we take it that the solar totals include TransitionZero-sourced capacity. How should
>    that be credited, or can you give us totals without it?
>
> Thank you,
> [Name], [Company], for Inframation

Save the reply with its date for the register (`docs/13` §2.2 and this section).

### 1.9 Proposed register changes (the coordinator applies them; `data/sources.yaml` and `docs/13` are not this lane's files)

**A new row for the GIPT,** the source the global figures would use:

```yaml
- id: global.gem.gipt
  name: Global Energy Monitor — Global Integrated Power Tracker (unit-level power plants, eight technologies, all statuses)
  jurisdiction: global
  category: registry
  operator: Global Energy Monitor (non-profit)
  url: https://globalenergymonitor.org/projects/global-integrated-power-tracker
  access: bulk_file
  auth: 'download form (name, email, organisation, sector, use, licence acknowledgement); submitted by a person, never by an agent or a connector (docs/06 §1.7)'
  format: xlsx (zip)
  cadence: rolling, after each underlying tracker release (September 2026 release on 2026-10-23)
  license: 'No licence prose on the dataset page (retrieved 2026-10-23); the page gives only "The recommended citation is "Global Integrated Power Tracker, Global Energy Monitor, September 2026 release."" GEM''s download form requires "I have reviewed and understand Global Energy Monitor''s Creative Commons Public License" and records "Creative Commons Attribution 4.0 International (CC BY 4.0) — /creative-commons-license" (https://api.globalenergymonitor.org/static/gem-download-form.bundle.js, sha256 e5ecbeab…905e5b, retrieved 2026-10-23). The Solar tracker page''s CC BY sentence is conditional ("When … our Creative Commons CC BY 4.0 International license applies … Necessary attribution elements are included in our data download files"). https://globalenergymonitor.org/commercial-exemption/ answers 301 to the CC BY 4.0 legal code. In-file notice unread.'
  licence_url: https://creativecommons.org/licenses/by/4.0/
  reuse: attribution
  publication: none
  attribution: 'Global Integrated Power Tracker, Global Energy Monitor, September 2026 release (https://globalenergymonitor.org/projects/global-integrated-power-tracker), CC BY 4.0'
  changes_statement: "Modified by Inframation: TransitionZero-sourced rows removed; statuses and technologies mapped to Inframation's vocabulary; figures aggregated."
  tier: 2
  effort: M
  probe: {method: GET}
  verified: {date: 2026-10-23, result: ok, note: 'terms review only (docs/06 §1); no form submitted, no file downloaded'}
  notes: >-
    Nothing is loaded until the in-file notice is recorded (docs/06 §1.7); then publication becomes derived_only, or
    raw_ok if the notice confirms CC BY 4.0 for the rows. Global pipeline figures beside Ember's operating figures,
    never mixed as one measure (2026-10-23 row). TransitionZero rows (CC BY-NC 4.0) are excluded from every use,
    the internal tier and the owner's deals included: drop rows with "TZ" in Other IDs, or GIPT solar rows matched
    to the Solar tracker's TZ rows; where matching is not reliable GIPT solar is left out (solar from the Solar
    tracker with its TZ rows dropped and its own citation, or none), never derived_only (docs/06 §1.5). Never
    ingest gem.wiki prose; drop any column named for a proprietary source. GEM's Google-Sheets summary tables are
    not this source and are not used until GEM confirms their licence (docs/06 §1.8 question 5). attribution names
    the release: update it with each release, and changes_statement too if solar came from the Solar tracker. Hold
    before paid use: GEM's written answer to docs/06 §1.8 question 1 is recorded here before a paid tier or the
    commercial posture (docs/26) uses this row (a recommendation, not a licence condition). A person downloads each
    release; no connector replays the form.
```

**Amend `global.gem.trackers`.** Set `verified: {date: 2026-10-23, …}` and `publication: none` (was `raw_ok`; no
connector reads the row today, so nothing that is live changes). Append to `notes`: "2026-10-23 (docs/06 §1): of
the nine power-tracker pages only Solar states CC BY 4.0 in prose, and conditionally ('When … license applies …
Necessary attribution elements are included in our data download files'); the 'unless otherwise noted' and
'commercial exemption' wording is gone and /commercial-exemption/ redirects to the CC BY 4.0 text. Nothing from
this row is loaded until an in-file notice of the release being loaded is recorded; then raw_ok returns.
TransitionZero rows are excluded from every use, internal and the owner's deals included (docs/06 §1.5)." Matching
`docs/13` §6 rows:

| `source_id` | Class | Publication rule | Evidence | Confidence |
|---|---|---|---|---|
| `global.gem.gipt` | attribution-restricted (CC BY 4.0 named by GEM's download form; no licence prose on the page) | do not publish until the in-file notice is recorded (`docs/06` §1.7); then derived-only + credit "Global Integrated Power Tracker, Global Energy Monitor, <release> release" with the CC BY 4.0 link + statement of changes, and raw-ok if the notice confirms CC BY 4.0 for the rows; **TransitionZero (CC BY-NC) rows excluded from every use; GIPT solar left out where they cannot be identified** | `docs/06` §1.1–1.5, retrieved 2026-10-23 | mod-high |
| `global.gem.trackers` (amended) | open-attribution (CC BY 4.0) except TZ-ID rows (CC BY-NC 4.0) | **do not publish until an in-file notice of the release being loaded is recorded** (was raw-ok); then raw-ok + recommended citation; TZ rows excluded from every use | `docs/13` §2.2 (2026-09-12), `docs/06` §1.1–1.2 (2026-10-23: CC BY prose on Solar only, and conditional) | mod-high |

The proposed row is `attribution-restricted`, not `open-attribution`. That is the class `docs/13` §2.18 gives
`global.gem.ownership_tracker` for exactly the same evidence: a licence named by the download form but not stated on
the dataset page. The register's `<release>` is a placeholder the register check treats as optional; the manifest's
`attribution` names the actual release, because the loader renders it verbatim.

---

## 2. Statewide parcels: Virginia, Ohio, North Carolina, Wisconsin, Florida

### 2.1 What each layer is read against

The rule of 2026-10-23 (decisions log; `docs/25` §8.2): a parcel layer serves parcel id, acres, land-use category,
values and same-owner *holdings*. Owner names are read in memory only to group touching parcels, then dropped. No
keyed hash is kept, and care-of, mailing, situs and legal-description fields are never read. Publication is
`derived_only`; the per-parcel files stay in the private object store.

**Builders read an explicit allowlist.** Each state section below gives an allowlist in up to four parts: columns
that may be stored in the per-parcel files and served; the owner-name column read in memory only to group holdings,
then dropped; any column that may only become a yes/no; and examples of columns not read. A builder reads exactly the
first three, the way `pipeline/context/txgio_parcels.py` reads Texas ("Reads only the columns `FIELD_NAMES`
lists: never the care-of, mailing, situs or legal-description columns"). It never reads "every column except" a
list. A denylist misses fields (Florida's `OWN_STATE_` beside `OWN_STATE`, Wisconsin's `SITEADRESS_STAND`), and a
column a builder finds useful (a subsurface owner, a deed book and page) can carry the identity the rule forbids.
The "not read" examples are not the boundary; the allowlist is. Adding a column changes the allowlist and is reviewed
against this rule. The per-parcel file is therefore "the allowlisted columns", which is how the Texas description
"the source table less its owner columns" (2026-10-23 row; `us.tx.txgio.parcels` notes) should be read. Field names
come from each service's schema; a builder confirms them against the downloaded file.

For each state the questions are:

1. Do the terms allow display of derived facts in a paid product?
2. Are acreage and land use present?
3. Are owner names present, for in-memory grouping only?
4. Is there a bulk file, so we never page through a map service?
5. What is the cadence, and what is the size?

Never a private aggregator (CLAUDE.md; Regrid, LandGate, Acres, Land id and the rest are not used or recommended).

### 2.2 North Carolina: NC OneMap "NC Parcels"

**Layer.** NC Department of Information Technology, Center for Geographic Information and Analysis (CGIA), NC OneMap.
Program page https://www.nconemap.gov/pages/parcels. Catalogue item "North Carolina Parcels (Polygons)"
`1de3d7d828ce4813b838ddf055b40317` (modified 2026-09-17). Service
https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1.

**Terms.** https://www.nconemap.gov/pages/terms (the item's `licenseInfo` points there):

> The NC Center for Geographic Information and Analysis (CGIA) is charged with the development and maintenance of NC
> OneMap and, in cooperation with partner organizations, is committed to offering access to current State geospatial
> information at no charge. By sharing their content, all partner organizations understand this free and unrestricted
> use policy.

> Written release agreements to authorize use of the geospatial data, web services, and applications found on the NC
> OneMap website are not required and will not be issued.

> Any sale of this data must not violate applicable state laws or regulations.

> When citing NC OneMap in printed or online publications please use the following citation: NC OneMap (2019) . North
> Carolina Department of Information Technology, Government Data Analytics Center, Center for Geographic Information
> and Analysis. Available at www.nconemap.gov.

The proposed credit (§2.9) is that citation, with the stray space before the first full stop removed, followed by the
counties' credit from the item.

The state law that sentence most plausibly points at, N.C.G.S. § 132-10
(https://www.ncleg.gov/EnactedLegislation/Statutes/HTML/BySection/Chapter_132/GS_132-10.html):

> As a condition of furnishing an electronic copy, whether on magnetic tape, magnetic disk, compact disk, or
> photo-optical device, a county or city may require that the person obtaining the copy agree in writing that the copy
> will not be resold or otherwise used for trade or commercial purposes.

Item `accessInformation`: "Source datasets were furnished by data managers in North Carolina Counties and the Eastern
Band of Cherokee Indians." The parcels page: "For copies of geospatial data, distribution policies and procedures vary
by county."

*Inference, high:* display of derived parcel facts in a paid product is within "free and unrestricted use".
§ 132-10 lets a *county* condition a copy *it* furnishes. Inframation would take the aggregate from the state, which
attaches no condition and "will not" issue release agreements. Inframation also does not sell the data; it shows
derived facts. Residual: a county that had imposed a § 132-10 condition on its copy to the state. Nothing read
suggests one; counsel item 4.

**Fields** (service schema, https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1?f=json,
71 fields, read 2026-10-23). Owner identity sits in more columns than the owner name: `ownname` ("Owner Name"),
`ownname2` ("Full Second Owner Name"), `ownfrst`, `ownlast`, `owntype` ("Owner Type"), **`subsurfown` ("Subsurface
Owner", a 200-character string)** and `subowntype` ("Subsurface Owner Type"). `sourceref` ("Source Document
Reference", the deed or plat reference) resolves to owner names at the register of deeds. Split estates matter for
energy siting, which is exactly why a builder might reach for `subsurfown`; it would store the mineral-rights
owner's name.

**Allowlist (North Carolina):**

- *Stored and served:* `parno`, `nparno`, `altparno`, `cntyname`, `stcntyfips`, `parusecode`, `parusedesc`,
  `landval`, `parval`, `improvval`, `parvaltype`, `transfdate` (vintage), and the geometry, from which acres are
  computed.
- *Read in memory only, then dropped:* `ownname`, to group holdings.
- *Yes/no only:* `subowntype` may become a `split_estate` flag (true where a subsurface owner type is recorded); its
  value is never stored. A blank means "not recorded", not "no split estate"; how many counties fill it was not
  measured.
- *Not read (examples):* `subsurfown`: the subsurface owner's name is never read and never stored, and a split-estate
  attribute comes only from `subowntype` as above. Also `ownname2`, `ownfrst`, `ownlast`, `owntype`, `mailadd`,
  `munit`, `mcity`, `mstate`, `mzip`, `maddpref` and the other `madd*` fields, `siteadd`, `sunit`, `scity`, `sstate`,
  `szip`, `saddno` and the other `sadd*` fields, `legdecfull`, `sourceref`, `sourcedate`, `saledate`, `subdivisio`.

The parcels page notes `gisacres` was not computed by the state ("Calculating GIS acres in the NC Parcels Transformer
was not in the project requirements"), so acres come from geometry, as for Texas, and `gisacres` is not read.

**Coverage, measured.** 5,938,900 parcels in 100 county groups. The latest `transfdate` per county runs from
2025-03-25 (Hyde) and 2025-04-17 (Clay) to 2026-06-01.

**Cadence.** "The update frequency depends on each county. At a minimum we ask their parcel data is uploaded and
transformed to the state schema quarterly." Statewide snapshots are quarterly; the S3 listing of
`dit-cgia-gis-data/NCOM-data/parcels/` shows `…-mar2026`, `…-jun2026` and earlier quarterly files back to August
2022. (`docs/25` §2 said "refreshed weekly", from a DIT brief; the state's own page says quarterly at minimum.)

**Format and size** (S3 listing, 2026-10-23):

- `nc-parcels-gdb-most-recent.zip`: 2,307,182,458 bytes, 2026-08-11 (file geodatabase, polygons and centroids);
- `nc-parcels-gpkg-most-recent.zip`: 3,269,152,493 bytes, 2026-08-11 (GeoPackage);
- per-county zips (shapefiles).

The link on the parcels page (`…/nc-parcels-fgdb-most-recent.zip`) answers 404; the key is `…-gdb-…`.

**Readiness: ready.** Register class `permissive`, publication `derived_only` (privacy rule). Holdings by in-memory
owner name, as in Texas, through the allowlist above.

### 2.3 Wisconsin: Statewide Parcel Map Initiative, V12

**Layer.** Wisconsin Department of Administration, Wisconsin Land Information Program (WLIP), built by the State
Cartographer's Office (SCO). Item "Wisconsin Statewide Parcels DB" `2386813b23ea4e51a009f7d1d6b76e02` (V12.0.0,
modified 2026-06-30). Service
https://services3.arcgis.com/n6uYoouQZW75n5WI/arcgis/rest/services/Wisconsin_Statewide_Parcels_DB/FeatureServer.
Project page https://doa.wi.gov/Pages/LocalGovtsGrants/Parcel-Initiative.aspx. Download page
https://www.sco.wisc.edu/parcels/data/. From this sandbox the download page could not be read: TLS verification
failed ("self-signed certificate in certificate chain" in the review round; the fetch tool got 503 the first time,
as in `docs/25`). The V12 Final Report (https://doa.wi.gov/DIR/V12_Final_Report.pdf) names it as the release channel
and says the hosted view is public:

> The V12 database was formally released to the general public on June 23, 2026, through the DOA land information
> email listserv and the data page at www.sco.wisc.edu/parcels/data.

> This view is available for public use and can be integrated into user-created custom applications and maps.

**Terms.** Statewide item `licenseInfo`: "This data free for public consumption as of: 06/30/2026". The V12 county
items carry the longer form (for example `68fd1b7089b84bc0a1da1ded0c72e8fc`, Richland):

> This data free for public consumption as of: 06/30/2026 Access and Use Constraints The information shown on this map
> was obtained from Wisconsin's counties and cities in 2026 and thus may not be the most current, comprehensive data
> available. Source data for parcel polygons was collected between January–June of 2026. However, the tax roll year
> for most records is 2025, as the assessment cycle lags a year behind. … The next release of the statewide parcel
> layer, V13, is tentatively scheduled for June 30, 2027. No warranty, expressed or implied, is made regarding
> accuracy, completeness, or legality of the information herein. The boundaries depicted on this map do not represent
> the legal ownership boundaries of any property.

Credit line (service `copyrightText`): "Product of the Version 12 Statewide Parcel Map Database Project (V12). Created
by: The Wisconsin State Cartographer's Office, Administrated by: The Wisconsin Land Information Program (WLIP) at the
Department of Administration (DOA)".

**Terms not read: the SCO download page.** The file comes from the SCO data page, and whatever terms that page or
the download carries were never read. The only terms read are the ArcGIS items' `licenseInfo`, which is why the
proposed row's `licence_url` is the statewide item while its `url` is the SCO page. **The person who downloads the
file reads the SCO data page and any licence, readme or metadata in the download, and records them verbatim in the
register before the file is used.** If they say more than "free for public consumption", this section is re-read.

**Owner-name redactions and a judicial-privacy statute**, from the V12 Final Report:

> Under §757.07(4m), providers of public-facing land records websites shall establish a process for judicial officers
> and immediate family members of judicial officers to opt out from the display and search functions of their names
> on the provider's public-facing land records website.

> For Wisconsin's statewide parcel map, the Wisconsin Department of Administration and the Wisconsin State
> Cartographer's Office do not process name redactions other than required by law.

> Partial owner name redaction was conducted by 8 counties for V12 … An additional county—Kenosha—withheld all owner
> names, consistent with a local county board resolution.

(Dane 10.90% of records redacted; the other seven under 1%. The report's submission rules say "Shielded names
removed by counties should appear as "NOT AVAILABLE" in the OWNERNME1 field", and "Eligible judicial officers are
asked to follow the shielding request procedure described on the following page: www.sco.wisc.edu/parcels/shielding.")

*Inference.*
1. **Terms, moderate.** "Free for public consumption" plus a disclaimer is permissive and says nothing about
   commercial use or redistribution. Silence in a state's free-data statement usually means no restriction was
   intended. With no licence to point to, though, a paid product should have WLIP's or SCO's one-line confirmation
   (§2.8), and the download page's own terms are still to be read.
2. **§ 757.07(4m), moderate-high that it does not reach us.** It concerns names shown on and searchable through a
   land-records website. Inframation never displays or indexes owner names; they exist only in memory during a build.
   Statute text not read first-hand (the legislature's site reset the connection); counsel item 5.
3. **Kenosha:** no names, so no holdings there; each parcel is its own holding. "NOT AVAILABLE" must be treated as
   blank, never as one owner, or every redacted parcel in a county would join into one false holding. This is the
   same rule as Texas's confidential owners.

**Allowlist (Wisconsin)** (service schema
https://services3.arcgis.com/n6uYoouQZW75n5WI/arcgis/rest/services/Wisconsin_Statewide_Parcels_DB/FeatureServer/0?f=json,
47 fields):

- *Stored and served:* `STATEID`, `PARCELID`, `TAXPARCELID`, `CONAME`, `PARCELFIPS`, `PROPCLASS`, `AUXCLASS`,
  `GISACRES`, `DEEDACRES`, `ASSDACRES`, `CNTASSDVALUE`, `LNDVALUE`, `IMPVALUE`, `ESTFMKVALUE`, `TAXROLLYEAR`,
  `PARCELDATE`, `LOADDATE`, and the geometry.
- *Read in memory only, then dropped:* `OWNERNME1`. "NOT AVAILABLE" and blanks are no owner, never one owner.
- *Not read (examples):* `OWNERNME2`, `PSTLADRESS`, `SITEADRESS`, `SITEADRESS_STAND` (the service's standardised
  site address; the report calls it "STAND_SITEADD"), `ADDNUMPREFIX`, `ADDNUM`, `ADDNUMSUFFIX`, `PREFIX`,
  `STREETNAME`, `STREETTYPE`, `SUFFIX`, `LANDMARKNAME`, `UNITTYPE`, `UNITID`, `PLACENAME`, `ZIPCODE`, `ZIP4`, `STATE`,
  `NETPRPTA`, `GRSPRPTA`.

**Coverage, measured.** 3,574,646 parcels in 73 `CONAME` groups: the 72 counties plus one stray record coded
"MENOMONIE" (a city).

**Cadence:** annual (V12 released 2026-06-23 per the report; V13 "tentatively" 2027-06-30). **Format and size:** a
statewide download from the SCO data page; its format and size were not read here (host unreachable), so the person
who downloads it records both. The hosted service pages at 2,000 records. A person downloads the file once a year, so
the unreachable host is not a blocker.

**Readiness: nearly ready.** Proposed class `permissive` (moderate), publication `derived_only`. **Hold before paid
use**, as `us.tx.txgio.parcels` records for TxGIO: WLIP's or SCO's written confirmation (§2.8) is recorded in the row
before Site finder Pro sells Wisconsin parcels or the posture flips to `commercial` (`docs/26`).

### 2.4 Florida: Department of Revenue statewide cadastral, via the Florida GIO

**Layer.** Florida Department of Revenue, Property Tax Oversight, published by the Florida Geographic Information
Office. Item "Florida Statewide Parcels" `efa909d6b1c841d298b0a649e7f71cf2` (modified 2026-09-30). Service
https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer. File
https://publicfiles.dep.state.fl.us/otis/gis/data/Cadastral_Statewide.zip.

**Terms.** Item `licenseInfo`:

> The Department does not generate or maintain the data used to compile this statewide parcel map. The Department does
> not guarantee the accuracy, or completeness, or the compliance with public records laws of this parcel GIS data. The
> data is owned and maintained by each Florida county's property appraiser's office and was provided to the
> Department. by each respective county property appraiser's office.

Item snippet and description:

> Each county is responsible for omitting confidential records subject to redaction under Chapter 119, Florida
> Statutes.

> If you discover the inadvertent release of a confidential record exempt from disclosure pursuant to Chapter 119,
> Florida Statutes, public records laws, immediately notify the Department of Revenue at 850-717-6570 and your local
> Florida Property Appraisers' Office.

The layer's `copyrightText` reads "Alachua County Property Appraiser". It names one of 67 county appraisers on a
statewide layer; *inference:* a template leftover, not a claim over the state set.

**The state GIO's request to UF** (https://www.geoplan.ufl.edu/parcel-layers-removed-from-fgdl-per-state-request/,
posted 2025-10-08, updated 2026-04-11):

> All parcel layers (including historic versions) have been removed from FGDL per a request from the State
> Geographic Information Office (GIO). Until such time as we receive guidance on what may be permissible, we recommend
> the following: General Overview Feature Service Zip file download … Please note, this version is the authoritative
> state version and will not completely match the standardized GeoPlan parcels layer.

The three links are the DOR item, service and zip above. No reason for the request was found (search, 2026-10-23).

**Florida's public-records law on licensing appraiser GIS data.** *Microdecisions, Inc. v. Skinner*, Fla. 2d DCA,
opinion filed 2004-12-01 (https://cases.justia.com/florida/second-district-court-of-appeal/2D03-3346.pdf):

> The issue before us is whether a county property appraiser may require prospective commercial users of the records
> created in his office to first enter into a licensing agreement. We conclude that he may not.

> We hold that Skinner has no authority to assert copyright protection in the GIS maps, which are public records.

> [Chapter 119] requires State and local agencies to make their records available to the public for the cost of
> reproduction. § 119.07(1)(a). This mandate overrides a governmental agency's ability to claim a copyright in its work
> unless the legislature has expressly authorized a public records exemption.

*Inference.*
1. **Licence, moderate-high:** under Florida law neither DOR nor a county appraiser may require a commercial licence
   for this data, so commercial display of derived facts is lawful. The opinion is from one district court of appeal,
   and later statutory exemptions were not surveyed (counsel item 6).
2. **The GIO request, unresolved.** It reads as concern about third-party *copies* (UF's standardised and historic
   layers) rather than about the state's own service, which UF now points users to. One plausible driver is
   confidential records surviving in old copies; that is a guess. Ask the GIO before Florida ships. This is a
   relationship and reputation step more than a licence one.
3. **Confidential records:** DOR disclaims "compliance with public records laws". Our rule means an exempt owner's name
   is never stored or shown; their parcel's acreage and value are. DOR asks to be notified of an inadvertent
   confidential release. Inframation needs a takedown path that can drop a parcel on request, which the existing
   takedown process covers.

**Allowlist (Florida)** (service schema
https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer/0?f=json,
122 fields):

- *Stored and served:* `PARCEL_ID`, `STATE_PAR_`, `CO_NO`, `ASMNT_YR`, `DOR_UC` (state use code), `PA_UC`, `JV`
  (just value), `LND_VAL`, `LND_SQFOOT` (acres = square feet / 43,560), `PUBLIC_LND`, and the geometry.
- *Read in memory only, then dropped:* `OWN_NAME`.
- *Not read (examples).* Owner and fiduciary contacts: `OWN_ADDR1`, `OWN_ADDR2`, `OWN_CITY`, `OWN_STATE`,
  `OWN_STATE_`, `OWN_ZIPCD`, `FIDU_NAME`, `FIDU_ADDR1`, `FIDU_ADDR2`, `FIDU_CITY`, `FIDU_STATE`, `FIDU_ZIPCD`,
  `FIDU_CD`. Situs: `PHY_ADDR1`, `PHY_ADDR2`, `PHY_CITY`, `PHY_ZIPCD`. Legal: `S_LEGAL`. Deed references, which
  resolve to owner names in the county's official records: `OR_BOOK1`, `OR_PAGE1`, `CLERK_NO1`, `OR_BOOK2`,
  `OR_PAGE2`, `CLERK_NO2`. Homestead and exemption fields, which describe the owner's personal circumstances:
  `JV_HMSTD`, `AV_HMSTD`, `JV_NON_HMS`, `AV_NON_HMS`, `PREV_HMSTD`, `CONO_PRV_H`, `ASS_TRNSFR`, `ASS_DIF_TR`,
  `YR_VAL_TRN`, and the assessed and taxable values `AV_SD`, `AV_NSD`, `TV_SD`, `TV_NSD`, which net out personal
  exemptions. Special circumstances: `SPC_CIR_CD`, `SPC_CIR_YR`, `SPC_CIR_TX`. Sales (`SALE_PRC1`, `SALE_YR1` and the
  rest) are not needed by the finder; adding them is an allowlist change.

**Coverage, measured:** 10,950,670 parcels (`returnCountOnly`), "all 67 counties" per the item. **Cadence:**

> The GIS linework and related attributions for the statewide parcel map are updated annually by the Department every
> August. Minor data maintenance is also completed every May and November.

**Format and size:** zip of 2,768,353,501 bytes, `Last-Modified` 2026-10-23 (HEAD; `docs/25` recorded 2,768,059,632
bytes on 2026-09-30, so the file is regenerated between releases). The hosted layer's item size is 29,276,921,856
bytes.

**Readiness: ready on the law; wait on the GIO's answer.** Proposed class `unknown`, publication `none`, until the
GIO replies. Then `permissive`, `derived_only`.

### 2.5 Ohio: OGRIP "Ohio Statewide Parcels"

**Layer.** Ohio Geographically Referenced Information Program (OGRIP), Department of Administrative Services. Item
"Ohio Statewide Parcels Public View" `26ab5fad8d5d4258a7492a14de83bc0e` (modified 2025-09-12; snippet "Updated Jan 21,
2024"). Service
https://services2.arcgis.com/MlJ0G8iWUyC7jAmu/arcgis/rest/services/OhioStatewidePacels_full_view/FeatureServer (layer
last edited 2026-09-23). Hub https://ohioparcels-geohio.hub.arcgis.com.

**Terms.** Item `licenseInfo` (the same text on OGRIP's land-use view and the older ODNR `Statewide_Parcels_2022`):

> The State of Ohio's geospatial data are available for public use at the user's own risk and provided for
> informational purposes only. The State of Ohio provides these data "as is" and does not warrant or guarantee that
> these data are complete, accurate or error free. The State of Ohio disclaims any warranties regarding these data and
> assumes no liability for the use of or reliance on these data.

The hub's layout has a template header whose "Terms of Use" link points to `#` (`<a href="#">Terms of Use</a>` in
the hub's site configuration, item `90e679445f3f4c87b7a054943567b9a0`), beside the template title "My
Organization". No terms page was found. The hub text:

> This annual parcel data collection is a statewide project that engages each of the 88 Ohio counties. Participation
> is voluntary …

> Credentialed Parcel GIS Data Access the credentialed Ohio parcel data resources.

The credentialed page was not readable without an account.

**Owner names: not in the public view.** The view's 22 fields (schema
https://services2.arcgis.com/MlJ0G8iWUyC7jAmu/arcgis/rest/services/OhioStatewidePacels_full_view/FeatureServer/0?f=json)
are `County`, `LocalParcelID`, `StateParcelID`, `StateLUC`, `SitusAddressAll`, the `Mail*` fields, `LandArea`,
`LandArea_PY`, `CurrentTo`, `CAMADataSite` and system fields. The layer's
`advancedQueryCapabilities.fullTextSearchableFields` is `['OwnerAll']`. *Inference, high:* the underlying table holds
owner names, and OGRIP chose to keep them out of the public view. The public view does expose the owner's
**mailing address**. Our rule never reads it, which supersedes `docs/25` §5.2's suggestion to group Ohio holdings by
a key over `MailAddressAll`.

**ODNR's parcel layers are not used.** ODNR's "Statewide Parcels" map layer
(https://gis.ohiodnr.gov/arcgis/rest/services/OIT_Services/odnr_landbase_v2/MapServer/4, schema read 2026-10-23)
carries `OWNER1` and `OWNER2`; `docs/25` §2 names it. It is not used for Ohio holdings or anything else, and neither
is ODNR's older `Statewide_Parcels_2022`. OGRIP, which compiles the statewide parcels, keeps owner names out of its
public view, and taking them from another agency's copy would defeat that choice. The ODNR layer also has no
licence text (`docs/25` §2) and pages at 1,000 records.

**`LandArea` has no stated unit.** Neither the service (alias "LandArea", no description) nor the item gives one,
and a `sum` statistic on it was refused, so the unit was not inferred. Acres are computed from geometry, and
`LandArea` is not read until OGRIP states its unit (§2.8).

**What that leaves:** parcel id, `StateLUC` (state land-use code), geometry (so acres) and the export date. Acreage
and land-use filters work; same-owner holdings cannot be built. Asking OGRIP for credentialed access to group by
owner name would be asking for what it deliberately withholds from the public. Not recommended.

**Allowlist (Ohio):**

- *Stored and served:* `County`, `LocalParcelID`, `StateParcelID`, `StateLUC`, `CurrentTo`, and the geometry;
  `CAMADataSite` as the link to the county's own record, if it holds one (its values were not read).
- *Read in memory only:* none; the view has no owner name.
- *Not read (examples):* `SitusAddressAll`, `MailAddressAll`, `MailNumber`, `MailStreetPrefix`, `MailStreetName`,
  `MailStreetSuffix`, `MailUnitNumber`, `MailCity`, `MailZip`, `MailState`; and `LandArea` and `LandArea_PY` until
  their unit is stated.

**Coverage, measured.** 6,313,610 parcels: 88 counties plus one record with no county. 637,109 rows (10.1%) have no
`CurrentTo` (export date). The service refused every date predicate and date statistic, so vintage by county could not
be measured.

**Cadence:** "annual parcel data collection". **Format:** no prepared statewide file is published. The public route
is the feature service (`maxRecordCount` 2,000, about 3,200 pages for the state) or the hub's on-demand export, which
was not tested. Paging a service of 6.3 million rows is the kind of load the Texas build avoided. Ask OGRIP for a file.

**Readiness: usable for acreage and land use only.** Proposed class `permissive` (moderate), publication
`derived_only`. No holdings in Ohio. **Hold before paid use:** OGRIP's answer to §2.8 question 4 is recorded in the
row before Site finder Pro sells Ohio parcels or the posture flips to `commercial`.

### 2.6 Virginia: VGIN "Virginia Parcels"

**Layer.** Virginia Geographic Information Network (VGIN), Department of Emergency Management, Virginia Base
Mapping Program (VBMP). Items "Virginia Parcels" `29627d7c051a47dc8ce71b4484531ab3` (file geodatabase, "2026 Q3
Product", modified 2026-09-25, 991,292,611 bytes) and "Virginia Parcels: Local Schema Tables"
`523d89ebf23d4d84957f9fe5b9158bd9` (373,316,934 bytes). Service
https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VA_Base_Layers/VA_Parcels/MapServer, copyright "Virginia
Geographic Information Network (VGIN)". It answered today; `docs/25` found the older `gismaps.vdem.virginia.gov`
host returning 502.

**Terms.** Item `licenseInfo`, a disclaimer:

> The Virginia Base Data layers are intended for cartographic use and spatial analysis only, and not for use as a
> legal description. Best efforts were undertaken to ensure the correctness of RCL, Address Points, Parcels,
> Administrative Boundaries, and Building Footprint data throughout the Commonwealth of Virginia, however, all
> warranties regarding the accuracy of the map data and any representation or inferences derived there from are
> hereby expressly disclaimed.

VGIN's page titled "VGIN Data Licensing" (https://vgin.vdem.virginia.gov/pages/vgin-data-license, modified
2026-10-21) holds no licence. It requires a notice:

> Pursuant to §54.1-402 subsection C of the Code of Virginia, which are quoted below, geospatial data in any form must
> be used as referenced by paragraph C. … Any person not licensed pursuant to subsection B of §54.1-404 or §54.1-406
> preparing documentation pursuant to subsection C of §54.1-402 shall note the following on such documentation: "Any
> determination of topography or contours, or any depiction of physical improvements, property lines or boundaries is
> for general information only and shall not be used for the design, modification, or construction of improvements to
> real property or for flood plain determination."

The current Appropriation Act, 2026 Special Session I, HB30 Chapter 1, Item 403 B (FY2027–FY2028)
(https://budget.lis.virginia.gov/item/2026/2/HB30/Chapter/1/403/):

> All digital orthophotography, Digital Terrain Models and ancillary data produced by the VBMP, but not including
> digital road centerline files, shall be the property of the Commonwealth of Virginia and administered by VGIN. The
> VGIN, or its counterpart, will be responsible for protecting the data through appropriate license agreements and
> establishing appropriate terms, conditions, charges and any limitations on use of the data. … VGIN in its discretion
> may release certain data by posting to the Internet. Distribution of the data for commercial or private use or to
> users outside the Commonwealth will be the sole responsibility of VGIN or its agent(s) and shall require payment of a
> license fee to be determined by VGIN.

The same wording stood in 2012 (HB1301, Item 424). The statute lets VGIN charge "at rates that reflect the fair
market value" (Va. Code § 44-146.18:6 C 3, https://law.lis.virginia.gov/vacode/title44/chapter3.2/section44-146.18:6/).

*Inference.*
1. **Ambiguous, and it matters.** Item 403 B covers "ancillary data produced by the VBMP". The parcel layer is
   aggregated by VGIN from localities' submissions under the VBMP, so it may count, but the item never names parcels.
   If it applies, distributing parcel data "for commercial or private use" needs a VGIN licence and fee. That reaches
   Inframation even under the `noncommercial` posture, since "private use" is broad. VGIN posts the data free with a
   disclaimer, consistent with "may release certain data by posting to the Internet", but posting is not a licence.
   **Confidence moderate that a written answer from VGIN is required before Virginia parcels are shown to anyone
   outside the company.**
2. **The § 54.1-402 C notice** should render with any Virginia parcel display, like the Texas no-endorsement notice.

**Owner names: not in the statewide layer.** Fields (schema
https://vginmaps.vdem.virginia.gov/arcgis/rest/services/VA_Base_Layers/VA_Parcels/MapServer/0?f=json): `VGIN_QPID`,
`FIPS`, `LOCALITY`, `PARCELID`, `PTM_ID`, `LASTUPDATE` and the geometry. The description says: "Attributes for these
Virginia parcels are limited to locality identification and parcel id." Owner, acreage and use come only from the
local schema tables, where "Attributes differ from locality to locality". Whether they carry owner names was not
measured, since that needs the 373 MB download. Acres can be computed from geometry. Land use would need
per-locality mapping.

**Allowlist (Virginia):** *stored and served*, the statewide layer's `VGIN_QPID`, `FIPS`, `LOCALITY`, `PARCELID`,
`PTM_ID`, `LASTUPDATE` and the geometry; *read in memory only*, none. The local schema tables are not read at all
until VGIN answers (§2.8 question 5); then each locality gets its own allowlist.

**Coverage, measured.** 4,174,364 parcels. 132 of 133 county-equivalents (95 counties, 38 cities); **Rappahannock
County (51157) is missing**; four town-level codes also appear. `LASTUPDATE` per locality runs from 2017-02-22
(Martinsville, 51690) and 2021-06-22 (Lee, 51105) to 2026-09-04.

**Cadence.** Neither item states a publication cadence. Both label themselves "2026 Q3 Product". The descriptions say
the dataset "is aggregated as part of the VGIN Local Government Data Call update cycle" and "Localities are
encouraged to submit data bi-annually". *Inference, moderate:* a quarterly product, in which each locality is as fresh
as its last submission (`LASTUPDATE` runs back to 2017). **Format and size:** file geodatabase (991 MB zipped item)
plus the local tables (373 MB); the item lists a shapefile download and the REST endpoint too.

**Readiness: not ready.** Proposed class `unknown`, publication `none`, until VGIN answers. If VGIN says Item 403 B
applies, the class becomes `restricted` until a licence is bought. Virginia is the largest data-centre market, so the
answer is worth getting quickly. The data-centre counties' own open-data terms (Loudoun, Prince William, Fairfax,
Culpeper and others) are a separate reading, not done here.

### 2.7 Summary

| | NC | WI | FL | OH | VA |
|---|---|---|---|---|---|
| Publisher | NC DIT CGIA (NC OneMap) | WI DOA WLIP / SCO | FL DOR PTO via FL GIO | OGRIP (DAS) | VGIN (VDEM) |
| Terms (verbatim key words) | "free and unrestricted use policy"; no release agreements | "free for public consumption"; no warranty; download page unread | disclaimer; data "owned and maintained by each … property appraiser" | "available for public use … informational purposes only" | disclaimer; Item 403 B licence fee for "commercial or private use" of VBMP data |
| Commercial display | yes (high) | likely (moderate; silent) | yes by Florida law (mod-high); GIO question open | likely (moderate; silent) | unclear; ask VGIN |
| Owner names (in memory only) | yes, `ownname` only; the subsurface owner (`subsurfown`) is never read, and `subowntype` gives a split-estate yes/no only | yes, `OWNERNME1` (Kenosha none; 8 counties partial) | yes, `OWN_NAME` | **no** (hidden) | not statewide; varies by locality |
| Acreage / land use | geometry / `parusecode` | `GISACRES` / `PROPCLASS` | `LND_SQFOOT` / `DOR_UC` | geometry (`LandArea` unit unstated) / `StateLUC` | geometry / locality tables |
| Same-owner holdings | yes | yes (not Kenosha) | yes | no | not now |
| Allowlist | §2.2 | §2.3 | §2.4 | §2.5 | §2.6 |
| Parcels (measured) | 5,938,900 | 3,574,646 | 10,950,670 | 6,313,610 | 4,174,364 |
| Coverage | 100/100 counties | 72/72 | 67/67 | 88/88 (10% no export date) | 132/133 (Rappahannock missing) |
| Cadence | quarterly snapshot; county uploads ≥ quarterly | annual (June) | annual (Aug) + May/Nov | annual collection | "2026 Q3 Product" label; locality submissions bi-annual |
| Bulk file | GDB 2.31 GB, GPKG 3.27 GB | on the SCO data page (format and size not read) | zip 2.77 GB | none published | GDB 0.99 GB + tables 0.37 GB |
| Required notice | citation requested ("NC OneMap (2019) …") | none stated (download page unread) | none; notify DOR of confidential leaks | none | § 54.1-402 C note |
| Proposed register class / publication | permissive / derived_only | permissive / derived_only; hold before paid use | unknown / none → permissive / derived_only | permissive / derived_only; hold before paid use | unknown / none |
| **Readiness rank** | **1** | **2** | **3** | **4** | **5** |

### 2.8 Owner actions, by state (questions for the owner to send; none sent by an agent)

1. **North Carolina:** none required. Optional courtesy note to NC OneMap via https://www.nconemap.gov/pages/feedback.
2. **Wisconsin:** wlip@wisconsin.gov, or help@sco.wisc.edu. Both are in the V12 Final Report
   (https://doa.wi.gov/DIR/V12_Final_Report.pdf): its cover lists "wlip@wisconsin.gov" for DOA's WLIP, and its
   feedback section says "you can email the State Cartographer’s Office at help@sco.wisc.edu". Ask: "Does 'free for
   public consumption' on the V12 statewide parcel layer cover showing derived parcel facts (acreage, property class,
   value, parcel counts) to paying subscribers of a commercial web service, without showing owner names? Are there
   terms on the SCO data page or in the download that we should follow?"
3. **Florida:** FloridaGIO@floridadep.gov, from the Florida Geographic Information Office's page
   (https://floridadep.gov/otis/enterprise-application-services/content/florida-geographic-information-office). No
   DOR email was read; the item gives DOR's number for confidential-record reports, 850-717-6570. Ask: "Your office
   asked UF GeoPlan to remove its parcel layers. May a commercial web service use DOR's statewide cadastral file to
   show derived parcel facts (acreage, use code, value) without owner names or addresses? Is there a reason for the
   request we should design around?"
4. **Ohio:** gis.support@das.ohio.gov, from the parcels hub's own page. The hub's site configuration (item
   `90e679445f3f4c87b7a054943567b9a0`,
   https://www.arcgis.com/sharing/rest/content/items/90e679445f3f4c87b7a054943567b9a0/data?f=json, rendered at
   https://ohioparcels-geohio.hub.arcgis.com) says "If you have any questions, contact the GIS Support Center", and
   its footer reads "Ohio Geographically Referenced Information Program 30 East Broad Street 19th Floor, Columbus,
   OH 43215 614-466-4747 gis.support@das.ohio.gov". Ask: "Is there a downloadable statewide file of the public view,
   do the 'public use' terms cover display of derived facts (acreage, land-use code) to paying subscribers, and in
   what unit is `LandArea`?"
5. **Virginia:** VBMP@VDEM.Virginia.gov, from VGIN's download page (https://vginmaps.vdem.virginia.gov/download/).
   Ask: "Does Appropriation Act Item 403 B's licence fee for 'commercial or private use' apply to the Virginia Parcels
   layer and its local schema tables? If so, what is the licence and fee for showing derived facts (acreage, parcel
   counts) to subscribers of a web service?"

Save each reply with its date for the register.

### 2.9 Proposed register rows (the coordinator adds them, with matching `docs/13` §6 rows)

Pattern: the Texas row `us.tx.txgio.parcels`. Each row is `derived_only` for the privacy rule even where the licence
would allow raw rows. Verified 2026-10-23, terms review only.

```yaml
- id: us.nc.nconemap.parcels
  name: NC OneMap NC Parcels (statewide standardized parcels, 100 counties and the Eastern Band of Cherokee Indians), for the site finder's land parcels
  jurisdiction: US-NC
  category: land
  operator: NC Department of Information Technology, Center for Geographic Information and Analysis (NC OneMap)
  url: https://www.nconemap.gov/pages/parcels
  access: bulk_file
  auth: none
  format: 'statewide zip, file geodatabase (nc-parcels-gdb-most-recent.zip, 2,307,182,458 bytes, 2026-08-11) or GeoPackage (3,269,152,493 bytes) at https://dit-cgia-gis-data.s3.amazonaws.com/NCOM-data/parcels/; per-county zips beside them'
  cadence: quarterly statewide snapshot; counties upload at least quarterly
  license: 'NC OneMap terms (https://www.nconemap.gov/pages/terms, retrieved 2026-10-23): "committed to offering access to current State geospatial information at no charge. By sharing their content, all partner organizations understand this free and unrestricted use policy." "Written release agreements to authorize use of the geospatial data, web services, and applications found on the NC OneMap website are not required and will not be issued." "Any sale of this data must not violate applicable state laws or regulations."'
  licence_url: https://www.nconemap.gov/pages/terms
  reuse: open
  publication: derived_only
  attribution: 'NC OneMap (2019). North Carolina Department of Information Technology, Government Data Analytics Center, Center for Geographic Information and Analysis. Available at www.nconemap.gov. Data from North Carolina counties and the Eastern Band of Cherokee Indians.'
  tier: 2
  effort: M
  verified: {date: 2026-10-23, result: ok, note: 'terms review, schema and aggregate counts only (docs/06 §2.2): 5,938,900 parcels, 100 counties, latest transform per county 2025-03-25 to 2026-06-01; no record downloaded'}
  notes: >-
    Same rule as us.tx.txgio.parcels, through an explicit allowlist (docs/06 §2.2), never "every column except":
    stored parno, nparno, altparno, cntyname, stcntyfips, parusecode, parusedesc, landval, parval, improvval,
    parvaltype, transfdate and the geometry; ownname read in memory only to group holdings, then dropped;
    subowntype only as a split-estate yes/no; subsurfown (the subsurface owner's name) is never read or stored; no
    other column is read. Acres from geometry (the state does not compute gisacres). attribution is NC OneMap's
    requested citation. N.C.G.S. 132-10 lets a county condition copies it furnishes; the state's aggregate carries
    no such condition (docs/06 §2.2).

- id: us.wi.sco.parcels
  name: Wisconsin Statewide Parcel Map (V12, DOA WLIP and State Cartographer's Office), for the site finder's land parcels
  jurisdiction: US-WI
  category: land
  operator: Wisconsin Department of Administration, Wisconsin Land Information Program; built by the State Cartographer's Office
  url: https://www.sco.wisc.edu/parcels/data/
  access: bulk_file
  auth: none
  format: statewide download from the SCO data page (format and size recorded at download; host unreachable from the agent sandbox); hosted service https://services3.arcgis.com/n6uYoouQZW75n5WI/arcgis/rest/services/Wisconsin_Statewide_Parcels_DB/FeatureServer
  cadence: annual (V12 released 2026-06-23; V13 tentatively 2027-06-30)
  license: 'Item licenseInfo (https://www.arcgis.com/home/item.html?id=2386813b23ea4e51a009f7d1d6b76e02, retrieved 2026-10-23): "This data free for public consumption as of: 06/30/2026"; county items add "No warranty, expressed or implied, is made regarding accuracy, completeness, or legality of the information herein." The SCO data page''s own terms are unread.'
  licence_url: https://www.arcgis.com/home/item.html?id=2386813b23ea4e51a009f7d1d6b76e02
  reuse: open
  publication: derived_only
  attribution: 'Source: Wisconsin Statewide Parcel Map Database Project (V12), Wisconsin State Cartographer''s Office and the Wisconsin Land Information Program, Department of Administration'
  tier: 2
  effort: M
  verified: {date: 2026-10-23, result: partial, note: 'terms review, schema and aggregate counts only (docs/06 §2.3): 3,574,646 parcels, 72 counties; SCO download host unreachable (TLS verification failed)'}
  notes: >-
    Through an explicit allowlist (docs/06 §2.3): stored STATEID, PARCELID, TAXPARCELID, CONAME, PARCELFIPS,
    PROPCLASS, AUXCLASS, GISACRES, DEEDACRES, ASSDACRES, CNTASSDVALUE, LNDVALUE, IMPVALUE, ESTFMKVALUE, TAXROLLYEAR,
    PARCELDATE, LOADDATE and the geometry; OWNERNME1 read in memory only; "NOT AVAILABLE" and blanks are never one
    owner; no other column is read. Kenosha withholds all names (no holdings there). Wis. Stat. 757.07(4m)
    (judicial opt-out) concerns showing or searching names; we do neither. The SCO data page's terms were not read:
    whoever downloads the file reads that page and any licence or readme in the download and records them here
    before the file is used. Hold before paid use: WLIP's or SCO's written confirmation that "free for public
    consumption" covers a paid product (docs/06 §2.8) is recorded here before Site finder Pro sells Wisconsin
    parcels or the posture flips to commercial (docs/26).

- id: us.fl.dor.parcels
  name: Florida Department of Revenue statewide cadastral (67 county property appraisers), for the site finder's land parcels
  jurisdiction: US-FL
  category: land
  operator: Florida Department of Revenue, Property Tax Oversight; published by the Florida Geographic Information Office
  url: https://www.arcgis.com/home/item.html?id=efa909d6b1c841d298b0a649e7f71cf2
  access: bulk_file
  auth: none
  format: 'zip https://publicfiles.dep.state.fl.us/otis/gis/data/Cadastral_Statewide.zip (2,768,353,501 bytes, Last-Modified 2026-10-23)'
  cadence: annual (August), minor maintenance May and November
  license: 'Item licenseInfo (retrieved 2026-10-23): "The Department does not generate or maintain the data used to compile this statewide parcel map. The Department does not guarantee the accuracy, or completeness, or the compliance with public records laws of this parcel GIS data. The data is owned and maintained by each Florida county''s property appraiser''s office". No licence. Microdecisions, Inc. v. Skinner (Fla. 2d DCA 2004): a property appraiser may not require a licensing agreement of commercial users of public-record GIS maps.'
  reuse: unknown
  publication: none
  tier: 2
  effort: M
  verified: {date: 2026-10-23, result: partial, note: 'terms review, schema and a count only (docs/06 §2.4): 10,950,670 parcels; the state GIO asked UF to remove its parcel copies (reason not found)'}
  notes: >-
    Held until the Florida GIO answers docs/06 §2.8 question 3; then permissive / derived_only. Through an explicit
    allowlist (docs/06 §2.4): stored PARCEL_ID, STATE_PAR_, CO_NO, ASMNT_YR, DOR_UC, PA_UC, JV, LND_VAL, LND_SQFOOT,
    PUBLIC_LND and the geometry; OWN_NAME read in memory only; no other column is read (in particular no owner,
    fiduciary or situs address, no S_LEGAL, no OR_BOOK/OR_PAGE/CLERK_NO deed reference, no homestead, exemption or
    special-circumstance field). DOR asks to be told of any inadvertently released confidential record
    (850-717-6570).

- id: us.oh.ogrip.parcels
  name: Ohio Statewide Parcels Public View (OGRIP), for the site finder's land parcels (acreage and land use; no holdings)
  jurisdiction: US-OH
  category: land
  operator: Ohio Geographically Referenced Information Program, Department of Administrative Services
  url: https://ohioparcels-geohio.hub.arcgis.com
  access: api
  auth: none
  format: hosted feature service only (maxRecordCount 2,000; about 3,200 pages statewide); no prepared statewide file found
  cadence: annual county collection (item "Updated Jan 21, 2024"; layer edited 2026-09-23)
  license: 'Item licenseInfo (https://www.arcgis.com/home/item.html?id=26ab5fad8d5d4258a7492a14de83bc0e, retrieved 2026-10-23): "The State of Ohio''s geospatial data are available for public use at the user''s own risk and provided for informational purposes only."'
  licence_url: https://www.arcgis.com/home/item.html?id=26ab5fad8d5d4258a7492a14de83bc0e
  reuse: open
  publication: derived_only
  attribution: 'Source: Ohio Geographically Referenced Information Program (OGRIP), Ohio Statewide Parcels; data from Ohio county auditors'
  tier: 3
  effort: M
  verified: {date: 2026-10-23, result: partial, note: 'terms review, schema and aggregate counts only (docs/06 §2.5): 6,313,610 parcels, 88 counties; 637,109 rows without an export date'}
  notes: >-
    Through an explicit allowlist (docs/06 §2.5): stored County, LocalParcelID, StateParcelID, StateLUC, CurrentTo
    and the geometry (acres computed from it), and CAMADataSite as the link to the county's record; no other column
    is read. No owner name in the public view (OwnerAll is withheld), and the Mail* and situs fields are never read,
    so no same-owner holdings in Ohio. LandArea has no stated unit and is not read until OGRIP states it. ODNR's
    parcel layers (OWNER1/OWNER2) are not used. Build only after OGRIP provides a file (docs/06 §2.8 question 4); do
    not page the service. Hold before paid use: OGRIP's answer that the "public use" terms cover a paid product is
    recorded here before Site finder Pro sells Ohio parcels or the posture flips to commercial (docs/26).

- id: us.va.vgin.parcels
  name: VGIN Virginia Parcels (VBMP statewide parcel geometry and local schema tables)
  jurisdiction: US-VA
  category: land
  operator: Virginia Geographic Information Network, Department of Emergency Management
  url: https://www.arcgis.com/home/item.html?id=29627d7c051a47dc8ce71b4484531ab3
  access: bulk_file
  auth: none
  format: file geodatabase (991,292,611 bytes, "2026 Q3 Product") plus local schema tables (373,316,934 bytes)
  cadence: 'quarterly product label ("2026 Q3 Product"); localities "are encouraged to submit data bi-annually"'
  license: 'Disclaimer only on the item (retrieved 2026-10-23). Appropriation Act 2026 Sp. Sess. I, HB30 Ch. 1, Item 403 B: "Distribution of the data for commercial or private use or to users outside the Commonwealth will be the sole responsibility of VGIN or its agent(s) and shall require payment of a license fee to be determined by VGIN" (VBMP data; parcels not named).'
  reuse: unknown
  publication: none
  notice: 'Any determination of topography or contours, or any depiction of physical improvements, property lines or boundaries is for general information only and shall not be used for the design, modification, or construction of improvements to real property or for flood plain determination.'
  tier: 3
  effort: L
  verified: {date: 2026-10-23, result: partial, note: 'terms review, schema and aggregate counts only (docs/06 §2.6): 4,174,364 parcels; 132 of 133 localities (Rappahannock missing)'}
  notes: >-
    Held until VGIN answers whether Item 403 B covers parcels (docs/06 §2.8 question 5); restricted if it does. The
    statewide layer has no owner, acreage or use. Through an explicit allowlist (docs/06 §2.6): VGIN_QPID, FIPS,
    LOCALITY, PARCELID, PTM_ID, LASTUPDATE and the geometry; the local schema tables are not read until VGIN
    answers, and then only through a per-locality allowlist.
```

`docs/13` §6 rows to match (R1 of `scripts/check_manifest_licences.py`):

| `source_id` | Class | Publication rule | Evidence | Confidence |
|---|---|---|---|---|
| `us.nc.nconemap.parcels` | permissive | derived-only (a privacy rule, as `us.tx.txgio.parcels`: no owner, subsurface-owner, mailing or situs value stored or served; columns read through an allowlist) + credit "NC OneMap (2019). North Carolina Department of Information Technology, Government Data Analytics Center, Center for Geographic Information and Analysis. Available at www.nconemap.gov." | `docs/06` §2.2 quoted | high |
| `us.wi.sco.parcels` | permissive | derived-only (privacy rule, as above) + credit "Source: Wisconsin Statewide Parcel Map Database Project (V12)"; hold before paid use until WLIP or SCO confirms | `docs/06` §2.3 quoted; commercial use unstated; download page unread | moderate |
| `us.fl.dor.parcels` | unknown | do not publish until the Florida GIO answers; then derived-only (privacy rule) | `docs/06` §2.4 quoted | moderate |
| `us.oh.ogrip.parcels` | permissive | derived-only (privacy rule; no holdings) + credit "Source: Ohio Geographically Referenced Information Program (OGRIP), Ohio Statewide Parcels"; hold before paid use until OGRIP confirms | `docs/06` §2.5 quoted | moderate |
| `us.va.vgin.parcels` | unknown | do not publish until VGIN answers on Item 403 B; then notice "Any determination of topography or contours, or any depiction of physical improvements, property lines or boundaries is for general information only and shall not be used for the design, modification, or construction of improvements to real property or for flood plain determination." | `docs/06` §2.6 quoted | moderate |

Each credit above is a prefix of the proposed row's `attribution`, and the Virginia notice is the row's `notice`
verbatim, so R5 passes when both move together. All seven rows (with the two GEM rows of §1.9) were merged into
scratch copies of `data/sources.yaml` and `docs/13` and pass `scripts/check_manifest_licences.py`.

---

## 3. Items requiring counsel

1. **GEM and the API terms** (§1.6): whether `docs/13-legal-customer-terms.md` §2.4's bar on republishing bulk
   extracts breaches CC BY 4.0's "No downstream restrictions" for CC BY sources, and whether the proposed carve-out
   cures it.
2. **GEM commercial use** (§1.2): whether any obligation survives from GEM's retired "commercial exemption" wording
   for data received under the CC BY 4.0 that GEM's form names today.
3. **GEM coal-plant provenance** (§1.1): whether GEM's use of the Platts database for "preliminary lists" leaves any
   third-party right in coal-plant rows that CC BY cannot reach.
4. **North Carolina § 132-10** (§2.2): whether a county's § 132-10 no-commercial-use condition can follow county data
   into the state aggregate that NC OneMap distributes "free and unrestricted".
5. **Wisconsin § 757.07(4m)** (§2.3): whether a service that never displays or indexes owner names is a "provider of
   a public-facing land records website" with an opt-out duty.
6. **Florida** (§2.4): the current reach of *Microdecisions* (2004) and any later Chapter 119 exemption affecting
   commercial reuse of DOR's statewide cadastral file.
7. **Virginia Item 403 B** (§2.6): whether "ancillary data produced by the VBMP" includes the locality-sourced parcel
   layer, and whether "private use" reaches a noncommercial public website.

## 4. Assumptions

- A1. Search-engine snippets of the retired GEM `/download-data/` pages reflect what those pages said. They are
  secondary and unverified; the Internet Archive was unreachable.
- A2. The GIPT carries the Solar tracker's TransitionZero rows. Inferred from "built on … eight power sector trackers"
  and the Solar page's sourcing sentence; confirmed only by reading a file. Until then §1.5 treats every GIPT solar
  row as possibly TransitionZero-sourced.
- A3. The form's recorded licence string is GEM's statement of the licence for each download. It is the only licence
  statement that covers the eight trackers without page prose, and the in-file notice is read before any use.
- A4. Coverage figures are each state service's own counts on 2026-10-23. Ohio's and Wisconsin's include one stray or
  county-less record each.
- A5. A state's free-data statement that is silent on commercial use (Wisconsin, Ohio) reflects no intended
  restriction. Hence "moderate", and a one-line confirmation before a paid launch.
- A6. The allowlists use field names from each state's hosted service schema. The bulk files are expected to carry
  the same names; a builder confirms them against the downloaded file and fails on a missing allowlisted column, as
  the Texas builder does for its required columns.

## 5. What changed elsewhere because of this reading (for the coordinator)

- The 2026-10-23 decisions-log row on global figures describes GEM's pages as stating CC BY 4.0 "unless otherwise
  noted" with a commercial-exemption process. That was true of the retired pages, not of today's (§1.2). A correcting
  row is returned in `decision_rows`.
- `global.gem.trackers` moves to `publication: none` until an in-file notice is read (§1.9); no connector reads it
  today.
- `docs/25` §5.2's next-state order, and its suggestion to key Ohio holdings on `MailAddressAll`, are superseded by
  §2.7 and the 2026-10-23 privacy rule. `docs/25` §2's ODNR layer with `OWNER1`/`OWNER2` is not used (§2.5).
- Per-parcel files for every state are built from an allowlist (§2.1). The Texas wording "the source table less its
  owner columns" (2026-10-23 row; `us.tx.txgio.parcels` notes) is read as `FIELD_NAMES`, the allowlist the Texas
  builder already uses.
- The switch-back runbook (`docs/26` §5) needs a step: before `PLATFORM_POSTURE=commercial` or any Pro sale, check
  every register row whose notes record a hold before paid use (`us.tx.txgio.parcels`, `us.wi.sco.parcels`,
  `us.oh.ogrip.parcels`, `global.gem.gipt`). Not this lane's file.
- `docs/13-legal-customer-terms.md` §2.4 needs the open-licence carve-out in §1.6 (not this lane's file).
