# 39 — Utility RFP issuers: permission requests (drafts for the owner to send)

**Status: DRAFT, automation level L1.** An agent prepared these. Nothing in this file has been sent, posted or
submitted (`CLAUDE.md`; `docs/13-legal-outreach-and-social.md` §7.1: "agent drafts, human edits and sends").
The owner reads, edits and sends each one personally, from their own address, one recipient per send, and signs
it themselves. No draft names or addresses a person: each goes to a role mailbox, a web form or an office the
issuer itself publishes. Every bracketed placeholder is for the owner to fill in or delete.
**Reads:** `docs/28-utility-rfp-registry.md` §3, §5, §7, §8.2, §9; `data/vendored/rfp_issuers/issuers.yaml`;
`docs/13-legal-outreach-and-social.md` §0, §1, §7, §8.2; `docs/37-pjm-data-licence-request.md` (format);
`docs/26-platform-posture.md` §1, §3, §5; `docs/63-operations.md` §6 (channel register); `docs/00-PLAN.md`
decisions log, rows 2026-09-18 (legal entity), 2026-10-16 (name), 2026-10-21 (developers first).
**I am not a lawyer.** Clause quotes are verbatim from the registry's `terms_quote`, which the docs/28 build
checked word for word against the captured pages (read 2026-10-21). Contact channels are quoted from the docs/28
captures of the same pages, not re-fetched. Anything marked *Inference* is my reading.
**§7, added 2026-10-22:** two commercial-reuse letters to the six `noncommercial` sources (one to AEP for its four
operating companies, one to the Texas Railroad Commission), with clauses and channels re-fetched live that day.

---

## 0. Send kit (read first)

### 0.1 Sender block: fill in once, every draft uses it

| Placeholder | What goes in it | Rule |
|---|---|---|
| `[Owner name]` | your own name | no persona or alias (`CLAUDE.md`: no impersonation, no fake personas) |
| `[Role]` | your role | one you actually hold (`docs/37` checklist) |
| Inframation | fixed text: "Inframation (inframation.io)" | the product's name since 2026-10-16 |
| `[Organisation]` | the legal entity that runs Inframation | decisions log 2026-09-18: Compass International Trading Group LLC, Denver, Colorado. Recommended: name it. A permission is granted to someone, and the FTC guide quoted in docs/13 §1 says header information must "identify the person or business who initiated the message" |
| `[Contact email]` | your address on a domain you control | SPF, DKIM and DMARC aligned (docs/13 §1, checklist E-US) |
| `[Phone]` | optional | delete the field if unused |
| `[Postal address]` | a valid physical postal address | in every draft; see §0.2. A registered PO box is fine (docs/13 §1) |

docs/13 §7.2 item 1 still says "Bankable" as the organisation. That text predates the 2026-10-16 rename; use
Inframation and `[Organisation]`.

### 0.2 Why every draft carries a postal address and a "stop" line

**The text** (FTC, CAN-SPAM compliance guide, quoted in docs/13 §1, retrieved 2026-09-12): "Tell recipients
where you're located. Your message must include your valid physical postal address." The rules apply to
commercial email. docs/13 §7.2 separately asks every outbound human message to state "How to stop hearing from
us" and a "Postal address (US)".

*Inference (moderate confidence):* a request for a licence is probably not a commercial message, because its
primary purpose is to ask permission, not to advertise. But every draft describes Inframation, who it is for and
that paid plans may come later, and a recipient could read that as promoting the site. The address and one
"reply stop" line cost nothing and settle the question. All twelve recipients are US issuers, so PECR and CASL
(docs/13 §2, §4) do not arise.

### 0.3 Before you send any of them

- [ ] **Channel register.** docs/13 §0: a channel "is **inactive** until the owner records" a row. The register
      lives in `docs/63` §6 and is empty. §0.4 has a proposed row, for the coordinator to add with your
      sign-off. The PJM request in `docs/37` needs the same row.
- [x] **The reader's contact links.** *Done 2026-10-22: `/bot` and `/contact` now answer 200 (`web/legal.py`, `web/test_contact_bot.py`); the original finding follows.* Every automated-reading paragraph says our reader names itself with a
      contact link. `pipeline/connectors/http.py` sends `Bankable/0.2 (+https://inframation.io/bot; contact
      https://inframation.io/contact)` after a browser-like prefix. On 2026-10-21 both
      <https://inframation.io/bot> and <https://inframation.io/contact> answered **404** (one GET each, to our own
      site), and no route for either exists in `web/`. Do not send a draft that offers the automated reader until
      both answer 200, or until you change that sentence to point at a page that exists.
- [x] **The reader's name.** *Done 2026-10-22: the product token is now `Inframation` (`pipeline/connectors/http.py`); the original finding follows.* The product token in that User-Agent is `Bankable`, not Inframation. An issuer that
      looks for us in its logs or writes a robots.txt rule will need the token. Either the token is renamed first
      (a pipeline change, not this lane's), or each draft gains one sentence: "Our reader identifies itself as
      Bankable, our working name."
- [ ] **Send only if you will abide by a no** (`docs/37`). A written refusal turns counsel item C-1 for that issuer
      from an open question into a known "no". Publishing that issuer's facts afterwards, from its site, would be
      knowing.
- [ ] **One recipient per send.** No BCC, no mail merge, no sequencer (L1).
- [ ] **Re-read before sending.** Open the issuer's page and terms in an ordinary browser. The quotes are dated
      2026-10-21 and most of these terms say they may change without notice. Confirm the To address still says
      what §3 quotes.
- [ ] **No bid portals.** Do not register on PowerAdvocate, Accion, Enel X, Jaggaer, XpressDRIVE or any other
      portal, even to look (docs/28 C-9).
- [ ] **Records.** Keep each thread and any posted copy in private storage, not the repo. Tell the legal lane the
      date sent; it records the date in the registry row's `notes`.
- [ ] **Pre-steps.** Entergy, Duke and Xcel (§3.10 to §3.12) each need their terms read in a browser first. Austin
      Energy (§3.3) needs one City of Austin page read.

### 0.4 Proposed channel-register row (`docs/63` §6)

| Channel | Sending identity | Recipient jurisdictions | Legal basis | Platform rules | AI disclosure | Automation level | Suppression / records | Activated |
|---|---|---|---|---|---|---|---|---|
| Owner's own mailbox and post: data-permission requests to issuers | `[Owner name]`, personally | US | CAN-SPAM (docs/13 §1): probably not commercial; the §1 checklist is applied anyway (accurate From, honest subject, postal address, "stop" line) | n/a (email, post, one web form) | none required at L1 (docs/13 §7.1) | L1, one recipient per send | a "stop" reply ends contact with that address within 24 h (§8.2); threads kept in private storage; message log per §8.3 | `[owner, date]` |

---

## 1. Which twelve, and in what order

**Rule.**

1. **Pool:** the 56 registry rows whose verdict is link-out only (12), manual only (36) or excluded (8).
2. **Permission is the lever:** keep investor-owned and municipal utilities whose own terms or gate stand in the
   way. Drop the public bodies whose terms are silent (SCPPA, Illinois IPA, CPA, Georgia PSC, Maryland PSC,
   Santee Cooper, NYPA, Massachusetts and the rest). Their route is counsel item C-3 plus a one-line
   confirmation (docs/28 §9), not this letter. Drop Puerto Rico too, because its listing needs a login.
3. **Rank:** first by `developer_value`. Then by the market-research lane's 17 strongest first connectors
   (docs/28 §6). Then by solicitation volume (`cadence`, times operating companies). A row with no standing
   listing to read (`access: none`) goes last within its value band, because a permission to read a page it
   does not have buys little. `access: unverified` (the site refused us) is unknown, not none.

**Result.** Twelve `high` rows:

| Send order | Issuer (`id`) | Verdict (basis) | Cadence | Channel | Pre-step |
|---|---|---|---|---|---|
| 1 | PacifiCorp (`us.rfp.pacificorp`) | link-out only (`terms_restrict_information`) | several a year | RFP mailbox | none |
| 2 | Southern California Edison (`us.rfp.sce`) | link-out only (`terms_restrict_content`) | several a year | procurement mailbox | none |
| 3 | Austin Energy (`us.rfp.austin_energy`) | link-out only (`terms_restrict_information`) | several a year | Legal Services mailbox, for the role the terms name | read one City of Austin page |
| 4 | Arizona Public Service (`us.rfp.aps`) | link-out only (`terms_restrict_information`) | annual | the terms' own contact mailbox | none |
| 5 | El Paso Electric (`us.rfp.el_paso_electric`) | link-out only (`terms_restrict_content`) | annual | the terms' own contact mailbox | none |
| 6 | Portland General Electric (`us.rfp.portland_general`) | link-out only (`terms_restrict_information`) | about every 2 years | RFP contact in the RFP document, else the terms' channel | find the RFP contact |
| 7 | Puget Sound Energy (`us.rfp.puget_sound_energy`) | manual only (`terms_bar_automation`) | annual or more | RFP mailbox | none |
| 8 | Consumers Energy (`us.rfp.consumers_energy`) | manual only (`terms_bar_automation`) | twice a year | web form on the notices page | none |
| 9 | Dominion Energy Virginia (`us.rfp.dominion_virginia`) | manual only (`terms_bar_automation`) | annual | RFP mailbox | none |
| 10 | Entergy (`us.rfp.entergy`) | manual only (`blocked`) | 5 to 10 a year | RFP contact on the RFP site | read the terms in a browser |
| 11 | Duke Energy (`us.rfp.duke_energy`) | manual only (`blocked`) | annual, four operating companies | to be found in a browser | read the terms; find the listing and contact |
| 12 | Xcel Energy (`us.rfp.xcel`) | manual only (`terms_not_located`) | annual, three operating companies | RFP contact on the RFP page | find and read the terms |

The order puts link-out-only rows first. Their terms do not bar reading, and docs/28 §8.1 tranche 1b already
specifies their detectors, so a yes publishes soonest.

**Where this departs from the suggested list.** The coordinator's brief suggested Pacific Gas and Electric.
The registry rates PG&E `medium`, and El Paso Electric is `high` and on the market-research lane's strongest
list, so EPE takes the slot. PG&E heads the next batch (§5). The template in §2 fits it unchanged, and its channel is already
found. If the owner values CAISO coverage over EPE's New Mexico and Texas RFPs, swap them.

*Judgement calls inside the rule.* After the eight strongest-list rows, Duke (four operating companies) and Xcel
(three) lead on volume. DTE (`high`, two RFPs a year) would come next on volume but has no standing listing
(`access: none`; it announces RFPs in press releases). That leaves two slots for five `high`, annual,
single-company rows: APS, Dominion, PNM, NV Energy and SRP. APS and Dominion take them for different reasons.
APS's terms come closest to a yes: "APS encourages you instead to tell others about the Site and direct them to
the Site". For Dominion, the Virginia SCC docket is robots-disallowed (`us.rfp.virginia_scc`), so permission is
the only route to its RFPs.

---

## 2. The reusable template

Use this for any issuer not drafted in §3. Pick one reading paragraph (R-A to R-D) by the registry `basis`. The
§3 bodies run 360 to 490 words with the signature (counted with `wc -w` without the `> ` markers); those that
quote two clauses (R-B, R-C) are the longest. Quote each clause exactly as the registry has it, with no
ellipsis inside the quotation marks.

**To:** a role mailbox, web form or office the issuer publishes (an RFP mailbox, the address its terms give for
questions about them, or its legal department). Never a named person. Never a mailbox whose stated purpose is
something else: an accessibility inbox, a security-report address, or an infringement-notice agent.
**From:** `[Contact email]`
**Subject:** Permission request: listing `[Issuer]`'s `[RFPs]` on Inframation, with a link to your page (not a bid question)

<!-- template-start -->
> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address on your [page title] page. This isn't a bid question
> and needn't go in any bidder Q&A. Could you pass it to whoever grants permission to use your website content?
>
> We'd like to list [Issuer]'s [RFPs] so that more developers see them. Your terms of use say: "[operative
> clause, verbatim]" So we've published nothing from your site, and I'm asking for written permission.
>
> For each [RFP] on [listing URL], we'd show its title, posted dates, MW sought, technology, and contract type
> and state where given. Each entry would link to your page and carry the credit "Source: [Issuer], [page
> title]" (or your wording). It would appear on our site, in its API and feeds, and in readers' email alerts;
> not on social media unless you agree. We would not copy your RFP documents, forms, Q&A or logos, and we would
> take nothing from [bid portal].
>
> [Reading paragraph R-A, R-B, R-C or R-D]
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your [RFPs] before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we [read your page and] list your [RFPs] as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only[, a person checking the page
>    instead of an automated reader], or credit wording you choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your [RFPs]; alert emails already sent can't be recalled. Until you reply, we'll publish
> nothing from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]
<!-- template-end -->

