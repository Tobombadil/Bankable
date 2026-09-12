# Social operating playbook

Owner: content-social. Status: draft for owner review, 2026-09-12. Depends on `01-feasibility.md` §3.5, `03-agent-operating-model.md` §3–§4, `data/sources.yaml` section J.

Scope: how change events from the proposal graph become posts, on which channels, at what cost, under which rules. Social is acquisition for the owned channel (RSS + email alerts), not the product. Every post links to a proposal or opportunity page that carries provenance and an alert sign-up.

Platform facts below were checked on 2026-09-12 against the URLs cited. Prices and limits change; re-verify quarterly (calendar entry in §7).

---

## 1. Channel strategy

### 1.1 Summary

| Channel | Role | Launch? | Cadence (target) | API cost/month at 30 posts/day | at 100 posts/day | Automation at launch |
|---|---|---|---|---|---|---|
| Email + RSS (owned) | Primary. Alerts, digests, the asset we own | Yes | Event-driven alerts (paid) + weekly digests (free) | $0–20 | $20–90 | Auto (transactional alerts); digest reviewed |
| Bluesky | Firehose feed of public-tier events; cheap reach into energy/policy/journalist community | Yes | Up to 30/day, spread | $0 | $0 | Reviewed → auto after graduation (§4.6) |
| LinkedIn Company Page | Highest-value audience (developers, lenders, EPCs, utilities, advisers); low volume, curated | Yes (bridge until API approved) | 1–3/day + weekly digest; never the firehose | $0 API (scheduler bridge until approval, cost not verified) | $0 | Reviewed, always |
| X | Journalists, analysts, policy staff; metered per post | Yes, capped | ≤30 link posts/day, hard credit cap | ≈$182 + ≈$7 metrics reads | ≈$608 + ≈$22 reads | Reviewed → auto after graduation; "Automated" label mandatory |
| Threads | Later (month 6 review) | No | — | $0 | $0 | — |
| Mastodon | Later, low priority | No | — | $0 | $0 | — |
| Reddit | Never automated; owner posts manually if at all | No | — | — | — | Manual only |
| YouTube Shorts | Never for the event feed | No | — | — | — | — |

Working assumption on cost basis: 30.4 days/month; X link posts at $0.20; metrics reads at $0.001 per owned resource, each post read ~7 times over its first week. Month total across recommended channels at 30 posts/day: **≈ $190–210** (X ≈ $190, email $0–20, Bluesky $0, LinkedIn $0). At 100 posts/day: ≈ $650–720.

### 1.2 Email newsletter + RSS (owned)

**Audience fit.** This is the industry's working channel. Developers, lenders, EPCs and utility planners read email and subscribe to feeds; nobody in the ICP discovers a 400 MW storage proposal via a social algorithm and acts on it without a saved search behind it. Every social post exists to convert to a sign-up here.

**Formats.**
- RSS/Atom: one feed per region × technology × event type, plus a global feed. Delayed tier only. Each item: title, structured summary, link to proposal page, `source` attribution, `pubDate` = public-tier release time.
- Email alerts (Pro, live tier): per saved search, event-driven, batched at most hourly; plain text + HTML, one event per section, attribution per section.
- Weekly digest (free tier): per region and per technology; top changes by capacity, RFPs opening/closing this week, awards, withdrawals. Sent Monday 07:00 local to the region.

**Legal.** CAN-SPAM applies to commercial email: accurate headers and subject, physical postal address, clear opt-out that is honoured within 10 business days, and the sender is liable even when a vendor sends; each violating email is subject to penalties of up to $53,088 (FTC compliance guide, https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business, fetched 2026-09-12). Alerts a user explicitly subscribed to are transactional/relationship messages; the digest and anything promotional is commercial. Treat all sends as commercial for footer purposes; it costs nothing. EU/UK subscribers: consent-based opt-in only; no pre-ticked boxes (legal-compliance to confirm in `docs/13-*`).

