# GTM and sales playbook

**Status:** Sprint 0 deliverable (Phase 1, GTM) · 2026-09-12 · owner: sales-bd · reviewed by: owner
**Depends on:** `01-feasibility.md` §3.1, §3.4, §5, §6 (price anchors, TAM bound, validation gates);
`02-data-sources.md` §5 (event vocabulary); `03-agent-operating-model.md` §4 (human gates).
**Rule of this document:** agents research, score, draft and report; a human sends, connects, calls and negotiates.
No undisclosed AI personas. Company- and role-level information only; no personal data about individuals.

## 0. What we are selling, in one paragraph

A continuously updated graph of energy and infrastructure proposals (supply) fused with RFPs, funding and tenders
(demand), with a change feed. The buyer pays for *timeliness on changes that affect a deal they are working*: a
competitor's queue position changing, an RFP opening in a footprint they can serve, a funding award being cancelled
or reinstated, a large-load request landing in a service territory. Free delayed tier for reach; Pro for the
individual originator; Team/API for the desk that needs it in their own tools. Downstream, the same events feed
Bankable's route-to-capital workflow (`01-feasibility.md` §5). The TAM is narrow (on the order of 1,000–2,000 US
organisations that develop, finance, build or advise on utility-scale projects, §3.4), so this is an account-based
motion, not a funnel.

## 1. Ideal customer profiles (six segments)

Trigger events are the `event.event_type` values emitted by the run-time pipeline (`02-data-sources.md` §5; loop
step 9 in `03-agent-operating-model.md` §3). The names below are the proposed event vocabulary for the lead scorer:

```
proposal.first_seen          new queue/docket/permit record resolved to a new proposal
proposal.status_change       lifecycle_state moved (e.g. studied → permitted; active → withdrawn)
proposal.withdrawn           terminal negative
proposal.milestone_filed     LGIA/FERC filing, NEPA notice, siting-board decision, EIA-860M appearance
load.request_filed           large-load / data-centre interconnection request or docket
opportunity.opened           RFP / FOA / tender / auction published
opportunity.due_soon         ≤21 days to due date
opportunity.awarded          award announced (winner org resolved where public)
opportunity.cancelled        funding cancelled or frozen
opportunity.reinstated       cancellation reversed (litigation or agency action)
match.created                proposal ↔ opportunity match above threshold
org.first_seen               an organisation appears in our graph for the first time
org.activity_spike           ≥3 events on one org's proposals in 30 days
```

### 1.1 Developers and IPPs

| | |
|---|---|
| Who | Utility-scale developers and IPPs (solar, storage, gas, wind, geothermal, nuclear SMR, transmission, CCS) with ≥1 active queue position or docket. Sub-segments: national IPPs (portfolio desks), regional developers (2–20 projects), and land/greenfield originators. |
| Timely when | `proposal.status_change` or `proposal.milestone_filed` on *their own* project (they are in the news, and will want to know who saw it); `proposal.withdrawn` on a *competitor's* project in the same cluster or county (freed capacity); `opportunity.opened` in a state/ISO where they hold positions; `match.created` between their proposal and an RFP; `opportunity.cancelled` on a programme they had applied to. |
| Buyer / champion | Buyer: VP/Head of Origination or Development; Director of Business Development. Champion: origination manager or development analyst who currently tracks queues by hand in spreadsheets. Economic approver: CCO/CDO. |
| Job to be done | "Tell me first when something changes on a project, an RFP or a funding line that affects a deal I am working, so I can call the counterparty before my competitor does." Secondary: "Give me a clean, cited pipeline view across ISOs and non-ISO utilities without paying Enverus prices." |
| Objections | "We already have Interconnection.fyi (free) / Cleanview / Enverus." · "Our analysts do this already." · "Queue data is stale and noisy." · "Another dashboard nobody will open." · "Why not wait for GridTracker to add RFPs?" |
| Proof needed | A cited change event on one of their own projects that arrived before they knew (the "you didn't know this yet" test). Side-by-side: our fused record vs the raw queue row. Precision on entity resolution for their portfolio (≥90% of their projects correctly stitched). Attribution and licence line on every record. |

### 1.2 Lenders, tax equity and infrastructure funds

| | |
|---|---|
| Who | Project-finance banks, tax-credit transfer buyers/brokers, infrastructure and energy-transition funds, DFI/green-bank teams, credit funds doing development-stage capital. |
| Timely when | `proposal.milestone_filed` (LGIA executed, NEPA decision, siting approval) on a sponsor they lend to or are courting; `opportunity.awarded` (award = bankable revenue); `opportunity.cancelled` / `reinstated` on a federal programme in a borrower's stack; `org.activity_spike` (a sponsor accelerating); `proposal.withdrawn` in a portfolio they hold (covenant / watchlist). |
| Buyer / champion | Buyer: Head of Origination (project finance), Portfolio Manager, Head of Investments. Champion: associate/VP who runs the screening list and asset-monitoring spreadsheets. Approver: MD / IC chair. |
| Job to be done | "Screen the universe of projects reaching financeable milestones, and monitor my book for adverse status changes, from primary sources I can cite to IC." |
| Objections | "We get deal flow from sponsors and advisors; we don't need a screen." · "Compliance won't allow unvetted data in the IC memo." · "Enverus/Halcyon already sit on the desk." · "The signal is in dockets, not queues." |
| Proof needed | Provenance on every field (source, URL, retrieved_at, licence) so it can be cited in a memo. A back-test: events we would have emitted for three of their known deals, dated, versus when the news reached them. Export/API in a format their model ingests. |

### 1.3 Utilities, co-ops and CCAs