**Reading paragraphs.**

- **R-A, the terms restrict reuse but not reading** (link-out only): "An automated reader would load the page
  weekly, and daily while [an RFP] is open, at least two seconds between requests, following your robots.txt
  and naming itself with a contact link."
- **R-B, the terms bar automated reading** (manual only, `terms_bar_automation`): "Your terms also say:
  "[automation clause, verbatim]" So I'm asking about reading the page as well as listing it. We'd like an
  automated reader to load it weekly, and daily while [an RFP] is open, at least two seconds between requests,
  following your robots.txt and naming itself with a contact link. If you'd rather not, a person on our team
  would check the page in an ordinary browser instead." Use the bracketed words in both questions.
- **R-C, the site refuses our reader or the terms could not be read** (`blocked`, `terms_not_located`): first
  read the terms in an ordinary browser, save a PDF and give it to the legal lane. If they restrict nothing and
  the site answers our reader, send nothing; the legal lane reclassifies the row. Otherwise: "Your website
  answers our reader, which names itself in every request, with HTTP 403, so we've read nothing from it
  automatically. I read your terms in a browser instead." Follow with R-B's last three sentences, and add to question 1: "and would you let our reader through
  at that rate?"
- **R-D, the terms bar even manual monitoring, or links** (excluded: `terms_bar_manual_monitoring`,
  `terms_bar_linking`, `terms_bar_manual_and_derivative_use`): quote the clause, then: "So we'd need your
  permission for each step: checking the page, by an automated reader or by hand, linking to it, and listing
  the facts." Then R-B's cadence sentence. Do not offer manual checking as a fallback without asking for it.

**Attachment A** (optional, one page, PDF): the field table below, with one sample row the owner types from the
issuer's page in a browser.

| Shown on Inframation | From the issuer's page |
|---|---|
| Issuer | the company's name |
| Title | the solicitation's title as posted: the only text of theirs we copy |
| Dates | issue, notice-of-intent, proposals-due and shortlist dates, where posted |
| Size | capacity or energy sought (MW, MWh) |
| Technology | resource type: solar, wind, storage, thermal, all-source, demand response |
| Contract type | PPA, build-transfer or purchase-and-sale, tolling, capacity, REC, where stated |
| Location | state or service territory, where stated |
| Link and credit | the issuer's page URL; "Source: [Issuer], [page title]" or their wording |

Not shown: RFP documents and appendices, form agreements, Q&A, bidder lists, anything behind a portal, logos,
and any text beyond the title.

---

## 3. Tailored drafts

Each draft gives the registry facts, the channel with the issuer's own words for it, and the clause that blocks
us. Then comes the full body, ready to send, with the §0.1 placeholders. Then notes. The closing paragraphs (who
runs Inframation, the questions, withdrawal, "stop", signature) are the same in every draft on purpose. If you
change one, change them all.

### 3.1 PacifiCorp (`us.rfp.pacificorp`)

| | |
|---|---|
| Registry | link-out only, `terms_restrict_information`, value high, several RFPs a year; listing <https://www.pacificorp.com/suppliers/rfps.html>; bidding by a per-RFP email distribution list |
| To | `SupplyRFP@PacifiCorp.com`. The RFP page says: "For questions or more information, please email SupplyRFP@PacifiCorp.com." |
| Blocking clause | <https://www.pacificorp.com/terms>: "The Content on this site may not be used, unless specifically permitted in writing by PacifiCorp or the owner of the rights to the Content." Content is "the information included on this site, including but not limited to documents, images and graphics" |
| Subject | Permission request: listing PacifiCorp's RFPs on Inframation, with a link to your page (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address on your Energy Supply Requests for Proposal page. This
> isn't a bid question and needn't go in any bidder Q&A. Could you pass it to whoever grants permission to use
> your website content?
>
> We'd like to list PacifiCorp's RFPs so that more developers see them. Your terms of use say: "The Content on
> this site may not be used, unless specifically permitted in writing by PacifiCorp or the owner of the rights to
> the Content." They define Content to include "the information included on this site". So we've published
> nothing from your site, and I'm asking for written permission.
>
> For each RFP on https://www.pacificorp.com/suppliers/rfps.html, we'd show its title, posted dates, MW sought,
> technology, and contract type and state where given. Each entry would link to your page and carry the credit
> "Source: PacifiCorp, Energy Supply Requests for Proposal" (or your wording). It would appear on our site, in
> its API and feeds, and in readers' email alerts; not on social media unless you agree. We would not copy your
> RFP documents, forms, Q&A or logos, and we would take nothing you send only to bidders.
>
> An automated reader would load the page weekly, and daily while an RFP is open, at least two seconds between
> requests, following your robots.txt and naming itself with a contact link.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, or credit wording you
>    choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- *Text:* the terms also license "Reporters and media companies" to reproduce "Content intended for press
  release". *Inference:* that covers press releases, not the RFP page, and the letter does not rely on it
  (counsel item C-7).
- PacifiCorp's RFPs also go before its state commissions, such as the OPUC and the Utah PSC (general
  knowledge, not retrieved). Each docket system is its own source with its own terms (docs/28 §4, inference 3).

### 3.2 Southern California Edison (`us.rfp.sce`)

| | |
|---|---|
| Registry | link-out only, `terms_restrict_content`, value high, several RFOs a year; listing <https://www.sce.com/about-sce/energy-procurement/solicitations>; bidding on PowerAdvocate |
| To | `SCEEnergyProcurement@sce.com`. The Solicitations page says: "For any other inquiries about SCE's Request for Offers (RFOs), please email SCEEnergyProcurement@sce.com" |
| Blocking clause | <https://www.sce.com/terms-conditions/website-terms-of-use>: "Subject to the terms of this TOU, you may use the Website and the Website Content for your non-commercial personal or internal business use, but not for service bureau, time-sharing or other similar purposes." |
| Subject | Permission request: listing SCE's RFOs on Inframation, with a link to your page (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address on your Solicitations page. This isn't a bid question
> and needn't go in any bidder Q&A. Could you pass it to whoever grants permission to use your website content?
>
> We'd like to list SCE's Requests for Offers so that more developers see them. Your website terms of use say:
> "Subject to the terms of this TOU, you may use the Website and the Website Content for your non-commercial
> personal or internal business use, but not for service bureau, time-sharing or other similar purposes." A
> public listing isn't personal or internal use. So we've published nothing from your site, and I'm asking for
> written permission.
>
> For each RFO on https://www.sce.com/about-sce/energy-procurement/solicitations, we'd show its title, posted
> dates, MW sought, technology, and contract type where given. Each entry would link to your page and carry the
> credit "Source: Southern California Edison, Solicitations" (or your wording). It would appear on our site, in
> its API and feeds, and in readers' email alerts; not on social media unless you agree. We would not copy your
> RFO documents, protocols, Q&A or logos, and we would take nothing from PowerAdvocate.
>
> An automated reader would load the page weekly, and daily while an RFO is open, at least two seconds between
> requests, following your robots.txt and naming itself with a contact link. If you'd prefer, we could instead
> join your RFO Distribution List and take the same facts from your market notices.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFOs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we list your RFOs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, or credit wording you
>    choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFOs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- *Text:* the bot clause bars automated access only "in a manner that interferes with, destabilizes, degrades
  the performance of, or places an unreasonable or disproportionately large load on, the Website or our
  computing systems and network". *Inference:* one weekly request per listing is outside that on its face (C-5).
  The letter states the rate anyway.
- *Inference:* the noncommercial posture does not help here. The grant is for "personal or internal business
  use", and a public listing is neither. The letter says so rather than leaning on "free". The same reading is in
  the registry's EPE note.
- The Distribution List route is offered from the page's own words: "please submit the form using the link:
  Request to be added to SCE's RFO Distribution List". Joining is a human act, and a yes to it is not a yes to
  republishing the notices. Question 1 still has to be answered.
- The Newsroom editorial licence (C-7) is not relied on.

### 3.3 Austin Energy (`us.rfp.austin_energy`)

| | |
|---|---|
| Registry | link-out only, `terms_restrict_information`, value high, several RFPs a year; listing <https://austinenergy.com/contractors/working-with-austin-energy/rfps>; bidding through the City of Austin |
| To | `custinfo@austinenergy.com`, marked for the **Manager of Utility Marketing Services**. The terms name that role as the one whose "explicit written permission" authorises use. Under "How to Contact Us", the same page gives "Legal Services Austin Energy 4815 Mueller Blvd. Austin, TX 78723-3573". Its "Email: Legal Services" link opens `custinfo@austinenergy.com` with a preset subject, URL-encoded in the page's HTML, that decodes to "AE Legal Services - Web Site Privacy Policy" |
| Copy by post (optional) | Manager of Utility Marketing Services, c/o Legal Services, Austin Energy, 4815 Mueller Blvd., Austin, TX 78723-3573 |
| Blocking clause | <https://austinenergy.com/about/company-profile/privacy-policy>: "Many areas of our website provide data, information and images that are copyrighted. Austin Energy owns the copyright in such works, and no unauthorized use is permitted, and no authorized use is allowed without explicit written permission of the Austin Energy Manager of Utility Marketing Services." |
| Subject | Attn: Manager of Utility Marketing Services. Permission request: listing Austin Energy's RFPs on Inframation (not a bid question) |
| Pre-step | The page says its terms are "also found on the City of Austin website". That City page was not read (registry note). Read it in a browser before sending. If it says anything about reuse or automated access that differs, give it to the legal lane first |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address under "How to Contact Us" on your privacy policy and
> terms of use page. This isn't a bid question. Could you pass it to the Manager of Utility Marketing Services?
>
> We'd like to list Austin Energy's RFPs so that more developers see them. Your terms say: "Many areas of our
> website provide data, information and images that are copyrighted. Austin Energy owns the copyright in such
> works, and no unauthorized use is permitted, and no authorized use is allowed without explicit written
> permission of the Austin Energy Manager of Utility Marketing Services." Your terms already allow links to any
> page, so I'm asking only about the facts. We've published nothing from your site.
>
> For each RFP on https://austinenergy.com/contractors/working-with-austin-energy/rfps, we'd show its title,
> posted dates, MW sought, technology, and contract type where given. Each entry would link to your page and
> carry the credit "Source: Austin Energy, Requests for Proposals (RFPs)" (or your wording). It would appear on
> our site, in its API and feeds, and in readers' email alerts; not on social media unless you agree. We would
> not copy your RFP documents, forms, Q&A or logos, and we would take nothing from the City of Austin's
> procurement system.
>
> An automated reader would load the page weekly, and daily while an RFP is open, at least two seconds between
> requests, following your robots.txt and naming itself with a contact link.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, or credit wording you
>    choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- *Text:* "Austin Energy grants permission for websites to link to the home page of austinenergy.com or to any
  page within." The letter mentions the link permission and does not quote it back.
- *Text:* "As a governmental entity, Austin Energy is subject to the Texas Public Information Act". *Inference:*
  this is the C-3 question by analogy, whether a public body may restrict reuse of public records. The letter
  does not argue it (`docs/37` §3(a): a plain request draws a business answer).
