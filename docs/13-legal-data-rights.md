# Data rights register and publication policy

Phase 1 legal deliverable. Owner: legal-compliance agent. Status: v1, 2026-09-12.

**I am not a lawyer and this is not legal advice.** Everything below is a reading of primary text that I
retrieved and quoted. Section 7 lists the items that must go to counsel before launch.

**Reading convention.** Blockquotes are verbatim source text. Everything outside a blockquote marked
*Inference* is my reading and carries a stated confidence. Where a page could not be retrieved I say so and
classify the source `unknown` rather than guess.

**Retrieval.** All retrievals are dated **2026-09-12** unless otherwise stated. Method: HTTPS GET with a
desktop browser user-agent from a datacentre IP, plus a fetch-and-summarise tool. Failure modes encountered
were Cloudflare JS challenge (MISO, LBNL), Akamai deny (Business Wire), and 503 (SPP on first attempt,
succeeded on retry). Raw captures are in the session scratchpad, not committed; re-run the fetches to verify.

**Classification scheme** (this document's, not the sources'):

| Class | Meaning |
|---|---|
| `public-domain` | No copyright asserted or 17 U.S.C. §105 applies. Free to republish and resell. |
| `open-attribution` | Named open licence (CC BY, OGL, NESO Open Data, EU reuse) permitting commercial reuse with credit. |
| `permissive` | Not a named open licence, but the operative clause expressly permits redistribution of the data. |
| `attribution-restricted` | Reuse permitted with credit, but the same document also reserves rights or restricts republication; residual risk. |
| `restricted` | Redistribution or commercial use prohibited, or permitted only under a separate licence. |
| `unknown` | Operative terms could not be retrieved. |

**Publication rules** (this document's vocabulary, used by the matrix in §6):

- `raw-ok` — the ingested row may be rendered in full on a public page and included in exports.
- `derived-only` — publish normalised, enriched, stitched fields and change events; do not mirror the source
  table; link out for the raw row.
- `paid-api-only` — may be served to authenticated paying subscribers under a pass-through licence term, not
  on public pages.
- `link-out-only` — store internally for resolution and alerting; publish nothing but headline/title, a
  one-line factual summary, and the source link.

---

## 1. US ISOs and RTOs

### 1.1 ERCOT — retrieved, permissive

Source: https://www.ercot.com/help/terms — "ERCOT Website User Agreement", retrieved 2026-09-12 (HTTP 200).

> 5. The information presented on this website has been compiled by or for ERCOT for general information
> purposes only. The publicly available contents of this website may be used, reproduced, and redistributed,
> provided that the contents are not modified and that you maintain all copyright and other notices contained
> in the contents, including this Agreement. Notwithstanding the foregoing, raw data provided in public
> portions of this website may be used, reproduced, and redistributed in compilations, charts, and analyses
> without maintaining such notices. ERCOT does not guarantee the accuracy of any such compilations, charts,
> or analyses.

> 6. Use of this website in a manner that negatively affects the performance of this website or other ERCOT
> systems is prohibited. ERCOT reserves the right to take any and all action reasonably necessary to prevent
> such activity, including but not limited to monitoring website use and blocking access to this website.

> 8. Use of ERCOT list Server (email) list for marketing products and/or services is strictly prohibited.
> Host domains of violators will be denied access to the ERCOT server.

> 1. By accessing this website, you agree to the terms of this Website User Agreement ("Agreement") set out
> below. If you do not agree with the terms of this Agreement, do not access this website or any pages
> thereof.

*Inference:* the GIS Report and Large Load Interconnection Status Report are raw data in a public portion of
the site, so clause 5's second sentence applies: we may redistribute them inside compilations and analyses.
Bankable's product *is* a compilation and analysis, so this is the cleanest ISO permission in the US. Clause 1
is a browsewrap-style assent; it binds us as a matter of contract risk regardless of enforceability arguments,
and clause 6 means we must rate-limit. Clause 8 is an outbound-marketing rule, carried into
`13-legal-outreach-and-social.md` §9. **Confidence: high.**

Classification: `permissive`. Publication rule: `raw-ok` (attribution rendered anyway as a matter of policy).

### 1.2 CAISO — retrieved, attribution with a reserved-rights clause

Source: https://www.caiso.com/privacy-terms-of-use — "Terms of Use" last updated 2025-05-14; "API Terms of
Use" last updated 2019-08-22. Retrieved 2026-09-12 (HTTP 200).

> **Restrictions on Use of Materials**
> This Website and the California ISO mobile application is operated and maintained by the California
> Independent System Operator Corporation (referred to as the "California ISO" herein). No material or
> information from this Website or the California ISO mobile application may be copied, reproduced,
> republished, uploaded, posted, transmitted or distributed except (i) as authorized in these Terms of Use,
> (ii) as expressly authorized in the materials and/or information contained on this Website, or (iii) as
> expressly authorized in writing by the California ISO.

> Materials and information on this Website and the California ISO mobile application are provided as a public
> service. Most of the materials and information contained on this Website and the California ISO mobile
> application were generated, compiled or assembled from materials and information that are freely available
> for public use consistent with the general policies of the Public Records Act (California Government Code
> section 6250, et seq.), as provided by California Public Utilities Code section 345.5, and may be used by
> you provided that you keep intact all copyright, trademark and other proprietary notices and that you credit
> the California ISO when using such materials and/or information.

API Terms of Use, Restrictions:

> **Adverse and Excessive Use.** Users are prohibited from using the CAISO API in a manner that adversely
> impacts the performance of CAISO's systems, including but not limited to CAISO's servers or other
> applications. The ISO reserves the right to suspend or terminate a user's access to the CAISO API if that
> user is connecting to or retrieving data from an ISO Production system for non-production purposes or is
> using CAISO systems in an excessive or unnecessary manner that adversely impacts the performance of those
> systems.

> **Monitoring.** You may not use or access the CAISO API for purposes of monitoring the availability,
> performance or functionality of any of CAISO's systems or services or for any other benchmarking or
> competitive purposes.

> **Ownership.** CAISO owns all right, title and interest in and to the CAISO API and CAISO Data.

*Inference:* the two paragraphs are in tension. The first bars republication except as authorised; the second
authorises use of "most of the materials" if notices are kept intact and CAISO is credited. The Public Queue
Report is Public Records Act material of exactly the kind the second paragraph describes, so I read it as
falling in the permitted set. But "most of" is not "all of", and CAISO does not identify which materials fall
outside. That residual ambiguity is why I do not rate this `permissive`. The API "Monitoring" clause is a
genuine constraint on one Bankable feature class: an availability/latency monitor over CAISO endpoints would
breach it; a queue change-detector over published report contents would not, because it monitors the *data*
not the *system*. **Confidence: moderate-high on the queue report; high on the API monitoring restriction.**

Classification: `attribution-restricted`. Publication rule: `derived-only` at launch, with a credit line
"Source: California ISO" and a link to the source file. Move to `raw-ok` only if counsel reads the tension the
other way or CAISO confirms in writing.

### 1.3 PJM — retrieved; three documents that do not agree

This is the most consequential finding in this document, and it corrects `01-feasibility.md` §3.2 and
`data/sources.yaml`, both of which record only the most restrictive of the three texts.

**(a) Data Miner 2 Terms of Use** (the signed licence PDF), source:
https://www.pjm.com/-/media/DotCom/etools/edatafeed/data-license-agreement-edata-feed-data-miner-2.pdf —
retrieved 2026-09-12 (HTTP 200, 104,439 bytes; md5 f2e554a41e5b40c9852fcfed05ca6fa5).

> 1. Permitted Use. Subject to Section 2 below, you may use the Data and the Tools for your internal business
> use or commercial purposes, including publishing and making derivatives of the Data, Members may republish
> data, however non-members are specifically prohibited from republishing data.

> 2. Use Restrictions. You may not (i) reverse engineer, disassemble, decompile, decode, adapt, or otherwise
> attempt to derive or gain access to the source of the Data or methods used to compile the Data; (ii) remove
> any proprietary notices that may be included or contained within the Data or the Tools; (iii) access the
> Tools more than six-hundred (600) times per minute for Members and six (6) times per minute for
> non-members; or (iv) use the Data or Tools in any manner or for any purpose that infringes, misappropriates,
> or otherwise violates any intellectual property right or other right of any person, including PJM, or that
> violates any applicable law.

> 7. Intellectual Property Ownership. You acknowledge that, as between You and PJM, PJM owns all rights,
> titles, and interests, including all intellectual property rights, in and to the Tools. You further
> acknowledge that: (a) each of the Tools is an original compilation of Data and is protected by United States
> copyright laws; (b) PJM has dedicated substantial resources to collect, manage, and compile the Data; and
> (c) each of the Tools constitutes trade secrets of PJM.

> 9. Indemnification. … You shall indemnify, hold harmless, and, at PJM's option, defend PJM from and against
> any losses resulting from any Third-Party Claim based on Your: (i) negligence or willful misconduct; (ii) use
> of the Data in a manner not authorized by this Agreement; (iii) your provision of Data or access to the Tools
> to any third party; or (iv) any derivative created by you based on the Data or Your use of the Tools.

> (g) Governing Law; Submission to Jurisdiction. This Agreement is governed by and construed in accordance
> with internal laws of the Commonwealth of Pennsylvania … instituted exclusively in the federal courts of the
> United States or the courts of the Commonwealth of Pennsylvania in each case located in the city of
> Norristown and County of Montgomery …

**(b) PJM API portal "Acceptable Terms of Use"**, source: https://apiportal.pjm.com/ (rendered widget at
`https://apiportal.pjm.com/content/html_widgets/39vgb.html`), retrieved 2026-09-12.

> Acceptable Terms of Use
> PJM Members may not exceed 600 data connections per minute. Non-members may not exceed 6 data connections
> per minute.
> Information and data contained in Data Miner 2 is for internal use only and redistribution of information
> and or data contained in or derived from Data Miner 2 is strictly prohibited without an effective PJM-issued
> Redistribution License.

**(c) pjm.com site-wide "Legal & Privacy"**, source: https://www.pjm.com/about-pjm/legal — retrieved
2026-09-12 (HTTP 200). This is the notice linked from the footer of the planning pages, including the New
Services Queue. Its complete operative text on rights is:

> This website has been compiled, and is maintained by PJM Interconnection. PJM® is a registered trademark of
> PJM Interconnection, L.L.C. Thus, use of PJM Trademarks and Logos by third parties is not generally permitted
> except through express written PJM authorization.

> Access to this website does not confer any license or ownership interest in either the form or content of the
> website, including any confidential or proprietary information or intellectual property of any kind or
> nature, and PJM hereby expressly reserves such rights and property in its entirety.

**On the specific question asked — does the New Services Queue published on pjm.com planning pages fall under
the Data Miner restriction?**

I checked the page directly. `https://www.pjm.com/planning/service-requests/services-request-status` returns
HTTP 200 and reads "The information on this page has moved here", redirecting to
`https://www.pjm.com/planning/service-requests/serial-service-request-status`. That page carries downloadable
queue tables and a "Guide to Obtaining Interconnection Information" PDF. Its footer links only to "Legal and
Privacy" — document (c). There is **no** Data Miner terms reference, no Redistribution Licence reference, and
no clickwrap on those pages. `https://www.pjm.com/robots.txt` (retrieved 2026-09-12, HTTP 200) disallows only
`/xsl/`, `/temp/`, `/sitecore*`, `/App_*`, `/Calendar*`, `/sitecore modules/` and `/PJMWebToolsAdmin/`; the
planning paths are not disallowed and no AI/TDM content-signal is present.

*Inference (the load-bearing one):* the New Services Queue as published on pjm.com planning pages is **not**
governed by the Data Miner 2 Terms of Use. Those terms are, by their own words, an agreement "entered into by
and between you and PJM" that governs "your access to and use of PJM's Data Miner 2 (the Tools)". Terms (b)
likewise scopes itself to "information and data contained in Data Miner 2". Neither purports to reach the
whole of pjm.com. The planning pages are governed by (c), which reserves IP rights and restricts trademarks
but contains no redistribution prohibition at all. **Confidence: moderate-high** that the documents mean what
they say; **low-moderate** that PJM would agree in a dispute, because PJM plainly regards its queue compilation
as proprietary (clause 7 of (a) calls the Tools "trade secrets" and asserts compilation copyright), and a
court could read the compilation copyright claim as reaching the same rows however obtained.