| | |
|---|---|
| Who | IOUs (resource planning / procurement), G&T co-ops (60–64 nationally) and their distribution members (830 per NRECA), statewide co-op associations, municipal utilities and joint action agencies, the 25 California CCAs and CCAs in ten other states. This is the owner's home network. |
| Timely when | `opportunity.opened` by a *peer* utility (benchmarking their own procurement); `load.request_filed` in their territory or a neighbour's; `proposal.first_seen` / `status_change` for projects in their footprint (supply they could contract); `opportunity.awarded` in their state programme; `opportunity.cancelled` on USDA/DOE lines they had planned around. |
| Buyer / champion | Buyer: VP Power Supply (G&T), Director of Resource Planning, Procurement/Origination Manager (CCA). Champion: power-supply analyst or planning engineer. Approver: CEO/GM or board for co-ops (slow, consensus-driven). |
| Job to be done | "Know what is being proposed in and around my territory (generation, storage, large loads), what my peers are procuring, and which sponsors are real, before the next IRP or all-source RFP." A second job for the RFP *issuer*: "Get my RFP in front of qualified bidders" (this is the issuer-listing side of the marketplace). |
| Objections | "We are a member of [G&T/joint action agency] and they do this for us." · "Procurement is periodic; we don't need live." · "Budget is board-approved annually." · "We can't share our data." |
| Proof needed | Territory-level map/list with sources; a list of large-load or generation requests in their footprint they had not seen; a peer-RFP benchmark table. For issuers: distribution numbers (RSS/email subscribers, social reach) and zero-cost listing. |

### 1.4 EPC and OEM

| | |
|---|---|
| Who | EPCs, balance-of-plant contractors, OEMs (modules, inverters, storage systems, turbines, transformers/switchgear, gas turbines), owner's engineers. |
| Timely when | `proposal.status_change` to permitted/contracted (the project is now buying equipment); `opportunity.awarded` (winning bidder now needs an EPC); `proposal.milestone_filed` with a proposed COD inside their delivery window; `load.request_filed` (data-centre campuses buy switchgear, generation, cooling). |
| Buyer / champion | Buyer: VP Sales / Business Development; Regional Sales Director. Champion: sales-operations or market-intelligence analyst who builds target lists. Approver: CCO. |
| Job to be done | "Give my sales team a weekly list of projects that just became real (permitted, contracted, awarded) with sponsor and COD, so we bid early." |
| Objections | "Our reps already know every project in their region." · "Wood Mackenzie / Enverus is in the budget." · "We need contact names" (we do not sell personal data; we give organisation and public role). |
| Proof needed | A region-filtered weekly list, with the projects their reps did not know; CRM-ready export (CSV or API); technology filters that match their product lines. |

### 1.5 Advisory and law

| | |
|---|---|
| Who | Energy/project-finance law firms (regulatory, transactional, environmental), transaction and technical advisors, independent engineers, consultancies doing market entry and interconnection strategy. |
| Timely when | `proposal.milestone_filed` (a docket opened or an order issued — legal work follows); `opportunity.opened` for large procurements (bid counsel); `opportunity.cancelled` / `reinstated` (litigation and appeals work); `org.first_seen` (new entrant needs counsel); `load.request_filed` (large-load tariff and co-location work). |
| Buyer / champion | Buyer: Practice group leader / Partner (energy), Head of BD or Marketing (law firm). Champion: BD manager or knowledge-management lead who briefs partners. |
| Job to be done | "Alert our partners to filings, awards and cancellations involving clients and prospects, and give BD a cited reason to reach out the same day." Secondary: thought-leadership material (client alerts) sourced from our change feed. |
| Objections | "We have docket alerts from the agencies and from Halcyon." · "Partners won't use another tool." · "Conflicts: we can't act on client data." |
| Proof needed | A one-week digest of events in their practice areas, with links to the primary document; a co-branded alert or webinar pilot; a demonstration that the feed is derived from public sources only. |

### 1.6 Data-centre operators and large-load developers

| | |
|---|---|
| Who | Hyperscaler energy/site-selection teams, colocation and data-centre developers, "powered land" developers, behind-the-meter generation providers. Context: ERCOT's large-load queue is 438–474 GW, ~90% data centres (`01-feasibility.md` §3.6); PJM filed large-load and co-location tariff reforms in February 2026; AEP Ohio's data-centre pipeline fell from >30 GW to 13 GW after its large-load tariff. |
| Timely when | `proposal.status_change` or `proposal.withdrawn` on generation near their target site (available capacity or co-location partner); `load.request_filed` by a *competitor* in a target territory; `proposal.milestone_filed` for transmission projects that unlock a region; regulatory events (large-load tariff dockets) once state dockets are in scope; `opportunity.opened` for utility large-load or flexible-load programmes. |
| Buyer / champion | Buyer: Head of Energy / Energy Procurement, VP Site Selection / Development. Champion: energy strategy manager or site-selection analyst. Approver: VP Infrastructure. |
| Job to be done | "Find speed-to-power: which territories and substations have generation, storage or transmission proposals maturing on my timeline, and what my competitors are filing." |
| Objections | "We have Cleanview's data-centre tracker and Halcyon." · "Our utility relationships tell us this." · "We need substation-level headroom, not queues." |
| Proof needed | A territory dossier: generation/storage proposals by status and COD, large-load requests, transmission milestones, with sources; a back-test on one of their known sites. Honest scoping: we do not provide hosting-capacity or substation headroom data. |

### 1.7 Segment priority for the first 90 days

