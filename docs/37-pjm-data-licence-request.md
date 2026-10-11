# 37 — PJM interconnection queue: permission request (draft for the owner to send)

**Status: DRAFT.** An agent prepared this, and nothing in it has been sent, posted or submitted (`CLAUDE.md`:
an agent drafts outbound communication to real people, and a human sends it). The owner sends it from their own
address, signs it themselves and edits it as they see fit. It claims no entity, title or relationship: every
bracketed placeholder is for the owner to fill in or delete.
**Reads:** `docs/13-legal-data-rights.md` §1.3, §6, §7 items 1 and 15; `data/sources.yaml`
`us.iso.pjm.gen_queue`; `docs/26-platform-posture.md` §1; `docs/00-PLAN.md` decisions log, rows 2026-09-21
(no lag knob) and 2026-10-10 (persona, audience first, lawyer friends before counsel); `docs/51-review-2026-10-10.md`
§2.3 item 5; `docs/35-louisiana-dce-data-request.md` (house style).
**I am not a lawyer.** Quotes are verbatim. Anything marked *Inference* is my reading.

## 1. Facts verified on 2026-10-11

Retrieved 2026-10-11 between 14:22 and 14:31 UTC with HTTPS GET and a desktop-browser user-agent. Each answered
HTTP 200 unless stated. I did not open Data Miner 2 or the API portal's data, and did not use a PJM Tools
account, because using those Tools is itself acceptance of their terms (Data Miner 2 Terms of Use, preamble: "By
using the Tools, you agree to abide by these Terms of Use").

**Where the queue is published.** It is on two pjm.com planning pages, not one. Both are listed on
<https://www.pjm.com/planning/service-requests>.

- <https://www.pjm.com/planning/service-requests/serial-service-request-status>: pre-reform and
  Expedited Process projects. The page now says: "Transition Cycle 2 (TC2) project data on the Serial Service
  Request Status page is no longer current. Please refer to the Cycle Service Request Status page for all updated
  TC2 data." Its XLS export is served by PJM's Planning API (`services.pjm.com/PJMPlanningApi`), using a key
  embedded in the page's own script. Its XML export is a static file,
  <https://www.pjm.com/pjmfiles/media/planning/queues-data/PlanningQueues.xml> (HEAD: 23,243,139 bytes,
  Last-Modified 2026-10-11). The file carries `ProjectNumber`, `Name`, `State`, `County`, `Fuel`, `MWEnergy`,
  `MWCapacity`, `Status`, `SubmittedDate`, `ProjectedInServiceDate` and `WithdrawalDate`.
- <https://www.pjm.com/planning/m/cycle-service-request-status>: Transition Cycles 1 and 2 and later cycles
  ("1 - 100 of 1792" in its default view), with XLS and XML exports.
- PJM's "Obtaining Interconnection Information" guide
  (<https://www.pjm.com/-/media/DotCom/planning/services-requests/guide-for-obtaining-interconnection-information.pdf>,
  PDF metadata 2025-07-25, marked "For Public Use" on every page) says: "FERC requires that all information related
  to all interconnection projects be made available to the public. PJM posts this information on one of two
  pages, depending on how the project was processed".
- I found no Data Miner 2 queue feed. None is listed in PJM's eDataFeed-to-Data-Miner feeds list (dated
  2021-03-24) or in the Data Miner roadmap for the 2026-05-18 Tech Change Forum. This is moderate confidence only, because I did not
  open the live Data Miner catalogue (see above).

**Which terms govern each candidate.**

| Product | Terms it points to | Operative text (verbatim) |
|---|---|---|
| Both queue pages and their exports | Footer "Legal and Privacy", <https://www.pjm.com/about-pjm/legal>. There is no clickwrap and no terms notice on either page. The footer's "Data Shortcuts" block links to Data Miner 2 as navigation only. | "Access to this website does not confer any license or ownership interest in either the form or content of the website … and PJM hereby expressly reserves such rights and property in its entirety." Unchanged from `docs/13` §1.3(c). |
| Planning API (`services.pjm.com/PJMPlanningApi`) | None found. The API root returns a bare page reading "Planning Api". | — |
| Data Miner 2, Terms of Use PDF | <https://www.pjm.com/-/media/DotCom/etools/edatafeed/data-license-agreement-edata-feed-data-miner-2.pdf> | Byte-identical to the 2026-09-12 capture (104,439 bytes, md5 `f2e554a41e5b40c9852fcfed05ca6fa5`): "Members may republish data, however non-members are specifically prohibited from republishing data." |
| API portal | <https://apiportal.pjm.com/content/html_widgets/39vgb.html> | Unchanged: "… strictly prohibited without an effective PJM-issued Redistribution License." |
| Data Miner page on pjm.com | <https://www.pjm.com/markets-and-operations/etools/data-miner-2> | "Information and data contained in Data Miner is for internal use only and redistribution of information and or data contained in or derived from Data Miner is strictly prohibited without an active PJM Membership. A minimum level of Associate Membership is required." |
| Learning Center FAQ, "Are there any limitations to using Data Miner?" | <https://learn.pjm.com/three-priorities/keeping-the-lights-on/data-miner-faqs/are-there-any-limitations> | Same sentence, ending "without an active PJM Membership." |
| Associate Membership | <https://www.pjm.com/about-pjm/member-services/membership-enrollment> | "Associate members are responsible for paying the annual membership fee in the amount of $2,500." No application fee. |