*Second inference:* documents (a) and (b) conflict for non-members. (a) grants commercial use "including
publishing and making derivatives of the Data" and then bars non-members from *republishing*; (b) says all
Data Miner data is "for internal use only" and bars redistribution of data "contained in or **derived from**"
Data Miner without a Redistribution Licence, which would also bar derivatives. (a) is the executed agreement
with an entire-agreement clause (10(e)); (b) is a portal notice. I would expect (a) to govern between the
parties, but the safe reading is the union of both restrictions. **Confidence: moderate.**

*Practical consequence:* there is a route to publishing PJM queue data that does not require a Redistribution
Licence — ingest the New Services Queue from the pjm.com planning pages, not from Data Miner 2, and never
accept the Data Miner Terms of Use for the queue connector. This is a meaningful change to the launch plan in
`00-PLAN.md` ("PJM rows are not public until a licence exists"). **I am not confident enough to act on it
without counsel.** It is item 1 in §7.

Classification: `restricted` for Data Miner 2 / API portal; `attribution-restricted` for the pjm.com planning
pages. Publication rule: `derived-only` for planning-page-sourced rows *after* counsel signs off, otherwise
`link-out-only`; `paid-api-only` for anything Data Miner-sourced and only under a Redistribution Licence.

### 1.4 SPP — retrieved, and materially worse than the repo assumed

Source: https://www.spp.org/terms-conditions/ — retrieved 2026-09-12 (HTTP 200 on retry; a fetch-and-summarise
attempt against the same URL returned 503 at the same moment, which is why earlier probes recorded 503).

> **Copyright**
> The posting of documents on this web site is done for the convenience of Southwest Power Pool, Inc.'s (SPP)
> members, customers, market participants and other interested visitors. The copyright on information within
> this website is intended to protect SPP and the members of SPP, as well as any consultants or entities
> performing work with or for SPP and/or its member base.

> All materials posted or otherwise available on this website are the exclusive copyrighted material of the
> author(s) or SPP. Permission is implicitly granted to copy and distribute (via computer network or printed
> form) in whole or in part (with appropriate citation) EXCEPT when such materials will be used, in whole or in
> part, within a commercial publication (printed or otherwise) or when the author(s) or SPP will be quoted in
> commercial materials, forums or publications. Any commercial use of these materials requires prior, express
> written authorization from the author(s) or a duly authorized officer of SPP.

> Your access to SPP's website does not confer any license or ownership interest in either the form or content
> of SPP's website, including any confidential information, proprietary information, or intellectual property
> of any kind or nature; SPP hereby expressly reserves such rights and property in their entirety.

> Links to SPP's website from other websites are permitted and encouraged.

> Access to or use of the information posted on this website in any manner constitutes an agreement to hold
> harmless and indemnify SPP …

The page also links an **Acceptable Use Policy** PDF at
`https://www.spp.org/Documents/22819/SPP_External_Systems_Acceptable_Use_Policy%20Final%20Approved.pdf`,
which I did not retrieve or read. That document may contain automated-access rules and must be read before
the SPP connector ships.

*Inference:* this is the single most restrictive US ISO clause found. The grant is explicitly conditioned on
non-commercial use: copying and distribution is permitted "EXCEPT when such materials will be used, in whole
or in part, within a commercial publication". Bankable's free tier is a commercial publication (it is the
funnel for a paid product); the Pro/Team/API tiers plainly are. On the face of the text, republishing SPP
generator-interconnection rows in any Bankable tier requires "prior, express written authorization". Note
also "Links to SPP's website from other websites are permitted and encouraged" — link-out is expressly
blessed. **Confidence: high** on what the text says; **moderate** on whether SPP would enforce it against a
derived-data product, since the clause is aimed at reproducing documents rather than facts.

Facts are not copyrightable in the US (*Feist Publications v. Rural Telephone Service*, 499 U.S. 340 (1991)),
so a normalised record consisting of project name, capacity, county, status and dates is not obviously
"materials" within the clause. But the clause is also a contract term, and a browsewrap contract can restrict
what copyright would not — see §3.2.

Classification: `restricted`. Publication rule: `link-out-only` at launch; `derived-only` only after either
written SPP authorisation or a counsel opinion that normalised facts fall outside the clause. This changes the
`01-feasibility.md` §3.2 expectation that SPP would prove benign, and it removes SPP from the "daily cadence,
14-day free lag" row in `11-market-and-competition.md` §3 unless resolved.

### 1.5 NYISO — retrieved, and permissive by omission

Source: https://www.nyiso.com/legal-notice — "Legal Notice - Web Content", retrieved 2026-09-12 (HTTP 200).
Complete operative text on rights (the rest of the notice is warranty disclaimer and limitation of liability):

> Access to this Web site does not confer any license or ownership interest in either the form or content of
> the Web site, including any confidential or proprietary information or intellectual property of any kind or
> nature, and the NYISO hereby expressly reserves such rights and property in its entirety. Downloading,
> republishing, retransmitting, reproducing, or other use of any image or video on this website as a
> stand-alone file is strictly prohibited

> The NYISO's trademarks (including its logo) are owned by the NYISO and may only be used with the NYISO's
> prior written permission. Even if prior written permission is obtained, the NYISO may revoke permission to
> use the NYISO's trademarks at any time.

> The NYISO maintains this Web site for the benefit of its Market Participants and other authorized users. The
> materials on this site are provided "as is."

*Inference:* the only express prohibition is on standalone reuse of **images and video**. The
Interconnection Queue is an .xlsx workbook, not an image or video, and no clause restricts its reuse. The
reserved-rights sentence is a reservation, not a prohibition. There is no clickwrap, and unlike ERCOT or ISO-NE
there is no "by using this site you agree" sentence, so the notice is weak even as browsewrap. "Market
Participants and other authorized users" is descriptive of purpose, not a restriction on who may read it.
**Confidence: moderate-high.** The residual risk is the reserved-rights sentence plus an unasserted compilation
copyright claim.

Classification: `attribution-restricted` (attribution is our policy, not their requirement). Publication rule:
`derived-only` at launch, moving to `raw-ok` on counsel sign-off. This upgrades NYISO from `unknown`.

### 1.6 ISO New England — retrieved, restrictive on its face

Source: https://www.iso-ne.com/participate/support/legal-privacy (ISO-NE "Legal and Privacy"), retrieved
2026-09-12 (HTTP 200 via browser-UA fetch; the page 403s to plain non-browser clients, which is why earlier
probes recorded 403).

> By using this website, you signify your assent to these Terms and Conditions.

> You are also hereby put on notice that the Content is protected by copyright under United States laws. Any
> duplication of the Content or non-personal use may violate copyright, trademark, and other laws.

> You agree to defend, indemnify, and hold ISO New England, its officers, directors, employees, agents,
> licensors, and suppliers, harmless from and against any claims, actions or demands, liabilities and
> settlements, including without limitation, reasonable legal and accounting fees, resulting from, or alleged
> to result from, your violation of these Terms and Conditions.

> You may not use the website in any manner that could damage, disable, overburden or impair any ISO New
> England server(s), or the network(s) connected to any ISO New England server(s), or interfere with any other
> party's use of this Web site. … You may not obtain or attempt to obtain any materials or information through
> any means not intentionally made available through this Web site.

> ISO New England will monitor the performance of the website and the amount of requests and bandwidth used.
> In the event of excessive bandwidth, excessive concurrent requests, or invalid URL requests, ISO New England
> may, at its discretion, employ a throttling mechanism to mitigate any degradation of service. The process of
> enabling and disabling throttling of incoming connections is done by IP address.

> Activity that may be considered excessive usage includes, but is not limited to:
> Bandwidth by IP address—a volume of megabytes by IP address sent or received per time period
> Concurrent Requests—a number of requests by IP address to the same URL per time period
> Invalid URL Requests—a number of invalid URL requests by IP address per time period

> To request permission to use a graph, chart, or other image on this website, please email info@iso-ne.com.

*Inference:* "Any duplication of the Content or non-personal use may violate copyright" is drafted as broadly
as an ISO clause gets, and "Content" is defined earlier in the page to include "all information and data on
this website". Read literally it forbids commercial reuse of the IRTT public report. It is hedged ("may
violate") and is a statement of copyright law rather than a contractual grant or denial — and copyright does
not protect the facts in the queue (*Feist*). But the first sentence quoted is an express assent clause,
which makes the whole notice a browsewrap contract and strengthens a breach-of-contract theory (§3.2). The
explicit throttling and "excessive concurrent requests" language sets a clear engineering constraint: one
sequential request per report per polling interval, from a stable IP, with backoff.

Classification: `restricted`. Publication rule: `link-out-only` at launch; `derived-only` on counsel sign-off
or written ISO-NE permission. This upgrades ISO-NE from `unknown` to a known-bad posture, which is worse news
but actionable.

### 1.7 MISO — NOT RETRIEVED

- `https://www.misoenergy.org/meet-miso/legal-and-privacy/` — HTTP 403 with a Cloudflare "Just a moment…"
  managed-challenge interstitial, on both a plain fetch and a full desktop-browser header set
  (Accept/Accept-Language/Sec-Fetch-*). Retrieved (failed) 2026-09-12.
- `https://www.misoenergy.org/meet-miso/contact-us/terms-and-conditions/` — same challenge.
- `web.archive.org` — the Wayback availability API reports `{"archived_snapshots": {}}` for
  `misoenergy.org/meet-miso/legal-and-privacy/` and `misoenergy.org/legal/`. Direct
  `web.archive.org/web/2025id_/…` requests were reset by the egress proxy, and the CDX search endpoint is
  blocked by egress policy from this environment. I could not obtain an archived copy.
- Search-engine summaries assert MISO's terms cover copyright in written content, photographs, graphics,
  logos and video clips. **I did not verify this against the page and do not rely on it.**

**MISO's terms are unknown. I am not guessing them.**

What I *did* retrieve is `https://www.misoenergy.org/robots.txt` (HTTP 200, 2026-09-12), which is materially
relevant and is quoted in full for the operative part:

> \# As a condition of accessing this website, you agree to abide by the following
> \# content signals:
> \#
> \# (a)  If a Content-Signal = yes, you may collect content for the corresponding
> \#      use.
> \# (b)  If a Content-Signal = no, you may not collect content for the
> \#      corresponding use.
> …
> \# ANY RESTRICTIONS EXPRESSED VIA CONTENT SIGNALS ARE EXPRESS RESERVATIONS OF
> \# RIGHTS UNDER ARTICLE 4 OF THE EUROPEAN UNION DIRECTIVE 2019/790 ON COPYRIGHT
> \# AND RELATED RIGHTS IN THE DIGITAL SINGLE MARKET.
>
> User-agent: \*
> Content-Signal: search=yes,ai-train=no,use=reference
> Allow: /
>
> User-agent: Amazonbot
> Disallow: /
> …

*Inference:* crawling MISO pages is permitted by robots (`Allow: /` for `*`); AI **training** on MISO content
is expressly refused; `use=reference` signals that AI systems may reference but not fully reproduce. This is a
Cloudflare-managed default block, not bespoke MISO drafting, but it is on MISO's domain and MISO is the party
asserting it. See §4.2 for how we honour it. Note the tension: robots.txt says `Allow: /` while the WAF
serves a JS challenge to non-browser clients — MISO's stated policy and its enforcement do not match.

Classification: `unknown`. Publication rule: `link-out-only`. No MISO row is published in any tier until the
terms are read from a browser, a PDF is captured, and this file is updated. `CLAUDE.md` already forbids
challenge bypass; that stands. Rendering the page in a real browser session (a human opening it and saving a
PDF) is the correct next step and is item 2 in §7.

---

## 2. Open licences, research datasets and news sources

### 2.1 LBNL "Queued Up" / GridTracker — NOT DIRECTLY RETRIEVED