1. Developers/IPPs and utilities/co-ops (owner's network; fastest to conversations; both sides of the marketplace).
2. Lenders/funds (higher ACV, longer cycle; start discovery now, sell in Q2).
3. Data-centre operators (highest urgency in 2026, but they are well served by Cleanview/Halcyon; enter via co-location and large-load events once dockets are covered).
4. Advisory/law and EPC/OEM as channels first (§5), customers second.

## 2. Account research method (public sources only)

### 2.1 Sources, in order of use

| Source class | What we take | Rule |
|---|---|---|
| Our own graph | The org's proposals, statuses, events, matches, footprint | Primary; every fact is already cited |
| ISO/utility queues, EIA-860M, FERC eLibrary, siting boards, Permitting Dashboard | Which projects, where, what stage | Public; cite `source_url` |
| Grants.gov, DOE eXCHANGE, SAM.gov, USAspending, TED, World Bank | Awards, applications where public, cancellations | Public domain / attribution |
| Company website, press releases, investor decks, annual reports, 10-K | Strategy, portfolio size, target markets, hiring for origination | Company-level facts only |
| Trade press (Utility Dive, RTO Insider, pv magazine, Canary Media, DCD) | Headline, link, snippet | Headline/link/snippet only, per `02-data-sources.md` §4 |
| LinkedIn **company page**, viewed manually in a browser | Headcount band, HQ, recent company posts, open roles | No scraping, no member-profile collection, no extensions or bots (LinkedIn's "Prohibited software" and "Automated activity" policies) |
| Conference agendas, webinars, association member directories (NRECA, CalCCA, ACP, SEIA) | Who is speaking/participating, which topics | Company and public role only |

Not used: private aggregators (Interconnection.fyi, Cleanview, Halcyon, Enverus, BidNet, Energy Adepto) in any
form, paid contact databases, email-finder tools, LinkedIn profile scraping.

### 2.2 Fields captured per account (`organization` + CRM account)

```
org_id, name_canonical, aliases[], type, hq_state, footprint_states[], isos[], website
segment (1.1–1.6), sub_segment
size_band (LinkedIn company-page headcount band; company statement), portfolio_mw (from our graph)
active_proposals (count), proposals_by_state{}, last_event_at, last_event_type
opportunities_issued (utilities/CCAs), opportunities_won (developers/EPC)
tools_in_use (only if stated publicly: e.g. "cites Interconnection.fyi in deck")
buyer_roles_present[] (from open roles / org chart pages, role titles only)
research_notes (≤5 bullets, each with a URL)
consent_basis (network introduction | inbound | public business contact), do_not_contact (bool)
```

Contact records hold business name, role title, company email/phone *only* where obtained from the owner's own
network, a public professional listing (company site, conference programme) or an inbound enquiry. No personal
mobile numbers, no home addresses, no inferred emails.

### 2.3 Lead-scoring rubric (agent-computed, recomputed nightly)

Score = fit (0–40) + timing (0–40) + engagement (0–20). Bands: **A ≥ 70**, **B 50–69**, **C 30–49**, park < 30.

| Component | Points | Rule |
|---|---|---|
| Fit: segment priority | 0–15 | 1.1/1.3 = 15; 1.2 = 12; 1.6 = 10; 1.4/1.5 = 6 |
| Fit: activity in graph | 0–15 | ≥10 active proposals or ≥3 RFPs issued in 24 mo = 15; 3–9 / 1–2 = 10; 1–2 / 0 = 5; none = 0 |
| Fit: footprint overlap with covered sources | 0–10 | ≥80% of their proposals in Tier-1 sources = 10; 50–79% = 6; <50% = 2 |
| Timing: strongest event ≤30 days | 0–25 | `match.created` 25 · `opportunity.opened` in footprint 22 · `load.request_filed` 20 · `proposal.status_change` (positive) 18 · `opportunity.cancelled/reinstated` affecting them 18 · `opportunity.awarded` 15 · `proposal.withdrawn` (competitor) 12 · `proposal.first_seen` 8 · `org.first_seen` 5 |
| Timing: recency decay | multiplier | ×1.0 ≤7 days; ×0.7 8–30 days; ×0.3 31–90 days; ×0 after |
| Timing: `org.activity_spike` | +10 | ≥3 events in 30 days |
| Timing: opportunity due date | +5 | `opportunity.due_soon` and they have a matching proposal |
| Engagement: owner's network | 0–10 | Warm (owner has worked with them) 10; second-degree via a named channel partner 6; cold 0 |
| Engagement: inbound signal | 0–10 | Signed up to free tier 4; opened ≥3 alert emails 3; replied to outreach 10 (cap 10) |

Output per lead: score, band, top event (type, subject, date, `source_url`), suggested segment sequence (§3), and a
one-line "why now" the owner can read in ten seconds. Weights are a starting point; recalibrate after the first
30 replies (§7.4 tracks reply rate by band).

## 3. Outreach sequences

### 3.1 Rules that apply to every sequence

- Sent by the owner from his own email and LinkedIn account, or by a named human he designates. Never by an agent,
  never through LinkedIn automation tools, never through a persona.
- Every message contains at least one fact from our graph with its `source_url`, and one about the account from a
  public company source. No fact, no send.
- Three touches over ~10 business days, then stop unless they engage. No fourth touch inside 60 days.
- Email compliance (CAN-SPAM applies to B2B): truthful From/subject, the business's physical postal address, a
  working opt-out honoured within 10 business days; one-to-one messages from a personal account are still
  commercial messages if the primary purpose is promotion. EU/UK recipients: use TED/FTS-sourced facts and the
  same footer; B2B email to corporate addresses is generally permitted under PECR/ePrivacy soft-opt-in and
  legitimate-interest rules but must offer opt-out — confirm with legal-compliance (`docs/13-*`) before any EU send.
- LinkedIn: manual connection requests with a note; no InMail blasts; no third-party tools (account restriction risk).
- Calls: B2B calls are exempt from the national Do-Not-Call registry under the FTC's TSR, but no auto-dialers or
  pre-recorded messages (TCPA applies equally to B2B), and some states do not exempt B2B — check the state before
  cold-calling outside the owner's network. Calls in this playbook are to people who have replied or been introduced.
- Personalisation tokens: `{Company}`, `{ProjectName}`, `{State}`, `{ISO}`, `{EventType}`, `{EventDate}`,
  `{SourceURL}`, `{PublicFact}` (from company site/press with URL), `{Role}`.

**Compliance footer (append to every email):**

```
{Owner name} · Bankable (bankablehq.com) · {legal entity name}, {physical postal address}
You are receiving this because {reason: we worked together at X / you are listed as the RFP contact for Y / you
signed up at bankablehq.com}. Reply "unsubscribe" or click {opt-out link} and you will not hear from us again.
Drafted with AI assistance and reviewed and sent by me personally.
```

**AI disclosure standard:** the footer line "Drafted with AI assistance and reviewed and sent by me personally" is
mandatory on email. On LinkedIn messages, where footers are unnatural, the disclosure sits on the owner's profile
"About" section and on bankablehq.com/outreach (one sentence: "Some of my outreach is drafted with AI assistance;
every message is reviewed and sent by me."). On calls, no disclosure is needed because the call is entirely human;
if a call summary is sent afterwards, it carries the email footer.

### 3.2 Developers / IPPs

**Email 1 (day 0)** — Subject: `{ProjectName}: status change on {EventDate}` · alt: `Saw {ProjectName} move in {ISO}`

> {First name},
>
> {ProjectName} ({Capacity} MW, {County}, {State}) changed to "{NewStatus}" in the {ISO} queue on {EventDate}
> ({SourceURL}). Congratulations, if that is the milestone it looks like.
>
> I am building Bankable's proposal graph: one record per project stitched across queues, EIA-860M, FERC, siting
> dockets and RFPs, with a change feed. Your portfolio has {N} projects in it; {N2} had events in the last 30 days.
> I would value 20 minutes to learn how your origination team tracks this today and whether a cited change feed
> would replace any of it. {PublicFact} suggests {State/technology} is a focus — happy to send that slice first.
>
> {Footer}

**Email 2 (day 4)** — Subject: `Re: {ProjectName}` · body: one new event or an RFP match: "Since I wrote,
{IssuerOrg} opened an all-source RFP in {State} due {DueDate} ({SourceURL}); {ProjectName} matches on
{technology/COD}. Useful, or noise? A one-word reply helps me calibrate."

**Email 3 (day 10)** — Subject: `Closing the loop` · body: "I'll stop here. If a weekly, cited list of changes on
{State}/{ISO} projects would help, the free delayed feed is at {link}; Pro (live, alerts) is {price}. Either way,
thanks for reading." {Footer}

**LinkedIn (connection note, ≤300 chars, sent manually):** "{First name} — I track {ISO} queue and RFP changes for
Bankable; saw {ProjectName} moved to {NewStatus} on {EventDate}. Would like to compare notes on how your team
follows this. — {Owner}"

**Call talk-track (after reply or introduction, 15 min):** 1) Confirm the event and ask what they knew and when.
2) "Walk me through what happened the last time a competitor withdrew in a cluster you're in." 3) Who tracks queues
today, how many hours/week, what tools. 4) Would a live, cited change feed replace any of that; what would it need
to show. 5) Offer: 60-day pilot on their portfolio (§6.3) — ask for a yes/no on a date.