**Contact channel.** PJM publishes no data-licensing or redistribution desk, and I found none by search. Three
channels are published, each quoted with its stated purpose:

- **Postal:** "Any and all inquiries regarding this disclaimer should be addressed to: General Counsel, PJM
  Interconnection, 2750 Monroe Blvd., Audubon, PA 19403" (<https://www.pjm.com/about-pjm/legal>). This is the
  channel PJM names for the notice that governs the queue pages. It names an office, not a person.
- **Email:** `custsvc@pjm.com`, "For general PJM support"
  (<https://www.pjm.com/about-pjm/communications/contact-directory>). It is also the Learning Center FAQ's answer
  to "Who can I contact with general questions about Data Miner?"
- **Queue mailbox (follow-up only):** `SystemPlanning.Admin@pjm.com`, "For interconnection queue requests and
  data associated with those interconnection queue requests" (same directory). Member Relations' phone numbers,
  (610) 666-8980 and (866) 400-8980, are printed in the footer of both queue pages.

*Inference:* the request should go to `custsvc@pjm.com` by email, with a signed copy by post to the General
Counsel. Sending both, each saying the other was sent, reaches the office that owns the governing notice and also
the inbox that routes internally. It stays one request.

**What changed since `docs/13` §1.3 (retrieved 2026-09-12).**

1. The queue is split across two pages. `docs/13` and `data/sources.yaml` name only the serial page, and TC2
   data there is now "no longer current".
2. A fourth PJM text exists: the Data Miner page and FAQ make membership, at minimum Associate ($2,500 a year),
   the redistribution route. This is not new on PJM's side. The Wayback Machine has the same sentence on the page
   on 2026-07-03 (`web.archive.org/web/20260703030940/https://www.pjm.com/markets-and-operations/etools/data-miner-2/`).
   `docs/13` did not record it. It conflicts with the API portal's "Redistribution License" wording.
3. Neither queue export is Data Miner 2: one is a static pjm.com file, and the other comes from the Planning API,
   for which no terms were found. This supports `docs/13` §1.3's main inference.
4. PJM's own guide puts the two queue pages directly after "FERC requires that all information related to all
   interconnection projects be made available to the public". `docs/13` had no such statement.
5. `docs/13` §1.3(a) quotes clause 9 with an ellipsis. The elided opening reads: "In addition to and not limiting
   the indemnity provisions set forth in the Operating Agreement, Section 16 and the Open Access Transmission
   Tariff, Section 10, to which you agree to be bound, You shall indemnify …"
6. The Legal & Privacy page names the General Counsel's postal address for inquiries. `docs/13` did not record it.
7. `data/sources.yaml` names the source "Queue Point / Data Miner" at `apiportal.pjm.com`. The Contact Directory
   describes Queue Point as the application through which interconnection requests are submitted ("Interconnection
   Queue Requests submitted using the Queue Point application"). The public queue is the two planning pages.
8. Unchanged: the robots.txt rules (planning paths and `/pjmfiles/` are not disallowed), the operative sentences
   of Legal & Privacy, the Data Miner 2 PDF and the API portal widget.

---

## 2. The draft

**To:** custsvc@pjm.com
**Post (signed copy):** General Counsel, PJM Interconnection, 2750 Monroe Blvd., Audubon, PA 19403
**From:** [Contact email], the owner's own address
**Subject:** Permission request: derived PJM interconnection queue fields on a free, noncommercial site

<!-- body-start -->
> Hello,
>
> I'm [Owner name], [Role] at [Organisation]. I'm writing to ask for written permission to republish a few
> derived fields from PJM's interconnection queue. Could you pass this to whoever handles data licensing? I'm
> also sending it by post to the General Counsel, as your Legal & Privacy notice asks.
>
> [Organisation] runs [Site name] ([Site URL]), an attributed public-records aggregator: a free, noncommercial
> website and API that maps proposed energy and infrastructure projects. Its readers are energy and
> infrastructure analysts, including analysts at investment firms. It is in private beta, and no PJM queue record
> is published on it.
>
> For each project on your Serial and Cycle Service Request Status pages, we would publish the project name,
> queue number, county and state, fuel or technology, capacity in MW, status and key dates. Every record would
> credit PJM and link back to PJM's page (Attachment A).
>
> We would not republish PJM's tables, export files, study reports or agreements, and we would not resell raw
> data. We would not use Data Miner, a PJM Tools account, CEII or any non-public data, and we would not use PJM's
> name or logo beyond the credit. We do not scrape: we would download the queue file PJM already publishes, at
> most once a day under an identified user-agent, or use any route you prefer.
>
> My questions:
>
> 1. May we publish these derived fields, attributed and linked, on a free, noncommercial site and API?
> 2. If not, is a narrower permission available? For example: fewer fields, a publication delay, or credit
>    wording you specify.
> 3. If the platform later has paying users, what licence or fee would apply? Your Data Miner page says
>    redistribution requires at least Associate Membership. Does that also apply to the queue pages?
>
> We will correct or remove anything at PJM's request. I'm happy to talk by phone if that's easier.
>
> Thank you,
> [Owner name]
> [Role], [Organisation]
> [Contact email] | [Phone]
> [Postal address]
<!-- body-end -->

(321 words with the placeholders and signature, counted with `wc -w`. For the posted copy, change the third sentence of the first paragraph to "I have sent the same
request to custsvc@pjm.com.")

**Attachments**

- **A. Fields and credit (one page, PDF).** The table below, with one sample row typed from PJM's own page.
- **B. A screenshot of one record page from the beta,** showing how a source credit and link-out render. Use a
  NYISO or CAISO record: no PJM record exists to show.

| Shown on the site | PJM field (queue export) |
|---|---|
| Project name | `Name` |
| Queue number | `ProjectNumber` |
| County, state | `County`, `State` |
| Fuel or technology | `Fuel` |
| Capacity (MW) | `MWEnergy`, `MWCapacity` |
| Status | `Status` |
| Dates | `SubmittedDate`, `ProjectedInServiceDate`, `ActualInServiceDate`, `WithdrawalDate` |
| Credit and link | "Source: PJM Interconnection", or PJM's wording, linked to the Serial or Cycle Service Request Status page |

Not shown: study reports, agreements (which carry developers' contact details), transmission-owner columns and
PJM's own tables or files.

**Before you send**

- [ ] Fill in the placeholders. Decide whether [Organisation] names the LLC in the decisions log (2026-09-18,
      row (5)), as `docs/35` does. Do not describe a role or affiliation you do not hold.
- [ ] Do not register for PJM Tools or accept the Data Miner terms before PJM answers (`docs/51` §2.3 item 5).
      `docs/00-PLAN.md` "Immediate next actions" still lists "Register free API keys: PJM Tools".
- [ ] Send only if you will abide by a "no". See §3(b).
- [ ] Keep the thread and the posted copy. The legal lane records the date sent in `docs/13` §1.3.

---

## 3. Notes

**(a) Which terms the request is made under, and why.** The request rests on the pjm.com Legal & Privacy notice,
because that is the only text the queue pages point to (§1). It does not rest on Data Miner 2 or the API portal.
The platform has accepted neither, and both name their own product only.

The notice contains no redistribution prohibition. We ask anyway because it "does not confer any license" and
"expressly reserves" rights, and because Data Miner 2 clause 7 asserts compilation copyright and trade secret
over PJM's data. A written grant replaces `docs/13` §1.3's moderate-confidence inference with a document.

Question 3 cites Associate Membership because PJM's own page names that route, so PJM may answer in its terms.

*Inference:* the letter deliberately does not argue *Feist*, FERC posting duties or the planning-page reading.
A plain request tends to draw a business answer, and an argued one tends to draw a legal answer.

**(b) Answers to expect, and what each allows.**

| Answer | What it allows |
|---|---|
| Yes, in writing, for derived fields with credit | Publish `derived_only` from the planning pages, under the grant's words. |
| Yes, with conditions (fields, credit, delay) | Publish within the conditions. A delay needs an owner decision and new code: the 2026-09-21 decision removed the lag knob, and `tests/test_publication_is_never_time_delayed.py` forbids it. A licence-mandated delay is a different thing from a tier lag, but it is still a new mechanism. |
| Join as an Associate Member, or take a Redistribution License with a fee | Owner decision. First ask what membership binds the LLC to: Data Miner clause 9 points to Operating Agreement §16 and OATT §10. Also ask whether an Associate Member counts as one of the "Members" who "may republish data": the PDF says "All capitalized terms have the meaning as defined in the PJM Governing Agreements". |
| No, or "the Data Miner terms govern" | No PJM rows; stay link-out-only. **A written "no" largely closes `docs/13` §7 item 1.** Publishing from the planning pages after PJM has refused would be knowing. This is why the checklist says to send only if you will abide by the answer. |
| Silence | Status quo, link-out-only. Silence is not consent. |

**(c) Follow-up timing.**

- **10 business days:** reply on the same thread.
- **20 business days:** one email to `SystemPlanning.Admin@pjm.com` referencing the first, or a call to Member
  Relations.
- **About 8 weeks:** the legal lane records "no response" in `docs/13` §1.3, and the request stops there. No
  further chasers.

Every message is drafted by an agent and sent by the owner (`docs/13-legal-outreach-and-social.md` §7.1, L1).

**(d) Changes once permission arrives.** These are listed only; nothing has been changed.

1. `docs/13` §1.3: quote the grant verbatim with the date and the PJM sender's role. Keep the letter itself in
   private storage, not the repo. Close or amend §7 item 1, then update the §6 row (class and rule). The register
   must move before the manifest, because `scripts/check_manifest_licences.py` refuses a manifest more
   permissive than §6.
2. `data/sources.yaml` `us.iso.pjm.gen_queue`:
   - `name`: drop "Queue Point / Data Miner".
   - `url`: the two Service Request Status pages, or two source ids, one per page.
   - `access`: `bulk_file`. `auth`: none. `format`: xml.
   - `probe`: the XML file.
   - `license`: the grant's operative sentence.
   - `reuse`: `attribution` if the grant allows commercial use, `noncommercial` if it does not.
   - `publication`: `derived_only`, never `raw_ok` unless the grant says so.
   - `attribution` and `licence_url`: PJM's wording.
   - `notes`: conditions, revocation and the takedown contact.
3. Connector: read the published XML or exports only, never Data Miner 2. At most daily. Declared user-agent.
   Fields limited to the grant.
4. Gate: the visibility predicate reads `reuse` and `publication`, so the source opens without new code, unless
   the grant sets a delay or a field subset. Then:
   - the pricing-page coverage line updates itself from `/v1/coverage` and the manifest
     (`web/coverage_statement.py`). If the source is split into two ids, add both to its `ISO_QUEUE_NAMES`;
   - lift the social-feed exclusion (`docs/13-legal-outreach-and-social.md` §10) only if the grant covers social;
   - check that PJM-named interconnection points are no longer dark (`docs/25` D-17).
5. Decisions log row. `CLAUDE.md`'s "PJM rows are not public until a licence exists" is changed by the owner or
   the coordinator, not a lane.

**(e) Two questions for the owner's lawyer friends, asked now.**

1. **Commercial users of a noncommercial tool.** If PJM grants permission for a "free, noncommercial" site:
   - Does it stay within that grant when its readers are deal teams at commercial investment firms (persona,
     decisions log 2026-10-10), and when the operator is an LLC that may add paid tiers later?
   - What wording should we ask PJM for, so that the grant cannot be read against us?
2. **PJM's indemnity clause.** Data Miner 2 clause 9 makes the user indemnify PJM for third-party claims based on
   "(iv) any derivative created by you based on the Data", "in addition to" Operating Agreement §16 and OATT §10.
   If PJM's answer is "accept the Data Miner terms" or "become an Associate Member":
   - What does that expose the LLC to?
   - Should a permission letter exclude 9(iv)?
   - What does the exclusive Montgomery County, Pennsylvania forum add?

---

## 4. The other missing US queues: the first step for each

- **MISO** (`unknown`): the terms page answers a Cloudflare challenge, HTTP 403 (`docs/13` §1.7; re-checked
  2026-10-11, still 403). First step: the owner opens <https://www.misoenergy.org/meet-miso/legal-and-privacy/>
  in a browser and saves a PDF, and the clauses are quoted into `docs/13` (§7 item 2). No letter until then.
- **SPP** (`restricted`): "Any commercial use of these materials requires prior, express written authorization
  from the author(s) or a duly authorized officer of SPP" (<https://www.spp.org/terms-conditions/>, `docs/13`
  §1.4; unchanged 2026-10-11). First step: a written-authorisation request in this form, after reading SPP's
  unread Acceptable Use Policy PDF. No authorisation contact is published; the page's only address,
  `communication@spp.org`, is given for removing incorrect information.
- **ISO-NE** (`restricted`): an assent-by-use clause, and "Any duplication of the Content or non-personal use may
  violate copyright" (`docs/13` §1.6). First step: a written-permission email to `info@iso-ne.com`, the only
  permission address the terms give, which they state is for "a graph, chart, or other image". The terms moved to
  <https://www.iso-ne.com/legal-privacy>: the old URL returned 404 on 2026-10-11, and the clauses are unchanged.