- docs/28 §8.2: check whether the year tabs are server-rendered before building. If a tab needs JavaScript, a
  yes still leaves the row manual.

### 3.4 Arizona Public Service (`us.rfp.aps`)

| | |
|---|---|
| Registry | link-out only, `terms_restrict_information`, value high, annual; listing <https://www.aps.com/en/About/Our-Company/Doing-Business-with-Us/Resource-Planning/Request-for-Proposals>; bidding on PowerAdvocate |
| To | `aps@aps.com`. The terms say: "Please direct any questions or comments you have concerning the Site or this Agreement to aps@aps.com" |
| Copy by post (optional) | the same sentence goes on: "APS' Customer Service Mailing Address is PO Box 53999, Phoenix, AZ 85072-3999." |
| Blocking clause | <https://www.aps.com/en/Utility/Regulatory-and-Legal/Terms-of-Use>: "Commercial use, broadcasting or otherwise disseminating any Content in any media, without the prior written consent of APS, is strictly prohibited." Content "means any and all information, data, software, content, materials, documents, text" and more |
| Subject | Permission request: listing APS's RFPs on Inframation, with a link to your page (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address in your website Terms of Use, which ask for questions
> about them to come here. This isn't a bid question. Could you pass it to whoever grants permission to use your
> website content?
>
> We'd like to list APS's requests for proposals so that more developers see them. Your terms say: "Commercial
> use, broadcasting or otherwise disseminating any Content in any media, without the prior written consent of
> APS, is strictly prohibited." Content includes "any and all information, data". They also say "APS encourages
> you instead to tell others about the Site and direct them to the Site", and that is what we'd like to do. We've
> published nothing from your site, and I'm asking for written consent.
>
> For each RFP on your Request for Proposals page, we'd show its title, posted dates, MW sought, technology, and
> contract type where given. Each entry would link to your page and carry the credit "Source: Arizona Public
> Service, Request for Proposals" (or your wording). It would appear on our site, in its API and feeds, and in
> readers' email alerts; not on social media unless you agree. We would not copy your RFP documents, forms, Q&A
> or logos, and we would take nothing from PowerAdvocate.
>
> An automated reader would load the page weekly, and daily while an RFP is open, at least two seconds between
> requests, following your robots.txt and naming itself with a contact link.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, or credit wording you
>    choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- `LawDept@aps.com` is not used. The terms give it as APS's "designated agent" for notices "of alleged copyright
  infringement", which is a different purpose.
- *Text:* the automation clause catches only technology used "to attempt to gain unauthorized access" (C-5).
  The letter states the rate anyway.
- A yes on title, dates and link alone answers C-2 for APS.

### 3.5 El Paso Electric (`us.rfp.el_paso_electric`)

| | |
|---|---|
| Registry | link-out only, `terms_restrict_content`, value high, annual; listing <https://www.epelectric.com/company/request-for-proposals>; states TX and NM; no portal named |
| To | `public_relations@epelectric.com`. The terms say: "If you have a comment, question or request, or if you need to contact EPE, there please do so via the following information." Then: "Public Relations Department c/o Public Relations P.O. Box 982 El Paso, Texas 79960" and "We can also be reached by e-mail at public_relations@epelectric.com." |
| Blocking clause | <https://www.epelectric.com/company/terms-of-use>: "The content of the Site is for your personal, noncommercial use." and "For commercial use of any portion of the contents you must secure the written consent from the appropriate content provider." |
| Subject | Permission request: listing El Paso Electric's RFPs on Inframation, with a link to your page (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address in your website terms of use. This isn't a bid
> question. Could you pass it to whoever grants permission to use your website content?
>
> We'd like to list El Paso Electric's RFPs so that more developers see them. Your terms say: "The content of
> the Site is for your personal, noncommercial use." A public listing isn't personal use, even on a free site.
> So we've published nothing from your site, and I'm asking for written consent.
>
> For each RFP on https://www.epelectric.com/company/request-for-proposals, we'd show its title, posted dates,
> MW sought, technology, and contract type and state where given. Each entry would link to your page and carry
> the credit "Source: El Paso Electric, Request for Proposals" (or your wording). It would appear on our site,
> in its API and feeds, and in readers' email alerts; not on social media unless you agree. We would not copy
> your RFP documents, forms, Q&A or logos, and we would take nothing you send only to bidders.
>
> An automated reader would load the page weekly, and daily while an RFP is open, at least two seconds between
> requests, following your robots.txt and naming itself with a contact link.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, or credit wording you
>    choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- *Inference, as in the registry:* "Grant is personal, noncommercial; a public listing is not "personal", so the
  noncommercial posture does not convert this to a grant." The letter says this in one sentence, so a reply
  cannot rest on a misunderstanding.
- `customercare@epelectric.com` appears on the same page for text-message help. Not used.
- NMPRC and PUCT filings are separate sources.

### 3.6 Portland General Electric (`us.rfp.portland_general`)

| | |
|---|---|
| Registry | link-out only, `terms_restrict_information`, value high, an RFP about every 2 years; listing <https://portlandgeneral.com/about/who-we-are/resource-planning/procuring-clean-energy>; no portal named |
| To | **`[the RFP contact address in PGE's 2025 All-Source RFP Main Document]`**. The listing page captured for docs/28 names none, and the Main Document is a PDF the connector must never fetch, so you read it in a browser. Fallback: PGE Customer Service at <https://portlandgeneral.com/help/connect>. The Legal Notices say: "Please direct any questions or comments you have concerning the Terms and Conditions to PGE Customer Service or call 800-542-8818." |
| Blocking clause | <https://portlandgeneral.com/legal-notices>: "Commercial use or broadcasting or otherwise disseminating any information in the proprietary materials in any media, without prior written approval from PGE, is strictly prohibited. For example, you may not post any content from the site on other Web sites, news groups, mail lists or electronic bulletin boards." |
| Subject | Permission request: listing PGE's RFPs on Inframation, with a link to your page (not a bid question) |
| Pre-step | Find the RFP contact (above). If it is a named person's address rather than a role mailbox, use the fallback |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address [in your 2025 All-Source RFP document / on your
> Legal Notices page]. This isn't a bid question and needn't go in any bidder Q&A. Could you pass it to whoever
> grants approval to use your website content?
>
> We'd like to list PGE's requests for proposals so that more developers see them. Your Legal Notices say:
> "Commercial use or broadcasting or otherwise disseminating any information in the proprietary materials in
> any media, without prior written approval from PGE, is strictly prohibited." So we've published nothing from
> your site, and I'm asking for written approval.
>
> For each RFP on your Requests for Proposal (RFP) and Resource Acquisition page, we'd show its title, posted
> dates, MW sought, technology, and contract type where given. Each entry would link to your page and carry the
> credit "Source: Portland General Electric, Requests for Proposal (RFP) and Resource Acquisition" (or your
> wording). It would appear on our site, in its API and feeds, and in readers' email alerts; not on social media
> unless you agree. We would not copy or download your RFP documents, appendices, form agreements, Q&A or logos.
>
> An automated reader would load the page weekly, and daily while an RFP is open, at least two seconds between
> requests, following your robots.txt and naming itself with a contact link.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, or credit wording you
>    choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- The Legal Notices also give a "designated agent" for notices of claimed infringement. That block names an
  individual and serves another purpose. Not used.
- *Text:* "For example, you may not post any content from the site on other Web sites". *Inference:* a yes must
  cover posting facts from the page on our site, and the letter's description does. A reply that approves only
  "linking" does not.
- The OPUC docket (UM 2274 and successors) is a separate source.

### 3.7 Puget Sound Energy (`us.rfp.puget_sound_energy`)

| | |
|---|---|
| Registry | manual only, `terms_bar_automation`, value high, annual or more often; listing <https://www.pse.com/en/pages/energy-supply/acquiring-energy>; bidding on <https://rfp.pse.com/> (registration) |
| To | `AllSourceRFPmailbox@pse.com`. The page says: "Questions about the All-Source RFP may be sent to AllSourceRFPmailbox@pse.com." It also lists a DER RFP mailbox. Send to one only |
| Copy by post (optional) | the terms say: "You may also write to us at Customer Care, Puget Sound Energy, 355 110th Ave NE Bellevue, P.O. Box 97034, Bellevue, Wash., 98009-9734." |
| Blocking clauses | <https://www.pse.com/en/pages/terms-of-use>, under "For example, you will not:": "Copy, reproduce, distribute, publicly perform or publicly display all or portions of the Services, except as expressly permitted by us or our licensors;" and "Use any data mining, robots or similar data gathering or extraction methods designed to scrape or extract data from the Services;" |
| Subject | Permission request: reading and listing PSE's RFPs on Inframation (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address on your Acquiring New Energy Sources page. This isn't
> a question about the All-Source RFP and needn't go in any bidder Q&A. Could you pass it to whoever grants
> permission to use your website content?
>
> We'd like to list PSE's RFPs so that more developers see them. Your terms of use say you will not "Copy,
> reproduce, distribute, publicly perform or publicly display all or portions of the Services, except as
> expressly permitted by us or our licensors". So we've published nothing from your site, and I'm asking for
> written permission.
>
> For each RFP on https://www.pse.com/en/pages/energy-supply/acquiring-energy, we'd show its title, posted
> dates, MW sought, technology, and contract type where given. Each entry would link to your page and carry the
> credit "Source: Puget Sound Energy, Acquiring New Energy Sources" (or your wording). It would appear on our
> site, in its API and feeds, and in readers' email alerts; not on social media unless you agree. We would not
> copy your RFP documents, forms, Q&A or logos, and we would take nothing from your RFP submission portal.
>
> Your terms also say you will not "Use any data mining, robots or similar data gathering or extraction methods
> designed to scrape or extract data from the Services". So I'm asking about reading the page as well as listing
> it. We'd like an automated reader to load it weekly, and daily while an RFP is open, at least two seconds
> between requests, following your robots.txt and naming itself with a contact link. If you'd rather not, a
> person on our team would check the page in an ordinary browser instead.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we read your page and list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, a person checking the page
>    instead of an automated reader, or credit wording you choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- A yes to the person-checks-the-page option alone leaves the registry at `manual_only` with `automated_reading:
  not_allowed`, but lets the title, dates and link be published. That needs the link-out surface (docs/28 §3.3).
- WUTC filings are a separate source.

### 3.8 Consumers Energy (`us.rfp.consumers_energy`)

| | |
|---|---|
| Registry | manual only, `terms_bar_automation`, value high, two RFPs a year; listing <https://www.consumersenergy.com/residential/savings-and-clean-energy/renewable-energy/customer-generation/electricity-sales/electric-power-notices>; bidding on Enel X Exchange (registration) |
| Channel | the **Contact Us form** on the Electric Power Notices page (fields: Name, Business Name, Email, Phone Number, Questions or Comments). Submit it yourself in an ordinary browser: the terms allow "generally available third-party web browsers" and nothing else. The only email the terms give is for security reports, so it is not used. Send the full text below by email only when they reply with an address |
| Blocking clauses | <https://www.consumersenergy.com/terms-and-conditions>: "You may not reproduce (except as noted above), publish, transmit, distribute, display, modify, create derivative works from, sell or participate in any sale of or exploit in any way, in whole or in part, any of the Contents, the Site, the App or any related software." and "You agree, further, not to use or attempt to use any engine, software, tool, agent or other device or mechanism (including without limitation browsers, spiders, robots, avatars or intelligent agents) to navigate or search the Site or App other than the search engine and search agents available from Consumers Energy on the Site or App and other than generally available third-party web browsers (e.g., Google Chrome or Microsoft Explorer)." |
| Subject (email) | Permission request: reading and listing Consumers Energy's RFPs on Inframation (not a bid question) |