### 3.3 Lenders, tax equity, infra funds

**Email 1** — Subject: `{SponsorOrg} executed LGIA — {ProjectName}` · alt: `Milestone on a {State} {technology} asset`

> {First name},
>
> {SponsorOrg}'s {ProjectName} ({Capacity} MW, {State}) filed {Milestone} with {Agency} on {EventDate}
> ({SourceURL}). {PublicFact} says {Fund} is deploying into {technology/geography}, so this may be on your screen.
>
> Bankable's graph emits dated, cited events (filings, awards, cancellations, withdrawals) per project and sponsor.
> The pitch to a project-finance desk is screening plus monitoring from primary sources you can cite to IC. Could I
> run a back-test on three of your recent deals — the events we would have emitted and when — and show you the
> result in 20 minutes?
>
> {Footer}

**Email 2** — Subject: `Re: {SponsorOrg}` · a funding-status event: "DOE {Programme} award to {Org} was
{cancelled/reinstated} on {EventDate} ({SourceURL}). This class of event is what we track for a monitoring list."

**Email 3** — Subject: `Back-test offer stands` · one line; free-tier link; pricing for Team/API; footer.

**LinkedIn:** "{First name} — I run Bankable's proposal graph (queue + docket + funding events, cited). Saw
{Fund}'s {PublicFact}. Would like to compare how you monitor sponsor milestones. — {Owner}"

**Call:** 1) What triggers a deal review today; who watches the pipeline. 2) How they learned of the last adverse
status change on a portfolio asset. 3) Compliance requirements for third-party data in memos. 4) Offer the
back-test; agree the three deals and a date.

### 3.4 Utilities, co-ops, CCAs

**Email 1** — Subject: `{N} new requests in {Utility} territory since {Month}` · alt: `What {PeerUtility} just put out to RFP`

> {First name},
>
> Since {Month}, {N} generation/storage proposals and {M} large-load requests have appeared in or adjacent to
> {Utility}'s territory ({SourceURL}s). {PeerUtility} opened an all-source RFP on {EventDate} for {Capacity} MW,
> due {DueDate} ({SourceURL}).
>
> I spent {years} in origination working with co-ops and utilities and I am building Bankable to make this
> visible in one place: proposals, large loads and peer procurements, with sources. Two things I'd like to ask
> you: does a territory view like this help resource planning, and would you list your next RFP with us (free,
> to our subscriber base) when it opens?
>
> {Footer}

**Email 2** — Subject: `Re: {Utility} territory` · a large-load or funding event: "{DataCentreOrg} filed a
{MW} MW load request in {NeighbourUtility} on {EventDate} ({SourceURL})" or "USDA {Programme} status changed…".

**Email 3** — Subject: `Territory list, no strings` · attach/link the territory list as a one-off PDF; free-tier
link; note the G&T/statewide association route (§5.2) if they prefer a group arrangement. Footer.

**LinkedIn:** "{First name} — from my co-op origination days: I now track proposals, large-load requests and peer
RFPs by territory at Bankable. Would like your read on whether a {Utility} view is useful. — {Owner}"

**Call:** 1) How the IRP/procurement cycle works and when the next all-source RFP is. 2) How they learned about
the last large-load request nearby. 3) Whether the G&T or statewide provides market intelligence today. 4) RFP
listing ask (issuer side). 5) Pilot: territory alerts for one planning cycle at Pro price, or a G&T-level Team
licence.

### 3.5 EPC / OEM

**Email 1** — Subject: `{N} {technology} projects went permitted/contracted in {Region} this month`

> {First name},
>
> In {Region}, {N} {technology} projects moved to permitted or contracted status in the last 30 days, totalling
> {MW} MW with CODs {Year}–{Year} ({SourceURL}s). {PublicFact} says {Company} is targeting {product/region}.
>
> Bankable emits these transitions weekly with sponsor and COD so a sales team can bid early. Could I send your
> regional director the {Region} list for two weeks and ask whether it contained projects the team did not know?
>
> {Footer}

**Email 2** — Subject: `Re: {Region} list` · one `opportunity.awarded` event: "{Sponsor} won {Issuer}'s RFP for
{MW} MW ({SourceURL}) — they will be sourcing {equipment} on a {Year} COD."

**Email 3** — Subject: `Two-week list offer` · repeat the offer; note CSV/API export; footer.

**LinkedIn:** "{First name} — Bankable tracks when projects go permitted/contracted by region (cited). Happy to
share a {Region} {technology} list to test against your team's pipeline. — {Owner}"

**Call:** 1) How reps find projects today; where the blind spots are (non-ISO utilities, storage retrofits).
2) Lead time they need before procurement. 3) CRM they use (export format). 4) Two-week list pilot; success metric
= projects unknown to the team.

### 3.6 Advisory / law

**Email 1** — Subject: `Docket activity in your practice area this week` · alt: `Cancellations and reinstatements, cited`