**Tooling and cost.** Transactional: Resend free tier is 3,000 emails/month with a 100/day cap; Pro is $20/month (Automation Atlas summary, https://automationatlas.io/answers/resend-pricing-explained-2026/, 2026-09-12; confirm on resend.com before purchase). Newsletter: Beehiiv Launch plan is free to 2,500 subscribers (emailtooltester, https://www.emailtooltester.com/en/reviews/beehiiv/pricing/, 2026-09-12). Recommendation: Resend for alerts and digests from our own templates so subscriber data stays in our database; add a newsletter product later only if long-form editorial appears.

**Automation policy.** Alerts: fully automated (they are the product). Digests: model-drafted from structured fields, reviewed by the owner until month 2, then auto. Every send carries the disclosure footer (§3.4).

### 1.3 Bluesky

**Audience fit.** Energy journalists, policy analysts, academics, climate-tech operators migrated here in 2024–25; the developer/finance ICP is thinner than on LinkedIn but present. Zero API cost makes it the right place to run the full public-tier firehose and to learn which event types earn engagement before spending on X.

**Format limits.** 300 graphemes per post (3,000 bytes UTF-8); up to 4 images at 2 MB each; 1 video, 100 MB, 3 minutes (PublishQ, citing the `app.bsky.feed.post` lexicon, https://publishq.com/blog/bluesky-api-post-limits, updated 2026-06-19). Links are not auto-detected: the poster must attach facets (byte ranges) and, for a card, an external embed with title/description/thumbnail. Our adapter builds both from the proposal page's OpenGraph fields.

**API route.** AT Protocol XRPC. Write endpoint `com.atproto.repo.createRecord` (collection `app.bsky.feed.post`). Auth: app password (Settings → Privacy and security → App passwords) via `com.atproto.server.createSession`, or OAuth (Bluesky's preferred path for new clients). Start with an app password stored as a secret; migrate to OAuth when the client library in use supports token refresh cleanly.

**Rate limits.** Per account: 5,000 points/hour and 35,000/day; create = 3 points, update = 2, delete = 1. `createSession`: 30 per 5 minutes, 300/day. Per IP: 3,000 requests per 5 minutes (PublishQ summary of the Bluesky rate-limit doc, above; the official page moved to https://bsky.network/docs/advanced-guides/rate-limits and returned empty on fetch, 2026-09-12; a 2023 maintainer statement of 3,000 req/5 min is at https://github.com/bluesky-social/atproto/discussions/697). At 100 posts/day we use 300 of 35,000 daily points. Cache the session; never create a session per post.

**Cost.** $0 at any volume in scope.

**Verification.** Set the handle to a domain we control (`bankablehq.com` or a subdomain such as `feed.bankablehq.com`) via a DNS TXT record; that is self-verification and cannot be faked. Bluesky also issues blue badges and lets Trusted Verifiers issue scalloped badges; apply once the account has a track record (Bluesky blog, https://bsky.social/about/blog/04-21-2025-verification, 2025-04-21).

**Automation policy and disclosure.** Bluesky has no mandatory automation label. We label anyway: bio states the account is automated and names the human contact (§2.2). Pipeline publishes only after graduation (§4.6); until then every post is reviewed.

### 1.4 LinkedIn Company Page

**Audience fit.** The ICP lives here: project developers, tax-equity and debt teams, EPCs, IPPs, utility resource planners, advisers, regulators' staff. It is also the channel where the owner's professional reputation is most exposed. Volume is the enemy of reach on a Company Page; the firehose stays off LinkedIn.

**Cadence.** 1–3 posts/day of the highest-signal events (large proposals ≥200 MW or ≥$250m, RFP opened/closing within 14 days, awards, cancellations/reinstatements), plus the Monday digest. Everything else goes to the RSS feed the page links to.

**Format limits.** 3,000 characters per post; text truncates at roughly 140 characters on mobile behind "See more" (LinkedIn Help, https://www.linkedin.com/help/linkedin/answer/a528176, 2026-09-12). Article posts via the API do not scrape the URL: `content.article.source`, `title`, `description` and an uploaded `thumbnail` image URN must be supplied (Posts API, https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-07, updated 2026-05-13). Lead with the fact in the first 140 characters.

**API route.** Community Management API, product "Community Management API" on the Marketing Developer Platform. Endpoint `POST https://api.linkedin.com/rest/posts` with `author = urn:li:organization:{id}`, headers `Linkedin-Version: YYYYMM` and `X-Restli-Protocol-Version: 2.0.0`. Scope `w_organization_social` (post as organisation; the authorising member must be ADMINISTRATOR, CONTENT_ADMIN or DIRECT_SPONSORED_CONTENT_POSTER on the page) and `r_organization_social` for reading comments; Share/Page Statistics endpoints for metrics (same doc set).

**Approval steps and timeline** (Community Management App Review, https://learn.microsoft.com/en-us/linkedin/marketing/community-management-app-review?view=li-lms-2026-07, updated 2026-02-11; Overview, https://learn.microsoft.com/en-us/linkedin/marketing/community-management/community-management-overview?view=li-lms-2026-07, updated 2026-05-15):
1. Prerequisites: a registered legal organisation with a commercial use case (individuals are not eligible); business email on the organisation's domain (personal addresses fail vetting); legal name, registered address, website, privacy policy URL.
2. Create the app at developer.linkedin.com; app name must not contain "LinkedIn", "Linked" or "In". Associate it with the Company Page; a Page super admin must verify the association.
3. Request **Development Tier** access via the product's access form. LinkedIn checks: approved use case, verified business email, verified organisation, verified website/domain, page-verified app. If rejected, you cannot re-apply with the same app; create a new one.
4. Development Tier limits: 500 requests per app and 100 per member (FAQ in the Overview). Enough for our volume (≤3 posts/day plus metrics) — Development Tier may be sufficient indefinitely; confirm no expiry applies at approval.
5. **Standard Tier** (only if needed): integrate fully, then submit the Standard form plus a downloadable, narrated, high-resolution screencast showing the OAuth flow, a post to the page, how comments and commenter personal data are displayed, and any other member-data handling. Provide test credentials and a privacy policy.
6. Timeline: LinkedIn publishes none. Third-party trackers report 4–8 weeks fast path, 3–4 months typical (Phyllo, https://www.getphyllo.com/post/linkedin-api-access-in-2026-partner-program-approval-timeline-alternatives, 2026-09-12). Plan on 8 weeks; apply on day 1 of setup.
7. Tokens: access tokens live 60 days; programmatic refresh tokens (365 days) are issued to approved MDP partners; the member must re-authorise when the refresh token expires (https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens, updated 2025-10-08). Calendar the re-auth at day 350.
8. API version header must track LinkedIn's monthly versions; versions sunset after ~12 months (202508 sunsets 2026-08-17 per the banner on every doc page). Add a quarterly bump to the maintenance calendar.

**Bridge until approval.** Use a scheduler that already holds Community Management access (Buffer or Hootsuite are the named candidates in `sources.yaml`; pricing not verified here — owner to confirm, budget ≤$30/month). The pipeline writes approved LinkedIn drafts to the review queue; the owner pastes or the scheduler's API posts them. Do not use browser automation against linkedin.com: it breaches the User Agreement and risks the page.

**Cost.** $0 API. Bridge scheduler until approval: unverified, assume ≤$30/month.

**Automation policy and disclosure.** No platform label exists for automated Company Page posts. The page's About section states that event posts are generated from public filings by Bankable's pipeline and reviewed by a named person. Every LinkedIn post stays human-reviewed indefinitely (reputation exposure; low volume makes review cheap).

### 1.5 X

**Audience fit.** Energy journalists, trade-press reporters, policy staff, some analysts. The developer/finance ICP has partly left, but the press has not, and press pick-up of a large proposal is a real acquisition path. Worth a capped budget, not an uncapped firehose.

**Format limits.** 280 characters for non-Premium accounts; links count as ~23 characters regardless of length; Premium raises the limit (ferryman.io, https://ferryman.io/character-limits/x, 2026-09-12). Do not budget on Premium for the automated account; 280 is enough for the templates in §3.3.

**API route.** X API v2, `POST /2/tweets`, OAuth 2.0 user context (PKCE) for the automated account, app registered in the Developer Console. Follow, like and quote-post write endpoints were removed from self-serve tiers in April 2026 and are Enterprise-only (SocialNexis, https://socialnexis.com/guides/x-api-basic-enterprise-automation-rules, July 2026); we need none of them.

**Pricing** (X docs, https://docs.x.com/x-api/getting-started/pricing, fetched 2026-09-12): post creation $0.015; post containing a URL $0.200; standard reads $0.005 per post and $0.010 per user; owned reads (your own posts, followers) $0.001 per resource; post reads capped at 3 million per billing cycle; no minimum spend; credits are deducted in real time and the balance can go slightly negative, which blocks access until topped up; resources are deduplicated within a 24-hour UTC window. Pay-per-use became the default for new developers on 2026-02-06 and the $0.20 URL surcharge arrived in April 2026 (Postproxy, https://postproxy.dev/blog/x-api-pricing-2026/, updated 2026-09-10; Blotato, https://www.blotato.com/blog/twitter-api-pricing, 2026-09-12 — the two sources disagree on the exact April date; the console is authoritative). Basic ($200/month) is closed to new sign-ups; Pro is being migrated to pay-per-use (Blotato, same page).

Every Bankable post contains a URL, so the price per post is $0.20:

| Volume | Posts/month | Post cost | Metrics reads (7 owned reads/post) | Total |
|---|---|---|---|---|
| 30/day | 912 | $182.40 | ≈ $6.40 | **≈ $189** |
| 100/day | 3,040 | $608.00 | ≈ $21.30 | **≈ $629** |

Set a hard credit top-up cap of $250/month in the pipeline's budget config, not only in the console. When the cap is reached, X posts fall back to the review queue with `reason=budget` and the weekly report flags it.

**Required labelling.** X's developer guidelines require automated accounts to enable the "Automated" profile label, state clearly in the bio that the account is a bot and who operates it (their example: "Bot by @yourcompany"), and link to a human-managed account for contact; allowed automated content includes scheduled informational posts, RSS-style updates and alerts with no unsolicited mentions; prohibited: identical content across multiple accounts, trend manipulation, bulk cross-posting, automated replies to random posts, bulk DMs (https://docs.x.com/developer-guidelines, fetched 2026-09-12; X's automation rules page at https://help.x.com/en/rules-and-policies/x-automation returned 403 to our fetcher, owner to read it directly). The label is enabled from the account's settings under account information/automation, naming the managing human account; the owner does this, not the pipeline.

**Automation policy and disclosure.** "Automated" label on; bio disclosure; one account only (no duplicate content across accounts); no automated replies, mentions or DMs, ever. Auto-publish only after graduation (§4.6).

### 1.6 Later or never

**Threads — later (review at month 6).** Free API; 250 API-published posts per profile per rolling 24 hours; 500-character text posts; `link_attachment` supported on text posts; two-step create-container-then-publish flow (Meta docs, https://developers.facebook.com/docs/threads/overview and https://developers.facebook.com/docs/threads/posts, fetched 2026-09-12). Requires a Meta developer app and Meta's business verification for advanced access (`sources.yaml` social.meta). Audience is consumer-skewed; almost no ICP presence today. Zero marginal cost makes it a cheap experiment once the Bluesky adapter exists (same post shapes), but not before the owned channel is proven.

**Mastodon — later, low priority.** Free; a proper `bot` flag on the account (https://docs.joinmastodon.org/methods/accounts/, 2026-09-12); default 300 requests per 5 minutes per account and per IP, 30 media uploads per 30 minutes (https://docs.joinmastodon.org/api/rate-limits/, 2026-09-12); 500-character default limit, instance-configurable. The audience is small and instance moderation policies vary; an automated commercial feed can be defederated by instances without notice. Only worth it if a relevant energy/climate instance emerges or a customer asks. Threads' fediverse bridge may make this moot.

**Reddit — never automated.** Reddit's Responsible Builder Policy (updated June 2026) requires explicit approval before any API access and prohibits automated posts, comments or DMs that place identical or substantially similar content across subreddits (https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy, via search 2026-09-12); commercial API use needs a negotiated licence, reported from ~$12,000/month (`sources.yaml` social.reddit; SocialCrawl, https://www.socialcrawl.dev/blog/reddit-data-api-2026). Community norms (the 90/10 convention, subreddit-specific promotion bans) make an automated feed a ban magnet. If the owner wants Reddit, they post manually in r/energy, r/RenewableEnergy or state subreddits, disclosed as Bankable, at most weekly, and only where subreddit rules allow.

**YouTube Shorts — never for the event feed.** Our unit of content is a structured fact with a link; video adds production cost and removes the link (Shorts do not carry clickable links in the description that convert). Reach is consumer-algorithmic, not professional. Explainer video is a marketing decision outside this playbook.

---

## 2. Account setup checklist (owner)

Agents cannot create accounts (CAPTCHAs, identity checks, terms). The owner performs every step below and hands back only the items in §2.5. Nothing here touches personal accounts; the automated identity is the company.

### 2.1 Handles

Register the same handle everywhere; fallbacks in order.

| Channel | Preferred | Fallbacks | Notes |
|---|---|---|---|
| Bluesky | `bankablehq.com` (domain handle) | `feed.bankablehq.com`, `@bankablehq.bsky.social` | Domain handle needs a `_atproto` TXT record; that is the verification |
| LinkedIn | existing Bankable Company Page | — | Confirm the page is the one linked from bankablehq.com; do not create a second page |
| X | `@bankablehq` | `@bankable_feed`, `@bankableproposals` | Separate from the owner's personal account; the personal account is the "managed by" link |
| Email | `alerts@bankablehq.com` (from), `hello@bankablehq.com` (reply-to) | — | SPF, DKIM, DMARC on the domain before the first send |
| RSS | `https://bankablehq.com/feeds/...` | — | No account; served by the app |

### 2.2 Bios and disclosure text

Use verbatim; agents may not change these without the owner's approval.

**Bluesky (256-char bio limit; keep under):**
> Public energy & infrastructure proposals and opportunities, from official filings. Automated feed run by Bankable; a human reviews. Sources cited on every item. Contact: hello@bankablehq.com

**X (160 chars; "Automated" label ON):**
> Automated account operated by Bankable (@{owner_handle}). Posts derived from public filings, sources linked. Not advice. hello@bankablehq.com

**LinkedIn About (add one paragraph):**
> Bankable publishes change events from public energy and infrastructure registers. Posts on this page are generated from structured public data by Bankable's pipeline and reviewed by {owner name} before publication. Every post cites its source. Nothing here is investment advice.

**Email footer (every send):**
> You are receiving this because you subscribed at bankablehq.com. Data derived from public sources cited above; see each item's source and licence. Unsubscribe: {one-click link} · Manage alerts: {link} · Bankable, {registered postal address}.

### 2.3 Verification

- Bluesky: set the domain handle (DNS TXT). Optionally apply for a blue badge later via the form linked from the Bluesky verification post.
- LinkedIn: Page must have a super admin (the owner) and be associated with the developer app; that association is the verification LinkedIn checks.
- X: enable the "Automated" label and link the managing account. Do not buy Premium for the bot; consider Verified Organizations only if press engagement justifies it (not costed here).
- Email: SPF/DKIM/DMARC (`p=quarantine` minimum) verified in the sending provider before launch; send a seed test to Gmail, Outlook and Apple Mail addresses.

### 2.4 Developer app registration

**Bluesky.** Settings → Privacy and security → App passwords → create `bankable-pipeline`. No app registration is needed for app-password auth. If moving to OAuth: register client metadata JSON at a URL on bankablehq.com per the AT Protocol OAuth spec.

**LinkedIn.** developer.linkedin.com → Create app: name "Bankable Proposals" (no "LinkedIn"/"Linked"/"In"), company page = Bankable, privacy policy URL, logo. Verify with the page as super admin. Products → Community Management API → request Development Tier; use the business email. Set OAuth redirect URL to the admin panel's callback. After approval, run the authorisation code flow once as page admin with scopes `w_organization_social r_organization_social`; the pipeline stores the tokens. Note the organisation URN (`urn:li:organization:{id}`) from the page admin URL.

**X.** developer.x.com → Developer Console → create project and app "Bankable Proposals"; enable pay-per-use billing and add a card; set a console spend alert at $150 and $250. App permissions: Read and Write (no DM). OAuth 2.0 with PKCE; redirect URL = admin panel callback. Authorise the automated account (not the owner's personal account). Record client ID/secret.

**Email.** Resend (or chosen provider): add domain, create DNS records, create API key scoped to sending only; create a webhook endpoint for bounces/complaints/unsubscribes pointing at the pipeline.

### 2.5 Secrets to store and hand back to the pipeline

Store in the secret manager defined by devops (`infra/`), never in the repo or `sources.yaml`. Names are the contract with the publisher service.

| Secret | Channel | Rotation |
|---|---|---|
| `BSKY_HANDLE`, `BSKY_APP_PASSWORD` | Bluesky | Revoke and reissue on any incident; otherwise annually |
| `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_REFRESH_TOKEN`, `LINKEDIN_ORG_URN` | LinkedIn | Access 60 days (auto-refresh); refresh 365 days (owner re-auth, calendar day 350) |
| `X_CLIENT_ID`, `X_CLIENT_SECRET`, `X_ACCESS_TOKEN`, `X_REFRESH_TOKEN`, `X_ACCOUNT_ID` | X | Refresh tokens rotate on each refresh; owner re-auth on revocation |
| `EMAIL_API_KEY`, `EMAIL_WEBHOOK_SECRET`, `EMAIL_FROM`, `EMAIL_POSTAL_ADDRESS` | Email | Annually |
| `SOCIAL_BUDGET_X_MONTHLY_USD` (default 250) | Budget | Owner-only |

Hand back also: the LinkedIn organisation URN, the X user ID of the automated account, the Bluesky DID, the DMARC report mailbox, and written confirmation (a line in `docs/00-PLAN.md` decisions log) of which channels are enabled for review-mode posting. Auto-publish is a separate, later decision (§4.6).

---

## 3. Editorial standards

### 3.1 Which events earn a post

Events are emitted by the pipeline (`03-agent-operating-model.md` §3 steps 2–6). The publisher subscribes to a filtered subset. Thresholds are configuration, not code; initial values below.

| Event type | Post when | Channels | Timing |
|---|---|---|---|
| `proposal.new` | capacity ≥ 50 MW, or ≥ 100 MW for solar/storage in high-volume ISOs, or load ≥ 100 MW, or transmission ≥ 100 kV, or capex ≥ $100m where stated | Bluesky, X; LinkedIn if ≥ 200 MW / ≥ $250m / nuclear, LNG, CCS, transmission ≥ 230 kV | Public-tier release (after the delayed-tier lag) |
| `proposal.status_changed` | any transition between canonical stages (e.g. queued → study, study → agreement, agreement → construction, → operational) on a proposal that met the size threshold | Bluesky, X; LinkedIn for reaching interconnection agreement, construction or operation | Public-tier release |
| `proposal.withdrawn` | any withdrawal of a proposal that met the threshold | Bluesky, X; LinkedIn if ≥ 200 MW | Public-tier release |
| `opportunity.rfp_opened` | any RFP/tender/solicitation in scope from an open-licence procurement source | All | Live (public procurement is already public and time-bound) |
| `opportunity.rfp_closing` | 14 days and 3 days before the response deadline | All (LinkedIn: 14-day only) | Live |
| `opportunity.awarded` | any award in scope with a named awardee from the official record | All | Live if from an open source; else public-tier release |
| `funding.cancelled` / `funding.reinstated` | any change to award status in federal/state funding registers | All | Live (official record) |
| `digest.weekly` | every Monday, per region (ISO/state group) and per technology | Email, RSS, LinkedIn (one national digest), Bluesky (thread, one per region) | Monday 07:00 region-local |

Never posted: source-health events, resolver merges, enrichment-only changes (e.g. a new document linked with no status change), anything from a source whose `reuse` is `restricted` or `unknown` in `sources.yaml` (PJM until licensed; MISO/SPP/NYISO/ISO-NE until terms are recorded), anything derived from news text rather than a register.

Assumption recorded: proposal-graph events post on public-tier release, not live, so that live alerts keep their value; open procurement and official funding-status events post live because the source is already public and time matters to the reader. Owner to confirm; change in `00-PLAN.md` decisions log if not.

### 3.2 Post anatomy

Every post, on every channel, in this order:
1. Fact line: what changed, size, technology, place. Built only from structured fields.
2. Identifiers where useful: queue ID, docket, solicitation number.
3. Link to the proposal/opportunity page on bankablehq.com (UTM: `utm_source={channel}&utm_medium=social&utm_campaign={event_type}&utm_content={event_id}`).
4. Attribution line: `Source: {source_name}, {retrieved_date}` (full URL and licence are on the page; on LinkedIn and email include the source URL inline too).
5. Delayed-tier notice (proposal events only): `Public feed runs {lag_days} days behind. Live alerts: bankablehq.com/alerts`.

Fields available to templates (nothing else is passed to the model): `event_type, event_date, proposal_name, technology, capacity_mw, capacity_unit, load_mw, voltage_kv, capex_usd, county, state, country, iso_rto, queue_id, docket_id, solicitation_id, status_from, status_to, developer_org, issuer_org, awardee_org, award_usd, deadline_date, source_name, source_url, retrieved_at, licence, page_url, lag_days, digest_items[]`. `developer_org`, `issuer_org`, `awardee_org` are used only when present in the official record; never inferred.

### 3.3 Templates per event type

Placeholders in braces. Omit a clause if its field is null; never invent a value. Character budgets: Bluesky ≤ 300 graphemes, X ≤ 280 (link = 23), LinkedIn ≤ 1,300 (first 140 carry the fact), email/RSS unbounded but structured.

**proposal.new**
- Short (Bluesky/X): `New in {iso_rto} queue: {capacity_mw} MW {technology}, {county} County, {state}. Queue {queue_id}. {developer_org}. {page_url} Source: {source_name}, {retrieved_at:date}. Public feed {lag_days}d behind; live alerts on the page.`
- LinkedIn: first line `{capacity_mw} MW {technology} proposed in {county} County, {state} ({iso_rto} queue {queue_id}).` then one paragraph: developer (if in record), requested in-service date (if in record), point of interconnection (if in record). Then `Details, provenance and alert sign-up: {page_url}` and `Source: {source_name} — {source_url} (retrieved {retrieved_at:date}; {licence}). This public feed runs {lag_days} days behind our live tier.`
- Email/RSS item: title `{capacity_mw} MW {technology} — {county}, {state} — new in {iso_rto} queue`; body = LinkedIn body without the preamble.

**proposal.status_changed**
- Short: `{proposal_name or capacity_mw MW technology}, {state}: status {status_from} → {status_to} ({iso_rto} {queue_id}). {page_url} Source: {source_name}, {retrieved_at:date}. Public feed {lag_days}d behind.`
- LinkedIn: first line `{capacity_mw} MW {technology} in {state} moves to {status_to}.` paragraph: from/to, date of change per source; then link, attribution, lag notice.

**proposal.withdrawn**
- Short: `Withdrawn from {iso_rto} queue: {capacity_mw} MW {technology}, {county} County, {state} ({queue_id}). {page_url} Source: {source_name}, {retrieved_at:date}. Public feed {lag_days}d behind.`
- No reason is ever stated unless the source record carries a coded reason field; then quote the code verbatim (e.g. "Reason per record: Withdrawn by IC").

**opportunity.rfp_opened**
- Short: `RFP open: {issuer_org} — {solicitation_title}. {capacity_mw} MW {technology} / {scope}. Responses due {deadline_date}. {page_url} Source: {source_name}, {retrieved_at:date}.`
- LinkedIn: first line `{issuer_org} opens {solicitation_title}: responses due {deadline_date}.` then scope, eligibility as stated in the record, deadline, link, attribution. No lag notice (live).

**opportunity.rfp_closing**
- Short: `Closing in {days_left} days: {issuer_org} — {solicitation_title}. Due {deadline_date}. {page_url} Source: {source_name}.`

**opportunity.awarded**
- Short: `Awarded: {issuer_org} selects {awardee_org} for {solicitation_title}{, award_usd formatted}. {page_url} Source: {source_name}, {retrieved_at:date}.`
- Only when the awardee is named in the official record. If the award amount is absent, omit; never estimate.

**funding.cancelled / funding.reinstated**
- Short: `{Cancelled|Reinstated}: {award_usd formatted} {funding_program} award to {awardee_org} for {project_name}, {state}, per {source_name} record dated {event_date}. {page_url}`
- LinkedIn: add the prior status and the record's stated status text verbatim. No commentary on cause.

**digest.weekly**
- Email/RSS: sections per event type in the order: RFPs closing this week, RFPs opened, awards, new proposals (top 10 by MW), status changes (top 10), withdrawals (top 5), funding changes. Each item is the short template without the lag notice; one lag notice at the top of the digest.
- LinkedIn (national): first line `This week in US energy proposals: {n_new} new proposals ({sum_mw} MW), {n_rfps} RFPs open, {n_awards} awards.` then the top 5 items as one-line bullets, then link to the digest page.
- Bluesky: thread, first post as LinkedIn first line + link; up to 5 replies with one item each.

### 3.4 Style guide

- **Facts only, from the record.** A post states what a register says, when it said it, and where. It does not say what it means. Banned words in generated text: "likely", "reportedly", "appears", "sources say", "rumoured", "plans to" (unless the record uses it), "controversial", "surprising", "massive", "huge", "game-changing".
- **No speculation about named parties.** No characterisation of a developer's, issuer's or awardee's intent, capability, finances or prospects. No linking a party to another proposal unless the resolver produced the link from official records with confidence ≥ 0.9, and even then phrase as "also listed on {n} other {iso_rto} queue entries" with the page link, not as a narrative.
- **No text from news articles.** Enrichment from news feeds the proposal graph (with citation) but never the post. Posts are generated from structured fields only (§4.2); the model does not see article text.
- **Tone.** Plain, declarative, specific. Numbers with units. No exclamation marks, no hashtags except one channel-appropriate technology tag on Bluesky/X where it aids discovery (`#solar`, `#storage`, `#transmission`, `#datacenters`), none on LinkedIn. No emoji.
- **Units and formats.** MW with no decimal unless < 10 MW; USD as `$1.2bn`, `$450m`, `$8.5m`; dates as `12 Sep 2026`; kV for voltage; state two-letter code on short formats, full name on LinkedIn/email.
- **Attribution on every post.** Source name and retrieval date on short formats; name, URL, date and licence on LinkedIn, email and RSS. Attribution text comes from `sources.yaml` and cannot be edited in the review queue.
- **Delayed-tier notice** on every proposal-graph post. Not on procurement/funding posts published live.
- **Not advice.** The bio carries "Not advice"; posts do not repeat it.
- **Corrections policy.** A factual error (wrong number, wrong party, wrong status, wrong source) is corrected within 24 hours of confirmation. LinkedIn: edit the post and add a comment "Corrected {date}: {what changed}." Bluesky and X (no edit on the automated account): delete the original and post `Correction to our {date} post: {what changed}. {page_url}`; log both post IDs. Email: correction item in the next digest; individual correction email only if the error concerned a deadline or an award. The proposal page shows a change log with the correction. If the error originated in the source register, say so in the correction and link the source. Requests from a named party to remove a post are routed to the owner; posts derived from public records are not removed for being unwelcome, but are corrected if wrong. Personal data removal requests are honoured per the guardrails.

---

## 4. Post pipeline spec

### 4.1 Overview

```
change_event ──▶ filter (§3.1 rules, source licence, budget) ──▶ draft (per channel) ──▶ validate
      ──▶ review queue (admin panel) ──▶ schedule ──▶ publish adapter ──▶ post record ──▶ metrics poll
                                   └── auto-approve path only for graduated (channel, event_type) pairs
```

Components: `publisher/filter.py`, `publisher/draft.py`, `publisher/validate.py`, `publisher/queue.py`, `publisher/adapters/{bluesky,linkedin,x,email,rss}.py`, `publisher/metrics.py`. Config in `config/social.yaml` (thresholds, cadence, budgets, graduation flags); secrets from the secret manager. Tests next to code, with adapters mocked; a contract test per adapter against the platform's documented request shape.

### 4.2 Draft generation

- Input: the change event plus the field set in §3.2, nothing else. The drafting prompt receives a JSON object of those fields and the template for `(event_type, channel)`; it never receives free text from sources, article bodies, PDFs, or prior posts.
- Output: for each channel, `{text, facets/links, embed{title,description,image}, alt_text}`.
- Deterministic first: templates are string-formatted in code. The model is invoked only to (a) compress to a channel limit when the formatted template overflows, (b) write the LinkedIn paragraph and digest summaries. Model output is validated (§4.3) and, if it fails twice, the deterministic template is used with clauses dropped in the order defined in the template file.
- Model calls are logged with event ID, prompt hash, output hash, tokens and cost; per-post model cost budget ≤ $0.01, monthly cap in config.

### 4.3 Validation (hard gates, all must pass)

1. Length within channel limit (graphemes for Bluesky; X's weighted count; LinkedIn 3,000).
2. Contains `page_url` exactly once, and the URL resolves (HEAD 200) to a page whose canonical proposal ID matches the event.
3. Contains the attribution line from `sources.yaml` unchanged.
4. Contains the lag notice when `event_type` starts with `proposal.`.
5. Every number in the text appears in the event fields (regex extract → set membership; allows unit conversion MW↔GW and USD rounding).
6. Every organisation name in the text appears in `developer_org|issuer_org|awardee_org`.
7. No banned words (§3.4 list); no `@` mentions of any account on X and Bluesky (no unsolicited mentions); no URLs other than `page_url`.
8. Source `reuse` ∈ {open, attribution} and any per-source `publish_after` condition met.
9. Not a duplicate (§4.8).

Failures write a `validation_failed` record with the reason; three failures for the same event page the owner via the supervision Routine.

### 4.4 Review queue

States: `drafted → approved | edited | rejected → scheduled → published | failed → withdrawn`. Reviewer sees: the event, source link, all channel variants side by side, validation results, and the proposal page. Edits are stored as diffs (used for graduation metrics). Rejection requires a reason code: `wrong_fact`, `not_newsworthy`, `source_doubt`, `style`, `duplicate`, `other`. Queue SLA: LinkedIn/X drafts older than 24 hours expire to `withdrawn` (stale news is worse than no news); Bluesky 48 hours; RFP-closing drafts expire at the deadline.

### 4.5 Scheduling and publishing

- Per-channel calendar slots: Bluesky spread across 06:00–20:00 ET at a minimum 10-minute gap; X the same with the daily cap from budget; LinkedIn slots 07:30, 12:00, 16:30 ET weekdays only; email digests Monday 07:00 region-local; RSS immediate.
- Bursts (e.g. a queue window opening with 300 new entries) are smoothed: the filter ranks by capacity and emits at most the daily cap; the remainder becomes a single "cluster" post (`{n} new proposals totalling {sum_mw} MW entered {iso_rto} queue on {event_date}: {page_url}`) linking a page that lists them.
- Adapter contract: `publish(post) -> {platform_id, url, published_at}`; idempotent on `post.idempotency_key`; raises typed errors `RateLimited(retry_after)`, `AuthExpired`, `Rejected(reason)`, `Transient`.
- Post record stores: channel, platform ID and URL, text as published, event ID, idempotency key, cost (X: $0.20 for URL posts, $0.015 otherwise; model cost), reviewer, approval mode (manual/auto), timestamps.

### 4.6 Graduation to auto-publish

A `(channel, event_type)` pair may auto-publish only when all hold, and the owner has set `auto_publish: true` for that pair in `config/social.yaml` with a date and their name:
- ≥ 200 posts of that pair published in review mode, over ≥ 30 days.
- Edit rate ≤ 5% and `wrong_fact` rejections = 0 over the trailing 100 drafts.
- Zero platform policy incidents (removed posts, warnings, label disputes) on that channel in the trailing 30 days.
- Adapter error rate ≤ 1% over the trailing 30 days.
- Channel is labelled/disclosed per §2.2.
- LinkedIn: never; it stays manual by decision (§1.4).

Auto-publish is revoked automatically on any `wrong_fact` correction, any platform enforcement action, or the X budget cap; re-graduation requires a fresh 100-draft window. Digests graduate separately from event posts.

### 4.7 Rate limits and back-off

| Channel | Platform limit (verified §1) | Our ceiling | Back-off |
|---|---|---|---|
| Bluesky | 5,000 pts/h, 35,000/day; createSession 30/5 min | 100 posts/day; 1 session per process, refreshed on `ExpiredToken` | On 429: honour `RateLimit-Reset`; else exponential 30 s → 60 min with jitter; circuit-break after 5 consecutive failures |
| LinkedIn | Dev tier 500 req/app, 100/member | 3 posts/day + metrics polls ≤ 100/day | On 429: back off 15 min, then 1 h; `AuthExpired` → refresh; if refresh fails → page owner, queue holds |
| X | Pay-per-use, endpoint-specific limits in console | 30 posts/day (config), credit cap $250/mo | On 429: honour `x-rate-limit-reset`; on 402/insufficient credit → hold queue, `reason=budget`, notify owner; never auto top-up |
| Email | Provider limits (Resend free 100/day; Pro higher) | Batch alerts hourly; digests once weekly | Provider 429 → retry with provider guidance; bounces/complaints suppress the address immediately |

All adapters: single worker per channel (no parallel writes), token-bucket throttle in front of the adapter, dead-letter queue after 3 retries, and a daily count against ceilings exposed as a metric.

### 4.8 Duplicate suppression

- Idempotency key: `sha256(channel | proposal_or_opportunity_id | event_type | status_to | event_date[:10])`. Publishing checks the key before every call; adapters store the platform ID against it so retries after a timeout do not double-post.
- Cross-event suppression: the same proposal is not posted to the same channel more than once in 7 days unless the new event is higher-priority (`withdrawn` > `status_changed` > `new`; `rfp_closing` always posts).
- Resolver merges: if two proposals merge after one was posted, the surviving ID inherits the posting history; no re-announcement.
- Content-hash guard: a draft whose text (minus URL and dates) matches any post on that channel in the last 30 days is rejected as `duplicate` (X's identical-content rule and our own hygiene).
- One account per channel; the same event may go to Bluesky, X and LinkedIn with channel-specific text, which is not cross-account duplication on any platform.

### 4.9 Metrics collection

- Bluesky: `app.bsky.feed.getPostThread` / `getPosts` for likes, reposts, replies, quotes (no impressions exposed); free.
- LinkedIn: Share Statistics and Social Metadata endpoints (impressions, clicks, reactions, comments); free within Dev tier limits.
- X: owned reads of our own posts' `public_metrics` at $0.001 per resource; poll at +1 h, +6 h, +24 h, +72 h, +7 d (5 reads; the cost table assumes 7 to leave headroom). Verify in the console that own-post reads bill at the owned rate.
- Site: UTM-tagged clicks → proposal page sessions → alert sign-ups, joined on `utm_content=event_id` and stored on the post record. Email: opens (unreliable; report clicks), clicks, unsubscribes, bounces, complaints via provider webhooks.

---

## 5. Engagement rules

- **Every reply, comment and DM is drafted by the pipeline and sent by a human.** Drafts appear in the review queue with the original message, the poster's public profile link and a suggested reply. This does not graduate; it stays manual per the guardrails and per X's rules on automated replies.
- **Draft a reply for:** factual questions about a post; requests for the source; corrections offered by readers (verify against the source before replying); questions about alerts, pricing or coverage; journalists asking for data (route to owner with a suggested reply and the page link).
- **Never engage with:** disputes about the merits of a named party or project; anyone asking us to characterise a developer, issuer or awardee; legal threats or takedown demands (route to owner, no public reply); political or policy arguments; requests for non-public or restricted-tier data in public; anonymous accounts asking for contact details of people named in filings; anything that would require speculation. Trolling and abuse: no reply, mute, log.
- **Never do:** automated follows, likes, reposts, mentions or DMs; @-mention any party in a post; reply to unrelated posts with our links; run polls on other parties' projects; use the automated account to comment on personal profiles.
- **DMs:** the automated X account has DMs off; Bluesky DMs set to followers only and monitored; LinkedIn Page inbox is the owner's.
- **Reader-reported errors** trigger the corrections policy (§3.4) and a thank-you reply naming the fix, sent by the owner.
- **Escalation:** any message from a party named in a post, any press enquiry, any legal wording → owner within 4 working hours; the queue shows these at the top with a red flag.

---

## 6. KPIs and weekly report

### 6.1 KPIs

| KPI | Definition | Source | Target (month 3) |
|---|---|---|---|
| Reach | Impressions (LinkedIn, X); followers × posts as a proxy on Bluesky | Platform metrics | Trend up week over week |
| Engagement rate | (reactions + reposts + replies) / posts | Platform metrics | Bluesky ≥ 1.0/post; LinkedIn ≥ 15/post |
| Clicks to proposal pages | UTM-attributed sessions | Site analytics | ≥ 2% of impressions (LinkedIn), ≥ 0.5% (X) |
| Alert sign-ups | New free accounts with ≥1 saved search, first-touch attributed to a channel | App DB | 50/week by month 3 |
| Pro conversions | Paid seats with social first-touch | Billing + attribution | Report only until month 4 |
| Unsubscribes / complaints | Per 1,000 sends | Email provider | < 3 unsub, < 0.3 complaints per 1,000 |
| Cost per sign-up | (API cost + model cost + tooling) / sign-ups, per channel | Post records + billing | X < $5; Bluesky/LinkedIn ≈ model cost only |
| Review load | Drafts reviewed, edit rate, wrong_fact count | Queue | Edit rate < 5%; wrong_fact = 0 |
| Pipeline health | Publish failures, expired drafts, budget holds | Publisher metrics | Failures < 1% |

### 6.2 Weekly report template (generated Monday 08:00 ET, posted to the owner; stored under `reports/social/YYYY-WW.md`)

```
# Social weekly — week {ISO week}, {date range}

## Headline
{one sentence: sign-ups this week vs last, best channel, one problem}

## Per channel
| Channel | Posts | Impressions | Engagements | Clicks | Sign-ups | Unsubs | Cost (USD) | Cost/sign-up |
|---|---|---|---|---|---|---|---|---|
| Email/RSS | | n/a | | | | | | |
| Bluesky | | n/a | | | | n/a | 0.00 | |
| LinkedIn | | | | | | n/a | 0.00 | |
| X | | | | | | n/a | | |

## Top 5 posts by clicks
{event type, channel, text (first 80 chars), clicks, sign-ups, link}

## Bottom 5 by impressions (same volume class)
{…} — what they have in common

## Event-type performance
| Event type | Posts | Clicks/post | Sign-ups/post |

## Review queue
Drafts: {n}; approved {n}; edited {n} ({%}); rejected {n} by reason; expired {n}; wrong_fact corrections {n}.

## Budget
X spend {USD} of {cap}; model spend {USD}; email sends {n} of plan; forecast month-end {USD}.

## Incidents
Platform actions, corrections issued, escalations (with links and status).

## Graduation status
Per (channel, event_type): posts in window, edit rate, eligible yes/no.

## Recommendations (max 3)
{concrete config changes: thresholds, cadence, slot times, budget}
```

---

## 7. 30-day launch calendar

Day 0 = the day the owner starts account setup. Assumes the public proposal pages and RSS feeds exist (Phase 4 build). LinkedIn API approval will not have landed by day 30; the bridge scheduler covers it.

| Day | Owner | Pipeline / content-social |
|---|---|---|
| 0 | Register handles (§2.1); DNS for Bluesky domain handle and email auth; create X developer project, enable pay-per-use, set $150/$250 alerts; create LinkedIn app, request Development Tier; sign up for email provider | Write `config/social.yaml` with §3.1 thresholds, cadence, X cap $250; adapters in review-only mode |
| 1 | Set bios and disclosure text (§2.2); enable X "Automated" label; hand back secrets (§2.5) | Contract tests against platform request shapes; dry-run drafts against the last 7 days of events; owner reviews 50 sample drafts |
| 2–3 | Approve or edit sample drafts; confirm the public-tier-release timing assumption (§3.1) | Fix templates from edits; RSS feeds live; email footer and unsubscribe verified with seed sends |
| 4 | First posts: Bluesky and X (3 each, hand-approved); LinkedIn "what this page is" post written by the owner, not the pipeline | Metrics polling live; UTM attribution verified end to end |
| 5–7 | Review queue daily (15 min); reply drafts approved as they come | Ramp Bluesky to 10/day, X to 10/day; first weekly report Monday day 7 |
| 8 | Owner posts first LinkedIn digest via scheduler bridge | Digest generator tuned from owner edits |
| 8–14 | Daily queue; note every `wrong_fact` | Ramp Bluesky to 20/day, X to 20/day; add RFP-closing 3-day reminders; weekly report day 14 with first cost/sign-up |
| 15 | Decision: keep X at 20/day, raise to 30, or cut based on clicks and cost/sign-up | Apply config |
| 15–21 | Bluesky thread digests per region start (Monday); owner replies to any press enquiries | Ramp to target cadence (Bluesky 30, X ≤ 30); weekly report day 21 |
| 22 | Owner checks LinkedIn developer portal for Development Tier status; chases via Developer Support ticket if silent | If approved: run OAuth once, store tokens, switch LinkedIn adapter to API in review mode; else keep bridge |
| 22–28 | Daily queue; first corrections drills (deliberately test the delete-and-repost path on Bluesky with a harmless correction) | Duplicate-suppression and burst-smoothing verified against a real queue window; weekly report day 28 |
| 29 | Owner reviews graduation dashboard | Report which (channel, event_type) pairs are on track for the 200-post/30-day gate (none will have passed yet; earliest Bluesky `proposal.new` around day 40) |
| 30 | Retro: keep/cut channels, adjust thresholds, set month-2 budget; record decisions in `00-PLAN.md` | Quarterly reminder created: re-verify X pricing, LinkedIn version header, Bluesky limits (next 2026-12-12); LinkedIn refresh-token re-auth reminder at day 350 |

---

## 8. Sources consulted (2026-09-12 unless dated)

- X API pricing — https://docs.x.com/x-api/getting-started/pricing
- X developer guidelines (automated account labelling, allowed/prohibited automation) — https://docs.x.com/developer-guidelines
- X automation rules (returned 403; owner to read) — https://help.x.com/en/rules-and-policies/x-automation
- X pay-per-use timeline — https://postproxy.dev/blog/x-api-pricing-2026/ (updated 2026-09-10); https://www.blotato.com/blog/twitter-api-pricing; https://socialnexis.com/guides/x-api-basic-enterprise-automation-rules (July 2026)
- X character limit — https://ferryman.io/character-limits/x
- LinkedIn Community Management overview — https://learn.microsoft.com/en-us/linkedin/marketing/community-management/community-management-overview?view=li-lms-2026-07 (updated 2026-05-15)
- LinkedIn Community Management app review — https://learn.microsoft.com/en-us/linkedin/marketing/community-management-app-review?view=li-lms-2026-07 (updated 2026-02-11)
- LinkedIn Posts API — https://learn.microsoft.com/en-us/linkedin/marketing/community-management/shares/posts-api?view=li-lms-2026-07 (updated 2026-05-13)
- LinkedIn refresh tokens — https://learn.microsoft.com/en-us/linkedin/shared/authentication/programmatic-refresh-tokens (updated 2025-10-08)
- LinkedIn post length — https://www.linkedin.com/help/linkedin/answer/a528176
- LinkedIn approval timelines (third party) — https://www.getphyllo.com/post/linkedin-api-access-in-2026-partner-program-approval-timeline-alternatives
- Bluesky limits — https://publishq.com/blog/bluesky-api-post-limits (updated 2026-06-19); https://github.com/bluesky-social/atproto/discussions/697; official page https://bsky.network/docs/advanced-guides/rate-limits (empty on fetch)
- Bluesky verification — https://bsky.social/about/blog/04-21-2025-verification (2025-04-21)
- Threads API — https://developers.facebook.com/docs/threads/overview ; https://developers.facebook.com/docs/threads/posts
- Mastodon — https://docs.joinmastodon.org/methods/accounts/ ; https://docs.joinmastodon.org/api/rate-limits/
- Reddit Responsible Builder Policy — https://support.reddithelp.com/hc/en-us/articles/42728983564564-Responsible-Builder-Policy ; pricing summary https://www.socialcrawl.dev/blog/reddit-data-api-2026
- CAN-SPAM — https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business
- Email tooling — https://automationatlas.io/answers/resend-pricing-explained-2026/ ; https://www.emailtooltester.com/en/reviews/beehiiv/pricing/