**Form text** (about 140 words; the form's length limit is unknown):

> Permission request, not a bid. I'm [Owner name], [Role] at Inframation (inframation.io), a free website
> listing energy solicitations for developers. Your terms say we may not "publish, transmit, distribute,
> display" any of your site's Contents, and they bar tools other than ordinary browsers. We'd like written
> permission to list each RFP on your Electric Power Notices page: title, dates, MW sought, technology and a link
> back, credited to Consumers Energy. No RFP documents and nothing from Enel X. We'd read the page weekly, by an
> identified automated reader or, if you prefer, by hand. Free site, no paid plans today; it is run by
> [Organisation]. You can say no or withdraw at any time. Who should I send the full request to? I'm at
> [Contact email]. Reply "stop" and I won't write again. [Postal address]

**Full text** (for the address they give):

> Hello,
>
> Thank you for replying to my note on your Electric Power Notices form. I'm [Owner name], [Role] at Inframation
> (inframation.io), a free website that lists energy projects and the solicitations developers bid into. This
> isn't a bid question.
>
> We'd like to list Consumers Energy's RFPs so that more developers see them. Your terms say: "You may not
> reproduce (except as noted above), publish, transmit, distribute, display, modify, create derivative works
> from, sell or participate in any sale of or exploit in any way, in whole or in part, any of the Contents, the
> Site, the App or any related software." So we've published nothing from your site, and I'm asking for written
> permission.
>
> For each RFP on your Electric Power Notices page, we'd show its title, posted dates, MW sought, technology,
> and contract type and market where given. Each entry would link to your page and carry the credit "Source:
> Consumers Energy, Electric Power Notices" (or your wording). It would appear on our site, in its API and
> feeds, and in readers' email alerts; not on social media unless you agree. We would not copy your RFP
> documents, forms or logos, and we would take nothing from Enel X Exchange.
>
> Your terms also bar using "any engine, software, tool, agent or other device or mechanism (including without
> limitation browsers, spiders, robots, avatars or intelligent agents) to navigate or search the Site" other
> than ordinary web browsers. So I'm asking about reading the page as well as listing it. We'd like an automated
> reader to load it weekly, and daily while an RFP is open, at least two seconds between requests, following
> your robots.txt and naming itself with a contact link. If you'd rather not, a person on our team would check
> the page in an ordinary browser instead.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we read your page and list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, a person checking the page
>    instead of an automated reader, or credit wording you choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- *Inference:* the browser clause catches a headless browser too ("including without limitation browsers").
  Only a person using an ordinary browser is outside it, so the manual fallback means exactly that.
- MPSC filings are a separate source (docs/28 §9, C-11).

### 3.9 Dominion Energy Virginia (`us.rfp.dominion_virginia`)

| | |
|---|---|
| Registry | manual only, `terms_bar_automation`, value high, annual; listing <https://www.dominionenergy.com/about/delivering-energy/renewable-projects/solar-onshore-wind-and-energy-storage-proposals>; PPA and REC bidding on the Accion portal (registration) |
| To | `DEVCleanEnergyRFP-PPA@dominionenergy.com`. The page says: "For additional information or questions regarding the PPA RFP to be issued October 1, 2026, contact DEVCleanEnergyRFP-PPA@dominionenergy.com." Not the Acquisition mailbox, which takes bid submissions and Q&A questions |
| Copy by post (optional) | the only legal office the terms name, given for copyright notices: "Dominion Energy Legal Department – Counsel for Intellectual Property Matters Dominion Energy, Inc. 120 Tredegar Street Richmond, VA 23219". Address it to the office. That block's email address is an individual's and is not used |
| Blocking clauses | <https://www.dominionenergy.com/terms-and-conditions>: "You agree You will not access or use the Site Material for any public or commercial purpose, except as expressly permitted herein." and, under "In exchange for the license to use the Website, You agree You will not:", "use spiders, robots, data mining techniques or other automated devices or programs to catalog, download or otherwise reproduce, store or distribute content available on this Website;" |
| Subject | Permission request: reading and listing Dominion Energy Virginia's RFPs on Inframation (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address on your Solar, Onshore Wind and Energy Storage
> Proposals page. This isn't a question about the PPA RFP and needn't go in any bidder Q&A. Could you pass it to
> whoever grants permission to use your website content?
>
> We'd like to list Dominion Energy Virginia's RFPs so that more developers see them. Your terms say: "You agree
> You will not access or use the Site Material for any public or commercial purpose, except as expressly
> permitted herein." So we've published nothing from your site, and I'm asking for written permission. Your
> terms already allow links that don't frame or alter your pages, and ours wouldn't.
>
> For each RFP on your Solar, Onshore Wind and Energy Storage Proposals page, we'd show its title, posted dates,
> MW sought, technology, and contract type where given. Each entry would link to your page and carry the credit
> "Source: Dominion Energy Virginia, Solar, Onshore Wind and Energy Storage Proposals" (or your wording). It
> would appear on our site, in its API and feeds, and in readers' email alerts; not on social media unless you
> agree. We would not copy your RFP documents, intent-to-bid forms, confidentiality agreements, Q&A or logos,
> and we would take nothing from the Accion portal.
>
> Your terms also say you will not "use spiders, robots, data mining techniques or other automated devices or
> programs to catalog, download or otherwise reproduce, store or distribute content available on this Website".
> So I'm asking about reading the page as well as listing it. We'd like an automated reader to load it weekly,
> and daily while an RFP is open, at least two seconds between requests, following your robots.txt and naming
> itself with a contact link. If you'd rather not, a person on our team would check the page in an ordinary
> browser instead.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we read your page and list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only, a person checking the page
>    instead of an automated reader, or credit wording you choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- *Text:* "You may link to the Website, provided (i) Your link does not alter in any way the visual presentation
  or appearance of the Website, or create a frame around the Website;". The letter paraphrases this, because an
  ellipsis inside a quotation would misquote.
- *Text:* the PPA RFP was "to be issued October 1, 2026". *Inference:* it is probably open as this is written,
  so the mailbox is busy with bidders and may run under a communications protocol. That is why the subject line
  says "not a bid question". If the RFP's rules restrict non-bidder contact with that mailbox, send by post only.
- The Virginia SCC docket is robots-disallowed (`us.rfp.virginia_scc`). For Dominion's own RFPs, permission is
  the only route.

### 3.10 Entergy (`us.rfp.entergy`)

| | |
|---|---|
| Registry | manual only, `blocked`, value high, 5 to 10 RFPs a year across five operating companies; listing <https://rfp.entergy.com/> (answers 200; no robots.txt); an independent monitor runs bidding |
| Why blocked | "Terms page on www.entergy.com answers 403 to an identified client; rfp.entergy.com footer "© 2022 Entergy Corporation, All Rights Reserved."" The RFP site links its terms as "Termss of Use" to <http://www.entergy.com/privacy_legal/legal.aspx> |
| **Pre-step** | Open <http://www.entergy.com/privacy_legal/legal.aspx> in an ordinary browser and save a PDF. Give it to the legal lane, which quotes the operative clauses into the registry row and docs/28 §5. If the terms neither restrict reuse of information nor bar automated reading, **do not send**: the legal lane reclassifies the row. Otherwise paste the operative clause into the draft. A third-party summary says the terms bar scraping (registry note); it is unverified and not relied on |
| To | **`[the contact address on the current RFP's own page on rfp.entergy.com, e.g. the 2026 EML Renewables and Storage RFP; read in a browser]`**. Use it only if it is a role mailbox. The RFP site's footer names "Entergy Services, LLC" and "System Planning and Operations"; address the request to that function |
| Also relevant | the footer: "The Entergy name and logo are registered service marks of Entergy Corporation may not be used without the express, written consent of Entergy Corporation." The credit line uses the name, so question 1 asks for that too |
| Subject | Permission request: reading and listing Entergy's RFPs on Inframation (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address on [RFP name]'s page on your RFP site. This isn't a
> bid question and needn't go to the independent monitor or into any bidder Q&A. Could you pass it to whoever
> grants permission to use your website content?
>
> We'd like to list the RFPs on rfp.entergy.com so that more developers see them. Your RFP site's terms of use
> link points to www.entergy.com, which answers our reader with HTTP 403, so I read your terms in a browser.
> They say: "[operative clause, verbatim]" So we've published nothing from your sites, and I'm asking for
> written permission.
>
> For each RFP on https://rfp.entergy.com/, we'd show its title, operating company, posted dates, MW sought,
> technology, and contract type where given. Each entry would link to the RFP's page and carry the credit
> "Source: Entergy, rfp.entergy.com" (or your wording). It would appear on our site, in its API and feeds, and
> in readers' email alerts; not on social media unless you agree. We would not copy your RFP documents, forms,
> Q&A or logos, and we would take nothing that needs registration.
>
> [If the terms bar automated reading:] Your terms also say: "[automation clause, verbatim]" So I'm asking about
> reading the site as well as listing it. We'd like an automated reader to load the RFP index weekly, and daily
> while an RFP is open, at least two seconds between requests, following your robots.txt and naming itself with
> a contact link. If you'd rather not, a person on our team would check it in an ordinary browser instead.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we read your RFP site and list your RFPs as described, naming Entergy in the credit?
> 2. If not, would a narrower permission work, such as title, dates and a link only, a person checking the site
>    instead of an automated reader, or credit wording you choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> site and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your sites. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- This is the highest-value single yes in the registry: one listing, five operating companies, 5 to 10 RFPs a
  year.
- A browser read by a person is the docs/13 §1.7 pattern. Routing the connector through browser egress to get
  past the 403 is not: it needs legal sign-off (docs/20 §12; docs/28 C-12).
- The LPSC, APSC, MPSC and PUCT filings are separate sources.

### 3.11 Duke Energy (`us.rfp.duke_energy`)

| | |
|---|---|
| Registry | manual only, `blocked`, value high, annual (Carolinas solar), per IRP elsewhere; four operating companies; `access: unverified` |
| Why blocked | "Site answers 403 "Access Denied" to an identified client, robots.txt included; terms not retrieved" (Akamai, 2026-10-21) |
| **Pre-step** | In an ordinary browser: (1) open <https://www.duke-energy.com/legal/terms-of-use>, save a PDF and give it to the legal lane to quote; (2) find the page or pages where Duke posts its solicitations, and the contact address those pages name. Send this one whatever the terms say: the 403 itself is the gate for automated reading (docs/28 C-12: "the 403 governs") |
| To | **`[the procurement or RFP mailbox named on Duke's solicitation page; a role mailbox only]`** |
| Subject | Permission request: reading and listing Duke Energy's RFPs on Inframation (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address on your [page title] page. This isn't a bid question
> and needn't go in any bidder Q&A. Could you pass it to whoever grants permission to use your website content?
>
> We'd like to list Duke Energy's RFPs so that more developers see them. Your website answers our reader, which
> names itself in every request, with "Access Denied" (HTTP 403), robots.txt included. We've read nothing from
> it automatically and published nothing from it. I read your terms in a browser instead. [They say:
> "[operative clause, verbatim]" / They don't address reuse or automated reading, but your site's response
> does.] I'm asking for written permission.
>
> For each RFP on [listing URL], we'd show its title, operating company, posted dates, MW sought, technology, and
> contract type and state where given. Each entry would link to your page and carry the credit "Source: Duke
> Energy, [page title]" (or your wording). It would appear on our site, in its API and feeds, and in readers'
> email alerts; not on social media unless you agree. We would not copy your RFP documents, forms, Q&A or logos,
> and we would take nothing from any bid portal.
>
> We'd like an automated reader to load that page weekly, and daily while an RFP is open, at least two seconds
> between requests, following your robots.txt and naming itself with a contact link. If you'd rather not, a
> person on our team would check the page in an ordinary browser instead.
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we read your page and list your RFPs as described, and would you let our reader through at that rate?
> 2. If not, would a narrower permission work, such as title, dates and a link only, a person checking the page
>    instead of an automated reader, or credit wording you choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- *Inference:* an Akamai 403 to a datacentre client is often a default bot-manager rule, not a decision anyone
  made about us. Asking is how to find out. Until Duke answers, it stays as it is: no automated request, and no
  escalation to browser egress without legal sign-off (docs/20 §12).