> {First name},
>
> This week our feed recorded {N} events in {practice area}: {example 1 with SourceURL}; {example 2}. {PublicFact}
> says {Firm}'s {practice group} focuses on {topic}.
>
> Bankable is a cited change feed over queues, dockets, permits and funding programmes. For a firm the use is a
> same-day, sourced reason for a partner to call a client or prospect, and raw material for client alerts. Could I
> send your BD lead a one-week digest for {practice area} and hear whether it beats the agency alerts you already get?
>
> {Footer}

**Email 2** — Subject: `Re: digest` · a `opportunity.cancelled` or `load.request_filed` event with URL and one line
on the legal work it implies. **Email 3** — Subject: `Digest offer + co-marketing` · propose the co-branded alert
or webinar (§5.2). Footer.

**LinkedIn:** "{First name} — I run a cited change feed over energy dockets, queues and funding programmes. Would
like to test a one-week {practice area} digest with {Firm}'s BD team. — {Owner}"

**Call:** 1) What alerts partners get today and what they ignore. 2) How BD turns a filing into an outreach.
3) Conflicts and data-use constraints. 4) Digest pilot and the channel arrangement (§5.2).

### 3.7 Data-centre operators / large-load developers

**Email 1** — Subject: `Generation maturing near {Territory}: {N} projects, {MW} MW` · alt: `Who else is filing load requests in {Utility}`

> {First name},
>
> Around {Territory}, {N} generation/storage proposals ({MW} MW) reached {status} in the last 60 days, with CODs
> {Year}–{Year} ({SourceURL}s); {N2} competing large-load requests were filed in {Utility} ({SourceURL}).
> {PublicFact} says {Company} is developing in {Region}.
>
> Bankable tracks these as dated, cited events. We do not have substation headroom data; what we have is who is
> proposing what, where, at what stage, and how that changes. Could I build a territory dossier for one of your
> target sites and let your energy team judge it in 20 minutes?
>
> {Footer}

**Email 2** — Subject: `Re: {Territory}` · a `proposal.withdrawn` or transmission milestone near the site.
**Email 3** — Subject: `Dossier offer` · one line, Team/API pricing, footer.

**LinkedIn:** "{First name} — Bankable tracks generation, storage and large-load filings by territory with
sources. Would like to test a dossier on one of {Company}'s target regions. — {Owner}"

**Call:** 1) How the energy team screens territories today (utility conversations, Cleanview, Halcyon, consultants).
2) The timeline they need power on. 3) Co-location appetite (behind-the-meter, surplus interconnection). 4) Dossier
pilot; Team/API if it lands.

## 4. Customer discovery plan: 20 conversations in 4 weeks

Feeds `01-feasibility.md` §6, weeks 4–6: kill signal if <10% name a workflow they would pay for; target ≥50%.

### 4.1 Mix and sourcing

| Segment | Conversations | Source |
|---|---|---|
| Developers/IPPs | 6 | Owner's network (counterparties from origination) + 2 from A-band leads |
| Utilities/co-ops/CCAs | 5 | Owner's network (co-ops, G&Ts, one CCA) |
| Lenders/funds | 3 | Owner's network + one via a law-firm introduction |
| Data-centre / large load | 2 | Second-degree via developer or utility contacts |
| EPC/OEM | 2 | Owner's network |
| Advisory/law | 2 | Owner's network |

Weekly cadence: week 1 = 12 asks, 5 booked; week 2 = 6 held, 10 more asks; week 3 = 7 held; week 4 = 8 held and
synthesis. Agents draft the asks from the owner's list of names (kept in the CRM only, provided by the owner);
the owner sends. No conversation is recorded without consent; notes are the record.

### 4.2 Scheduling script (email from the owner; LinkedIn variant is the first two sentences)

> Subject: 25 minutes on how you track {queues / RFPs / funding} today
>
> {First name}, I have left {previous role/company} and am building Bankable, a cited change feed over energy
> proposals, RFPs and funding programmes. Before I build the wrong thing I am doing 20 conversations with people
> who do this work. Not a sales call: I want to hear how you track {topic} today, what breaks, and what you would
> pay for, if anything. 25 minutes, any of {three slots}? I will send a two-line summary afterwards for your
> correction. {Footer}

### 4.3 Note template (one file per conversation, stored in the CRM; company/role level only)

```
date, segment, org, role_title, warm/cold, source_of_intro
1. Current workflow: what they track, which sources, which tools, hours/week, who does it
2. Last time something changed and they found out late: what, cost, how they found out
3. Trigger events they care about (map to §1 vocabulary); which are noise
4. Willingness to pay: would they pay / who approves / budget line it comes from / price reaction to
   $150–250/mo Pro and $5–15k Team-API (state the anchor, record the reaction verbatim, not interpreted)
5. Proof they would need before buying
6. Objections raised (map to §1 table; add new ones)
7. Issuer side (utilities only): would they list RFPs with us; why/why not
8. Referrals offered (org and role, only if they volunteered)
9. Verbatim quotes worth keeping (≤3)
10. Follow-up promised, by whom, by when
```

### 4.4 Synthesis rubric (feeds `docs/10-*` PRD; run at 10 and 20 conversations)

| Question | Metric | PRD consequence |
|---|---|---|
| Is there a paid workflow? | % naming a workflow + budget + approver | <10% → internal tool only (feasibility kill); ≥50% → proceed to pre-sale |
| Which events matter? | Count of mentions per event type, by segment | Ranks the change-feed roadmap and alert defaults |
| Which sources matter? | Mentions of non-ISO utilities, state dockets, RFP issuers | Re-orders ingestion (`02-data-sources.md` §6) |
| Price reaction | Distribution of reactions to each anchor (cheap / fine / expensive / no budget) | Sets Pro and Team price for the pre-sale |
| Proof required | Most-cited proof type | Defines the pilot deliverable (§6.3) |
| Issuer willingness | % of utilities who would list RFPs | Sizes the demand-side wedge |
| Competitor incumbency | Tools named, by segment | Positioning and the "do not compete on" list |

Each finding is written as fact → inference → confidence, with the count behind it, and the PRD cites the
conversation ids (not names).

## 5. Partnerships

### 5.1 Data partners