`https://emp.lbl.gov/queues` returned HTTP 403 behind a Cloudflare block page ("Sorry, you have been blocked…
Ray ID a3a055ef085b0fbe") on 2026-09-12, as did `https://emp.lbl.gov/publications/queued-2026-edition-characteristics`.
I retrieved the *Queued Up: 2024 Edition* slide deck PDF successfully
(`https://emp.lbl.gov/sites/default/files/2024-04/Queued%20Up%202024%20Edition_1.pdf`, HTTP 200, 9.07 MB,
56 pages) and searched its full text: it contains **no** Creative Commons statement. Its only rights language
is the standard DOE contract notice:

> This work was funded by the U.S. Department of Energy under Contract No. DE-AC02-05CH11231. The views and
> opinions of the authors expressed herein do not necessarily state or reflect those of the United States
> Government or any agency thereof, or The Regents of the University of California.

> … Contract No. DE-AC02-05CH11231 with the U.S. Department of Energy. The U.S. Government retains, and the
> publisher, by accepting the article for publication, acknowledges, that the U.S. Government retains a
> non-exclusive, paid-up, irrevocable, worldwide license to publish or reproduce the published form of this
> manuscript, or allow others to do so, for U.S. Government purposes

A search-engine summary of the `emp.lbl.gov/queues` page states the data file is CC BY 4.0 with attribution to
LBNL and GridTracker. **That is second-hand and I did not verify it.** `data/sources.yaml` currently records
"CC BY 4.0 (attribute LBNL and GridTracker)" — that claim is unverified as of this document.

*Inference:* the underlying dataset is a GridTracker work product delivered under a DOE contract, not a pure
federal work, so 17 U.S.C. §105 does not automatically apply and the licence matters. Because
Interconnection.fyi (GridTracker) sells the same underlying data commercially, the licence terms on the free
file are exactly the kind of thing a data owner scopes narrowly. **Confidence: low** on the CC BY claim until
the page or the file's own licence tab is read.

Classification: `unknown` pending verification (was `attribution` in the YAML). Publication rule:
`derived-only` and attributed to "Lawrence Berkeley National Laboratory and GridTracker" until verified; do
not redistribute the file itself. Item 3 in §7.

### 2.2 Global Energy Monitor — retrieved, CC BY 4.0, with a trap

Source: https://globalenergymonitor.org/projects/global-solar-power-tracker/download-data/ — retrieved
2026-09-12 (HTTP 200). The same "Download Data" boilerplate appears on each tracker page.

> The recommended citation is "Global Solar Power Tracker, Global Energy Monitor, February 2026 release."

> When the data set is being shared or adapted and our Creative Commons CC BY 4.0 International license
> applies, please read the license for attribution requirements. Necessary attribution elements are included
> in our data download files.

> Please note that records with a "TZ ID" in the Other IDs (unit/phase) column were partially or fully sourced
> from the TransitionZero, Solar Asset Mapper, August 2025. Copyright © TransitionZero, Solar Asset Mapper,
> August 2025. Distributed under a Creative Commons Attribution-Non-Commercial 4.0 International License
> (CC BY-NC 4.0).

GEM hosts the full CC BY 4.0 Public License text at https://globalenergymonitor.org/creative-commons-public-license/
(retrieved 2026-09-12, HTTP 200), granting:

> Subject to the terms and conditions of this Public License, the Licensor hereby grants You a worldwide,
> royalty-free, non-sublicensable, non-exclusive, irrevocable license to exercise the Licensed Rights in the
> Licensed Material to: reproduce and Share the Licensed Material, in whole or in part; and produce,
> reproduce, and Share Adapted Material.

Note separately that the GEM **wiki** at gem.wiki is under a different, incompatible licence. From
https://www.gem.wiki/Global_Energy_Monitor:Copyrights (retrieved 2026-09-12):

> Original text of GEM Wiki entries is licensed to the public under the GNU Free Documentation License (GFDL)
> and Creative Commons Attribution-NonCommercial-ShareAlike.

*Inference:* two separate hazards for a commercial product. (1) **Rows carrying a `TZ ID` in the Solar Power
Tracker are CC BY-NC 4.0, not CC BY 4.0.** Ingesting the solar tracker wholesale and serving it to paying
subscribers would redistribute non-commercial-licensed records inside a commercial product. The connector must
detect and drop or quarantine any row with a non-empty `Other IDs (unit/phase)` value matching `TZ`. (2) GEM
wiki prose is CC BY-NC-SA and must never be ingested. **Confidence: high** on both.

Classification: `open-attribution` for tracker data, **except** TZ-flagged rows which are `restricted` for our
purposes. Publication rule: `raw-ok` with the recommended citation, TZ rows excluded at ingest.

### 2.3 EU reuse (Decision 2011/833/EU) as applied to TED — retrieved, clean

Source: https://ted.europa.eu/en/legal-notice ("Copyright notice"), retrieved 2026-09-12 (HTTP 200).

> © European Union, 1998-2026
>
> The European Commission's reuse policy is implemented by the Commission Decision of 12 December 2011 on the
> reuse of Commission documents. Unless otherwise noted, the procurement notices published in the Supplement
> to the Official Journal of the European Union can be freely reused, for commercial or non-commercial
> purposes.

> The copyright over the editorial content of the SIMAP websites (TED, TED eNotices2, TED Developer Docs and
> TED Developer Portal) is licensed under the Creative Commons Attribution 4.0 International (CC BY 4.0)
> license. This means that reuse is allowed provided appropriate credit is given and any changes made are
> indicated.

> You may be required to clear additional rights if specific content depicts identifiable private individuals
> or includes third-party works. To use or reproduce content that is not owned by the EU, you may need to seek
> permission directly from the rightholders. Software or documents covered by industrial property rights, such
> as patents, trademarks, registered designs, logos and names, are excluded from the Commission's reuse policy
> and are not licensed to you.

> The SIMAP system's logos (including that of TED) may not be used without the prior consent of the
> Publications Office of the European Union.

> The SIMAP's system metadata is dedicated to the public domain in accordance with the Creative Commons
> Universal Public Domain Dedication deed (CC0 1.0).

Underlying instrument, source: https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX%3A32011D0833 —
retrieved 2026-09-12. Article 3 makes documents available for reuse "for commercial or non-commercial
purposes" without charge and "without the need to make an individual application"; Article 6 permits
conditions including "the obligation for the reuser to acknowledge the source", "the obligation not to distort
the original meaning or message" and "the non-liability of the Commission for any consequence", which "shall
not unnecessarily restrict possibilities for reuse".

*Inference:* TED notices are free for commercial reuse including raw republication. Metadata is CC0. The only
live constraints are: credit the source, do not distort, do not use the TED logo, and clear personal data
separately (the notices contain contracting-authority contact names — see §5). **Confidence: high.**

Classification: `open-attribution` (metadata `public-domain`). Publication rule: `raw-ok`.

### 2.4 UK Open Government Licence v3 — retrieved

Source: https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/ — retrieved 2026-09-12.

> **You are free to:** copy, publish, distribute and transmit the Information; adapt the Information; exploit
> the Information commercially and non-commercially for example, by combining it with other Information, or by
> including it in your own product or application.

> **You must (where you do any of the above):** acknowledge the source of the Information in your product or
> application by including or linking to any attribution statement specified by the Information Provider(s)
> and, where possible, provide a link to this licence

Default attribution where none is specified: "Contains public sector information licensed under the Open
Government Licence v3.0." Exemptions include personal data, third-party rights, and logos/crests.

Classification: `open-attribution`. Publication rule: `raw-ok`. Applies to `gb.pins.nsip` and
`gb.find_a_tender`.

### 2.5 NESO Open Data Licence — retrieved

Source: https://www.neso.energy/data-portal/neso-open-licence — "National Energy SO Open Data Licence v1.0",
retrieved 2026-09-12 (HTTP 200). Note the licence URL in `sources.yaml` should be this, not the
`nationalgrideso.com` path, which now 403s.

> These licence terms and conditions apply to the National Energy System Operator ("NESO") datasets as
> specified within the NESO data portal service and are based on version 3.0 of the Open Government Licence
> with specific amendments for NESO (the "Licence").

> NESO may at any time revise this Licence without notice. It is up to you ("You") to regularly review the
> Licence, which will be available on this website, in case there are any changes.

> The Licensor grants you a worldwide, royalty-free, perpetual, non-exclusive licence to use the Information
> subject to the conditions below.

> **You are free to:** copy, publish, distribute and transmit the Information; adapt the Information; exploit
> the Information commercially and non-commercially for example, by combining it with other Information, or by
> including it in your own product or application.

> **You must (where you do any of the above):** Acknowledge NESO as the source of the Information by including
> the following attribution statement 'Supported by National Energy SO Open Data'. Ensure our other
> intellectual property rights, including all logos, design rights, patents and trademarks, are protected.

> These are important conditions of this licence and if you fail to comply with them the rights granted to you
> under this licence, or any similar licence granted by the Licensor, will end automatically.

> Where it is believed that the overall service is being degraded by excessive use, NESO reserve the right to
> throttle or limit access to feeds as considered appropriate.

> The licence does not cover: personal data in the Information; …

> **Non-endorsement** This licence does not grant you any right to use the Information in a way that suggests
> any official status or that the Information Provider and/or Licensor endorse you or your use of the
> Information.

> Please note, that each dataset on the NESO data portal is licenced on an individual basis and that this
> licence only applies to the datasets explicitly associated with this licence.

*Inference:* explicit commercial-exploitation grant with a **mandatory exact attribution string**: `Supported
by National Energy SO Open Data`. Automatic termination on breach means the attribution renderer must be
tested, not assumed. The per-dataset caveat means the TEC Register's own licence field must be checked at
ingest, not inherited from this page. **Confidence: high.**

Classification: `open-attribution`. Publication rule: `raw-ok` with the exact attribution string.

### 2.6 Google News RSS — retrieved robots.txt; terms not separately retrieved

Source: https://news.google.com/robots.txt — retrieved 2026-09-12 (HTTP 200, 484 bytes). Quoted in full:

> User-agent: \*
> Disallow: /
> Allow: /$
> Allow: /?
> Allow: /home$
> Allow: /home?
> Allow: /home/
> Allow: /nwshp$
> Allow: /topics/
> Allow: /publications/
> Allow: /stories/
> Allow: /swg/
> Allow: /about$
> Allow: /about?
> Allow: /about/
>
> User-agent: Googlebot
> Disallow: /
> Allow: /$
> Allow: /?
> Allow: /home$
> Allow: /home?
> Allow: /home/
>
> User-agent: CCBot
> User-agent: GPTBot
> User-agent: ChatGPT-User
> User-agent: PerplexityBot
> User-agent: anthropic-ai
> User-agent: ClaudeBot
> User-agent: Claude-Web
> Disallow: /

*Inference (important and adverse):* `data/sources.yaml` lists
`https://news.google.com/rss/search?q=…` as the `news.google_rss` connector URL. **The `/rss/` path is not in
the `Allow` list, so it is `Disallow`ed for `User-agent: *`.** Automated polling of Google News RSS search
feeds is contrary to Google's robots.txt. That is not itself unlawful in the Ninth Circuit (§3.1), but it is
(a) a plain signal of non-consent, (b) evidence in any breach-of-contract or trespass claim, and (c)
inconsistent with `CLAUDE.md`'s posture. Google's general Terms of Service, which I did not retrieve as a
separate document for this register, also prohibit abusing the services and using automated means to access
them except as permitted. **Confidence: high** on the robots reading.

Classification: `restricted`. Publication rule: `link-out-only` — and my recommendation is to **drop the
Google News RSS connector entirely** and replace it with GDELT DOC (which is licensed for this), publisher-own
RSS feeds (which are offered for syndication), and a small curated set of trade-press feeds. Item 4 in §7.

### 2.7 Business Wire — NOT RETRIEVED

`https://www.businesswire.com/terms-of-use`, `.../terms-of-use/`, `https://www.businesswire.com/portal/site/home/terms-of-use/`
and `https://services.businesswire.com/terms-of-use` all returned **HTTP 403 "Access Denied"** from Akamai
(reference #18.4f53d117.1789231858.3e8b65fb), to both a browser-UA curl and the fetch-and-summarise tool, on
2026-09-12.

A search-engine summary asserts that Business Wire's terms limit site use to submitting releases, retrieving
RSS feeds and reading releases, and that users agree not to "store, aggregate, reproduce, or distribute"
site information or to compete with Business Wire. **I could not verify this and do not rely on it.** If it
is accurate it is close to a total bar on what Bankable would want to do, including an anti-competition
clause.

Classification: `unknown`. Publication rule: `link-out-only`. Do not ingest Business Wire beyond headline,
publication timestamp and link until the terms are read. Item 5 in §7.

### 2.8 PR Newswire — retrieved, and restrictive

Source: https://www.prnewswire.com/terms-of-use/ — retrieved 2026-09-12 (HTTP 200).

From "General Rules of Conduct", the list of things "You agree that you will not do or assist any third party
to do":

> other than as explicitly stated herein or as posted elsewhere on the Site, reproduce, distribute, "frame,"
> "mirror," "scrape," republish or retransmit information or content provided by PR Newswire or found on the
> Site without the express prior written permission of PR Newswire;

> use any robot, spider, site search/retrieval application, or other manual or automatic device or process to
> access, search, download, retrieve, index or "data mine" any content or data from the Site, or in any way
> reproduce or circumvent the navigational structure or presentation of the content available through the
> Site;

> take any action that imposes an unreasonable or disproportionately large load on PR Newswire systems or
> infrastructure;

> You agree not to republish, upload, post, transmit, train a machine learning or artificial intelligence (AI)
> system, or distribute information or content submitted to the Site by other users, without the prior written
> approval of the owner of such information or content.

From "PR Newswire's Proprietary Rights":

> Except as otherwise permitted herein or by PR Newswire in advance and in writing, you may access and use the
> PR Newswire Materials, and download or print out a single copy from the Site, solely for your personal,
> noncommercial use.

> Except as expressly permitted herein, you may not reproduce, modify, create derivative works from, display,
> perform, publish, distribute (including any electronic redistribution or database storage and retrieval),
> disseminate, broadcast or circulate to any third party (including on or via a third party website), or
> otherwise use, any PR Newswire Materials, in full, in part, in full text or in abstract, without the express
> prior written consent of PR Newswire.

*Inference:* unambiguous. No scraping, no robots, no database storage, no commercial use, no derivative works,
no abstracts, and an express anti-AI-training term. "PR Newswire Materials" is defined to include "news
releases … issued by PR Newswire customers", so even the issuer's own release as it appears on prnewswire.com
is covered. The only safe interaction is reading the publicly offered RSS feed, storing nothing beyond the
identifiers needed to link out, and publishing headline + link. Even "in abstract" is barred, so we must not
publish our own summary of a PR Newswire-hosted release; we may publish the *fact* extracted from it (a
company announced a 300 MW project in X county), attributed to the company, with the link. Facts are not
"Materials". **Confidence: high** on the text; **moderate** on the facts-vs-materials line, which is the usual
hot-news boundary (§3.4).

Classification: `restricted`. Publication rule: `link-out-only`.

*Inference across 2.6–2.8:* the news layer is the weakest part of the current source plan. GDELT and issuer
RSS are viable; the wire services and Google News are not, on current terms. The product design consequence is
that news must be treated as a **signal to trigger a lookup in a licensed source**, not as publishable
content. That is consistent with `02-data-sources.md` §4 ("headline, link, snippet, extracted facts only") but
stricter on abstracts.

### 2.9 OurGridFuture (planned transmission) — NOT RETRIEVED

Candidate source for the built-infrastructure context layer (owner decision, `00-PLAN.md` 2026-09-14/09-15:
"OurGridFuture registered as a candidate source with a terms check").

Source attempted: https://ourgridfuture.org/ — 2026-09-15, **HTTP 403** (Cloudflare managed challenge,
"Just a moment…" interstitial) from a datacentre IP, on the root page and on nine guessed paths (`/terms`,
`/license`, `/licence`, `/about`, `/data`, `/faq`, `/terms-of-use`, `/data-use`, `/privacy`) — all nine returned
the identical 403 challenge page. `/robots.txt` returned **HTTP 404** (no robots file present), which at least
confirms this is Cloudflare bot-management on the app, not a full site outage. A Wayback Machine snapshot exists
(`web.archive.org/web/20260324231145/https://ourgridfuture.org/`, dated 2026-03-24) but this sandbox's egress
policy blocks `archive.org`, so it could not be read either, by curl or by the fetch tool.

The secondary page named in this task — https://opengridworks.com/attribution, where OurGridFuture's data is
credited per opengridworks.com's own attribution page — returned **HTTP 429** ("Vercel Security Checkpoint"
interstitial) on three attempts across roughly 15 seconds of backoff, on both `/attribution` and the site root.

