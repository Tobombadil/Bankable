# 38 — Invenergy briefing pack: what we hold, what they claim, what to ask

**Status:** briefing for the owner's call. No message is drafted or sent here. Store numbers were re-derived by this
lane on 2026-10-21 from the full local build (`study.db`, built 2026-10-20 from the sources production loads:
EIA-860M August 2026 release, EIA-860 2025, GHGRP RY2023), read-only. **"After the fix"** means the same build with
EIA-860M operator edges loaded (`sim.db`; the fix is in flight, not in production; docs/16 §5, backlog U-3).
**Reads:** docs/16 §5; docs/22 §21, §23, §24; docs/26 §7; docs/64 §2 step 9; docs/13-legal-outreach-and-social §7;
docs/33 §1.1; `data/match_rules.yaml`; `data/sources.yaml`; `data/vendored/organizations/developer_parents.yaml`.
**Invenergy's site:** robots.txt and four pages, each read once on 2026-10-21 to quote the wording. Nothing was
ingested or kept, and nothing from the project list was counted (see §3).

---

## 1. The gap in one table

| | Inframation today | After the fix | Invenergy states |
|---|---|---|---|
| Operating plants linked to the group | **8 plants, 4,143 MW** | **70 plants, 12,871 MW** | — |
| In construction (their status filter) | 10 projects (12 records), 2,840 MW | same | — |
| **Comparable to their list** (operating + in construction) | **18 projects, 7.0 GW** | **80 projects, 15.7 GW** | **"232 projects worldwide", "38 GW"** |
| Earlier stages (announced, filed, studied, permitted) | 18 projects (20 records), 4,965 MW | same | not listed: statuses are "COD Achieved", "Contracted", "In Construction" |
| Withdrawn queue requests | 29 records, 4,880 MW | same | not listed |
| Countries | US, GB | US, GB | 9 (§3) |

So even after the fix, Inframation holds about a third of their project count and about 40% of their GW. It also
holds something they do not publish: 20 early-stage records and 29 withdrawals. **Contracted** is 0 in our records:
no register we load says a project has an offtaker.

## 2. What Inframation holds (re-derived, 2026-10-21)

- **The group: 15 organisations.** Fourteen sit under INVENERGY LLC: 13 subsidiaries linked by
  `developer_parents.yaml`, each because its name carries "Invenergy" and an Invenergy page backs the link
  (docs/22 §24). The fifteenth, **Invenergy Nelson Expansion LLC** (380 MW, IL), is outside the tree.
- **Proposals: 62 records, 12,701 MW.** Sources: NYISO 31, EIA-860M 22, NESO TEC 7, ERCOT 3. Lifecycle: withdrawn
  29 (4,880 MW); under construction 12 (2,840); filed 7 (1,303); announced 6 (1,538); studied 5 (2,001); permitted 2
  (123); built 1 (16). Technology: solar 22 (3,911 MW); wind 13 (2,005); gas CT 7 (3,355); storage 12 (878); wind
  plus storage 6 (1,152); transmission 1 (1,300, CPNY-MH, withdrawn); unknown 1 (100). Place: NY 32 (29 of them
  withdrawn); GB 7 (1,312 MW; 6 of the 7 unplaced, because NESO gives only a connection substation); WI 7; TX 4;
  AZ 4; IN, OH, NM, AR, MI, NC and OR 7 between them.
  These are records, not projects. Foundry Ridge appears as three 108 MW unit rows, and Hashknife I and II each
  appear as a solar row plus a storage row.
- **Where the live pipeline comes from:** 22 of the 32 active records come from EIA-860M, which lists units close
  to construction. Only 4 are live US queue positions (3 ERCOT, 1 NYISO). We see Invenergy's US pipeline late.
- **Operating plants, today: 8 (4,143 MW).** Gas CC 3 (2,841 MW), gas CT 2 (767), wind 3 (535). All 8 come from
  owner edges: 5 from GHGRP parent strings and 3 from EIA-860 Schedule 4. With Nelson Expansion it is 9 (4,523 MW).
- **After the fix: 70 (12,871 MW) in 25 states.** Wind 34 (4,984 MW), solar 23 (3,126), gas 7 (4,458), storage
  5 (282), biomass 1 (21). TX 16 (3,278 MW) and IL 9 (2,152) lead. **Correction to the brief:** EIA names
  "Invenergy Services LLC" as operator of 69 of the 70, not all 70. The 70th, Black Oak Wind Farm (78 MW, MN), comes
  from an EIA-860 owner row, and its EIA operator is "Black Oak Wind, LLC". One retired plant also gains an
  operator edge (Buffalo Mountain, 27 MW, TN, retired 2024).