| Partner | What they have | What we want | What we offer | Term-sheet outline |
|---|---|---|---|---|
| **GridTracker / Interconnection.fyi** | Cleaned queue data for 50+ ISOs and non-ISO utilities, incl. distribution-level (500k+ DG projects, 28 utilities); sold as one-off, monthly CSV, Snowflake share, API | Non-ISO utility queue coverage we would otherwise build at ~1 connector-day per utility (`02-data-sources.md` §3); licence to publish *derived* records | A demand-side channel (RFP/funding matches they do not have); referral of our API customers who need raw rows; attribution on every derived record; co-marketing to the same audience | Data licence: fields, refresh, permitted derived use, no raw republication, attribution string, term 12 months, fee (flat or per-record; ask for a start-up tier), audit right, exit with data destruction. Answers open question 5. |
| **Cleanview** | Power-projects and data-centre trackers ($9,000/yr, 5 users; API +$5,000/yr; one-off download $4,900) | Data-centre project spine (they track ~1,000 data centres) if we don't build it; possibly reseller status | Demand-side events (RFPs, funding, awards) as a feed they can add; referral of their customers who need the fused view | Reciprocal API licence or reseller agreement; revenue share on referred subscriptions (10–20% is the range to open with); mutual attribution; non-compete carve-outs defined per data class. |
| **Halcyon** | Regulatory filings/dockets search with alerts; $99/mo to $50k+/yr; usage-priced API; expanding internationally | State PUC docket coverage (our stated non-goal for MVP, `01-feasibility.md` §5) via API rather than building | Our proposal graph as the entity spine their filings resolve to; developer/co-op audience; a joint large-load product | API subscription at usage price initially; upgrade to integration partnership (bi-directional links: our proposal page ↔ their docket page) with joint go-to-market on data-centre/large-load. |
| **Catalyst Cooperative / PUDL** | Open (CC BY 4.0 data, MIT code) cleaned EIA/FERC datasets; consultancy for data products | EIA-860M / FERC Form 1 spine done right; entity-resolution ground truth; possibly contract data engineering | Paid consulting engagement (they serve smaller business users occasionally); contribution back of our normalisers; sponsorship | SOW for a bounded engagement (e.g. EIA-860M ↔ queue matching tables); contributions under their licences; attribution; no exclusivity. |

Sequence: Catalyst first (open, cheap, improves resolution quality), GridTracker second (fills the biggest coverage
gap and answers open question 5), Halcyon and Cleanview as API customers before partners. Any partnership needs a
human signatory and a legal entity (open question 3).

### 5.2 Channel partners

| Channel | Why | What we offer | Ask | Term-sheet outline |
|---|---|---|---|---|
| **Energy law firms** (regulatory/transactional practices) | Trusted by every segment; already publish client alerts; want sourced content | Co-branded weekly digest in their practice area; early access; free Team seats for BD; webinar co-hosting | Introductions to developer and lender clients; the firm as a named reference; digest distribution to their list | Referral agreement: 15% of first-year subscription revenue from referred accounts, 12-month tail; content licence for the digest with attribution; no exclusivity; no personal data exchanged (they send from their list). |
| **Lenders / tax-equity desks** | Their sponsor clients are our ICP 1.1; they benefit when sponsors are better-informed | Team licence at a "portfolio" rate; sponsor-facing co-branded territory reports | Introductions to sponsors at milestone events; participation in the discovery plan | Customer-plus-referral: referral credit against their own subscription rather than cash. |
| **Co-op G&Ts and statewide associations** (60–64 G&Ts; statewide associations in most of the 47 co-op states) | One agreement covers many distribution members; the owner's home network; procurement benchmarking is a real member service | Group licence (G&T or statewide) with per-member seats; territory views per member; free RFP listing for members | G&T/statewide as the buyer; member webinar; RFP issuer commitments | Group licence: base fee + per-member seat, 12 months, member data isolation, attribution, no resale by the association; termination for convenience at 90 days in the first year. |
| **Trade press** (Utility Dive, RTO Insider, pv magazine, Canary Media, Data Center Dynamics) | Distribution and credibility; they need charts and data for stories | Monthly data brief (queue/RFP/funding movements) under CC BY with attribution; embargoed access | Attribution with link in stories; occasional co-published data piece | Content licence: our derived aggregates only, attribution string mandatory, no raw restricted-source rows (PJM etc.), no exclusivity. |

### 5.3 What we do not offer partners

Raw rows from restricted sources (PJM before licence; MISO/SPP/NYISO/ISO-NE until terms are recorded), personal
data of any kind, exclusivity in the first 12 months, or white-label removal of attribution.

## 6. Pricing and packaging talk-track

### 6.1 The ladder (anchors from `01-feasibility.md` §3.4)

| Tier | Price | What it is for | How to say it |
|---|---|---|---|
| **Free, delayed** | $0; 7–30 day lag; attribution; RSS/email digest; social feed | Reach, SEO, issuer listings, trust | "Everything is public data and we show our sources. The free tier is the same graph, a few weeks behind." |
| **Pro** | $150–250/month per seat (pre-sale $150; list $200–250); live; saved-search alerts; filters; CSV export; 1 seat | The individual originator, analyst, BD manager | "Less than a day of an analyst's time per month, and it is the part of the job nobody wants to do by hand." Anchor: Halcyon's individual tier is $99/month for filings only; Cleanview is $9,000/yr for 5 seats of the tracker. |
| **Team + API** | $5–15k/yr: 5–10 seats, API, webhooks, Snowflake/CSV delivery, SSO later | Desks, G&Ts, law-firm BD, data-centre energy teams | "Cleanview's platform plus API is $14,000/yr; Enterprise incumbents are multiples of that. We sit in the same band with the fused supply/demand view they do not have." |
| **Issuer listing** | Free for utilities/co-ops/CCAs/agencies to list RFPs | Demand-side supply for the marketplace | "It costs you nothing and puts your RFP in front of every subscriber who could bid." |
| **Bankable routing/certification** | Success or subscription fee (Phase 5; not sold yet) | Sponsors and capital partners | "Later. Today I am selling the feed." |

Do not discount Pro below $150. Do discount Team for group licences (G&T/statewide) and for founding customers who
commit before launch (§6.4). Annual prepay = two months free. No free Pro trials longer than 14 days; use the pilot
instead.

### 6.2 Objection responses (all segments)

- "Interconnection.fyi is free." — "It is, and we link to it. We are not selling queue rows; we sell the change
  feed across queues, dockets, funding and RFPs stitched to one project, with a citation on every field."
- "We have Enverus/Cleanview/Halcyon." — "Keep them. We are cheaper and narrower: events that matter to a deal,
  from sources you can cite. Run us alongside for 60 days and drop whichever earns less."