What turned up in general web search, **not retrieved from primary text and not evidence of licence terms**:
OurGridFuture is described as maintained by Horizon Energy Systems in association with the Great Plains
Institute; the planned-transmission GIS shapefile/database is described as available after a user "provides
contact information" (a registration gate, not an open download); OpenGridWorks describes itself as
incorporating select OurGridFuture layers. None of this states a reuse licence, and a registration gate alone
is not a signal either way — GEM (§2.2) also gates behind email registration but is CC BY 4.0 underneath.

**To retrieve by hand:** open https://ourgridfuture.org/ in a real browser — this passes the interactive
Cloudflare challenge that a headless/datacentre client fails — and find the terms/licence/attribution page
(most likely in the site footer or attached to the data-request/registration form). Register with an identity
that can accept terms on Bankable's behalf, not an anonymous address, since accepting terms is itself a fact
worth recording (§3.2 browsewrap/clickwrap distinction). Record the operative clause here verbatim with the
retrieval date. Great Plains Institute is also worth asking directly, since GPI publishes other datasets under
open licences elsewhere.

Classification: `unknown`. Publication rule: do not ingest; not published in any tier until terms are read.
**Confidence: none** on reuse — this section documents a blocked retrieval, not a legal reading.

### 2.10 OpenStreetMap-derived basemap tiles (Protomaps) — produced work, not derivative database

Basemap decision (owner, `00-PLAN.md` 2026-09-15): Protomaps PMTiles, self-hosted, pre-launch. This section
reads the licence question that decision depends on: rendering someone else's OSM-derived tile build is a
different act, under ODbL, from ingesting OSM's underlying data — and only the first is decided. The second is
open question 7(b), left for counsel, and nothing below resolves it.

**ODbL 1.0 definitions** (retrieved https://opendatacommons.org/licenses/odbl/1-0/, 2026-09-15, HTTP 200):

> "Produced Work" – a work (such as an image, audiovisual material, text, or sounds) resulting from using the
> whole or a Substantial part of the Contents (via a search or other query) from this Database, a Derivative
> Database, or this Database as part of a Collective Database.

**ODbL 1.0 §4.4–4.6** (same source):

> 4.4 Share alike. a. Any Derivative Database that You Publicly Use must be only under the terms of: i. This
> License; ii. A later version of this License similar in spirit to this License; or iii. A compatible
> license. … b. For the avoidance of doubt, Extraction or Re-utilisation of the whole or a Substantial part of
> the Contents into a new database is a Derivative Database and must comply with Section 4.4. c. Derivative
> Databases and Produced Works. A Derivative Database is Publicly Used and so must comply with Section 4.4. if
> a Produced Work created from the Derivative Database is Publicly Used. d. Share Alike and additional
> Contents. For the avoidance of doubt, You must not add Contents to Derivative Databases under Section 4.4 a
> that are incompatible with the rights granted under this License.

> 4.5 Limits of Share Alike. The requirements of Section 4.4 do not apply in the following: a. … You are not
> required to license Collective Databases under this License if You incorporate this Database or a Derivative
> Database in the collection, but this License still applies to this Database or a Derivative Database as a
> part of the Collective Database; b. Using this Database, a Derivative Database, or this Database as part of a
> Collective Database to create a Produced Work does not create a Derivative Database for purposes of Section
> 4.4; and c. Use of a Derivative Database internally within an organisation is not to the public and therefore
> does not fall under the requirements of Section 4.4.

> 4.6 Access to Derivative Databases. If You Publicly Use a Derivative Database or a Produced Work from a
> Derivative Database, You must also offer to recipients of the Derivative Database or Produced Work a copy in
> a machine readable form of: a. The entire Derivative Database; or b. A file containing all of the alterations
> made to the Database or the method of making the alterations to the Database (such as an algorithm) …

Note precisely what §4.6 conditions on: a Produced Work made **from a Derivative Database**. Read against
§4.5(b) — making a Produced Work does not itself create a Derivative Database — the share-back duty in §4.6
does not, on this text, attach to a Produced Work made directly from an unmodified Database or unmodified
extract of one.

**OSM Foundation, Produced Work Guideline** (retrieved
https://osmfoundation.org/wiki/Licence/Community_Guidelines/Produced_Work_-_Guideline, 2026-09-15, HTTP 200;
endorsed by the OSMF board 2014-06-06):

> The published result of your project is either a Produced Worked or a Derivative Database within the meaning
> of the ODbL. If the published result of your project is intended for the extraction of the original data,
> then it is a database and not a Produced Work. Otherwise it is a Produced Work. However, if you publish a
> produced work, the underlying database has to be published as well (or alternations to the original database
> as is the case of derived databases), according to section 4.6 of ODbL.
>
> We can clearly define things that are USUALLY Produced Works: .PNG, JPG, .PDF, SVG images and any raster
> image; a map in a physically printed work. Database dumps are usually not Produced Works, e.g a Planet dump.

*Inference:* the guideline's last sentence above reads, on its plain words, as extending a publish-the-database
duty to Produced Works generally — broader than ODbL §4.4(c)/§4.5(b)'s narrower trigger (only a Produced Work
made *from a Derivative Database*). I flag this tension rather than resolve it. The Attribution Guideline
(quoted next) itself says the legal text controls over the guidance where the two disagree, which favours
ODbL's narrower reading, but this is precisely a "which text governs" question I am not positioned to close.
**Confidence: low** on which of the two texts controls if they diverge.

**OSM Foundation, Attribution Guideline** (retrieved https://osmfoundation.org/wiki/Licence/Attribution_Guidelines,
2026-09-15, HTTP 200; adopted by the OSMF board 2021-06-25):

> OpenStreetMap (OSM) data is distributed under the Open Database License (ODbL). If you want to use
> OpenStreetMap data in something you create and distribute, you must attribute OpenStreetMap. … These
> guidelines are not a substitute for the legal text itself. If the two texts disagree, the legal text takes
> precedence.

> Attribution text: Attribution must be to "OpenStreetMap". Attribution must also make it clear that the data
> is available under the Open Database License. This may be done by making the text "OpenStreetMap" a link to
> openstreetmap.org/copyright … The historical forms of attribution "© OpenStreetMap contributors" or
> "© OpenStreetMap" are acceptable.

> Interactive maps: For a browsable map (e.g., embedded in a web page or application), the credit should
> typically appear in a corner of the map. … You may use a mechanism to fade/collapse the attribution under
> certain conditions: immediately with a dismiss interaction … automatically on map interaction such as
> panning, clicking, or zooming … automatically after five seconds … If the attribution has been collapsed, the
> user must still be able to find the licence information if they look for it, for example from an "(i)" button
> in the corner.

**Protomaps' own statement for its basemap builds** (retrieved https://docs.protomaps.com/basemaps/downloads
and https://docs.protomaps.com/, 2026-09-15, HTTP 200):

> The Protomaps Basemap is a general purpose vector base map - city labels, roads, water features and other
> essential location context derived from OpenStreetMap. It's available as a single PMTiles archive,
> distributed as an Open Database License Produced Work (OpenStreetMap attribution required)

> An open source mapping system released under the BSD and ODbL licenses. (site-wide footer, docs.protomaps.com)

I checked the docs homepage, the Downloads/Basemap Layers/Flavors/Build pages, and protomaps.com's root for a
Protomaps-branding attribution clause beyond the OSM one; I found none (`protomaps.com/terms` and
`protomaps.com/license` both return HTTP 404). **Confidence: moderate** that no separate Protomaps-attribution
term exists — absence of a found clause is not proof none exists elsewhere, and Protomaps LLC's own software
licence (BSD, per the footer) is a distinct question from the basemap build's data terms and is not reviewed
here.

