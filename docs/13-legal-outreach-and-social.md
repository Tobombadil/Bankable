# Outreach and social compliance checklist

Phase 1 legal deliverable. Owner: legal-compliance agent. Status: v1, 2026-09-12. Companion to
`13-legal-data-rights.md` (data in) — this file is about messages out.

**I am not a lawyer and this is not legal advice.** §11 lists what goes to counsel.

**Reading convention.** Blockquotes are verbatim source text with URL and retrieval date. Text marked
*Inference* is my reading with a stated confidence. All retrievals 2026-09-12 unless stated. Where a page
could not be retrieved I say so; nothing below is inferred from a page I did not read except where explicitly
labelled "unverified".

**Scope.** Every outbound channel Bankable might use: cold and warm email, newsletter, SMS/voice, LinkedIn
(personal and Page), X, Bluesky, Meta (Facebook/Instagram/Threads), Reddit, and any chat/agent surface on
bankablehq.com. `CLAUDE.md` already sets the baseline: humans send, agents draft, named channels only with
disclosure, no fake personas. This document turns that into a gate.

---

## 0. The gate

A channel is **inactive** until the owner records, in `docs/60-ops/channel-register.md` (to be created at
Phase 6; until then, in the decisions log of `00-PLAN.md`), a row containing: channel name; the sending
identity (real named human or a labelled organisation account); the jurisdictions of intended recipients;
the legal basis per jurisdiction (§1–§4); the platform rules checked (§5); the AI-disclosure position (§6);
the automation level from §7 and its disclosure text; and the suppression/unsubscribe/record-keeping
mechanism from §8. A channel with a blank cell does not ship.

---

## 1. Email — United States (CAN-SPAM)