- "Data is stale/noisy." — "Every record shows source, URL and retrieved time. Tell me which record is wrong and I
  will show you the merge history. We publish precision on entity resolution."
- "No budget until next year." — "Pro is a card payment at the analyst's discretion; Team can start as a pilot
  on a purchase order under $5k."
- "We need contact names." — "We don't sell personal data. We give you the organisation, the public role, and the
  filing, and you use your own relationships."

### 6.3 Pilot offer design

- **Scope:** one portfolio (developer, lender), one territory (utility, data centre), one region/technology
  (EPC/OEM), or one practice area (law). Agreed in writing before day 1.
- **Duration:** 60 days. **Price:** Pro price per seat for the pilot period, credited against a Team licence if
  they convert within 30 days of pilot end. Free only for the first three design partners, in exchange for a
  written case study and a reference call.
- **Deliverables:** week 1 back-test (events we would have emitted on three of their known items, dated);
  weekly alert digest; day-30 check-in; day-55 review with a scorecard.
- **Success metric agreed up front:** number of events they did not already know that they acted on; target ≥3
  in 60 days. If we miss it, no conversion pitch — ask what was missing and record it in the PRD.
- **What we ask from them:** a named champion, 30 minutes at day 30 and day 55, and a list of their projects or
  territory so the back-test is honest.

### 6.4 Pre-sale: 10 Pro commitments (feasibility §6, weeks 6–8; kill signal <3)

1. Universe: the 20 discovery conversations plus A-band leads = ~35 accounts. Segments 1.1 and 1.3 first.
2. Offer: "Founding Pro" at $150/month, locked for 24 months, first invoice at launch (not before); cancel any
   time before launch. Payment via a card-on-file authorisation, or a signed one-page letter of intent for
   organisations that cannot pre-authorise.
3. Ask happens in the pilot day-30 call or at the end of a discovery call where the person named a workflow and a
   budget. Script: "If the live feed on {scope} was available on {launch month}, would you take a Founding Pro
   seat at $150 a month? I am asking ten people; I will tell you where the count stands."
4. Track: commitments (card-on-file or signed LOI), soft yes, no + reason. Report weekly (§7.4).
5. Kill/continue: <3 by week 8 → revisit price or scope; 3–6 → continue and extend two weeks; ≥7 → open Team
   conversations with the G&T and lender accounts.

## 7. CRM plan

### 7.1 Minimum fields

**Account:** the `organization` fields in §2.2 plus `segment`, `owner_relationship (warm|second-degree|cold)`,
`lead_score`, `lead_band`, `top_event`, `top_event_url`, `top_event_at`, `tools_in_use`, `do_not_contact`,
`consent_basis`, `partner_source` (channel that introduced), `next_action`, `next_action_at`, `stage`.
**Contact (business only):** `name`, `role_title`, `company_email`, `company_phone (optional)`, `source_of_contact`,
`opt_out (bool, date)`, `linkedin_company_page_url` (company, not profile).
**Deal:** `tier`, `seats`, `annual_value`, `stage`, `pilot_scope`, `pilot_start`, `pilot_metric`, `pilot_result`,
`commitment_type (card|LOI|contract)`, `close_reason`, `subscription_id` (from billing).
**Activity:** `type (email|linkedin|call|meeting|alert_open)`, `sent_by (human name)`, `draft_by (agent|human)`,
`at`, `sequence_id`, `touch_n`, `reply (bool)`, `notes_ref`.

### 7.2 Pipeline stages

```
0 Identified   — in graph, scored, no contact
1 Researched   — §2.2 fields complete, sequence drafted (agent)
2 Contacted    — touch 1 sent by human
3 Engaged      — reply or accepted connection
4 Discovery    — conversation held, notes filed
5 Pilot        — scope agreed, back-test delivered
6 Committed    — Founding Pro card/LOI, or Team proposal accepted
7 Customer     — subscription live (from billing)
8 Expansion    — Pro → Team, or group licence
X Lost / Parked — with reason code (no need, incumbent, budget, timing, no reply)
```

Exit criteria are objective (an artifact exists: notes file, LOI, subscription id). Agents may move accounts
between 0→1 and record activities; humans move 2 and above.

### 7.3 Weekly pipeline report (Monday, agent-drafted, one page)

```
Week of {date}
Pipeline: count and $ by stage; movement since last week (+/-)
New A/B leads from platform events (top 10, with event, date, URL, suggested sequence)
Outreach: touches sent (by human), replies, reply rate by band and segment
Discovery: conversations held / scheduled / asked (against the 20-in-4-weeks plan)
Pilots: active, day, metric-to-date
Pre-sale: commitments / soft yes / no (against 10)
Partnerships: stage per partner (§5)
Blockers needing the owner: sends waiting, intros needed, contracts to sign
Data quality: accounts missing fields, duplicates merged, opt-outs processed (must be 100% within 10 business days)
```

### 7.4 CRM/ERP options (open question 4) and what sales needs from any of them

| Option | Cost signal | Fit for us |
|---|---|---|
| HubSpot Starter (CRM) + QuickBooks/Xero | Starter Customer Platform $20/seat/mo list ($7–10 promotional); Pro tier is ~$90/seat | Fastest; good email sequences and API; contact-tier pricing grows; CRM and finance are two systems |
| Odoo (CRM + invoicing + subscriptions) | Standard ~$31/user/mo first year, ~$39 after; Custom ~$61→$76 adds external API | One system for CRM, quotes, subscriptions, accounting; heavier; API only on Custom |
| ERPNext (Frappe) | Software free (MIT); Frappe Cloud from $5–25/site/mo; self-host $25–150/mo | One open system, lowest cost; smaller ecosystem; more integration work |
| Twenty (open-source CRM) + Stripe Billing | Self-host free (AGPL); cloud $9/seat Pro, $19 Organization | Modern, API-first, cheap; no finance module — Stripe + accounting tool needed; AGPL matters only if we embed it |

Sales does not need to choose. Sales needs, from whichever is chosen:

1. A REST API with webhooks so the lead scorer writes `lead_score`, `top_event`, `next_action` nightly and reads
   stage changes back (loop step 9).
2. Custom fields for everything in §7.1, including an `opt_out` flag the platform respects before any draft is
   generated.