- **Matches on their records: 7, all withheld.** All seven pair the Scottish wind rows with one Find a Tender
  notice, "Onshore Wind Energy Development – Northumberland"
  (https://www.find-tender.service.gov.uk/Notice/095821-2026; we have read its title only). As a project match it
  is wrong, because the projects are in Scotland and the site is in England. As a lead for the company it may be
  exactly the signal the owner wants to pitch (§6, Q7).

## 3. What Invenergy says publicly, and what its terms allow

- **Projects page.** https://invenergy.com/projects/our-projects answers HTTP 307 to
  https://wwww.invenergy.com/projects/our-projects. It shows "12 M homes powered", "38 GW power generated" and
  "232 projects worldwide", above the line "take a look at all our completed or in-progress projects". Filters:
  wind, solar, natural gas, storage. Statuses: COD Achieved, Contracted, In Construction. Countries: Canada, El
  Salvador, Japan, Mexico, Poland, Scotland, U.S., United Kingdom, Uruguay. The home page
  (https://invenergy.com/) says "232 projects", "38 gigawatts" and "4 continents". The history page
  (https://wwww.invenergy.com/who-we-are/our-history) describes "North America's largest privately held developer,
  owner and operator of innovative power infrastructure with more than 230 projects across four continents."
- **What 38 GW counts: not stated.** "Power generated" is a capacity unit (GW) used as a generation label. The
  page's embedded statistics name 31 GW as "gw_of_renewable_energy_developed", next to a 38 GW total. Our inference,
  moderate confidence: 38 GW is developed-to-date across all technologies. No page says whether it includes sold
  projects. The page's project data also marks "service" projects and an ownership type, and "Third-Party" appears
  as a value, so the 232 probably includes plants that others own and Invenergy Services runs. We did not count
  them; the terms forbid mining the list.
- **Terms of Service**, https://wwww.invenergy.com/legal/terms-of-service, read 2026-10-21. Verbatim:
  > None of the information, content or material from the Invenergy website … may be copied, reproduced,
  > republished, uploaded, posted, transmitted or distributed in any way without our written consent.

  > Invenergy's website is for Visitor's personal use only. Visitors may not use Invenergy's website for
  > commercial purposes …

  > Visitor may not use any software, bots, spiders, web-crawlers or any other similar data mining software,
  > tools or applications to monitor, copy, or otherwise mine any information contained on the Invenergy
  > website without prior express permission of Invenergy.

  > Visitor may not use Invenergy's website or any of the content obtained from its website … for the purpose
  > of competing with Invenergy or any of its affiliates or subsidiaries.

  robots.txt allows every path ("Disallow:" is empty), but CLAUDE.md makes the site terms the rule. **Verdict:
  Inframation may not ingest the list, or monitor it, without Invenergy's prior written permission.** The
  underlying facts may not be protected by copyright, but the terms bind a scraper whatever their enforceability,
  which is a counsel question. **One exposure to raise:** the 13 subsidiary links in `developer_parents.yaml` cite
  invenergy.com pages retrieved on 2026-10-16. A written confirmation of the group list (§5, ask d) would replace
  that evidence.

## 4. Why the gap exists, quantified

| Cause | Evidence | Size |
|---|---|---|
| **Missing operator edges** (fix in flight) | No EIA-860M power plant has an operator edge (docs/16 §5), yet EIA-860M names Invenergy Services LLC on 69 | 8 → 70 plants; 4.1 → 12.9 GW |
| **Countries not ingested** | Proposal registers are loaded for US and GB only. Canada's IESO and AESO are `unknown` with no connector; Mexico, El Salvador, Uruguay, Japan and Poland have no proposal register (`data/sources.yaml`) | 6 of their 9 countries: 0 records |
| **Link-out-only queues** | PJM, MISO and SPP rows are unpublished until terms clear (CLAUDE.md; docs/13) | 29 of the 70 plants (6,641 MW, 52%) sit in those balancing areas, so Invenergy's queue positions there show only once EIA-860M lists them (14 of the 62 records) |
| **Queues without developer names** | CAISO publishes no developer name: 214 active CAISO rows (61.5 GW) carry no sponsor | Any CAISO position cannot be attributed |
| **Project-company names** | Project LLCs are never linked without a document (docs/22 §24 rule 4) | 11 queue rows (1,676 MW, all built) behind plants Invenergy now operates sit under 10 project names (Delilah Solar Energy LLC, Samson Solar Energy II LLC, Sheldon Energy, LLC …) plus 1 unnamed CAISO row |
| **Developed and sold** | Registers name the current owner | 8 of the 70 (2,574 MW) name another owner: WEC Energy Group 90% at Jayhawk and Sapphire Sky; AEP Renewables 75% at Harry Allen Solar; Hardee Power Partners at Hardee; Marubeni 49% at Cannon Falls. A project sold outright and run by someone else carries no Invenergy name: **unknowable without them** |

## 5. The ask and the offer

**Ask** (the model is the PJM request, docs/37: attributed publication on a free, noncommercial site, plus a
statement of what applies if paid tiers start later):

- **(a) A project list under written terms that let us publish it with attribution.** It would be a new source with
  its own licence row and the same provenance quartet as every record. Every field it supplies would be labelled
  **"company-reported"**. It would never overwrite a register fact; where the two disagree, the page shows both
  (the precedent is docs/22 §23, which shows the EIA total beside the queue capacity). Today's `/submit` form takes
  one project at a time into review; bulk feeds and self-service editing are "Later" (docs/10 §2).
- **(b) A named role mailbox for corrections.** A role address is enough; we do not need a person's details.
- **(c) Discovery on the matching use** (§6).
- **(d) Confirmation of the 13 group companies, and of which project LLCs (and the GB rows' sites) are theirs.**

**Offer, stated honestly:**
- A group company page that already gathers 62 records from 14 entities. It shows withdrawals and early-stage
  positions their own list omits.
- After the fix, it shows the 70 plants Invenergy operates. Do not promise a date.
- **Matching is early.** It is withheld from readers (`data/match_rules.yaml` `publish: false`). Precision measured
  0.25 (5 of 20; 95% CI 0.11–0.47) on labels written by the lane that built the matcher, against a bar of 0.7
  (docs/22 §21). The demand side has no utility RFPs, no state solicitations and no large-load requests. In their
  markets: US 121 open federal funding notices (116 untagged), GB 16, Poland 110 (EU TED), Canada, Mexico, El
  Salvador, Uruguay and Japan 0. Within 50 miles of their plants we have 57 placed data-centre records from air
  permits, but 54 are built and 3 unknown: these are existing sites, not requests.
- **Alerts:** free accounts get 10 daily or weekly digests (docs/26 §7), but email is not live (docs/64 §2 step 9),
  and an alert follows one company record, not the 14-entity group. **No prices:** paid tiers are off (docs/26).

## 6. Discovery questions

1. Your 232 is "completed or in-progress". How many of those does Invenergy own today, how many has it sold, and
   how many does it only service for a third-party owner?
2. Does 38 GW count capacity developed to date, sold projects included? Does it include the 31 GW of renewables?
3. Registers name other owners at 8 of the 70 plants Invenergy Services runs (WEC at Jayhawk and Sapphire Sky, AEP
   at Harry Allen). How should a page describe a plant you developed, sold and still operate?
4. We see your US pipeline mostly once EIA-860M lists it: 22 of 32 live records. Which early-stage positions would
   you accept seeing published, and which would you rather we never infer?
5. Would you put a list under written terms, with the "company-reported" label and both values shown when a
   register disagrees? What would legal need to see first?
6. Which opportunity types do you track: utility RFPs, state solicitations, federal funding, CfD or other national
   auctions, large-load or data-centre requests near your plants? Which of these did you find late last year?
7. Take the seven Scottish wind rows we match to an England (Northumberland) site tender. Would you want a
   company-level lead like that, or only matches to a named project?
8. How do you find opportunities today: team, consultants, paid data? What does one missed RFP or deadline cost?
9. Which market first: Canada, Poland, Mexico, or more depth in PJM, MISO and SPP?
10. Who should corrections go to, by role, and how fast do you need them made?

## 7. Three things not to claim

1. **That we cover their portfolio.** Today we have 8 operating plants. After the fix we have 70, in 2 of their 9
   countries, and no PJM, MISO or SPP queue rows.
2. **That matching or alerts work for them now.** Matches are withheld at 0.25 precision, email is not live, and
   no large-load request feed exists.
3. **Any price, paid tier, SLA, API commitment or partnership.** The posture is noncommercial and paid tiers are
   off. Their terms also bar using their names or marks without written consent, so they are not a "partner" in
   any copy.

**Guardrails.** Any follow-up is an L1 draft that the owner reads and sends themselves (docs/13-legal-outreach-and-
social §7). It states the owner's name and role, "Inframation", why they are writing, what they want and how to stop. A
postal address is needed if the email pitches (docs/13 §1), and none is set yet (docs/13 §6.5, decision D5). Send
no unsolicited deck or ideas: their site terms deem unsolicited "pitches, ideas, concepts" assigned to Invenergy.
How far that clause reaches beyond the website is a counsel question. Call notes go into Attio at company and role
level only (docs/33 §4.3).