Source: FTC, "CAN-SPAM Act: A Compliance Guide for Business",
https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business — retrieved
2026-09-12 (HTTP 200; page note: "Edited January 2024 to reflect Inflation-Adjusted Civil Penalty
Maximums").

> Each separate email in violation of the CAN-SPAM Act is subject to penalties of up to $53,088, so
> non-compliance can be costly.

The seven requirements, quoted:

> Don't use false or misleading header information. Your "From," "To," "Reply-To," and routing information –
> including the originating domain name and email address – must be accurate and identify the person or
> business who initiated the message.

> Don't use deceptive subject lines. The subject line must accurately reflect the content of the message.

> Identify the message as an ad. The law gives you a lot of leeway in how to do this, but you must disclose
> clearly and conspicuously that your message is an advertisement.

> Tell recipients where you're located. Your message must include your valid physical postal address.

> Tell recipients how to opt out of receiving future marketing email from you. Your message must include a
> clear and conspicuous explanation of how the recipient can opt out of getting marketing email from you in
> the future. … You may create a menu to allow a recipient to opt out of certain types of messages, but you
> must include the option to stop all marketing messages from you.

> Honor opt-out requests promptly. Any opt-out mechanism you offer must be able to process opt-out requests
> for at least 30 days after you send your message. You must honor a recipient's opt-out request within 10
> business days. You can't charge a fee, require the recipient to give you any personally identifying
> information beyond an email address, or make the recipient take any step other than sending a reply email
> or visiting a single page on an Internet website as a condition for honoring an opt-out request. Once people
> have told you they don't want to receive more messages from you, you can't sell or transfer their email
> addresses, even in the form of a mailing list.

> Monitor what others are doing on your behalf. The law makes clear that even if you hire another company to
> handle your email marketing, you can't contract away your legal responsibility to comply with the law. Both
> the company whose product is promoted in the message and the company that actually sends the message may be
> held legally responsible.

On subscribers:

> Remember that subscribers and members can opt out of marketing emails, too. … Before sending a message
> without an unsubscribe link to subscribers or members, be sure that the primary purpose of the message fits
> within one of the five categories of "transactional or relationship" message set out in the Act.

*Inference:* CAN-SPAM is opt-out, not opt-in, so US B2B cold email is lawful if the seven rules are met. The
rules that bite an early-stage team are the ones people forget: a **physical postal address in every
marketing email** (a registered PO box is fine), a **working unsubscribe for 30 days after send**, **10
business days to honour it**, and **no exceptions for "personal" one-to-one sales emails** — a one-off email
whose primary purpose is commercial is a commercial message. The "Monitor what others…" paragraph means a
sequencer vendor's failure is our violation. **Confidence: high.**

Checklist E-US:
- [ ] accurate From/Reply-To on a domain we control, with SPF/DKIM/DMARC aligned;
- [ ] subject matches body; no "Re:" on first-touch;
- [ ] commercial nature obvious from the content (an explicit "this is a sales email" line is not required,
      but the message must not masquerade as a personal note);
- [ ] postal address in footer;
- [ ] one-click unsubscribe link plus `List-Unsubscribe` and `List-Unsubscribe-Post` headers;
- [ ] suppression applied within 10 business days (our SLA: 24 hours, §8);
- [ ] transactional emails (alerts the user asked for, receipts, password resets) are segregated from
      marketing so the "primary purpose" test is easy.

---

## 2. Email — UK and EU (PECR, ePrivacy, GDPR)

### 2.1 UK PECR — corporate vs individual subscribers

Source: ICO, "Guide to PECR — Electronic mail marketing",
https://ico.org.uk/for-organisations/direct-marketing-and-privacy-and-electronic-communications/guide-to-pecr/electronic-and-telephone-marketing/electronic-mail-marketing/
— retrieved 2026-09-12 (HTTP 200).

> The rules on electronic mail marketing are in regulation 22. In short, you must not send electronic mail
> marketing to individuals, unless: they have specifically consented to electronic mail from you; or they are
> an existing customer who bought (or negotiated to buy) a similar product or service from you in the past,
> and you gave them a simple way to opt out both when you first collected their details and in every message
> you have sent. You must not disguise or conceal your identity, and you must provide a valid contact address
> so they can opt out or unsubscribe.

> This same rule applies to emails, texts, picture messages, video messages, voicemails, direct messages via
> social media or any similar message that is stored electronically.

> The soft opt-in rule means you may be able to email or text your own customers, but it does not apply to
> prospective customers or new contacts (eg from bought-in lists).

> Sole traders and some partnerships are treated as individuals – so you can only email or text them if they
> have specifically consented, or if they bought a similar product from you in the past and didn't opt out from
> marketing messages when you gave them that chance.

> You can email or text any corporate body (a company, Scottish partnership, limited liability partnership or
> government body). However, it is good practice – and good business sense – to keep a 'do not email or text'
> list of any businesses that object or opt out, and screen any new marketing lists against that.

> You may also need to consider data protection implications if you are emailing employees at a corporate body
> who have personal corporate email addresses (eg [email protected]).

*Inference:* the UK position is unusually favourable to B2B cold email **to corporate bodies**: PECR
regulation 22 does not require consent for `firstname.lastname@company.co.uk` when the company is a limited
company, LLP or public body. Three traps: (1) **sole traders and non-LLP partnerships are individuals** — many
independent developers and consultants in our ICP are exactly that, and there is no reliable way to tell from
an email address; (2) the ICO's second-to-last sentence above is a direct statement that **LinkedIn/X/Bluesky
DMs are "electronic mail"** for PECR, so a cold DM to an individual UK subscriber is an opt-in matter, not a
grey area; (3) the GDPR overlay in §2.3 still applies to the named employee. **Confidence: high.**

### 2.2 EU — ePrivacy Directive Art. 13 as transposed

Article 13 of Directive 2002/58/EC sets opt-in for email marketing to natural persons with a soft opt-in for
existing customers, and leaves the treatment of legal persons to Member States. I did not retrieve each
transposition for this document. The practical map counsel should confirm (item 11.2): Germany (UWG §7 —
opt-in for B2B as well as B2C; the strictest), Austria and Italy similarly strict; France and the
Netherlands permit B2B on a legitimate-interest basis where the message is relevant to the recipient's
professional function; Ireland treats corporate addresses as B2B-permissible with opt-out. **Confidence:
moderate; unverified for this document.** Until confirmed, **Germany, Austria and Italy are opt-in only.**

### 2.3 GDPR overlay on any named EU/UK contact

Source: Regulation (EU) 2016/679,
https://eur-lex.europa.eu/legal-content/EN/TXT/HTML/?uri=CELEX:32016R0679 — retrieved 2026-09-12.

Article 6(1)(f):

> processing is necessary for the purposes of the legitimate interests pursued by the controller or by a third
> party, except where such interests are overridden by the interests or fundamental rights and freedoms of the
> data subject which require protection of personal data, in particular where the data subject is a child.

Article 21:

> 2. Where personal data are processed for direct marketing purposes, the data subject shall have the right to
> object at any time to processing of personal data concerning him or her for such marketing, which includes
> profiling to the extent that it is related to such direct marketing.
> 3. Where the data subject objects to processing for direct marketing purposes, the personal data shall no
> longer be processed for such purposes.
> 4. At the latest at the time of the first communication with the data subject, the right referred to in
> paragraphs 1 and 2 shall be explicitly brought to the attention of the data subject and shall be presented
> clearly and separately from any other information.

Article 14(5)(b) (information duty where data were not obtained from the data subject) is disapplied where

> the provision of such information proves impossible or would involve a disproportionate effort … In such
> cases the controller shall take appropriate measures to protect the data subject's rights and freedoms and
> legitimate interests, including making the information publicly available;

*Inference:* for a first-touch B2B email to an EU/UK individual, the minimum is: a documented
legitimate-interests assessment for prospecting; the source of their details stated in the email (Art. 14
"first communication"); the right to object stated **separately and clearly** in that first email (Art. 21(4));
and an objection honoured absolutely (Art. 21(3) — no balancing). A footer line such as *"We found your
details on [source]. You can object to further contact at any time by replying 'stop' or here: [link]. Our
privacy notice: [link]"* satisfies all three in one sentence. **Confidence: high.**

Checklist E-EU/UK:
- [ ] recipient classified: corporate body / sole trader-partnership / individual — unknown = individual;
- [ ] country classified: opt-in-only list (DE/AT/IT until counsel confirms) vs legitimate-interest list;
- [ ] LIA on file before first send (template in §8.3);
- [ ] Art. 14 source statement + Art. 21 objection line in first email;
- [ ] no DMs to UK/EU individuals without consent (PECR reg. 22 covers DMs);
- [ ] no bought lists.

---

## 3. Calls and SMS — TCPA (US)

Source: 47 C.F.R. § 64.1200 "Delivery restrictions",
https://www.ecfr.gov/current/title-47/chapter-I/subchapter-B/part-64/subpart-L/section-64.1200 — retrieved
2026-09-12 (HTTP 200, current eCFR).

> (a) No person or entity may:
> (1) Except as provided in paragraph (a)(2) of this section, initiate any telephone call (other than a call
> made for emergency purposes or is made with the prior express consent of the called party) using an
> automatic telephone dialing system or an artificial or prerecorded voice; … (iii) To any telephone number
> assigned to a paging service, cellular telephone service, specialized mobile radio service, or other radio
> common carrier service, or any service for which the called party is charged for the call.

> (2) Initiate, or cause to be initiated, any telephone call that includes or introduces an advertisement or
> constitutes telemarketing, using an automatic telephone dialing system or an artificial or prerecorded
> voice, to any of the lines or telephone numbers described in paragraphs (a)(1)(i) through (iii) of this
> section, other than a call made with the prior express written consent of the called party …

> (9) … As used in this paragraph (a)(9), the term "call" includes a text message, including a short message
> service (SMS) call.

> (10) A called party may revoke prior express consent, including prior express written consent, to receive
> calls or text messages … by using any reasonable method to clearly express a desire not to receive further
> calls or text messages from the caller or sender. Any revocation request made … using the words "stop,"
> "quit," "end," "revoke," "opt out," "cancel," or "unsubscribe" sent in reply to an incoming text message …
> constitutes a reasonable means per se to revoke consent. … All requests to revoke prior express consent or
> prior express written consent made in any reasonable manner must be honored within a reasonable time not to
> exceed ten business days from receipt of such request.

> (c) No person or entity shall initiate any telephone solicitation to: (1) Any residential telephone
> subscriber before the hour of 8 a.m. or after 9 p.m. (local time at the called party's location), or (2) A
> residential telephone subscriber who has registered his or her telephone number on the national do-not-call
> registry …

> (D) Accessing the national do-not-call database. It uses a process to prevent telephone solicitations to any
> telephone number on any list established pursuant to the do-not-call rules, employing a version of the
> national do-not-call registry obtained from the administrator of the registry no more than 31 days prior to
> the date any call is made, and maintains records documenting this process.

*Inference:* the TCPA does not have a B2B carve-out for **wireless** numbers: any autodialed or prerecorded
marketing call or **text** to a mobile requires prior express **written** consent, and "written" means a
signed (e-sign is fine) agreement that specifically authorises marketing calls/texts to that number. Statutory
damages are $500 per call/text, trebled for wilful violations, with no cap and an active class-action bar.
The 2025 revocation rule ((a)(10)) means **any reasonable reply ends consent** and a text platform that
cannot receive replies must say so on every message. **Confidence: high.**

Bankable rule (recorded as a decision): **no SMS channel and no automated or AI-voice calling at all.**
Human-dialled, live-voice calls to business landlines are outside (a)(1)–(2); mobiles are common in our ICP
and cannot be distinguished reliably, so the rule is *no cold calls to any number not obtained directly from
the person with a stated purpose*. If a customer opts into SMS alerts for the product later, that is a
transactional consent captured at signup with the exact TCPA disclosure text, reviewed by counsel (item 11.3).

---

## 4. Canada — CASL

Source: *An Act to promote the efficiency and adaptability of the Canadian economy…* (CASL), S.C. 2010, c. 23,
https://laws-lois.justice.gc.ca/eng/acts/E-1.6/FullText.html — retrieved 2026-09-12 (HTTP 200).

> 6 (1) It is prohibited to send or cause or permit to be sent to an electronic address a commercial
> electronic message unless (a) the person to whom the message is sent has consented to receiving it, whether
> the consent is express or implied; and (b) the message complies with subsection (2).
> (2) The message must be in a form that conforms to the prescribed requirements and must (a) set out
> prescribed information that identifies the person who sent the message and the person — if different — on
> whose behalf it is sent; (b) set out information enabling the person to whom the message is sent to readily
> contact one of the persons referred to in paragraph (a); and (c) set out an unsubscribe mechanism in
> accordance with subsection 11(1).
> (3) … must ensure that the contact information referred to in paragraph (2)(b) is valid for a minimum of 60
> days after the message has been sent.

> (5) This section does not apply to a commercial electronic message … (b) that is sent to a person who is
> engaged in a commercial activity and consists solely of an inquiry or application related to that activity;

> 11 (1) The unsubscribe mechanism referred to in paragraph 6(2)(c) must (a) enable the person to whom the
> commercial electronic message is sent to indicate, at no cost to them, the wish to no longer receive any
> commercial electronic messages, or any specified class of such messages … using (i) the same electronic
> means by which the message was sent, or (ii) if using those means is not practicable, any other electronic
> means that will enable the person to indicate the wish; and (b) specify an electronic address, or link to a
> page on the World Wide Web that can be accessed through a web browser, to which the indication may be sent.

> 20 (4) The maximum penalty for a violation is $1,000,000 in the case of an individual, and $10,000,000 in
> the case of any other person.

Implied consent (s. 10(9)–(10), not quoted at length): exists where there is an existing business
relationship, or where the recipient has **conspicuously published** their address without a statement that
they do not wish to receive unsolicited CEMs **and the message is relevant to their business, role, functions
or duties** — the "conspicuous publication" basis; and for addresses disclosed directly to the sender without
an opt-out statement, on the same relevance condition.

*Inference:* CASL is opt-in by default with the strictest penalties in this document, but B2B prospecting is
workable under two routes: (1) the s. 6(5)(b) exemption for a message that "consists solely of an inquiry"
about the recipient's commercial activity — a genuine question about their project or RFP qualifies; a pitch
does not; (2) implied consent by conspicuous publication, which is exactly the situation for a person named
on a public docket or an RFP contact page **provided the message is relevant to that role** and we record the
basis and the publication location. Unsubscribe must work for 60 days and be actioned within 10 business days
(s. 11(3)). **Confidence: moderate-high** — the "conspicuous publication" route has been litigated and
requires the relevance link to be real. Filer contacts in dockets are relevant for the filer's *project*, not
for selling them a subscription.

Checklist E-CA: consent basis recorded per contact (express / EBR / conspicuous publication with URL and date /
s. 6(5)(b) inquiry); sender and on-behalf identity in body; postal + email + web contact valid ≥60 days;
same-channel unsubscribe; implied-consent contacts expire per the statute (2 years EBR / 6 months inquiry) and
are purged.

---

## 5. Platform rules

### 5.1 LinkedIn

**User Agreement** — https://www.linkedin.com/legal/user-agreement, retrieved 2026-09-12 (via fetch tool).
"Dos and Don'ts", the relevant Don'ts:

> Create a false identity on LinkedIn, misrepresent your identity, create a Member profile for anyone other
> than yourself (a real person), or use or attempt to use another's account

> Develop, support or use software, devices, scripts, robots or any other means or processes (such as
> crawlers, browser plugins and add-ons or any other technology) to scrape or copy the Services, including
> profiles and other data from the Services

> Copy, use, display or distribute any information (including content) obtained from the Services, whether
> directly or through third parties (such as search tools or data aggregators or brokers), without the consent
> of the content owner

> Use bots or other unauthorized automated methods to access the Services, add or download contacts, send or
> redirect messages, create, comment on, like, share, or re-share posts, or otherwise drive inauthentic
> engagement

**API Terms of Use (Self-Serve)** — https://legal.linkedin.com/api-terms-of-use, retrieved 2026-09-12 (HTTP
200). Section 3.3, "you agree not to":

> Use the Content or the APIs to automate posting on the LinkedIn Services.

> Access, store, display, or facilitate the transfer of any LinkedIn content obtained through the following
> methods: scraping, crawling, spidering or using any other technology or software to access LinkedIn content
> outside the APIs …

> You must not capture, copy, cache, or store any Content or any information expressed by the Content (such as
> hashed or transformed data), except to the extent expressly permitted by these Terms or any applicable
> Additional Terms.

**Additional Terms for the LinkedIn Marketing API Program** — https://www.linkedin.com/legal/l/marketing-api-terms,
"Last revised on July 25, 2025", retrieved 2026-09-12 (HTTP 200).

> <<THE LINKEDIN MARKETING API PROGRAM IS A VETTED API PROGRAM AND YOU HAVE NO RIGHT TO USE ANY API OR DATA MADE
> AVAILABLE AS PART OF THIS PROGRAM UNLESS APPROVED BY LINKEDIN>>

> The Marketing APIs (and Marketing Services) currently available under the LMA Program … are: … (ii) for
> Community Management: Page Management, Member Profile Management, Page Messaging, Events Management, and
> Member Post Analytics.

> a. Page Management. Using the Page Management APIs, Marketing Applications can manage Pages on behalf of
> Authorized Clients.

> If there is a conflict between these LMA Terms and the API Terms of Use, these LMA Terms will control.

Member Data restriction, §3.1(e) (extract):

> You must not however: … (5) use Member Data … for advertising, sales, or recruiting use cases (including to
> identify sales or marketing prospects or prospective talent for hire, for lead creation, to enhance customer
> data in a CRM or marketing automation platform, to build an audience list, or for ad targeting purposes)

**Pages Terms** — https://www.linkedin.com/legal/l/linkedin-pages-terms, retrieved 2026-09-12 (HTTP 200).

> 2.3 Content and Conduct. You agree that: You will follow our "Do's and Don'ts," and keep content
> professional, respectful, relevant, and accurate. You will only post content that is truthful and does not
> infringe anyone else's rights. You will only use the Business Services to identify and promote your own
> Organization. … You will ensure that all of the Organization's actions (including by any Administrator)
> regarding the Page (such as all posts, additions, and deletions) comply with all applicable laws.

*Inference, and this corrects `data/sources.yaml` `social.linkedin` and `32-social-operating-playbook.md`:*

1. **Automated posting is prohibited under the self-serve API terms** ("Use the Content or the APIs to
   automate posting"). The *only* sanctioned route to scheduled or programmatic Page posts is the Community
   Management API under the vetted Marketing Developer Platform, which requires LinkedIn's approval and whose
   LMA Terms override the self-serve prohibition for approved Page Management. Until MDP approval, programmatic
   posting to our Page — including via an unvetted homegrown script — breaches the User Agreement ("bots or
   other unauthorized automated methods … create … posts"). **Using an approved third-party scheduler
   (Buffer, Hootsuite, etc.) is compliant because that vendor holds the MDP approval**, and the `sources.yaml`
   note to "bridge with an approved scheduler at launch; apply for MDP on day 1" is the right plan.
2. **No automated DMs, connection requests, or engagement, ever.** The User Agreement bars bots that "send or
   redirect messages" or "add … contacts"; PECR (§2.1) independently bars cold DMs to UK individuals.
3. **No scraping of LinkedIn for prospecting, and no LinkedIn data in the CRM beyond what the person gave
   us.** The §3.1(e)(5) prohibition on using Member Data for "lead creation" or "to enhance customer data in a
   CRM" is aimed at API partners, but the User Agreement's "copy, use, display or distribute any information …
   obtained from the Services" reaches manual copy-paste too. Sales research reads a profile and records a
   role and company (public facts); it does not export or enrich.
4. Page posts are the organisation's speech; the Page Terms make truthfulness and legality the Administrator's
   personal undertaking.

**Confidence: high** on 1–2 and 4; **moderate** on the manual-research line in 3, which counsel should draw.

### 5.2 X (Twitter)

**X Developer Policy** — https://developer.x.com/en/developer-terms/policy, retrieved 2026-09-12 (HTTP 200).

> The use of the X API and developer products to create spam, or engage in any form of platform manipulation,
> is prohibited. You should review the X Rules on platform manipulation and spam, and ensure that your service
> does not, and does not enable people to, violate our policies.

> Services that perform write actions, including posting Posts, following accounts, or sending Direct
> Messages, must follow the Automation Rules. In particular, you should: Always get explicit consent before
> sending people automated replies or Direct Messages … Never perform bulk, aggressive, or spammy actions,
> including bulk following

> If you're operating an API-based bot account you must clearly indicate what the account is and who is
> responsible for it. You should never mislead or confuse people about whether your account is or is not a
> bot. A good way to do this is by including a statement that the account is a bot in the profile bio.

> By building on the X API or accessing X Content, you must comply with ALL X policies. These include this
> Developer Policy, the Automation Rules, the Display Requirements, the API Restricted Uses Rules, the X Rules …

> Off-X matching involves associating X Content, including a X @handle or user ID, with a person, household,
> device, browser, or other off-X identifier. You may only do this if you have express opt-in consent from the
> person before making the association, or as described below.

The **Automation Rules** page itself (`https://help.x.com/en/rules-and-policies/x-automation`, and the older
`…/twitter-automation` slug) returned **HTTP 403** to both retrieval methods on 2026-09-12 and is **not
quoted**. The Developer Policy incorporates it by reference and restates its three operative rules above,
which is enough to act on.

Also note X's **Automated label**: X offers an in-product "Automated" account label that a developer can attach
to a bot account via the developer portal (I did not retrieve the help page for it; **unverified** as to
current mechanics). The Developer Policy's bio-statement rule is the floor.

*Inference:* an X account that auto-posts Bankable feed items via the API is permitted **if** it is labelled
as automated in the bio (and with the Automated label where available), names Bankable as responsible, never
auto-replies or auto-DMs, never auto-follows, and never matches a handle to a CRM record without opt-in. Cost
is a budget line, not a legal issue. **Confidence: high.**

### 5.3 Bluesky

Source: Bluesky Community Guidelines, https://bsky.social/about/support/community-guidelines — retrieved
2026-09-12 (HTTP 200).

> Trust & Transparency: We keep our platform trustworthy by prohibiting deceptive and manipulative practices.
> Do not send spam or repeatedly post content in ways that disrupt normal conversations or service use.

> Do not artificially manipulate features or social signals to gain unearned reach or mislead users, including
> engagement metrics, follower counts, or other measures of community interest.

> Do not post undisclosed commercial content, sponsored material, or advertising without clearly identifying
> its commercial nature to other users.

> Account Authenticity: We keep our community trustworthy by prohibiting impersonation and deceptive account
> practices. Do not impersonate others or official groups in ways that could mislead users, or create fake
> accounts to deceive others about who you are.

> These rules do not prevent you from using alternative identities without misleading other users, including:
> Clearly labeled parody, satire, or fan accounts that identify their nature in both display name and bio.

> Do not abuse Bluesky features (lists, labels, community moderation tools), engage in bad-faith mass
> reporting, use automated harassment systems, or create single-purpose harassment accounts.

*Inference:* Bluesky has no rule against automated posting as such — the AT Protocol is designed for it — and
no formal "bot label" requirement; the rules that apply are anti-spam, anti-manipulation, commercial-content
disclosure and authenticity. The "identify their nature in both display name and bio" formula for
alternative identities is the model to copy: an automated feed account should say "automated feed" in the
display name or handle **and** in the bio, and the bio should state that Bankable operates it and that posts
are of a commercial nature (they promote a paid product). Rate: post at the cadence of real events, not a
firehose. **Confidence: high.**

### 5.4 Meta (Facebook / Instagram / Threads)

Source: Meta Platform Terms, https://developers.facebook.com/terms/ — retrieved 2026-09-12 via fetch tool
only (direct requests returned HTTP 400). Quotes are as returned by that tool and are partial:

> Selling, licensing, or purchasing Platform Data.

> Processing Platform Data without valid User consent in order to build or augment user profiles for any
> purpose.

> Processing Platform Data for purposes other than the applicable permitted purposes set forth in Meta's
> Developer Docs.

The tool reported no clause in Section 2 mentioning bots or scraping; scraping is governed by the Facebook
Terms of Service and Meta's separate automated-data-collection terms, which I did **not** retrieve.

*Inference:* Meta is deprioritised in `00-PLAN.md` and nothing here changes that. If a Page is created, posting
through the Pages API with an app that has passed App Review, or through an approved scheduler, is the
compliant route; no Platform Data is stored; no audiences are built from it. **Confidence: moderate**
(partial retrieval).

### 5.5 Reddit

**Data API Terms** — https://www.redditinc.com/policies/data-api-terms, "Last Revised July 20, 2026",
retrieved 2026-09-12 (HTTP 200; text extracted from the page's embedded content since the page is
script-rendered; the fetch tool could not open this host).

> Reddit reserves the right to charge fees for future use or access to the Data APIs, rates to be determined
> at Reddit's sole discretion. If you are interested in using the Data APIs for commercial purposes, research
> in excess of rate limits, or for any use that is not expressly permitted under the Data API Terms, then you
> will need to enter into a separate agreement with Reddit.

> … use the Data APIs to spam, incentivize, or harass users.

> … Data APIs to encourage or promote illegal activity or violation of third party rights (including using User
> Content to train a machine learning or AI model without the express permission of rightsholders in the
> applicable User Content);

**Self-promotion norms.** Reddit's own wiki (`reddit.com/wiki/selfpromotion`) is script-rendered and its
text could not be extracted; the Reddit Help article on spam
(`support.reddithelp.com/hc/en-us/articles/28012014962580`) returned HTTP 403. The widely cited "10% rule"
(no more than one in ten contributions self-promotional) is therefore **unverified** for this document and is
in any case a per-community moderator norm, not a platform term.

*Inference:* Bankable's use of Reddit is commercial by definition, so any API use requires a paid agreement;
posting by hand as a named human in energy subreddits, following each subreddit's rules and disclosing the
affiliation, is the only route and it is a human-only channel. `sources.yaml` already says this.
**Confidence: high.**

### 5.6 A platform rule from a data source

ERCOT Website User Agreement §8 (https://www.ercot.com/help/terms, retrieved 2026-09-12):

> Use of ERCOT list Server (email) list for marketing products and/or services is strictly prohibited. Host
> domains of violators will be denied access to the ERCOT server.

*Inference:* ISO stakeholder lists, committee rosters and market-participant directories are never a
prospecting source. Breach costs us the data feed, not just a complaint. The same rule is applied to every
source in `13-legal-data-rights.md` §5.4 rule 4. **Confidence: high.**

---

## 6. AI disclosure

### 6.1 FTC — deception law applies to AI, with no exemption

Source: FTC press release, "FTC Announces Crackdown on Deceptive AI Claims and Schemes", 2024-09-25,
https://www.ftc.gov/news-events/news/press-releases/2024/09/ftc-announces-crackdown-deceptive-ai-claims-schemes
— retrieved 2026-09-12 (HTTP 200).

> "Using AI tools to trick, mislead, or defraud people is illegal," said FTC Chair Lina M. Khan. "The FTC's
> enforcement actions make clear that there is no AI exemption from the laws on the books."

> The cases being announced today include actions against a company promoting an AI tool that enabled its
> customers to create fake reviews, a company claiming to sell "AI Lawyer" services, and multiple companies
> claiming that they could use AI to help consumers make money through online storefronts.

The FTC business-blog post "Keep your AI claims in check" (February 2023) returned HTTP 404 at its previous
URL on 2026-09-12 and is not quoted; its four questions (exaggerating capability; promising more than AI can
deliver; awareness of foreseeable risks; whether the product uses AI at all) are consistent with the
enforcement posture above and are treated here as unverified secondary guidance.

*Inference:* two obligations follow for Bankable. (1) **Marketing claims about our own AI** (entity
resolution, scoring, "certification") must be substantiated — "AI-powered" is a claim about capability, and
"certified by Bankable" is a claim about a process; both need documented evidence before they appear in copy.
(2) **AI-drafted outbound messages** are not themselves deceptive under Section 5 if the *content* is
truthful and the sender is who it says it is; deception arises when the message pretends to be something it
is not — a personal note from a human who never wrote it, a "we noticed your project" claim with no human
having noticed, a fake review. The rule set in §7 is built around that line. **Confidence: high.**

### 6.2 EU AI Act Article 50

Source: Regulation (EU) 2024/1689 Article 50, consolidated text as published by
https://artificialintelligenceact.eu/article/50/ — retrieved 2026-09-12 (HTTP 200). (This is a secondary
publisher of the Official Journal text; cross-check against EUR-Lex before relying on exact wording.)

> 1. Providers shall ensure that AI systems intended to interact directly with natural persons are designed and
> developed in such a way that the natural persons concerned are informed that they are interacting with an AI
> system, unless this is obvious from the point of view of a natural person who is reasonably well-informed,
> observant and circumspect, taking into account the circumstances and the context of use.

> 4. … Deployers of an AI system that generates or manipulates text which is published with the purpose of
> informing the public on matters of public interest shall disclose that the text has been artificially
> generated or manipulated. This obligation shall not apply where the use is authorised by law to detect,
> prevent, investigate or prosecute criminal offences or where the AI-generated content has undergone a
> process of human review or editorial control and where a natural or legal person holds editorial
> responsibility for the publication of the content.

> 5. The information referred to in paragraphs 1 to 4 shall be provided to the natural persons concerned in a
> clear and distinguishable manner at the latest at the time of the first interaction or exposure.

*Inference:* Article 50 reaches Bankable in two places once it has EU users (it applies to deployers whose
output is used in the Union). (a) Any **chat or assistant surface on bankablehq.com** must say it is an AI
system at first interaction — paragraph 1. (b) Our **published project summaries and social posts** are text
"informing the public on matters of public interest" (energy infrastructure qualifies); paragraph 4 would
require an "AI-generated" disclosure **unless** the text "has undergone a process of human review or
editorial control" with a named person holding editorial responsibility. That exemption is the reason
Bankable's publishing pipeline should be **human-reviewed with a named editor of record**, not merely for
quality. Where we choose to auto-publish without review (for example a change-event feed), the item must
carry the disclosure. Paragraph 2 (machine-readable marking) is a *provider* obligation on the model vendor,
not on us. Article 50 obligations apply from 2 August 2026, so they are live. **Confidence: moderate-high;**
the "matters of public interest" boundary is untested.

### 6.3 California B.O.T. Act

Source: Cal. Bus. & Prof. Code §17941,
https://leginfo.legislature.ca.gov/faces/codes_displaySection.xhtml?lawCode=BPC&sectionNum=17941. — retrieved
2026-09-12 (HTTP 200).

> (a) It shall be unlawful for any person to use a bot to communicate or interact with another person in
> California online, with the intent to mislead the other person about its artificial identity for the purpose
> of knowingly deceiving the person about the content of the communication in order to incentivize a purchase
> or sale of goods or services in a commercial transaction or to influence a vote in an election. A person
> using a bot shall not be liable under this section if the person discloses that it is a bot.
> (b) The disclosure required by this section shall be clear, conspicuous, and reasonably designed to inform
> persons with whom the bot communicates or interacts that it is a bot.

*Inference:* the statute is narrow (intent to mislead about artificial identity, in order to deceive about
content, to incentivise a purchase) but the safe harbour is broad and cheap: disclose. "Bot" is defined in
§17940 as an automated online account where substantially all actions are not the result of a person; a
human-sent email drafted with AI is not a bot. An auto-posting social account is. An on-site sales chat is.
Both get the disclosure. **Confidence: high.**

### 6.4 Utah — Artificial Intelligence Policy Act as amended by SB 226 (2025)

Source: Utah S.B. 226 (2025), enrolled, https://le.utah.gov/Session/2025/bills/enrolled/SB0226.pdf —
retrieved 2026-09-12 (HTTP 200). Effective 2025-05-07; recodified at Utah Code Title 13, Chapter 75.

> 13-75-101 … (4) "Generative artificial intelligence" means an artificial intelligence technology system that:
> (a) is trained on data; (b) is designed to simulate human conversation with a consumer through one or more
> of the following: (i) text; (ii) audio; or (iii) visual communication; and (c) generates non-scripted outputs
> similar to outputs created by a human, with limited or no human oversight.

> 13-75-104 … Safe harbor. (1) A person is not subject to an enforcement action for violating Section
> 13-75-103 if the person's generative artificial intelligence clearly and conspicuously discloses: (a) at the
> outset of any interaction with an individual in connection with: (i) a consumer transaction; or (ii) the
> provision of regulated services; and (b) throughout the interaction that it: (i) is generative artificial
> intelligence; (ii) is not human; or (iii) is an artificial intelligence assistant.

The operative duty in §13-75-103(1) (disclosure when a consumer "clearly and unambiguously" asks whether they
are interacting with AI, and proactive disclosure in "high-risk" interactions) is in the same bill; I have
quoted the definitions and safe harbour, which are the parts that drive design.

*Inference:* the definition's "with limited or no human oversight" limb is decisive: a human-sent, human-edited
email is outside it; an autonomous chat agent is inside it. Utah's regime is consumer-transaction-scoped and
B2B prospecting is at its edge, but the safe harbour is trivially satisfied by disclosure at the outset and
throughout, which is the same design the EU AI Act requires. **Confidence: moderate-high.**

### 6.5 The disclosure texts the product uses

Recorded as a decision. The operator named in every text is the product, **Infraque**, not the owner's
other company: the 2026-09-18 audit found the shipped constant still read "Bankable (bankablehq.com)",
and a disclosure that names the wrong operator is itself a false statement about who runs the account
(§6.1, §6.3). `services/social/editorial.py` renders these from the environment — `PRODUCT_NAME`
(default `Infraque`), `PRODUCT_URL` (default the site host in `services/api/common.py`),
`PRODUCT_CONTACT_EMAIL` (default `hello@` on that domain) — so a rename is a deploy-time setting, not
a code change. The email footer's legal name and postal address come from `SENDER_LEGAL_NAME` and
`SENDER_POSTAL_ADDRESS` (`services/alerts/mail.py`); no send leaves production while either is unset
(owner decision D5: no legal entity or address yet).

| Surface | Text |
|---|---|
| Automated social account (X, Bluesky, any) — bio | "Automated feed run by Infraque (infraque.com). Posts are generated from public records and are commercial in nature. Not monitored for replies — contact: hello@infraque.com." |
| Automated social account — display name | contains "(automated)" or "feed" |
| Auto-published item without human review | trailing line "Auto-generated summary from [source]; not human-reviewed." |
| Human-reviewed published item | editor of record recorded in the CMS; no on-item AI disclosure required; site-wide editorial policy page states that drafting uses AI and a named editor reviews |
| On-site chat/assistant | first message: "I'm Infraque's AI assistant, not a human. …" and a persistent label; disclosure repeated on request |
| Alert/digest email — footer (every send) | "You are receiving this because you subscribed at infraque.com. Data derived from public sources cited above; see each item's source and licence." + the delayed-data notice for the reader's tier + one-click unsubscribe link + "Sent by Infraque <alerts@infraque.com> on behalf of {SENDER_LEGAL_NAME}, {SENDER_POSTAL_ADDRESS}". Headers: `List-Unsubscribe` (mailto + https), `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (RFC 8058). |
| Human-sent email drafted with AI | no AI disclosure required (human sender, human review); the sender's real name and the statements in §7.2 |

---

## 7. Human-sends rule set

### 7.1 Automation levels

| Level | Definition | Allowed at launch? | Disclosure |
|---|---|---|---|
| **L0 — human writes, human sends** | Agent may research; human composes | Yes | none beyond §1–§4 |
| **L1 — agent drafts, human edits and sends** | Agent produces a draft from a template and public facts; a named human reads, edits, and personally presses send; one recipient per action | **Yes — the default for all email and DMs** | none beyond §1–§4; sender is the human |
| **L2 — human approves, system sends** | Human reviews each message in a queue and approves; a sequencer sends it under the human's name, with follow-ups the human pre-approved by reading them | Email only, after the §0 gate; never DMs | none beyond §1–§4; every message in the sequence was read by the sender before approval; the sequencer vendor is contractually bound to §1 rules |
| **L3 — system publishes to an owned or labelled channel** | Newsletter, RSS, webhooks, labelled social feed accounts | Yes for owned channels and labelled X/Bluesky feeds | §6.5 labels; Art. 50(4) line if not human-reviewed |
| **L4 — system messages a person without per-message human review** | Auto-replies, auto-DMs, drip sequences the sender has not read, AI voice, SMS | **No.** | — |

*Inference:* L2 is where most sales teams live and where most compliance failures happen. The line I draw is
that **the named sender has personally read every message that goes out under their name**, including
follow-ups, before it is queued. "Pre-approving a template" is not reading a message. The practical cost is
small at our volume (tens per day) and the benefit is that the FTC-deception, PECR-DM, LinkedIn-bot and
bot-disclosure questions all resolve the same way: a human sent it.

### 7.2 What every outbound human message states

1. Real name, role, and "Bankable" as the organisation.
2. How we came to contact them (the public source, e.g. "your firm is listed as interconnection customer on
   the ERCOT GIS report") — this is both the GDPR Art. 14 source statement and the honest version of "we
   noticed your project".
3. What we want, in one sentence.
4. How to stop hearing from us (reply "stop" works everywhere; a link where required).
5. Postal address (US), and privacy-notice link (EU/UK/CA).

### 7.3 What agents may do

- Research: read public sources named in `data/sources.yaml`; read a person's public LinkedIn profile *by a
  human's hand* and record role + company (not export); never log in to a platform as a script.
- Draft: produce message drafts and research briefs into the CRM for a human.
- Score and route: propose who to contact and why; the human decides.
- Publish: to L3 channels only, with labels.
- Never: send, reply, follow, connect, like, comment, or DM on any platform; call or text anyone; use
  ingested filer contact data (`13-legal-data-rights.md` §5.4 rule 4).

### 7.4 What "explicitly enabled channel" means

`CLAUDE.md` allows automated outbound only where "the owner has explicitly enabled a named automated channel
with disclosure". Enabling means a row in the channel register (§0) with the L3 label text, signed off by the
owner, and a dated entry in the `00-PLAN.md` decisions log. Nothing else counts.

---

## 8. Record-keeping, suppression and unsubscribe

### 8.1 Suppression list (single, global, cross-channel)

- One suppression store keyed on a salted hash of normalised email / phone / platform handle; never the raw
  identifier once suppressed (`13-legal-data-rights.md` §5.4 rule 6). A plaintext field is kept **only** for
  the confirmation-of-suppression record where a regulator would expect it — counsel item 11.6.
- Entries carry: channel, reason (unsubscribe / objection / bounce / complaint / legal request / do-not-call),
  timestamp, source of the request (link click, reply text, DSAR), and the message id that triggered it.
- Suppression is **permanent** and **survives re-ingestion and CRM re-imports**: every list load runs against
  the store before it can be used; the CRM cannot mark a suppressed contact as contactable.
- Domain-level suppression exists for corporate opt-outs (PECR "do not email" list) and for domains that
  object (ERCOT §8-type obligations).
- A person who objects on one channel is suppressed on all channels for marketing; transactional messages
  they have asked for continue and are logged as such.

### 8.2 SLAs

| Obligation | Legal maximum | Bankable SLA |
|---|---|---|
| Honour email opt-out (CAN-SPAM) | 10 business days | 24 hours (automated) |
| Keep opt-out mechanism live after send (CAN-SPAM) | 30 days | permanent |
| Keep contact info valid after send (CASL s. 6(3)) | 60 days | permanent |
| Honour unsubscribe (CASL s. 11(3)) | 10 business days | 24 hours |
| Honour TCPA revocation | 10 business days | n/a — no channel |
| GDPR Art. 21 objection | "no longer process" — immediately | 24 hours; confirmed in writing within 72 hours |
| GDPR Art. 15/17 access/erasure | 1 month (extendable) | 14 days |
| National DNC registry refresh (if ever calling) | 31 days | n/a — no channel |

### 8.3 Records kept, and for how long

| Record | Content | Retention |
|---|---|---|
| Consent / basis ledger | per contact: basis (express consent text and timestamp / LIA reference / CASL implied-consent basis with URL and date / PECR corporate-subscriber classification), collector, source URL | life of relationship + 3 years (CASL limitation is 3 years from discovery; CAN-SPAM/TCPA 4 years — counsel item 11.6 sets the final figure) |
| Legitimate-interests assessment | purpose, necessity, balancing, safeguards, reviewer, date | permanent, versioned |
| Message log | every outbound marketing message: recipient hash, channel, sender (human), template id, full rendered content, timestamp, unsubscribe link id | 4 years |
| Suppression store | as §8.1 | permanent |
| Rights-request log | DSAR/objection/erasure: request, identity check, actions, completion date | 3 years after completion |
| Channel register | as §0 | permanent, versioned |
| Vendor contracts | sequencer, ESP, social scheduler: DPA, CAN-SPAM responsibility clause, sub-processor list | life of contract + 6 years |
| Editorial log | editor of record per published item (Art. 50(4) exemption) | life of item + 2 years |

### 8.4 Unsubscribe mechanics (all channels)

- Email: `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` headers and a footer link;
  reply-to inbox monitored for "stop/unsubscribe/remove/opt out/objection" in any language the recipient
  wrote in; both paths feed §8.1 automatically.
- Newsletter: one-click unsubscribe; preference centre allowed but "stop everything" must be one click.
- Social feeds: no per-person opt-out exists; the bio names a contact address for objections and the account
  never targets individuals.
- Site chat: "stop" ends the session and the disclosure is repeated.

---

## 9. Channel-by-channel gate summary

| Channel | Legal bases to record | Platform rules | AI disclosure | Max level | Status 2026-09-12 |
|---|---|---|---|---|---|
| Email — US B2B | CAN-SPAM (§1) | ESP terms | none (human-sent) | L2 | may activate after §0 row |
| Email — UK B2B corporate | PECR reg. 22 + GDPR LIA (§2) | ESP terms | none | L2 | may activate; sole-trader screen required |
| Email — EU B2B | ePrivacy transposition per country + GDPR LIA (§2) | ESP terms | none | L1 (L2 outside DE/AT/IT after counsel) | **blocked pending 11.2** |
| Email — Canada B2B | CASL s.6/s.10/s.11 (§4) | ESP terms | none | L1 | may activate with basis ledger |
| Newsletter (opt-in) | consent captured at signup; CAN-SPAM/PECR/CASL footers | ESP terms | Art. 50(4) editor of record | L3 | may activate |
| SMS / voice | TCPA (§3) | carrier 10DLC | — | — | **no channel** |
| LinkedIn — personal | PECR for UK DMs; User Agreement (§5.1) | no automation of any kind | none | L1 (human by hand) | may activate |
| LinkedIn — Page posts | Pages Terms (§5.1) | approved scheduler or MDP only | editor of record | L3 via approved vendor | may activate via Buffer/Hootsuite; MDP application open |
| LinkedIn — DMs / connects | PECR + User Agreement | human only | none | L1 | may activate, human only, no sequences |
| X — feed account | Developer Policy (§5.2) | bio bot statement; no replies/DMs/follows | §6.5 | L3 | may activate once labelled |
| Bluesky — feed account | Guidelines (§5.3) | authenticity + commercial disclosure | §6.5 | L3 | may activate once labelled |
| Meta | Platform Terms (§5.4, partial) | App Review or approved scheduler | §6.5 | L3 | deprioritised |
| Reddit | Data API Terms (§5.5) | human only; per-sub rules; disclose affiliation | none | L0 | human only, if at all |
| Site chat/assistant | AI Act Art. 50(1), B.O.T. Act, Utah 13-75 (§6) | — | §6.5 first-message text | L3 | build with disclosure from day one |

---

## 10. Interaction with the data-rights register

- Outreach contacts and ingested filer contacts live in **separate stores with no join key**
  (`13-legal-data-rights.md` §5.4 rule 4). The CRM cannot query the proposal graph for email addresses.
- A message may reference a public fact about a person's project (it is the honest "how we found you"), but
  must not reproduce restricted-source content — an SPP or ISO-NE row is not pasted into an email.
- Social feed posts are derived records plus link, with the source credit line rendered, and obey the
  per-source publication rule: no PJM/MISO/SPP/ISO-NE rows in a feed until those sources clear.

---

## 11. Items requiring counsel before any channel activates

1. **The L2 definition** (§7.1) — confirm that "sender has personally read every message before queueing"
   keeps a sequencer inside CAN-SPAM's initiator rules and outside the LinkedIn/B.O.T./Utah bot definitions,
   and draft the vendor clause that allocates CAN-SPAM responsibility.
2. **EU country map for B2B email** (§2.2) — confirm the opt-in-only list (DE/AT/IT at minimum) and the
   legitimate-interest countries; produce the per-country footer text. EU email is blocked until then.
3. **TCPA consent language** for any future product SMS alerts (§3), and confirmation that no current channel
   is a "call".
4. **CASL "conspicuous publication" basis** (§4) — confirm that a person named on a public docket or RFP may
   be contacted about *that* docket or RFP under s. 10(9)(b), and that a subscription pitch is outside it.
5. **LinkedIn manual-research boundary** (§5.1 inference 3) — what a human may record from a profile into the
   CRM without breaching "copy, use, display or distribute any information … obtained from the Services".
6. **Retention figures and the suppression-store plaintext question** (§8.1, §8.3).
7. **EU AI Act Art. 50(4)** — confirm that a named editor reviewing each published summary brings us inside
   the editorial-control exemption, and whether change-event feed items count as "informing the public on
   matters of public interest".
8. **AI claims substantiation file** (§6.1) — review the evidence behind "AI-powered", "certified" and any
   accuracy claim before they appear in marketing copy.
9. **Meta and Business Wire terms** could not be fully retrieved (§5.4; `13-legal-data-rights.md` §2.7);
   obtain and read before either is used.
10. **Privacy notice and LIA** — the same documents as `13-legal-data-rights.md` §7 item 10; outreach cannot
    start before they exist.