3. Activity logging with `sent_by` (human) and `draft_by` (agent) so the human-sends rule is auditable.
4. Subscription/billing linkage: `subscription_id` on the deal and customer status readable by the admin panel
   (the CRM/ERP is the commercial system of record, `00-PLAN.md` architecture principles).
5. Reporting that can produce §7.3 without a spreadsheet export.
6. Data residency and deletion: one action deletes a contact everywhere (CLAUDE.md: honour deletion requests).

Recommendation for speed: HubSpot Starter now (sequences, API, reporting) with Stripe Billing, and revisit Odoo or
ERPNext when finance complexity justifies one system. If the owner prefers open source from day one, Twenty +
Stripe covers points 1–5 with a small integration.

## 8. Automation boundary

| Task | Agent, autonomous | Human required | Notes |
|---|---|---|---|
| Account research from public sources (§2.1) | Yes | — | Manual browser viewing of LinkedIn company pages is the one step done by a human, because tooling that automates LinkedIn is prohibited |
| Lead scoring and nightly CRM write of score/event/next action | Yes | — | Weights reviewed monthly by the owner |
| Drafting emails, LinkedIn notes, call talk-tracks, personalised with cited facts | Yes | — | Drafts land in the CRM as activities with `draft_by=agent` |
| Sending email | — | Yes | From the owner's own account; footer mandatory |
| LinkedIn connection requests and messages | — | Yes | Manual only; no automation tools |
| Calling and meetings | — | Yes | Agent prepares the brief and the note template |
| Scheduling (drafting the ask, proposing slots) | Draft only | Send | Calendar invites are sent by the human |
| Discovery notes: transcription of the owner's notes into the template, tagging | Yes | Notes written by the human | No recording without consent |
| CRM hygiene: dedupe, field completion, stage 0→1, opt-out processing | Yes | Stage ≥2 moves | Opt-outs processed by agent immediately; humans verify weekly |
| Weekly pipeline report | Yes | Read | Owner decides actions |
| Pilot scorecards and back-tests | Yes (compute) | Present | Delivered by the human on the call |
| Pricing quotes, discounts, negotiation | Draft options | Decide and communicate | No agent commits a price |
| Partnership term sheets | Draft | Negotiate and sign | Needs entity (open question 3) |
| Social replies to prospects | Draft | Send | Until the owner enables a named, disclosed channel |
| Enabling any automated outbound channel | — | Owner, in writing, with disclosure text | Named channel, labelled as automated |

**Disclosure text (fixed strings):**

- Email footer line: `Drafted with AI assistance and reviewed and sent by me personally.`
- Profile/site statement: `Some of my outreach is drafted with AI assistance; every message is reviewed and sent by
  me. Bankable never messages people through automated or fictitious accounts.`
- If the owner later enables an automated channel (e.g. an alert email or a labelled social account):
  `This message was generated automatically by Bankable from public sources. Sources are linked. Reply to reach a
  person.` plus the CAN-SPAM footer.

Nothing in this document is triggered by an agent sending anything. If a draft was sent without a human, it is an
incident: log it in `docs/CHANGELOG.md`, notify the recipient, and disable the path.

## 9. Assumptions and open items

- Segment sizes above are bounded by the feasibility TAM (1,000–2,000 US organisations); we have not sized each
  segment. The market-researcher (`docs/11-*`) should supply counts.
- The event vocabulary is proposed here; the solutions-architect (`docs/20-*`, `21-*`) owns the final names.
- Outreach rules must be reconciled with the legal-compliance outreach checklist (`docs/13-*`) once written; EU
  sends wait for that.
- Prices are anchors, not decisions. The pre-sale (§6.4) is the decision instrument.
- The owner's network list is his; agents see it only inside the CRM and never enrich it from non-public sources.

## 10. Sources

- Interconnection.fyi data purchase options — https://www.interconnection.fyi/data-subscription and https://www.interconnection.fyi/dg/purchase-data
- Cleanview pricing — https://cleanview.co/pricing ; Project Feed & Alerts — https://newsletter.cleanview.co/p/introducing-cleanviews-project-feed
- Halcyon Series A and pricing tiers — https://www.businesswire.com/news/home/20260316032633/en/Halcyon-Raises-$21-Million-Series-A-To-Bring-AI-Powered-Intelligence-to-the-Energy-Industry ; https://halcyon.io/blog/machine-readable/series-a
- Catalyst Cooperative / PUDL licensing and consulting — https://catalyst.coop/ ; https://catalyst.coop/pudl/
- NRECA membership structure (830 distribution, 60 G&T members; 47 states) — https://www.electric.coop/our-organization/membership-structure ; https://www.electric.coop/our-mission/americas-electric-cooperatives
- CalCCA (25 operational California CCAs) — https://cal-cca.org/ ; CCA states — https://www.nationalccea.org/cca-by-state
- FTC CAN-SPAM compliance guide — https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business
- LinkedIn prohibited software and automated activity — https://www.linkedin.com/help/linkedin/answer/a1341387 ; https://www.linkedin.com/help/linkedin/answer/a1340567
- B2B calling: TSR exemption and TCPA limits — https://www.dnc.com/faq/are-there-exemptions-b2b-calls ; https://www.dnc.com/dnc-tcpa-guides-and-checklists/risks-b2b-under-tcpa
- PJM large-load / co-location tariff reforms (Feb 2026) — https://www.whitecase.com/insight-alert/pjm-proposes-carve-out-new-services-co-located-data-centers ; large-load tariffs — https://www.utilitydive.com/news/large-load-tariffs-proliferate-as-states-take-more-active-role-in-data-cent/816184/ ; EEI list of large-load projects and tariffs (Aug 2026) — https://www.eei.org/-/media/Project/EEI/Documents/Issues%20and%20Policy/List%20of%20Large%20Customer%20Projects%20and%20Tariffs
- AEP Ohio data-centre pipeline after tariff — https://www.datacenterdynamics.com/en/news/u/
- HubSpot Starter pricing — https://www.hubspot.com/products/crm/starter ; Odoo pricing — https://www.erpresearch.com/pricing/odoo ; ERPNext / Frappe Cloud — https://www.erpresearch.com/pricing/erpnext ; Twenty pricing — https://twenty.com/pricing
- Internal: `01-feasibility.md` §3.1, §3.4, §3.6, §5, §6; `02-data-sources.md` §3–§5; `03-agent-operating-model.md` §3–§4.