- If the listing turns out to sit on a portal that needs registration, the row becomes `login`, so excluded
  (docs/28 §3.2 rule 1). Do not send in that case.
- The NCUC is behind a challenge too (registry note). Each state docket is a separate source.

### 3.12 Xcel Energy (`us.rfp.xcel`)

| | |
|---|---|
| Registry | manual only, `terms_not_located`, value high, annual across three operating companies (PSCo, SPS, NSP) in eight states; listing <https://www.xcelenergy.com/company/rates_and_regulations/resource_plans>; bidding on XpressDRIVE; robots.txt absent (404) |
| Why not cleared | "No website-use terms located (only account-holder terms linked)". The footer is client-rendered, and the served HTML links only a My Account Terms PDF |
| **Pre-step** | In an ordinary browser, find xcelenergy.com's website terms of use (try the rendered footer), save a PDF and give it to the legal lane to quote. If they restrict neither reuse nor automated reading, **do not send**: the row becomes a cleared candidate for the legal lane. Otherwise paste the clause in |
| To | **`[the RFP contact named on the PSCo or SPS All-Source RFP page; read in a browser; a role mailbox only]`** |
| Subject | Permission request: listing Xcel Energy's RFPs on Inframation (not a bid question) |

> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a free website that lists energy projects and the
> solicitations developers bid into. I found this address on your [RFP name] page. This isn't a bid question and
> needn't go in any bidder Q&A. Could you pass it to whoever grants permission to use your website content?
>
> We'd like to list the RFPs that Xcel Energy's operating companies post on your Resource Plans page, so that
> more developers see them. Your website terms say: "[operative clause, verbatim]" So we've published nothing
> from your site, and I'm asking for written permission.
>
> For each RFP on https://www.xcelenergy.com/company/rates_and_regulations/resource_plans, we'd show its title,
> operating company, posted dates, MW sought, technology, and contract type and state where given. Each entry
> would link to your page and carry the credit "Source: Xcel Energy, Resource Plans" (or your wording). It would
> appear on our site, in its API and feeds, and in readers' email alerts; not on social media unless you agree.
> We would not copy your RFP documents, forms, Q&A or logos, and we would take nothing from XpressDRIVE.
>
> [R-A if the terms allow automated reading; R-B, with its automation clause quoted, if they bar it.]
>
> Inframation is run by [Organisation]. It's free, with no paid plans and no advertising. It's built for
> developers and investors, which are commercial firms, and we may add paid plans later. If you'd rather permit
> only the free site, say so: we'd remove your RFPs before any paid plan began, unless you agreed otherwise.
>
> My questions:
>
> 1. May we [read your page and] list your RFPs as described?
> 2. If not, would a narrower permission work, such as title, dates and a link only[, a person checking the page
>    instead of an automated reader], or credit wording you choose?
>
> You can withdraw permission at any time by replying here. Within five business days we'd stop reading your
> page and remove your RFPs; alert emails already sent can't be recalled. Until you reply, we'll publish nothing
> from your site. If you'd rather not hear from me about this again, reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]

Notes.
- One yes covers three operating companies. If Xcel answers per operating company, record each answer on the
  one registry row, with conditions per company.
- The CO PUC, PUCT, NMPRC and MN PUC dockets are separate sources.

---

## 4. After sending

### 4.1 Follow-up, the same for every issuer

- **10 business days:** one reply on the same thread.
- **20 business days:** one message by the second channel in the draft's table (post, or the terms' own
  contact), saying the first was sent. If there is no second channel, skip this step.
- **About 8 weeks:** the legal lane records "no response" in the registry row's `notes`, and the request stops.
  No further chasers.
- **A "stop" reply** ends all contact with that address within 24 hours (docs/13 §8.2). Record it; never write
  again from any address.

### 4.2 Answers to expect, and what each allows

| Answer | What it allows |
|---|---|
| Yes, in writing, covering paid plans too | `reuse: attribution` and verdict cleared, publishing derived fields under the grant's words |
| Yes, for the free site only | `reuse: noncommercial`. Publishable only while `PLATFORM_POSTURE=noncommercial`. A flip to `commercial` hides the rows at once (docs/26 §5), which is the promise the letter makes |
| Yes, title, dates and link only | Still `title_date_link`. Publishable once the link-out surface exists (docs/28 §3.3); the answer settles C-2 for that issuer |
| Yes to listing, no to the automated reader | `automated_reading: not_allowed` stays. A person checks the page; publication as granted |
| Yes, with a credit wording | Record the wording verbatim in the row's `attribution`. It renders as given |
| No | Nothing from that site, and C-1 is closed for that issuer. *Inference:* using the same facts from the issuer's regulator docket after a written refusal is a separate terms question for that docket (docs/28 §4 inference 3, C-11). It still needs counsel's view first, because it would look like a way around the refusal |
| Silence | Status quo. Silence is not consent |

Who may grant, and whether a reply from a role mailbox is enough evidence, is counsel item C-10. Until counsel
answers, ask a role-mailbox "yes" which function is giving the permission, and record that role.

### 4.3 What changes once permission arrives (listed only; nothing is changed here)

1. **Register first.** Add a docs/13 §6 row for the issuer's `source_id`, then the `data/sources.yaml` entry.
   `scripts/check_manifest_licences.py` refuses a manifest more permissive than §6 (docs/28 §10).
2. **Registry row:**
   - `verdict`, `automated_reading`, `publish`, `reuse`, `manifest` as §4.2 says;
   - `basis: written_permission`. This is a new value: docs/28 §3.2 gains a rule ahead of rule 1, because a
     grant overrides the site's terms for its scope;
   - `terms_quote` gains the grant's operative sentence, verbatim;
   - `notes` gets the date sent, the date granted, the granting role (not a person's name), the conditions and
     the withdrawal address;
   - keep the terms-drift check (docs/28 §8.2). A grant does not stop the terms changing.
3. **Social:** a grant does not cover social media unless it says so (every letter says "not on social media
   unless you agree"). But `services/social/editorial.py` admits every `open` and `attribution` source
   (`SOCIAL_REUSE_CLASSES` is the posture's publishable set minus `noncommercial`), and it has no per-source
   exclusion. So a granted row set to `attribution` would become postable at once, which breaks the letter's
   promise. Before the first grant is wired, the coordinator needs a per-source social opt-in, or the row stays
   out of the manifest until one exists.
4. **The letter** stays in private storage. The repo holds the quote, date and role only (`CLAUDE.md`: store the
   minimum personal data).

### 4.4 Keeping the withdrawal promise ("within five business days")

What makes the promise keepable with what exists today:

1. Set the row to `automated_reading: not_allowed`. The connector fails closed on any other value (docs/28
   §8.2).
2. Set the manifest to `reuse: restricted`, `publication: none`. After a deploy, the visibility predicate hides
   every record from that source on every non-admin surface: site, API, feeds and alerts (docs/21 §8).
3. Mark each record as a takedown in the admin record screen, for the audit trail.
4. Confirm to the issuer in writing, and record the date.

*Inference:* steps 1 and 2 are two lines and one deploy, so five business days leaves margin for a solo owner.
Alert emails already delivered cannot be recalled, and the letter says so.

---

## 5. Not drafted here: the next batch and the other routes

| Issuer | Value, verdict | Route | Channel found 2026-10-21 |
|---|---|---|---|
| Pacific Gas and Electric (`us.rfp.pge`) | medium, manual only (`terms_bar_automation`) | template, R-B. Post is the main channel | the terms say "You may direct any questions about these Terms of Use to:" "Pacific Gas and Electric Company Director, Digital Channels 300 Lakeside Drive, Suite 210 Oakland, CA 94612". The procurement page's only mailbox is for accessibility, so it is not used |
| DTE Electric (`us.rfp.dte`) | high, excluded (`terms_bar_linking`) | template, R-D. It must ask for the link as well; no standing listing | not looked for |
| Public Service Company of New Mexico (`us.rfp.pnm`) | high, link-out only | template, R-A | not looked for |
| Hawaiian Electric (`us.rfp.hawaiian_electric`) | high, link-out only | template, R-A | not looked for |
| Idaho Power (`us.rfp.idaho_power`) | high, excluded (`terms_bar_manual_and_derivative_use`) | template, R-D | the registry's `bid_portal` names `ResourceRFP@idahopower.com` for supplier IDs; check its stated purpose first |
| NV Energy (`us.rfp.nv_energy`), Salt River Project (`us.rfp.srp`) | high, manual only (terms not located; blocked) | browser read first, then R-C | not looked for |
| Long Island Power Authority (`us.rfp.lipa`) | medium, excluded | R-D to LIPA. The listing host, psegliny.com, bars deep links, so the request covers both sites | not looked for |
| SDG&E (`us.rfp.sdge`) and the other link-out-only rows | medium and low | template, R-A | not looked for |
| SCPPA, Illinois IPA, CPA, Georgia PSC, Maryland PSC, Santee Cooper, Massachusetts and other silent public bodies | high to low, manual only (`terms_not_located`, `terms_silent`) | C-3 plus a one-line confirmation (docs/28 §9), not this letter | — |
| NYSERDA, SDCP, MCE, TVA, Maine PUC, the four ACES co-ops | manual only (`blocked`) | browser read first (docs/28 §9) | — |
| Puerto Rico (`us.rfp.puerto_rico_preb`) | excluded (`login`) | none: never register on a portal (C-9) | — |

---

## 6. Facts used here, and their limits

- **Clause quotes** come from the registry's `terms_quote`, checked word for word against the 2026-10-21
  captures by the docs/28 build. They were not re-fetched for this document, and §0.3 asks the owner to re-read
  each page before sending.
- **Contact channels** were read from the docs/28 captures of each listing and terms page, which are in the
  session scratchpad and not committed. No issuer host was contacted for this document. The four channels left
  as placeholders (PGE, Entergy, Duke, Xcel) were not in the captures, or could not be captured.
- **The bot's URLs:** one GET each of <https://inframation.io/bot> and <https://inframation.io/contact>,
  2026-10-21, both 404 (our own site). <https://inframation.io/attribution> and `/legal/reuse` answered 200.
- **"No paid plans and no advertising":** `/pricing` prints "Paid tiers are not currently offered; every
  published record is free to read." while the posture is `noncommercial` (`web/templates/pricing.html`, docs/26
  §3(i)). A search of `web/templates` and `web/static/js` on 2026-10-21 found no advertising code. Re-check both
  before sending; the letters state them as facts.
- **"Built for developers and investors":** decisions log 2026-10-10 (investors) and 2026-10-21 (developers
  first). The letters say who the site is built for, not who reads it. No readership has been measured.
- **What counsel still has to answer** is in docs/28 §7. These letters touch C-1, C-2, C-5, C-7 and C-10. A yes
  from an issuer makes C-1 and C-2 moot for that issuer only.

---

## 7. Commercial-reuse requests for the noncommercial sources (2026-10-22)

**Status: DRAFT, L1, under the same rules as §0.** Nothing here has been sent. The owner edits, sends and signs
each letter personally. No draft names or addresses a person. **Reads:** `docs/00-PLAN.md` decisions log, the
2026-10-22 rows "Bankable is the origination and syndication half", "The Bankable bridge waits on the six
noncommercial-only sources" and the later "'Build a deal' is built natively in Inframation" (which overrules the
first two in part and keeps these letters); `docs/26-platform-posture.md` §3 (iii), §5;
`docs/13-legal-data-rights.md` §3.2, §6 (the six rows), §7 item 15; `docs/18-bankable-bridge.md` (now the
reference design); `docs/28` §7 (C-4, C-10); the six rows of `data/sources.yaml` and the four AEP rows of
`data/vendored/rfp_issuers/issuers.yaml`. Unlike §1 to §6, every clause and channel in this section was fetched
live on 2026-10-22 (§7.7).

### 7.1 Why we are asking

Six of the platform's 136 sources (decisions log, 2026-10-22) allow noncommercial reuse only
(`reuse: noncommercial`): `us.rfp.pso`, `us.rfp.appalachian_power`, `us.rfp.swepco` and
`us.rfp.indiana_michigan_power` (operating companies of American Electric Power), and `us.tx.rrc.class_vi` and
`us.tx.rrc.datasets` (Railroad Commission of Texas). Inframation publishes them only because it runs under the
`noncommercial` posture (`docs/26`). The owner wants to keep the option of charging later, and wants deals built
on the platform.

**The plan changed during the day, and the letters follow the later decision.** Earlier on 2026-10-22 the owner
wanted to connect Inframation to Bankable (bankablehq.com), a commercial deal service run by the same team,
and held that bridge until the six sources answered. Later the same day the owner decided that "Build a deal" is
built natively in Inframation, with Bankable as the reference design and "not an integration target"; Bankable
"holds test and demo data only". That row keeps "the permission letters to the six `noncommercial` sources". So
the commercial uses the letters disclose are the current ones:

- paid plans on Inframation, possibly;
- "Build a deal": a private project a developer opens from any public record, with the public facts around it
  shown live beside their own notes and a financing checklist, shared with named collaborators. It may become a
  paid feature, and a later stage may show projects to invited funders (after counsel, `docs/18` §4).

Each letter also says, in one sentence, that the same team built Bankable, that it is separate, and that the
source's data will not go to it without asking first. A source that looks us up will find bankablehq.com; it
should hear about it from us.

Each letter says plainly what we use, how we credit and link back, what we publish from the source today, and
that we withdraw within five business days on request. Each asks for any one of three answers: written
permission for commercial reuse with attribution; the terms, including any fee, on which that use is available;
or confirmation that the stated use is already within the existing terms.

**What is live today.** Measured on our own API, 2026-10-22, about 18:30 UTC. `GET /v1/health` reports
`posture: noncommercial`.

| Source | Records on inframation.io | Since |
|---|---|---|
| The four AEP sources | 0 each, every status (`GET /v1/opportunities?status=unknown,announced,open,frozen,reinstated,closed,cancelled,awarded&source_id=<id>`) | none; the 2026-10-21 runs loaded study-database copies only |
| `us.tx.rrc.class_vi` | 20 proposals, credited "Source: Railroad Commission of Texas", each linking to the RRC's application-list PDF | first seen 2026-10-19 |
| `us.tx.rrc.datasets` | 0; no connector exists | none |

The decision row of 2026-10-22 on the RFP connector says it "reads six cleared issuers daily". The production API
holds no record from any of the six cleared issuers, so either the schedule is not running in production yet or
its rows have not loaded. The AEP letter has to say what is true on the day it is sent (§7.4, "Before you send").

Inframation links to Bankable nowhere today: no "bankablehq" in `web/` or `services/`, and none on the live home
or `/about` pages (2026-10-22).

### 7.2 Recipients and channels

**AEP: one letter covers all four companies.** Whose terms govern each RFP page:

- All four Energy Provider RFPs pages carry the same footer, "Use of this site constitutes acceptance of the AEP
  Terms and Conditions .", linking to <https://www.aep.com/terms/> (read 2026-10-22 on psoklahoma.com,
  appalachianpower.com, swepco.com and indianamichiganpower.com).