**Position** (mine, not counsel's):

1. Rendering Protomaps PMTiles into map imagery is, on the ODbL definition and on Protomaps' own
   characterisation quoted above, a **Produced Work**: rendered tiles are the paradigm example in the OSMF
   guideline's own list.
2. As a Produced Work, visible attribution is required (§4.3, and the Attribution Guideline above): legible,
   near the map, and either persistent or collapsible with an always-reachable way to find the licence
   information (the OSMF "Interactive maps" safe harbour). This is an implementation requirement for the map
   component, not a policy question.
3. Bankable stores no OSM data itself — it self-hosts a Protomaps-built PMTiles archive and serves tiles from
   it, without extracting, retaining, or re-publishing the underlying vector database. On that basis Bankable's
   own database is not a Derivative Database of OSM, and §4.4's share-alike duty does not attach to it.
   **Confidence: moderate-high** on this reading as applied to *serving someone else's unmodified tile build*,
   precisely because it turns on the ODbL-vs-guideline tension flagged above, which is unresolved.
4. A **separate** question — pulling OSM vector data itself (roads, boundaries, points of interest as
   structured rows) into Bankable's own database, e.g. to enrich or geocode context-layer or project records in
   the paid tier — is a different act (arguably "Extraction or Re-utilisation… into a new database" under
   §4.4(b)) and remains **open question 7(b)** per the owner's 2026-09-14 decision. Point 3 does not answer it,
   and nothing above should be read as answering it.

Classification: n/a — this is a licence question for produced-work tiles, not a data-source register row.
Publication rule: visible OSM attribution required on every map view per the guideline above; no OSM vector
data is ingested into Bankable's own database under this decision.

### 2.11 EIA-860M for the built-infrastructure context layer — public domain, addendum

`us.eia.860m` is already recorded at §6 as `public-domain`/`raw-ok` under 17 U.S.C. §105 (US federal government
work; see the register table). For the context layer specifically (`00-PLAN.md` 2026-09-15: "Context layer =
EIA-860M operating plants, US only first"), I retrieved and quote EIA's own reuse statement directly rather
than relying only on the general federal-work inference.

Source: https://www.eia.gov/about/copyrights_reuse.php — "Copyrights and Reuse", retrieved 2026-09-15
(HTTP 200).

> U.S. government publications are in the public domain and are not subject to copyright protection. You may
> use and/or distribute any of our data, files, databases, reports, graphs, charts, and other information
> products that are on our website or that you receive through our email distribution service. However, if you
> use or reproduce any of our information products, you should use an acknowledgment, which includes the
> publication date, such as: "Source: U.S. Energy Information Administration (Oct 2008)."

> When quoting EIA text, the acknowledgment should clearly indicate which text is EIA content and which is not.

*Inference:* this confirms the §105 public-domain reading directly rather than by analogy, and gives the exact
form of the requested (not required) acknowledgment. Adopt "Source: U.S. Energy Information Administration
(<EIA-860M release month/year>)" as the rendered attribution string for context-layer plant records, matching
EIA's own example. Crediting is a request, not a licence condition — the data is public domain whether credited
or not — but it costs nothing and matches the platform's attribution-by-default posture. Separately: EIA's logo
and some third-party-contributed photographs/illustrations on eia.gov *are* protected and must not be used;
this has no bearing on the 860M data file. **Confidence: high.**

Classification: `public-domain` (unchanged). Publication rule: `raw-ok` (unchanged); attribution string above
adopted specifically for context-layer plant records.

---

### 2.12 EPA databases (LMOP, AgSTAR, RFS public data) — public domain by §105, with EPA's own hedge quoted

Source: https://www.epa.gov/web-policies-and-procedures/epa-disclaimers — "EPA Disclaimers", section "Copyright
Status", retrieved 2026-09-18 (HTTP 200).

> The U.S. Government retains a nonexclusive, royalty-free license to publish or reproduce these documents, or
> allow others to do so, for U.S. Government purposes. These documents may be freely distributed and used for
> non-commercial, scientific and educational purposes. Commercial use of the documents available from the EPA
> websites may be protected under the U.S. and Foreign Copyright Laws. Individual documents on the EPA website
> may have different copyright conditions, and that will be noted in those documents.

*Inference:* the paragraph is written for "documents", many of which EPA hosts but did not author (contractor
reports, submitted studies, third-party images), and it is those that "may be protected". A database compiled
by EPA staff in the course of their duties (the LMOP landfill and project database, the AgSTAR digester
database, the EMTS RIN and registration tables) is a work of the United States Government under 17 U.S.C. §105
and has no copyright to assert, commercial use included. The hedge does not change that; it warns that not
everything on epa.gov is a §105 work. Two cautions follow. First, LMOP and AgSTAR rows are partly
operator-submitted; the facts are not copyrightable either way, but the databases carry no licence grant we can
quote, so the position rests on §105 alone. Second, "individual documents may have different copyright
conditions": check each downloaded workbook's cover sheet for a notice before ingest and record it in the
register. **Confidence: moderate-high** (§105 reading strong; EPA's own wording weaker than EIA's).

Classification: `public-domain`. Publication rule: `raw-ok`, with the cover-sheet check above. Counsel item 13.

### 2.13 GLEIF LEI data — CC0 1.0, retrieved

Source: https://www.gleif.org/en/meta/lei-data-terms-of-use — "LEI Data Terms of Use", retrieved 2026-09-18
(HTTP 200).

> The data available through the Access Service are provided under the CC0 licence, see CC0 1.0 Universal

*Inference:* CC0 is a public-domain dedication; the Level 1 entity records and Level 2 relationship records
(direct and ultimate parents) can be stored, joined and republished, commercially, without attribution.
GLEIF asks to be cited as the source in its documentation, which the platform's attribution-by-default
rendering does anyway. **Confidence: high.**

Classification: `open` (CC0). Publication rule: `raw-ok`.

### 2.14 Argonne RNG Database and PHMSA pipeline data pages — NOT RETRIEVED

Both pages answered a Cloudflare challenge (HTTP 403, "Just a moment...") to the scripted probe on 2026-09-18,
so nothing can be quoted. They differ in what that means. PHMSA is a DOT agency; its compiled annual-report and
incident tables are §105 works and the only open point is the file layout and any notice on the data page
(classification `public-domain`, confidence high, pending a browser read). Argonne National Laboratory is
operated for DOE by UChicago Argonne, LLC; works of contractor employees are not automatically §105 works, and
DOE laboratories publish data under their own terms, so `us.anl.rng_database` stays `reuse: unknown` and is
gated until the owner or counsel reads its terms in a browser (`docs/40` §0). If the terms are restrictive,
LMOP, AgSTAR and the RFS tables cover operational RNG assets without it; only the planned-project view is lost.

### 2.15 EIA-923 (annual generation and fuel) — public domain, covered by §2.11

`us.eia.form923` was registered on 2026-09-19 by the features lane. No separate terms retrieval was
needed: EIA's reuse statement quoted in §2.11 is site-wide and covers "any of our data, files,
databases, reports, graphs, charts, and other information products that are on our website", which
is exactly what the EIA-923 workbooks are. Classification `public-domain`, publication `raw-ok`,
acknowledgment rendered as "Source: U.S. Energy Information Administration (Sep 2026)" per EIA's
own example form. **Confidence: high.**

Two access facts belong on the record, because they constrain how the connector may fetch rather
than what may be published:

- **`archive/` is robots-disallowed.** `eia.gov/robots.txt` (read live 2026-09-19) carries
  `Disallow: /*archive/`, which covers `electricity/data/eia923/archive/xls/f923_<year>.zip` — the
  path holding every year before the current two. The connector therefore reads only the
  non-archive years, and a backfill of older years would need a different, permitted route (EIA's
  bulk API, or a request to EIA) rather than crawling that path.
- **Early-release files are not final.** EIA publishes the current year monthly ("EIA-923 June
  2026") and finalises the prior year each September; the 2026 workbook is an early release of a
  partial year. Deriving a capacity factor from it would understate every plant, so the connector
  requires a `_Final` workbook member and steps back a year otherwise. This is a data-quality rule,
  not a licence one, and is recorded here because the year it lands on is visible in published
  attribute keys (`capacity_factor_2025`).

### 2.16 PHMSA data read from the Internet Archive — the data is §105, the access route is not PHMSA

`us.phmsa.pipeline_operator_reports` was recorded on 2026-09-18 as `public-domain` / `raw-ok` on the
§105 reasoning in §2.14, with the data page unread. On 2026-09-19 the features lane established more
precisely what is happening, and it is not a Cloudflare challenge as §2.14 assumed:

> `HTTP/2 403` · `server: AkamaiGHost` · `<TITLE>Access Denied</TITLE>` · "You don't have permission
> to access … on this server." (retrieved 2026-09-19 for the data page, both data files, and
> `https://www.phmsa.dot.gov/robots.txt` itself)

*Inference, two parts.* **The data.** PHMSA is a DOT operating administration and these are
compilations of operator filings prepared by federal employees: 17 U.S.C. §105 applies, the
classification `public-domain` stands, and it does not depend on which server the bytes arrive from.
**The route.** Because the origin refuses this egress — including `robots.txt`, so no crawl
directive can be read at all — the connector reads the Internet Archive's captures of the same two
files (`web.archive.org/web/<timestamp>id_/<phmsa url>`), recording the origin's answer, the capture
used and the origin's own `Last-Modified` in every run record, and keeping the PHMSA URL as the
published `source_url`. That is a second party's service, so its terms govern the *fetch*, not the
data: the Archive offers these captures publicly and without charge, the connector takes two files
at 0.5 rps, and nothing is bypassed or spoofed (a 403 is honoured as a 403 — no retry, no alternate
user-agent). Reading a public-domain federal file from a public mirror is not a licence question;
if the Archive's terms are later read and found to restrict automated retrieval, the fallback is
what stops, not the source. **Confidence: high** on the data, **moderate** on the route pending a
read of the Archive's terms of use (added to the browser tasks in `docs/40` §0).

A third fact, recorded because it affects what we can publish: three members of PHMSA's own
published annual zip are damaged at source (`annual_…_2010.xlsx`, `annual_…_2019.xlsx`, `GT AR 2025
Part J.csv` — zlib/CRC errors on two byte-identical downloads). The connector reads the newest
intact per-year workbook and reports which one; no year is silently substituted for another.

Finally, `us.epa.rfs_public_data`: the page named in the manifest publishes no facility-level data
(Qlik application; aggregate RIN volumes only). The D-code registrations come from EPA's Part 80
registered company/facility list on the same fuels-programs site (`cdxoarapps.epa.gov`, linked from
`epa.gov/fuels-registration-reporting-and-compliance-help/registered-companies-and-facilities-epas-fuel`).
Same publisher, same §105 basis and the same EPA hedge already assessed at §2.12; the workbook
carries no cover sheet or notice of its own (checked 2026-09-19, the §2.12 cover-sheet rule).
Classification and publication rule unchanged: `public-domain` / `raw-ok`.

---

## 3. US scraping law

I am summarising the state of the law as relevant to Bankable's design decisions. This section is the one
most in need of counsel review; it is written to frame the questions, not to answer them.

### 3.1 CFAA after *Van Buren* and *hiQ*

*Van Buren v. United States*, 593 U.S. 374 (2021). Source:
https://www.supremecourt.gov/opinions/20pdf/19-783_k53l.pdf, retrieved 2026-09-12. Syllabus:

> Held: An individual "exceeds authorized access" when he accesses a computer with authorization but then
> obtains information located in particular areas of the computer—such as files, folders, or databases—that
> are off-limits to him.

The opinion adopts what the syllabus calls a

> gates-up-or-down inquiry—one either can or cannot access a computer system, and one either can or cannot
> access certain areas within the system.

*hiQ Labs, Inc. v. LinkedIn Corp.*, 31 F.4th 1180 (9th Cir. 2022). On remand from the Supreme Court in light
of *Van Buren*, the Ninth Circuit reaffirmed its preliminary injunction, holding that scraping publicly
accessible pages that do not require an account does not access a computer "without authorization" under the
CFAA — where a site is public there is no gate to lower, so there is nothing to revoke. I did not retrieve the
opinion text directly for this register; the holding is reported consistently across practitioner summaries
retrieved 2026-09-12 (Fenwick, White & Case, Seyfarth, Loeb & Loeb).

*The important coda:* the case did not end with hiQ winning. In November 2022 the district court granted
LinkedIn summary judgment on its **breach of contract** claim, and the parties settled in December 2022 with
hiQ subject to a permanent injunction against scraping and paying LinkedIn $500,000. The CFAA theory failed;
the contract theory succeeded.

*Inference for Bankable:*

1. A pure CFAA exposure from reading public ISO pages is **low** in the Ninth Circuit (CAISO is in-circuit) and
   moderate-low elsewhere; the Supreme Court's narrowing is nationwide even if *hiQ* is not binding.
2. CFAA exposure becomes **real** if we ever (a) create an account and use it contrary to its terms, (b)
   bypass an authentication or anti-bot gate, or (c) continue after a cease-and-desist that revokes access to
   a non-public area. MISO's Cloudflare challenge is a gate. `CLAUDE.md` already forbids bypassing it. Keep
   that rule absolute — it is the difference between a contract dispute and a federal statute.
3. CFAA is not the risk to design around. Contract is.

**Confidence: high** on the doctrinal summary; **moderate** on how a court outside the Ninth Circuit would
treat a challenge-protected but nominally public page.

### 3.2 Breach of contract: browsewrap vs clickwrap

The distinguishing question in US courts is whether the user had **reasonable notice** of the terms and
manifested **assent** to them.

- **Clickwrap** — the user affirmatively clicks "I agree" before access. Routinely enforced. Bankable would be
  in clickwrap territory the moment it registers for PJM Data Miner 2, ERCOT's api.ercot.com, NRC ADAMS,
  Regulations.gov or any keyed API. Accepting those terms binds the company, and — see §3.5 — binds it in the
  named forum.
- **Browsewrap** — terms are linked from a footer and assert agreement by use. Enforceability turns on
  conspicuousness and on whether the defendant had actual knowledge. Courts have been markedly more willing to
  enforce browsewrap against a **sophisticated commercial defendant that read the terms** than against a
  consumer.

*Inference, and it cuts against us:* by writing this register I have given Bankable **actual knowledge** of
SPP's non-commercial clause, ISO-NE's assent clause, CAISO's restriction, PR Newswire's anti-scraping clause
and Google News's robots directive. Actual knowledge is precisely what converts a weak browsewrap into an
enforceable contract in most circuits. The register is still the right thing to build — proceeding in
deliberate ignorance is worse, both legally and ethically — but it means the publication rules in §6 are
operational constraints, not suggestions. Shipping a connector that we have documented as contrary to a
source's terms would be knowing breach.

ISO-NE's "By using this website, you signify your assent to these Terms and Conditions" and ERCOT's "By
accessing this website, you agree to the terms of this Website User Agreement" are both assent-by-use clauses;
SPP's copyright section has no assent sentence but does say "Access to or use of the information posted on this
website in any manner constitutes an agreement to hold harmless and indemnify SPP", which is an assent clause
for the indemnity at minimum. NYISO has no assent clause at all — its notice is the weakest of the five, which
supports the `derived-only` rather than `link-out-only` treatment.

**Confidence: moderate-high.** The doctrine is settled; its application to a specific page is fact-bound.

### 3.3 Trespass to chattels

The surviving common-law theory against scraping. In California (*Intel Corp. v. Hamidi*, 30 Cal. 4th 1342
(2003)) the plaintiff must show **actual damage to or impairment of** the computer system, not merely
unwanted contact. *eBay v. Bidder's Edge*, 100 F. Supp. 2d 1058 (N.D. Cal. 2000), is the classic
scraping-as-trespass case and turned on load imposed on eBay's servers.

*Inference:* this is the theory that ISO-NE's terms are drafted to support. They pre-define what counts as
harm — "excessive bandwidth", "excessive concurrent requests", "invalid URL requests" — and announce
throttling by IP. Every element of a trespass claim against us would be supplied by our own request logs.
Mitigation is entirely an engineering matter and belongs in the connector spec:

- one connector, one stable source IP, identified `User-Agent` naming Bankable with a contact URL;
- conditional requests (`If-Modified-Since`, `ETag`) so unchanged files are not re-downloaded;
- serial not parallel requests per host; a floor of ≥2 s between requests to the same host;
- exponential backoff on 429/503 and an immediate circuit-break on 403;
- no retry of URLs that 404 (ISO-NE counts invalid URL requests as abuse);
- poll no more often than the source's own publication cadence.

**Confidence: high** that this mitigation removes practical trespass exposure for file-download connectors.

### 3.4 Hot-news misappropriation

*International News Service v. Associated Press*, 248 U.S. 215 (1918); narrowed and largely confined by *NBA
v. Motorola*, 105 F.3d 841 (2d Cir. 1997), which set a five-element test: the plaintiff generates information
at cost; the information is time-sensitive; the defendant free-rides; the defendant is in direct competition
with the plaintiff; and free-riding would so reduce the incentive to produce the product that its existence or
quality would be substantially threatened. The doctrine is preempted by the Copyright Act except in that
narrow band.

*Inference:* Bankable's exposure is **not** from ISOs (they do not sell the data and are not in competition
with us). It is from the private aggregators — Interconnection.fyi/GridTracker, Cleanview, Energy Adepto,
Halcyon, Enverus — where all five *NBA* elements could be pleaded: they invest in collection, their alerts are
time-sensitive, we would be a direct competitor, and a free mirror would damage their subscription model. This
is a second, independent reason (alongside `CLAUDE.md`'s absolute rule and their ToS) never to scrape them,
and it extends to a subtler behaviour: **do not use a competitor's newsletter, alert email or public feed as a
trigger to go fetch the same record from the primary source and publish it first.** That is the free-riding
the doctrine targets. Discovery of *which* sources exist is fine; using their output as an operational
tip-sheet is not.

Second exposure: the wire services. PR Newswire's terms already bar it contractually; hot-news adds a tort
overlay for a real-time republication product. Our 7-day/14-day free-tier delays (`11-market-and-competition.md`
§3) do not help here, because the Pro tier is day-0 by design.

**Confidence: moderate.** The doctrine is narrow and rarely successful, but it is exactly the shape of claim a
well-funded incumbent would plead alongside contract.

### 3.5 Forum and governing law — a practical point

The terms we have read pick different forums: PJM → Montgomery County, Pennsylvania (exclusive); CAISO API →
Sacramento, California (exclusive); ERCOT → W.D. Tex. or Travis County, Texas (exclusive); NESO → England and
Wales. Accepting several keyed-API clickwraps means accepting several exclusive forums. This is a term-sheet
issue for the entity decision in `00-PLAN.md` open question 3, not a data question.

---

## 4. EU database right and DSM Article 4

### 4.1 The sui generis database right

Directive 96/9/EC, source: https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:31996L0009 —
retrieved 2026-09-12.

> **Article 7 — Object of protection**
> 1. Member States shall provide for a right for the maker of a database which shows that there has been
> qualitatively and/or quantitatively a substantial investment in either the obtaining, verification or
> presentation of the contents to prevent extraction and/or re-utilization of the whole or of a substantial
> part, evaluated qualitatively and/or quantitatively, of the contents of that database.

> 2. (a) 'extraction' shall mean the permanent or temporary transfer of all or a substantial part of the
> contents of a database to another medium by any means or in any form;
> (b) 're-utilization' shall mean any form of making available to the public all or a substantial part of the
> contents of a database by the distribution of copies, by renting, by on-line or other forms of transmission.

> 5. The repeated and systematic extraction and/or re-utilization of insubstantial parts of the contents of the
> database implying acts which conflict with a normal exploitation of that database or which unreasonably
> prejudice the legitimate interests of the maker of the database shall not be permitted.

> **Article 11 — Beneficiaries of protection under the sui generis right**
> 1. The right provided for in Article 7 shall apply to database whose makers or rightholders are nationals of
> a Member State or who have their habitual residence in the territory of the Community.
> 2. Paragraph 1 shall also apply to companies and firms formed in accordance with the law of a Member State
> and having their registered office, central administration or principal place of business within the
> Community …

> **Article 10** … It shall expire fifteen years from the first of January of the year following the date of
> completion. … 3. Any substantial change, evaluated qualitatively or quantitatively, to the contents of a
> database … which would result in the database being considered to be a substantial new investment … shall
> qualify the database resulting from that investment for its own term of protection.

*Inference:*

1. Article 7(5) is the clause that matters most to a continuous-ingestion product: even taking small slices is
   prohibited if done "repeatedly and systematically" in a way that conflicts with normal exploitation. A
   daily delta pull against an EU-maker database is exactly that pattern.
2. Article 11 limits protection to EU/EEA makers. US aggregators (Interconnection.fyi, Cleanview, Enverus)
   therefore get **no** sui generis protection in the EU — but they get US contract and hot-news protection
   instead, so nothing changes operationally.
3. EU-maker databases in our source list: ENTSO-E TYNDP, the PCI/PMI list, MaStR, EirGrid. TED is
   expressly reused under 2011/833/EU, which overrides. ENTSO-E and EirGrid are the real Article 7 exposure
   because neither publishes an open licence; `sources.yaml` currently records "ENTSO-E terms; attribution"
   and "EirGrid terms" with no quoted clause. Both should be `unknown` until read.
4. Article 10(3) means "rolling" databases can enjoy perpetual protection through successive substantial
   updates. Do not assume a 15-year expiry has run.
5. `01-feasibility.md` §3.2 is right that the database right protects private aggregators; it is incomplete in
   not flagging that **Bankable's own database will attract the sui generis right only if Bankable has an EU
   establishment**. That is an asset-protection argument for the entity decision, and a reason to consider an
   EU subsidiary if the EU feeds become material.

**Confidence: high** on the text; **moderate** on the application to ENTSO-E/EirGrid pending their terms.

### 4.2 DSM Article 4 TDM opt-outs

Directive (EU) 2019/790, source:
https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32019L0790 — retrieved 2026-09-12.

> **Article 4 — Exception or limitation for text and data mining**
> 1. Member States shall provide for an exception or limitation to the rights provided for in Article 5(a) and
> Article 7(1) of Directive 96/9/EC, Article 2 of Directive 2001/29/EC, Article 4(1)(a) and (b) of Directive
> 2009/24/EC and Article 15(1) of this Directive for reproductions and extractions of lawfully accessible works
> and other subject matter for the purposes of text and data mining.
> 2. Reproductions and extractions made pursuant to paragraph 1 may be retained for as long as is necessary for
> the purposes of text and data mining.
> 3. The exception or limitation provided for in paragraph 1 shall apply on condition that the use of works and
> other subject matter referred to in that paragraph has not been expressly reserved by their rightholders in
> an appropriate manner, such as machine-readable means in the case of content made publicly available online.

*Inference:* Article 4 is what makes commercial TDM over lawfully accessible EU content legal at all — and it
disapplies the Article 7(1) database right for that purpose, which is significant. It is switched off
per-rightholder by an Article 4(3) reservation. Two consequences.

**(a) We must detect and honour reservations.** The signals that currently function as Article 4(3)
reservations, in the order a connector should check them:

1. `robots.txt` `Content-Signal:` directives (the Cloudflare-promoted convention). MISO's file states
   expressly that "ANY RESTRICTIONS EXPRESSED VIA CONTENT SIGNALS ARE EXPRESS RESERVATIONS OF RIGHTS UNDER
   ARTICLE 4". Treat `ai-train=no` as a training prohibition and `use=reference` as a reproduction limit.
2. `robots.txt` `Disallow` for AI user-agents (`GPTBot`, `CCBot`, `ClaudeBot`, `Google-Extended`, etc.).
3. `X-Robots-Tag` HTTP headers, including `noai` / `noimageai`.
4. TDM Reservation Protocol (`/.well-known/tdmrep.json`) where present.
5. Express prose reservations in terms of use (PR Newswire's "train a machine learning or artificial
   intelligence (AI) system" clause is one).

**(b) Required engineering.** This is a connector-framework requirement, not a per-source note, and it belongs
in `20-architecture.md`:

- fetch and cache `robots.txt` per host on a ≤24 h TTL; parse `Content-Signal` as well as `Disallow`;
- persist, per source and per fetch, three booleans on every record: `tdm_index_ok`, `tdm_ai_input_ok`,
  `tdm_ai_train_ok`, derived from the signals above;
- the resolver/enricher may call a model on a record only where `tdm_ai_input_ok`;
- **no Bankable-owned model is ever fine-tuned or trained on ingested third-party content**, full stop. This is
  the cheapest way to comply with every `ai-train=no` signal at once and costs us nothing, since the product
  needs inference over records, not a trained model of them;
- a nightly report of sources whose signals changed, so a newly-added reservation stops ingestion rather than
  being silently overridden.

*Note on scope:* Article 4 is EU law and applies to EU-established activity and EU rightholders. We honour the
signals globally anyway — it is a single code path, it is what the publishers are asking for, and a product
that advertises provenance discipline cannot be seen to ignore machine-readable reservations.

**Confidence: high** on the legal reading; **moderate** on the signal list, which is a moving target
(no EU-harmonised standard yet exists; the AI Act's GPAI copyright-policy obligations and the Commission's
work on a reservation standard may change it within a year).

---

## 5. Personal data minimisation — filer contacts

### 5.1 What we actually collect

Personal data enters Bankable in four places, all incidental:

| Where | Typical fields | Volume |
|---|---|---|
| FERC/state docket filings, USACE public notices, siting-board applications | filer name, title, firm, email, phone, signature block | high |
| TED / Find a Tender / grants.gov notices | contracting-authority contact name, email, phone | high |
| ERCOT/CAISO/NYISO queue files | interconnection customer contact in some vintages | low |
| Press releases and trade press | named executives, quoted individuals | medium |

None of it is special-category data. Almost all of it is business-context contact data of people acting in a
professional capacity. That reduces the risk; it does not remove the obligations.

### 5.2 GDPR

- **Article 5(1)(c) data minimisation** — adequate, relevant and limited to what is necessary. Our purpose is
  *identifying and tracking projects*, not *building a contact database*. A person's name is sometimes needed
  for entity resolution (the same agent files for the same developer across states). A person's **direct email
  address and phone number are never needed for that purpose.**
- **Article 6(1)(f) legitimate interests** is the only realistic basis for storing the residue. It requires a
  documented three-part balancing test (legitimate interest / necessity / balancing against the data subject's
  rights), recorded before processing and available on request. B2B contact data in a published public record
  is close to the easy end of that test — but only while the use stays within the reasonable expectations of
  someone who filed a public document.
- **Article 14** — where data is not obtained from the data subject, we must provide the privacy notice within
  a reasonable period and at the latest within one month, or at first communication. Article 14(5)(b)
  disapplies this where provision "would involve a disproportionate effort", which is the usual basis for
  public-register aggregation, **but it requires us to publish the notice and make it easy to find** as a
  compensating measure.
- **Articles 15–21** — access, rectification, erasure and, critically, **Article 21(2) absolute right to
  object to direct marketing**. There is no balancing on that one.

### 5.3 CCPA/CPRA

- The B2B exemption sunset in 2023; business-contact personal information is now fully in scope.
- "Sale" and "share" are defined broadly. **Serving filer contact details through a paid API or CSV export is
  very likely a "sale" of personal information**, triggering the "Do Not Sell or Share My Personal
  Information" link, opt-out signal handling (Global Privacy Control), and notice-at-collection duties.
- Cal. Civ. Code §1798.100(c) imposes a minimisation duty in the statute itself: collection must be reasonably
  necessary and proportionate to the disclosed purposes.
- Applicability thresholds (revenue, volume, or ≥50% revenue from selling/sharing) will not be met at launch
  but the volume threshold is reachable, and several other state statutes have lower or no thresholds.

### 5.4 The rule Bankable adopts

Recorded here as a decision for `00-PLAN.md`:

1. **Drop personal contact identifiers at ingest.** The normaliser strips email addresses, direct phone
   numbers, postal addresses of individuals, and signature-block images before anything is written to the
   store. They are not persisted, not indexed, not embedded.
2. **Retain the minimum identity fields** needed for resolution: person name, role/title, and organisation —
   and only where the person is acting in a professional capacity on a public filing. Flag them
   `personal_data: true` on the record.
3. **Never publish a person's contact details in any tier**, free, Pro, Team or API. Show the organisation and
   link to the source document, which carries the contact for anyone who needs it. This also neutralises the
   CCPA "sale" analysis for contact data, because we do not transfer it.
4. **Never use ingested personal data for outreach.** Outreach contacts come from a separate,
   consent-and-legitimate-interest-documented CRM list, never from the ingestion pipeline. This is an
   architectural separation (different store, no join key), not a policy request. ERCOT's clause 8 (§1.1) is a
   specific instance of the same rule.
5. **Publish a privacy notice** covering Article 14 indirect collection, the LIA summary, retention, and the
   rights routes, before the first public page goes live.
6. **Honour deletion and objection requests within 30 days**, with a documented workflow and a suppression
   record that survives re-ingestion — a deletion that a later crawl silently undoes is not a deletion. The
   suppression list stores a salted hash of the identifier, not the identifier.
7. **Retention:** personal fields retained only while the parent project record is active, and reviewed
   annually. Withdrawn/cancelled projects have their personal fields purged at 24 months.

*Inference:* rules 1–4 together reduce Bankable's GDPR/CCPA posture from "data broker" to "publisher of
project records", which is a materially different regulatory category and a materially cheaper one. The cost
is losing a feature nobody has asked for. **Confidence: high** that this is the right trade.

---

## 6. Per-source publication matrix

Keyed to `data/sources.yaml` ids. "Evidence" = whether an operative clause was quoted in this document.

| `source_id` | Class | Publication rule | Evidence | Confidence |
|---|---|---|---|---|
| `us.iso.caiso.gen_queue` | attribution-restricted | derived-only + credit "California ISO" | §1.2 quoted | mod-high |
| `us.iso.ercot.gen_queue` | permissive | raw-ok | §1.1 quoted | high |
| `us.iso.ercot.large_load_queue` | permissive | raw-ok | §1.1 quoted | high |
| `us.iso.spp.gen_queue` | restricted | **link-out-only** | §1.4 quoted | high |
| `us.iso.nyiso.gen_queue` | attribution-restricted | derived-only | §1.5 quoted | mod-high |
| `us.iso.isone.gen_queue` | restricted | **link-out-only** | §1.6 quoted | mod-high |
| `us.iso.pjm.gen_queue` | restricted (DM2) / attribution-restricted (planning pages) | link-out-only now; derived-only from planning pages after counsel | §1.3 quoted ×3 | mod |
| `us.iso.miso.gen_queue` | **unknown** | link-out-only; do not publish | §1.7 not retrieved | n/a |
| `us.lbnl.queued_up` | **unknown** (CC BY unverified) | derived-only, credit LBNL + GridTracker | §2.1 not retrieved | low |
| `us.gridtracker.interconnection_fyi` | restricted | do not ingest at all | ToS + hot-news §3.4 | high |
| `us.eia.860m` | public-domain | raw-ok | 17 U.S.C. §105; §2.11 quoted | high |
| `us.eia.api` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.eia.form923` | public-domain | raw-ok | 17 U.S.C. §105; §2.11 quoted, applied at §2.15 (robots-disallowed `archive/`; final-release rule); added 2026-09-19 by the features lane | high |
| `us.oasis.non_iso_queues` | unknown (per-utility) | derived-only; read per-utility terms before each connector | not retrieved | low |
| `us.ferc.elibrary` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.ferc.pipelines_pending` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.doe.lng_exports` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.epa.eis_database` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.permits_dashboard` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.blm.eplanning` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.boem.renewables` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.usace.orm_public` | public-domain | raw-ok; strip personal data (§5) | 17 U.S.C. §105 | high |
| `us.epa.class_vi` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.nrc.adams` | public-domain | raw-ok; API key = clickwrap, read it | 17 U.S.C. §105 | high |
| `us.regulations_gov` | public-domain | raw-ok; API key = clickwrap, read it | 17 U.S.C. §105 | high |
| `us.state.siting_boards` | unknown (per-state) | derived-only; strip personal data | not retrieved | low |
| `us.state.puc_dockets` | unknown (per-state) | derived-only; strip personal data | not retrieved | low |
| `us.utility_rfps` | unknown (per-issuer) | derived-only from issuer sites; **never** Energy Adepto | not retrieved | low |
| `us.muni_procurement` | restricted (aggregators) / unknown (state portals) | link-out-only for aggregators | not retrieved | mod |
| `us.grants_gov.search2` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.sam_gov.opportunities` | public-domain | raw-ok; API key = clickwrap | 17 U.S.C. §105 | high |
| `us.doe.exchange_portals` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.usda.rd_energy` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `us.usaspending` | public-domain | raw-ok | 17 U.S.C. §105 | high |
| `gb.neso.tec_register` | open-attribution | raw-ok + exact string "Supported by National Energy SO Open Data" | §2.5 quoted | high |
| `gb.pins.nsip` | open-attribution (OGL v3) | raw-ok + OGL credit | §2.4 quoted | high |
| `gb.find_a_tender` | open-attribution (OGL v3) | raw-ok + OGL credit | §2.4 quoted | high |
| `eu.ted.api` | open-attribution (metadata CC0) | raw-ok; strip personal data | §2.3 quoted | high |
| `eu.entsoe.tyndp` | **unknown** | derived-only; EU database right applies (§4.1) | not retrieved | low |
| `eu.pci_pmi_list` | open-attribution (EU reuse) | raw-ok | §2.3 by analogy | mod |
| `de.bnetza.mastr` | open-attribution (dl-de/by-2-0) | raw-ok + credit | not re-verified | mod |
| `ie.eirgrid.connections` | **unknown** | derived-only; EU database right applies | not retrieved | low |
| `ca.ieso.connection_status` | unknown | derived-only | not retrieved | low |
| `ca.aeso.connection_list` | unknown | derived-only | not retrieved | low |
| `ca.iaac.registry` | open-attribution (OGL-Canada) | raw-ok + credit | not re-verified | mod |
| `au.aemo.connections_scorecard` | attribution-restricted (non-modification term recorded) | derived-only | not re-verified | low |
| `au.dcceew.epbc_referrals` | open-attribution (CC BY 4.0) | raw-ok + credit | not re-verified | mod |
| `in.seci_mnre.tenders` | unknown | derived-only | not retrieved | low |
| `br.aneel.leiloes` | unknown | derived-only | not retrieved | low |
| `za.ipp_office.reipppp` | unknown | derived-only | not retrieved | low |
| `mdb.worldbank.procnotices` | open-attribution (CC BY 4.0) | raw-ok + credit | not re-verified | mod |
| `mdb.worldbank.projects` | open-attribution (CC BY 4.0) | raw-ok + credit | not re-verified | mod |
| `mdb.others` | unknown (per-institution) | derived-only | not retrieved | low |
| `global.gem.trackers` | open-attribution (CC BY 4.0) **except TZ-ID rows (CC BY-NC 4.0)** | raw-ok + recommended citation; **drop TZ rows at ingest** | §2.2 quoted | high |
| `us.ourgridfuture.transmission_projects` | **unknown** | do not ingest | §2.9 not retrieved (403/429) | n/a |
| `global.iea.demo_projects` | unknown (IEA CC licences are dataset-specific) | derived-only | not re-verified | low |
| `news.gdelt.doc` | open-attribution (GDELT) / third-party copyright in article text | derived-only: metadata + link; never article body | not re-verified | mod |
| `news.google_rss` | restricted (robots `Disallow`) | **link-out-only — recommend dropping the connector** | §2.6 quoted | high |
| `news.wires` | restricted (PR Newswire) / unknown (Business Wire) | **link-out-only**; no abstracts of PRN releases | §2.7, §2.8 | high / n/a |
| `news.trade_press` | restricted (publisher copyright) | link-out-only | not re-verified | high |
| `social.bluesky` | platform terms — see `13-legal-outreach-and-social.md` | n/a (outbound) | see doc B | — |
| `social.x` | platform terms — see doc B | n/a (outbound) | see doc B | — |
| `social.linkedin` | platform terms — **API automation of posting is prohibited under the self-serve API terms**; see doc B | n/a (outbound) | see doc B §5.1 | high |
| `social.meta` | platform terms — see doc B | n/a (outbound) | see doc B | — |
| `social.reddit` | restricted — commercial API use requires a paid agreement; see doc B | n/a (outbound) | see doc B §5.5 | mod |
| `social.owned` | n/a — our own channel | raw-ok | — | high |
| `us.eia.860` | public-domain | raw-ok | 17 U.S.C. §105; §2.11 quoted | high |
| `us.eia.atlas.gas_processing_plants`, `us.eia.atlas.gas_storage`, `us.eia.atlas.lng_terminals`, `us.eia.atlas.ethanol_plants` | public-domain | raw-ok; Atlas `licenseInfo` unread (browser task) | 17 U.S.C. §105; §2.11 quoted | high |
| `us.eia.ethanol_capacity` | public-domain | raw-ok | 17 U.S.C. §105; §2.11 quoted | high |
| `us.epa.lmop`, `us.epa.agstar`, `us.epa.rfs_public_data` | public-domain | raw-ok, cover-sheet check per workbook | §2.12 quoted (EPA hedge); §2.16 records that the RFS rows come from EPA's Part 80 registration list, same publisher | mod-high |
| `us.anl.rng_database` | **unknown** | do not store; gated until terms read | §2.14 not retrieved | n/a |
| `us.phmsa.pipeline_operator_reports` | public-domain | raw-ok | 17 U.S.C. §105; §2.16 quotes the origin's 403 and records the archive fallback (§2.14 superseded on the nature of the block) | high |
| `global.gleif.lei` | open (CC0) | raw-ok | §2.13 quoted | high |
| `us.eia.atlas.gas_pipelines` | public-domain | raw-ok; Atlas `licenseInfo` unread (browser task) | 17 U.S.C. §105; §2.11 quoted; added 2026-09-18 from the manifest entry, not re-verified | high |
| `us.epa.rblc` | public-domain | raw-ok; dashboard terms unread before ingest | 17 U.S.C. §105; §2.12 by analogy; added 2026-09-18 from the manifest entry, not re-verified | mod-high |
| `gb.neso.fes_gsp_gazetteer` | open-attribution | raw-ok + exact string "Supported by National Energy SO Open Data" | §2.5 quoted (same licence id as the TEC register per the dataset's package_show); added 2026-09-18 | high |
| `us.census.cartographic_boundaries` | public-domain | raw-ok | 17 U.S.C. §105 (same basis as the vendored Gazetteer); added 2026-09-18 | high |
| `curated.organization_parents` | permissive | raw-ok | Not a third-party dataset: parent links curated by Infraque from each company's own published statements, one URL per rule in `data/vendored/organizations/parents.yaml` (first rule: Tallgrass Energy's natural-gas page, read 2026-09-19); facts, not expression; added 2026-09-19 | high |
| `curated.organization_aliases` | permissive | raw-ok | Not a third-party dataset: alias rules curated by Infraque from public filings, one URL per rule in `data/vendored/organizations/aliases.yaml` (all seven rows cite a `data.sec.gov` submissions document); facts, not expression; added 2026-09-20 | high |

**This matrix is machine-read.** `scripts/check_manifest_licences.py` (run by `tests/test_manifest_licences.py`
under the pytest job) parses every row above whose first cell is one or more backticked `source_id`s, takes the
strictest class named in the Class cell and the strictest rule named in the Publication rule cell, and fails
when `data/sources.yaml`'s `reuse` or `publication` is more permissive. Keep the vocabulary of §0 in these two
cells; prose belongs after it, as in the PJM and GEM rows.

### 6.1 Manifest reconciliation, 2026-09-18

The 2026-09-18 audit (`docs/50` §3.1) found fourteen sources this matrix classifies `unknown` recorded
`reuse: attribution` in `data/sources.yaml`, the field the loader and the API gate on, so their rows were
publishable raw on the free tier with no terms read. Each is now `reuse: unknown`, `publication: none` in the
manifest, gated until its terms are retrieved and quoted here. Per source, the register row that governs:

| `source_id` | Manifest before | Register row (this document) | Manifest now |
|---|---|---|---|
| `us.lbnl.queued_up` | attribution | §6: `**unknown** (CC BY unverified)`, derived-only; §2.1 not retrieved | unknown / none |
| `us.oasis.non_iso_queues` | attribution | §6: `unknown (per-utility)`, derived-only; read per-utility terms first | unknown / none |
| `us.state.siting_boards` | attribution | §6: `unknown (per-state)`, derived-only | unknown / none |
| `us.state.puc_dockets` | attribution | §6: `unknown (per-state)`, derived-only | unknown / none |
| `us.utility_rfps` | attribution | §6: `unknown (per-issuer)`, derived-only from issuer sites | unknown / none |
| `eu.entsoe.tyndp` | attribution | §6: `**unknown**`, derived-only; EU database right §4.1 | unknown / none |
| `ie.eirgrid.connections` | attribution | §6: `**unknown**`, derived-only; EU database right | unknown / none |
| `ca.ieso.connection_status` | attribution | §6: `unknown`, derived-only | unknown / none |
| `ca.aeso.connection_list` | attribution | §6: `unknown`, derived-only | unknown / none |
| `in.seci_mnre.tenders` | attribution | §6: `unknown`, derived-only | unknown / none |
| `br.aneel.leiloes` | attribution | §6: `unknown`, derived-only | unknown / none |
| `za.ipp_office.reipppp` | attribution | §6: `unknown`, derived-only | unknown / none |
| `mdb.others` | attribution | §6: `unknown (per-institution)`, derived-only | unknown / none |
| `global.iea.demo_projects` | attribution | §6: `unknown (IEA CC licences are dataset-specific)`, derived-only | unknown / none |

The register's `derived-only` rule for these fourteen described what *would* be allowed once terms are read; with
class `unknown` the loader refuses the source altogether (`CLAUDE.md`: unknown is treated as restricted), so
`none` is the only manifest value consistent with the class. Re-opening any of them is a register edit first
(quote the terms, set the class), then the manifest.

Four sources present in the manifest had no row here and were added above from their manifest `license` text
without fresh retrieval: `us.eia.atlas.gas_pipelines`, `us.epa.rblc`, `gb.neso.fes_gsp_gazetteer`,
`us.census.cartographic_boundaries`. Their evidence cells say so.

The manifest's `publication` field (`raw_ok | derived_only | none`) is new on every source (2026-09-18): it
replaces the loader's free-text match on "derived-only" in `notes`. CAISO, NYISO (`attribution-restricted`),
AEMO (non-modification term) and GDELT (metadata + link only) are `derived_only`; every `restricted`/`unknown`
source is `none`; the rest are `raw_ok`.

**Tier-1 MVP consequence.** Of the seven US ISO queues, only ERCOT (×2) is clearly publishable raw at launch.
CAISO and NYISO are publishable derived. SPP, ISO-NE, MISO and PJM are link-out-only until resolved. A US-first
launch that leads with "every interconnection queue" is not supportable on 2026-09-12 evidence. The honest
public framing is **"every queue tracked; ERCOT, CAISO and NYISO published; PJM, MISO, SPP and ISO-NE linked
pending licence"** — which is also a better sales story than a silent gap, and it sets up the licence
conversations.

**Pricing-ladder consequence** for `11-market-and-competition.md` §3: the "Daily or intraday → ISO queues
(CAISO/ERCOT/SPP/NYISO/ISO-NE) → 14-day free lag" row must be narrowed to CAISO/ERCOT/NYISO. The delayed-tier
premium is unaffected for those three. SPP and ISO-NE contribute change-event alerts (Pro) via link-out only,
which is still saleable — the alert says "SPP queue position GEN-2026-xxx changed status; see SPP" — because
the *event* is our observation, not SPP's content. **Confidence: moderate** that the event-without-content
alert survives SPP's clause; it is item 6 in §7.

---

## 7. Items requiring counsel before launch

Numbered, in the order they block work.

1. **PJM: does the pjm.com planning-page New Services Queue fall outside the Data Miner 2 Terms of Use?**
   §1.3 sets out the three conflicting documents. If the answer is yes, PJM rows become publishable
   derived-only without a Redistribution Licence and the largest US market opens at launch. If no, the
   Redistribution Licence enquiry in `00-PLAN.md` becomes a hard gate. This is the single highest-value legal
   question in the project. Ask it with the three quoted documents attached.

2. **MISO: obtain and read the terms.** Not a legal question so much as a task counsel or a human can do in a
   browser in five minutes: open `https://www.misoenergy.org/meet-miso/legal-and-privacy/`, save a PDF, and
   return the operative clauses. Until then no MISO row may be published. Also confirm whether MISO's
   `ai-train=no` content signal has any bearing on our (non-training) inference use.

3. **LBNL "Queued Up": confirm the licence.** Verify whether the data file is CC BY 4.0 and whether GridTracker
   imposes any additional term, given that GridTracker sells the same data through Interconnection.fyi. If it
   is CC BY, get the exact required attribution string. If it is not, the dataset drops out of the plan.

4. **Google News RSS: confirm the recommendation to drop the connector.** §2.6. Counsel should confirm that
   polling a `Disallow`ed path is an unacceptable risk posture for a company that markets provenance
   discipline, and sign off on the GDELT + issuer-RSS replacement.

5. **Business Wire: obtain and read the terms of use.** §2.7 — currently unretrievable from automated clients.
   Needed before any wire connector ships.

6. **Change-event alerts over restricted sources.** May Bankable publish "ISO-NE queue position X changed from
   *Feasibility Study* to *System Impact Study* on date D — see ISO-NE" as a Pro alert, where ISO-NE's terms
   say any duplication of Content may violate copyright and SPP's bar commercial use of materials? My reading
   is that a factual observation of change, plus a link, is not reproduction of their content, and that facts
   are uncopyrightable under *Feist*. This underpins a large part of the Pro value proposition and needs a
   real opinion, not mine.

7. **Terms-of-service breach exposure generally, given documented actual knowledge.** §3.2. Counsel should
   advise on whether publishing this register materially changes exposure and, if so, how to structure the
   ingestion policy (for example, a documented compliance procedure, a named responsible person, and evidence
   of honouring restrictions) so that knowledge is mitigating rather than aggravating.

8. **Customer terms and API licence pass-through.** The Team/API tiers promise "licence pass-through for
   restricted sources" (`11-market-and-competition.md` §3). That requires (a) customer terms binding
   subscribers to each upstream source's restrictions, (b) a per-record licence field surfaced through the API,
   and (c) an indemnity position — note PJM's clause 9 makes us indemnify PJM for third-party claims arising
   from *our customers'* use of derivatives. Draft before the first paid contract.

9. **Entity and forum.** §3.5 — accepting PJM, CAISO-API and ERCOT clickwraps commits the company to three
   exclusive US forums; NESO to England and Wales. Feeds `00-PLAN.md` open question 3. Also: whether to
   establish an EU entity so Bankable's own database attracts the sui generis right (§4.1).

10. **Privacy notice, legitimate-interests assessment and CCPA "sale" analysis.** §5. The LIA must exist in
    writing before ingestion of any docket containing filer contacts. Counsel should confirm that the §5.4
    minimisation rules take Bankable outside "data broker" registration requirements in California, Texas,
    Oregon and Vermont.

11. **ENTSO-E and EirGrid terms** (§4.1) — EU-maker databases with no quoted licence and live Article 7(5)
    exposure from systematic delta pulls. Read before either connector ships.

12. **Hot-news and competitor-signal policy** (§3.4). Confirm the rule that competitor outputs may inform
    *which sources exist* but must never trigger a fetch-and-publish. This is partly an ethics rule and
    partly a tort exposure; it should be written into the runbook either way.

---

13. **EPA databases: confirm the §105 reading against EPA's copyright hedge** (§2.12). LMOP, AgSTAR and the
    RFS public tables are the RNG and ethanol registries for the midstream and fuels wave (`00-PLAN.md`
    2026-09-18). EPA's disclaimer page says commercial use of "documents available from the EPA websites may
    be protected"; our reading is that EPA-authored databases are §105 works and the hedge covers third-party
    documents. Confirm before the RNG layer is published; low legal risk, but it is the one federal source
    whose own wording does not say "public domain".

## 8. What changed in the repo as a result

- `data/sources.yaml`: `license`, `reuse` and `notes` updated for the twelve sources where a verbatim licence
  clause was obtained (CAISO, ERCOT ×2, SPP, NYISO, ISO-NE, PJM, TED, NESO, GEM, Google News RSS, wires); `notes`
  only for MISO (verbatim robots.txt content-signal; terms still unknown) and LBNL (verbatim DOE contract notice;
  CC BY claim left in place but marked unverified). No other fields touched.
- `01-feasibility.md` §3.2 is now partly superseded: PJM's terms are more permissive than recorded and SPP's
  are far less. That section should be annotated by whoever next edits it; I have not edited another agent's
  document.
- `11-market-and-competition.md` §3 delay schedule needs the ISO row narrowed (see §6 above).
- `00-PLAN.md` decisions log should record the §5.4 personal-data rule and the §4.2 TDM-signal rule.