- Those terms say the content "is the property of American Electric Power Company, Inc., (AEP)" and reserve
  "the prior written permission of AEP".
- Each company's own "Legal Notices" page (`/company/legal-notices` on each host) carries regulatory notices,
  not site terms: "PCB Risk Based Disposal Approvals" on all four, plus D.R.S. legal notices and a boundary
  change on Indiana Michigan Power's.

*Inference:* AEP, the parent, grants for all four pages, so one letter goes to AEP and names all four. Each RFP
is its company's own procurement, so the letter asks that a grant name the four pages. A grant that names fewer
is recorded for those source ids only.

**AEP channel.** The terms name no contact for permissions. What AEP publishes:

| Route | What the page says | Use |
|---|---|---|
| Post to headquarters | <https://www.aep.com/contact/>: "AEP Corporate Headquarters 1 Riverside Plaza Columbus, Ohio 43215" | **Send 1:** the signed letter, to the Legal Department. *Inference:* the department that answers for a terms page; AEP does not publish it as a contact |
| Web form | Same page, "Send Us an Email". Department options (from the page's script): Customer Service, Economic Development, Educational Services, Investor Relations, Media Relations, Supplier/Vendor Assistance, Website Support, Other. Message limit: "Please enter 1000 characters or less". "By clicking submit, you agree to AEP's Privacy Policy." | **Send 2,** 15 business days after the letter: department "Other", the short note in §7.4.1. Too short for the letter itself |
| RFP mailboxes | For example <https://www.psoklahoma.com/business/b2b/energy-rfps/AllSource2026>: "Q&A will be compiled and posted here on a weekly basis. To submit a question, please email: PSO2026RFP@aep.com ." | **Not used.** Bidder Q&A for one RFP, which AEP publishes. §2 bars a mailbox with another stated purpose |
| Supplier/Vendor, Investor Relations, Media Relations | Same page | Not used: other stated purposes |

**RRC: one letter covers both sources.** Both rest on the same Site Policies page
(<https://www.rrc.texas.gov/site-policies/>). Neither the data-sets page nor the CO2 storage page carries an
item-level note that would override it (the policy's "Unless otherwise noted on an individual document"; both
pages searched 2026-10-22).

**RRC channel.**

| Route | What the page says | Use |
|---|---|---|
| Public assistance mailbox | <https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/>: "If you have any questions about the data sets, email the RRC at Publicassist@rrc.texas.gov ." | **Send 1,** asking that it go to the Office of General Counsel if that office decides |
| Office of General Counsel, by post | <https://www.rrc.texas.gov/contact-us>: "Office of General Counsel 512-463-7149" (no email given); "Mailing Address: P.O. Box 12967 Austin, Texas 78711-2967" | **Send 2,** 20 business days after send 1: the signed letter |
| Open Records | Site Policies: "Contact Open Records: Tel: 512-463-6947 Fax: 512-463-6684 open.records@rrc.texas.gov" | **Not used.** It takes requests under the Public Information Act; a permission question sent there may be logged as a records request |
| Injection-Storage Permits Unit | <https://www.rrc.texas.gov/oil-and-gas/applications-and-permits/injection-storage-permits/co2-storage/>: "contact the Injection-Storage Permits Unit at SIP@rrc.texas.gov before beginning a project" | Not used: it serves applicants |
| Central Records | <https://www.rrc.texas.gov/resource-center/research/data-sets-available-for-download/special-map-products/>: "Contact us at 512-463-6800 or IMS@rrc.texas.gov to place an order." | Not used: map orders |

**Does a purchase or subscription already grant commercial reuse? No.** Measured 2026-10-22:

- The data sets are free: "The following data sets are available from the Railroad Commission of Texas at free
  of charge." (data-sets page). There is no paid version to buy.
- "Subscription Services" (<https://www.rrc.texas.gov/resource-center/subscription-services/>) are email
  newsletters: "The subscription service is free; just provide an email address."
- What the RRC sells is printed maps, "Special Map Products Available for Purchase": for example OGM0180 "Top
  20 Producing Oil & Gas Fields" at "$23.00" and OGM0186 "Wells Permitted and Completed in the Eagle Ford Shale
  Play" at "$11.00". The page states no licence terms.
- The state policy that the Site Policies tell copiers to follow (§7.3) says: "A state agency may not charge a
  fee to access, use or reproduce information on its Web site or to link to information on its Web site, unless
  specifically authorized to do so by the Texas Legislature." *Inference:* a paid licence for website
  information may not be something the RRC can offer. The letter still asks for the terms, because the
  conditions of a permission matter more than its price.

So the RRC route is permission or confirmation, not purchase.

### 7.3 The clauses, re-read 2026-10-22

**AEP Terms and Conditions** (<https://www.aep.com/terms/>, HTTP 200, 82,120 bytes; on the page the text sits
under the heading "AEP - Legal Notice"). Verbatim:

> The content of this site, including but not limited to the text and images herein and their arrangement, is
> the property of American Electric Power Company, Inc., (AEP).
>
> AEP hereby authorizes you to copy and display the content herein, but for your personal use only. Any copy you
> make must include this copyright notice: Copyright 1996- 2026 by American Electric Power Company, Inc., all
> rights reserved. If attribution to AEP is included, limited quotations from the content are hereby permitted.
> You may not copy or display for redistribution to third parties for commercial purposes any portion of the
> content without the prior written permission of AEP.

Under "No Implied Licenses":

> Nothing contained herein shall be construed as conferring by implication, estoppel, or otherwise any license
> or right under any patent, trademark, copyright (except as expressly provided above), or proprietary rights of
> AEP or of any third party.

Under "Information Provided by You":

> Any information provided by you to AEP, including but not limited to feedback, data, questions, comments,
> suggestions, or the like, shall be deemed to be non-confidential; AEP shall have no obligation of any kind
> with respect to such information and shall be free to reproduce, use, disclose, and distribute the
> information to others without limitation.

*What the text says:* copying for personal use; limited quotations with attribution; no commercial
redistribution of any portion without written permission; no licence by implication. It does not grant
noncommercial redistribution in so many words.

*Inference (moderate confidence):* the `noncommercial` class of the four AEP rows rests on the
limited-quotations sentence read with the commercial bar, not on a general noncommercial licence, and the
no-implied-licence clause works against reading more into it. The rows' `derived_only` rule (the title as
posted, plus facts) fits that reading, so the class is not wrong. But the register quotes only two of these
passages: the copyright-notice sentence, the no-implied-licence clause and the non-confidentiality clause appear
nowhere in `data/sources.yaml`, `issuers.yaml` or docs/13. That is for the legal lane to add; it is not changed
here.

**RRC Site Policies** (<https://www.rrc.texas.gov/site-policies/>, HTTP 200, 68,255 bytes, the same size as
the 2026-09-25 read). Verbatim:

> The Railroad Commission of Texas (RRC) provides information via this website as a public service. Unless
> otherwise noted on an individual document, file, web page, or other website item, RRC grants permission to
> copy and distribute the information on its website for noncommercial use, as long as the content remains
> unaltered and is not presented in a misleading way. The RRC has no restrictions on linking to our website as
> long as a fee is not charged to access our material. However, we would appreciate acknowledgment of any items
> you use and request that you link to us in the proper context

> Because of the harm to State computer systems, including, but not limited to, the partial or total alteration
> or damage to data, or the interruption of computer services to other users, the RRC does not consent to
> access to its computer system by “robots,” denial of service attacks, or any other automated process that
> results in severe degradation of system performance.

> Entities or individuals who copy information from the RRC website or link to the RRC website must follow
> Sections 3 and 5 of The State Website Linking and Privacy Policy entitled, “Linking to State Agency Websites”
> and “Copying Information from State Agency Websites,” respectively.

**The State Website Linking and Privacy Policy, copying paragraph.** DIR's own page
(<https://dir.texas.gov/resource-library-item/state-website-linking-and-privacy-policy>) answered our one
request with a 403 challenge page ("Just a moment...") and was neither retried nor bypassed. The PDF host that
search results point to, prod.dir.texas.gov, did not resolve. The text below comes from a state agency's
reproduction, the Texas Board of Architectural Examiners (<https://www.tbae.texas.gov/links-privacy-policy/>,
read 2026-10-22). There the paragraph is headed "Copying and Use of Information by Web Site Owners Linking to
State Agency Sites", not the RRC's "Copying Information from State Agency Websites", and it is unnumbered.
Treat it as secondary until it is read on DIR's site:

> The information posted on a state agency Web site may be copied so long as it is presented in a
> non-misleading way and does not imply that either the site owner or the information, as it is presented on
> the site owner’s Web site, is endorsed by the State. Use of the information must identify the state agency
> that is the source of the information, its Web address, the date the information was copied from the state
> agency’s Web site by the site owner and must be accompanied by a statement that neither the site owner nor the
> information, as it is presented on the site owner’s Web site, is endorsed by the State or any state agency. A
> state agency may not charge a fee to access, use or reproduce information on its Web site or to link to
> information on its Web site, unless specifically authorized to do so by the Texas Legislature.

**RRC, Public Information Act page**
(<https://www.rrc.texas.gov/general-counsel/open-records/public-information-act/>):

> Texas Government Code, Chapter 552, gives you the right to access government records; and an officer for
> public information and the officer's agent may not ask why you want them.

What follows from these, kept apart:

1. *Text:* the RRC requires copiers to follow a state policy whose copying paragraph has no noncommercial limit,
   and four conditions: name the agency, give its web address, give the date copied, and state that neither
   the site nor the information is endorsed by the State or any state agency.
2. *Measured:* the 20 live Class VI records carry the credit "Source: Railroad Commission of Texas", the source
   URL and `retrieved_at` in their provenance. A search of the repository for "endorse" finds no such
   statement for the RRC (the World Bank rows carry one in `changes_statement`). The no-endorsement statement is
   therefore missing whichever grant governs. For the coordinator before the RRC letter goes (§7.4); not changed
   here.
3. *Inference:* the RRC's position may be broader than the register records, because the state copying
   paragraph has no noncommercial limit. Or the RRC's own noncommercial sentence governs, as the more specific
   grant. That is a question for counsel (§7.6), not a ground to reclassify. The register's `noncommercial`
   stands until the RRC answers.
4. *Inference:* the robots sentence is conditional ("results in severe degradation of system performance"). Our
   Class VI reader polls weekly at 0.5 requests per second (`data/sources.yaml`), well outside it. The sentence
   is not in the register; recommended for the two rows' `license` field.

### 7.4 The drafts

§0 applies unchanged: the §0.1 sender block, a postal address and a "stop" line in every message, one
recipient per send, the owner sends and signs. §0.4's channel row covers both (the owner's own mailbox and
post, one web form, US recipients).

**Before you send either letter**

- [ ] **The deal workspace and Bankable, stated truly.** The letters describe "Build a deal" as the decisions
      log does on 2026-10-22, before its spec (`docs/19-build-a-deal.md`) exists. If the spec changes what a
      private project shows, or whether facts are copied rather than shown live, change the letters to match.
      The Bankable sentence says the same team built it, that it is separate, holds only test data, and gets
      none of the source's data without asking first. Correct whatever is not so, and replace `[The same team
      built]` with the true relationship (for example "I also built"), naming Bankable's legal entity if it
      differs from `[Organisation]`.
- [ ] **What is live, on the day you send.** Run `curl -s
      "https://inframation.io/v1/opportunities?status=unknown,announced,open,frozen,reinstated,closed,cancelled,awarded&source_id=us.rfp.pso"`
      and the same for the other three AEP ids, and `curl -s
      "https://inframation.io/v1/proposals?source_id=us.tx.rrc.class_vi&limit=200"`. Keep the bracketed
      sentence that is true, and put in the count.
- [ ] **The RRC no-endorsement statement.** Before the RRC letter goes, either the RRC records show "Not
      endorsed by the State of Texas or the Railroad Commission of Texas" and the date copied (for example a
      `changes_statement` on both RRC rows, as the World Bank rows have, with the docs/13 §6 row moved first),
      or delete the bracketed words in the letter that say they do.
- [ ] **Withdrawal from private projects.** Keep the bracketed "including inside users' private projects" only
      if private projects show public facts live, through the same visibility predicate, so that withdrawing
      the source removes them there too. The decisions log says "with their facts live rather than copied";
      confirm it in `docs/19`. Otherwise write "facts already copied into a private project can't be recalled".
- [ ] **Nothing confidential.** AEP's terms make anything sent to AEP "non-confidential" and free for AEP to
      "disclose, and distribute". A letter to the RRC is a Texas public record that anyone may request. Put
      nothing in either that you would not publish.
- [ ] **Be ready for "stop all use".** Each letter tells the source about the commercial plans, and a source may
      answer that even today's free listing is not noncommercial. Send only if you will abide by that (§7.5,
      last two rows).
- [ ] **Re-read** both terms pages in an ordinary browser; the quotes are dated 2026-10-22. Read DIR's State
      Website Linking and Privacy Policy on dir.texas.gov and save a PDF for the legal lane (our fetch was
      refused, §7.3).
- [ ] **Records.** Keep threads and posted copies in private storage. Tell the legal lane the dates sent; it
      records them in the rows' `notes`.

#### 7.4.1 AEP: the four operating companies (one letter)

| | |
|---|---|
| Sources | `us.rfp.pso`, `us.rfp.appalachian_power`, `us.rfp.swepco`, `us.rfp.indiana_michigan_power`: `reuse: noncommercial`, `publication: derived_only`; registry verdict cleared, basis `terms_permit_noncommercial` |
| Send 1 (post, signed) | American Electric Power Company, Inc., Attn: Legal Department, 1 Riverside Plaza, Columbus, Ohio 43215 |
| Send 2 (15 business days later) | <https://www.aep.com/contact/>, "Send Us an Email", department "Other": the note below |
| Blocking clause | <https://www.aep.com/terms/>: "You may not copy or display for redistribution to third parties for commercial purposes any portion of the content without the prior written permission of AEP." |
| Re line | Request for written permission under AEP's Terms and Conditions: Energy Provider RFPs of four AEP companies on Inframation, including commercial use |

<!-- aep-letter-start -->
> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a website that lists energy projects and the
> solicitations developers bid into. I'm asking for AEP's written permission to list the Energy Provider RFPs of
> four AEP companies, including for commercial use. Your terms reserve that permission to AEP and name no
> contact, so I'm writing to your headquarters. Could you pass this to whoever grants it?
>
> The pages are the Energy Provider RFPs pages of Public Service Company of Oklahoma, Appalachian Power,
> Southwestern Electric Power Company and Indiana Michigan Power (Attachment A). Each says that use of the site
> constitutes acceptance of the AEP Terms and Conditions, which say: "You may not copy or display for
> redistribution to third parties for commercial purposes any portion of the content without the prior written
> permission of AEP." [We publish nothing from these pages today.] [Since [date] we have listed them on our free
> site, relying on your terms' permission for limited quotations with attribution.] Our reader loads each page
> at most once a day, two seconds between requests, naming itself with a contact link.
>
> For each generation RFP we'd show the company, the RFP title as posted, the issue and proposal-due dates, the
> MW sought, the technology, the state where given, and whether it is open. Each entry would carry the credit
> "Source: [company] (AEP), Energy RFPs" (or your wording) and link to your page. We would not copy RFP
> documents, forms, Q&A or logos, or anything from the AEP B2B register.
>
> Why commercial use: Inframation is free today, with no paid plans and no advertising, but we may add paid
> plans. We're also building a deal workspace: a developer opens a private project from a record such as one of
> your RFPs, with the public facts shown beside their own notes and a financing checklist. It may become paid,
> and may later let developers show projects to invited funders. [The same team built] Bankable
> (bankablehq.com), a separate deal service that holds only test data. Your RFPs would not go to it unless we
> asked you first.
>
> Any one of these answers would settle it:
>
> 1. Written permission for this use, on Inframation's free and paid services, with the credit and link.
> 2. The terms, including any fee, on which AEP would permit it.
> 3. If you consider it within your terms' permission for limited quotations with attribution, a line saying so.
>
> If you'd permit only part, for example the free site but not paid plans, please say which. You can withdraw
> permission at any time by writing to me. Within five business days we'd stop reading your pages and remove the
> entries from Inframation[, including inside users' private projects]; alert emails already sent can't be
> recalled. If you'd rather not hear from me about this again, write "stop" to [Contact email].
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]
<!-- aep-letter-end -->

(482 words with the placeholders and signature, counted with `wc -w` without the `> ` markers.)

**Send 2: the web-form note** (about 849 characters with the placeholders filled by typical values; the form
takes 1,000. The form's own fields carry your name and email.)

<!-- aep-form-start -->
> On [date] I posted a letter to AEP's Legal Department at 1 Riverside Plaza asking for written permission,
> under aep.com/terms, to list the Energy Provider RFPs of PSO, Appalachian Power, SWEPCO and Indiana Michigan
> Power on Inframation (inframation.io): title, dates, MW, technology, state and status, credited "Source:
> [company] (AEP), Energy RFPs" and linked to your pages. The use may be commercial: we may add paid plans and a
> paid deal workspace. Our team also built Bankable (bankablehq.com), a separate service; your data would not go
> to it. We'd withdraw within five business days of a request. Who at AEP decides this? You can reply to
> [Contact email]. To stop hearing from me, reply "stop". [Owner name], [Role], Inframation ([Organisation]),
> [Postal address]
<!-- aep-form-end -->

**Attachment A** (one page, PDF): the four pages, then the fields.

| Company | Page | Credit | Source id |
|---|---|---|---|
| Public Service Company of Oklahoma | <https://www.psoklahoma.com/business/b2b/energy-rfps/> | Source: Public Service Company of Oklahoma (AEP), Energy RFPs | `us.rfp.pso` |
| Appalachian Power | <https://www.appalachianpower.com/business/b2b/energy-rfps/> | Source: Appalachian Power (AEP), Energy RFPs | `us.rfp.appalachian_power` |
| Southwestern Electric Power Company | <https://www.swepco.com/business/b2b/energy-rfps/> | Source: SWEPCO (AEP), Energy RFPs | `us.rfp.swepco` |
| Indiana Michigan Power | <https://www.indianamichiganpower.com/business/b2b/energy-rfps/> | Source: Indiana Michigan Power (AEP), Energy RFPs | `us.rfp.indiana_michigan_power` |

| Shown on Inframation (site, API, feeds, email alerts, and inside private projects) | From your page |
|---|---|
| Company | the operating company |
| Title | the RFP's title as posted: the only text of yours we copy |
| Dates | issue date and proposal-due date, where posted |
| MW sought | where the page states one figure |
| Technology | solar, wind, storage, thermal or all-source, read from the title and text |
| State | where the RFP names one |
| Status | open or closed, from the due date |
| Link and credit | the RFP's page URL; the credit above, or your wording |

Not taken: RFP documents and appendices, forms, Q&A, bidder lists, the AEP B2B register, logos, and the fuel
and gas-lateral RFPs on the same pages (`pipeline/connectors/us_rfp/classify.py` leaves those out). Not on social
media: `services/social/editorial.py` excludes a `noncommercial` source under either posture, and a grant would
not change that unless it says so (§4.3 item 3).

Notes.

- *Text:* "Any copy you make must include this copyright notice" follows the personal-use grant. *Inference:* it
  does not attach to limited quotations. If AEP asks for the notice on each entry, it goes into the credit line.
- *Text:* AEP may "disclose, and distribute" whatever we send. The letter describes the deal workspace and
  Bankable only as far as either is public or decided.
- The reader cadence is daily (decision row 2026-10-22), not §2's "weekly, and daily while open", so the letter
  says "at most once a day".

#### 7.4.2 Railroad Commission of Texas (one letter)

| | |
|---|---|
| Sources | `us.tx.rrc.class_vi` (connector live; 20 records public since 2026-10-19) and `us.tx.rrc.datasets` (no connector; nothing published): `reuse: noncommercial`, `publication: raw_ok` |
| Send 1 (email) | Publicassist@rrc.texas.gov |
| Follow-up (10 business days) | one reply on the same thread |
| Send 2 (20 business days, post, signed) | Railroad Commission of Texas, Office of General Counsel, P.O. Box 12967, Austin, Texas 78711-2967 |
| Clauses | <https://www.rrc.texas.gov/site-policies/>, quoted in the letter; the state copying paragraph (§7.3) |
| Subject | Commercial reuse of the Class VI application list and the free data sets, with credit and link (for the Office of General Counsel) |

<!-- rrc-letter-start -->
> Hello,
>
> I'm [Owner name], [Role] at Inframation (inframation.io), a website that maps proposed energy and
> infrastructure projects. Your data sets page gives this address for questions about the data sets. Mine is
> about reuse, so it may belong with the Office of General Counsel. Could you pass it on if so?
>
> Your Site Policies say: "RRC grants permission to copy and distribute the information on its website for
> noncommercial use, as long as the content remains unaltered and is not presented in a misleading way." They
> also say: "The RRC has no restrictions on linking to our website as long as a fee is not charged to access our
> material." Inframation runs as a free, noncommercial service, and on that basis it has listed, since 19
> October 2026, the [20] applications in your Class VI Application List. We have not used the downloadable data
> sets.
>
> For each Class VI application we show the RRC tracking number, project name, operator, county and application
> status in your words, credited "Source: Railroad Commission of Texas" with a link to your list[, the date we
> copied it, and a statement that neither Inframation nor the entry is endorsed by the State of Texas or the
> RRC]. We'd like to add, from the UIC Database and the Drilling Permit Master, each well's or permit's API
> number, permit number, operator, county, field, location, well type, status and dates, credited the same way.
> We would not republish your PDFs or data files, and we don't change your wording where we quote it. Our reader
> loads the Class VI page weekly, two seconds between requests, naming itself with a contact link.
>
> Why I'm asking: Inframation is free, with no paid plans and no advertising, but we may add paid plans. We're
> also building a deal workspace: a developer opens a private project from a record, with the public facts shown
> beside their own notes and a financing checklist. It may become paid, and may later let developers show
> projects to invited funders. Those uses would be commercial, so your permission doesn't cover them. [The same
> team built] Bankable (bankablehq.com), a separate deal service that holds only test data. Your data would not
> go to it unless we asked you first.
>
> Any one of these answers would settle it:
>
> 1. Written permission for this use, on Inframation's free and paid services, still linking to your pages.
> 2. The terms, including any fee, on which the RRC would permit it.
> 3. If you consider this use already permitted, for example because these are public records, a line saying
>    so.
>
> If you'd permit only part, please say which. You can withdraw permission at any time by replying here. Within
> five business days we'd stop and remove the entries from Inframation[, including inside users' private
> projects]; alert emails already sent can't be recalled. If you'd rather not hear from me about this again,
> reply "stop".
>
> Thank you,
> [Owner name]
> [Role], Inframation ([Organisation])
> [Contact email] | [Phone]
> [Postal address]
<!-- rrc-letter-end -->

(496 words with the placeholders and signature, counted the same way.)

**The posted copy (send 2)** is the same letter with the first paragraph's last three sentences replaced by: "I
emailed this on [date] to Publicassist@rrc.texas.gov, the address your data sets page gives. I'm sending it to
your office because it concerns the Site Policies." Change "by replying here" to "by writing to me", and "reply
"stop"" to "write "stop" to [Contact email]".

**The 10-business-day reply**, on the same thread: "Hello again. I'm following up on my note below about
reusing the RRC's Class VI list and data sets commercially. If another office handles this, I'd be grateful for
its address. If you'd rather not hear from me, reply "stop"." Then the signature.

**Attachment A** (one page, PDF): the fields.

| Shown on Inframation (site, API, feeds, email alerts, and inside private projects) | From the RRC |
|---|---|
| Tracking number, project name, operator, county, application status (your words, beside our stage label) | Class VI Application List (live since 2026-10-19) |
| API number, permit number, operator, county, field, location, well type, status, dates | UIC Database; Drilling Permit Master with latitudes and longitudes (planned; not yet used) |
| Credit and link | "Source: Railroad Commission of Texas", or your wording; the list or data-set page URL[; the date copied; the no-endorsement statement] |

Not taken: the PDFs and data files themselves, imaged records, lease and well names, and the PDF's document
properties (`pipeline/connectors/us_tx_rrc_class_vi` never reads them; they name an RRC author). Not on social
media (as for AEP above).

Notes.

- *Text:* the Site Policies grant noncommercial copying "as long as the content remains unaltered". The record
  shows the RRC's own status words beside our stage label and links out to the RRC's PDF for the row itself
  (docs/13 §6.2). The letter's "we don't change your wording where we quote it" describes that practice.
- Lease and well names are left out of the planned fields because a lease name is often a family name (docs/13
  §5). The data-sets connector does not exist; when it is built, its fields must not go beyond Attachment A
  without a new request.
- If the RRC answers as in question 3 (for example "public information, use it as you like"), record its words
  verbatim. The credit and link stay either way: the RRC asks for them, and the state copying paragraph
  requires them.
- A reply from the public-assistance mailbox may not be the agency's decision. Ask which office is answering,
  and record the office, not the person (§4.2, counsel item C-10).

### 7.5 What each answer allows

"Relicence", "purge" and "flip" are the steps of `docs/26` §5. The gate these answers feed is now the flip to
`commercial`: paid plans, and any paid or funder-facing stage of "Build a deal". The bridge to bankablehq.com is
no longer planned (decisions log, 2026-10-22, "Build a deal" row); if it is ever revived, the 2026-10-22 bridge
row's rule returns with it: it goes live only when every one of the six has granted commercial reuse or been
withdrawn under §5. *Inference, the coordinator's and not legal advice (decisions log):* a free deal workspace
may keep the `noncommercial` posture longer. Register order is §4.3's: the docs/13 §6 row first, then
`data/sources.yaml`, then (AEP only) the `issuers.yaml` row with `basis: written_permission`.

| Answer | Register | `docs/26` §5 action | Paid plans and "Build a deal" |
|---|---|---|---|
| Yes: free and paid services, with attribution | Class `attribution`; the grant's operative sentence, date and granting office recorded verbatim; `publication` unchanged (`derived_only` for AEP, `raw_ok` for the RRC) unless the grant says otherwise | **Relicence** (step 4): a new licence row, and the admin reclassification repoints the source. Rows held under the old licence stay under it and go invisible at the flip; a re-ingest writes new rows under the new one. Do this before the flip so the source does not drop out | Survives the flip; its facts may appear in paid plans and in private projects at every stage the grant covers |
| Yes, with conditions the existing gates carry (credit wording, fewer fields, cadence) | As above, conditions verbatim | Relicence within the grant | As above, within the conditions |
| Yes, but not for a named use (for example paid plans yes, funders no) | Do **not** move to `attribution` yet. Every gate reads the class, so an `attribution` row would reach the excluded use. A per-source exclusion for that use is needed first, the same gap §4.3 item 3 records for social posts | Relicence once the exclusion exists; until then the source is withdrawn at the flip like a "no" | Kept out of the excluded use |
| Yes for the free site only | Stays `noncommercial` | Flip: rows invisible (steps 1 to 3), then purge or hold invisible (step 4) | Free use only; gone at the flip |
| Fee or licence terms offered | Nothing changes until a licence is signed. A signed licence is a contract; whether to have it read first is the owner's call | Relicence on signature; if declined, as "No" | Waits for signature or refusal |
| Confirmation that the use is within the existing terms | Class `attribution` (or `open`, if that is what is confirmed), with the confirmation as the evidence | Relicence | As "Yes" |
| No to commercial use | Stays `noncommercial`; refusal recorded (date, office, words) | At the flip: steps 1 to 3 hide the rows and close the connector gate; step 4, purge or hold invisible | Free use only, while the posture lasts; gone at the flip |
| No, and stop all use | §4.4 within five business days: `reuse: restricted`, `publication: none`; for AEP also `automated_reading: not_allowed`; for the RRC disable the Class VI schedule; takedown marks; written confirmation to the source | Purge through the redaction procedure (step 4); no flip needed for this source | None |
| No reply about 8 weeks after send 1 | "No response" recorded; silence is not consent; stays `noncommercial` | None until the owner chooses: keep the posture, or flip and withdraw the source (steps 1 to 4) | Free use only, while the posture lasts |

**Order at the flip, once every answer is in.** (1) Relicence and re-ingest each granted source under the
`noncommercial` posture. (2) Flip. Every remaining `noncommercial` row goes invisible on every non-admin surface.
(3) Decide purge or hold for each ungranted source. The flip is platform-wide: any of the six left
`noncommercial` disappears from the site at step 2. For private projects to follow the same rule, "Build a deal"
has to read public facts through the visibility predicate rather than copy them; that is a requirement for
`docs/19`, recorded here because these answers depend on it.

### 7.6 Open legal questions (noted, not answered)

No lawyer has reviewed this section, the letters, or the readings above. The owner withdrew the counsel
precondition (`docs/26` §3 (ii)) on 2026-09-26 and proceeds on their own reading of the noncommercial clauses,
with `docs/26` §5 as the backstop. `docs/13` §7 item 15 keeps counsel's view open as a recommendation.

1. **Linking to the owner's commercial service.** Would a link from Inframation to bankablehq.com, or the shared
   team itself, make Inframation's use commercial under AEP's "for redistribution to third parties for
   commercial purposes", or take it outside the RRC's "for noncommercial use", even though no field crosses?
   Today no such link exists (§7.1). The RRC's linking sentence adds its own condition, "as long as a fee is not
   charged to access our material".
2. **Whether Inframation can hold noncommercial status at all** (`docs/13` §7 item 15): run by an LLC, read by
   commercial developers and investors, and now building a deal workspace that may become paid. Whether a free
   deal workspace is still noncommercial use is the coordinator's inference in the decisions log, not a legal
   view. *Inference:* the letters give each source actual knowledge of these plans (`docs/13` §3.2), so a source
   that answers only "no" may view today's free listing differently than before.
3. **AEP.** Are derived facts "content" (docs/28 C-4)? Does "limited quotations" cover a running list? Does the
   no-implied-licence clause leave any noncommercial redistribution beyond limited quotations? Can AEP, the
   parent, grant for its operating companies' pages, and does the copyright-notice sentence reach quotations?
4. **RRC.** Is the state copying paragraph, which the RRC says copiers "must follow", itself a permission
   without a noncommercial limit, and which governs where the two differ? Can a state agency restrict the reuse
   of information the Public Information Act releases without asking why? Does the state policy's fee sentence
   mean the RRC cannot license this for a fee?
5. **Authority.** Is a reply from a role mailbox or a web form enough evidence of a grant, for AEP and for a
   Texas agency (docs/28 C-10)?

### 7.7 Facts used here, and their limits

- **Fetched live, 2026-10-22, between about 18:20 and 18:35 UTC**, one GET each with a generic browser user
  agent, not our reader. AEP: `aep.com/terms/`, `aep.com/contact/` and that page's script (for the department
  list and the message limit), the four Energy Provider RFPs pages, the four `/company/legal-notices` pages and
  PSO's 2026 All-Source RFP page. RRC: Site Policies, data sets, special map products, subscription services,
  open records, Public Information Act, contact, research, the FAQ index, the GIS data FAQ and the CO2 storage
  page. State policy: DIR (403 challenge), TBAE's reproduction, and SOAH's linking page (which says only that it
  follows DIR's policy). No archive copy was used: archive.org answered 429.
- **Our own API:** `/v1/health`, `/v1/opportunities` (every status, each AEP id; all 781 default-view rows paged
  once) and `/v1/proposals` for the two RRC ids.
- **Not done:** no message was sent, no form submitted, no account created, no person named. The only contact
  details recorded are offices' published role addresses, phone numbers and postal addresses.
- **`docs/19-build-a-deal.md`** is named by the decisions log as the spec; it was not in the working tree when
  this section was written, so the workspace is described from the decision row only.
- **Letter lengths** are counted on the drafts as written; the AEP web-form count fills each placeholder with a
  typical value (a 10-character date, a 30-character postal address, a 25-character email, a 20-character name,
  a 10-character role, a 35-character organisation, a 30-character company name).
