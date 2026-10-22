# 66 — Owner actions: the desk list

**Status:** live list, 2026-10-22. Only the owner can do these: each needs a person's account, signature, browser
or judgement. Agents keep this list current and never do the items themselves. Tick an item by telling the
coordinator; it is then moved to "Done" with the date.

**Secrets** go into GitHub, never into chat: [add a repository secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new) (Tobombadil/Bankable → Settings →
Secrets and variables → Actions → New repository secret). Detail lives in the linked docs; this page is the short
version. Doc links point at the working branch until it merges to `main`.

## Now (unlocks work already being built)

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 1 | **Register the ERCOT Public API account** at [apiexplorer.ercot.com](https://apiexplorer.ercot.com): Sign In/Sign Up → verify email → **Products** → subscribe to the public API product → **Profile** → Show the **Primary key**. Use a password used nowhere else. Then [add three secrets](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new): `ERCOT_API_SUBSCRIPTION_KEY` (the primary key), `ERCOT_API_USERNAME` (your email), `ERCOT_API_PASSWORD`. ERCOT's terms bar accounts registered by bots, so this must be you. ([ERCOT's registration guide](https://developer.ercot.com/applications/pubapi/user-guide/registration-and-authentication/)) | 10 min | ERCOT live and node prices beyond the public downloads | [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md) |
| 2 | **Read MISO's terms in a browser** (a security challenge blocks our fetcher): [Legal and privacy](https://www.misoenergy.org/meet-miso/legal-and-privacy/) and [Terms and conditions](https://www.misoenergy.org/meet-miso/contact-us/terms-and-conditions/). Save each as a PDF and paste the clauses on using, copying, redistributing or republishing market data (prices and the queue), commercial use and attribution. | 15 min | MISO hub prices and queue rows, public or internal | [`docs/62` §8](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md#8-miso-legal-and-reuse-terms) |
| 3 | **Deploy when told.** When the coordinator says a release is ready: open [deploy-beta](https://github.com/Tobombadil/Bankable/actions/workflows/deploy-beta.yml) → **Run workflow** → action `deploy`, tag empty → **Run workflow**. | 2 min | Each finished feature reaching [inframation.io](https://inframation.io) | [`docs/60`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/60-deployment.md) |

## Soon (internal tier and wider coverage)

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 4 | **Register a PJM account in your company's name** for [Data Miner 2](https://dataminer2.pjm.com), then get an API key from the [PJM API portal](https://apiportal.pjm.com) and [add it as secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new) `PJM_API_KEY`. Internal tier only: PJM's terms bar redistribution without a licence. | 15 min | PJM prices for your company's named accounts | [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md) |
| 5 | **Register an ENTSO-E account** at the [Transparency Platform](https://transparency.entsoe.eu) (Login → Register), then [email transparency@entsoe.eu](mailto:transparency@entsoe.eu?subject=Restful%20API%20access) with the subject "Restful API access" and your account email to get an API token; [add it as secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new) `ENTSOE_API_TOKEN`. Internal tier only. | 15 min + their reply | European day-ahead prices for your company's named accounts | [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md) |
| 6 | **Send the commercial-reuse letters**: AEP by post (address in the letter), then the [AEP contact form](https://www.aep.com/contact/) after 15 business days; the Texas Railroad Commission by [email to Publicassist@rrc.texas.gov](mailto:Publicassist@rrc.texas.gov). Not urgent while the site stays noncommercial. SPP becomes a seventh source to ask before any commercial switch (letter to be drafted). | 30 min | Keeping those sources if Inframation ever charges | [`docs/39` §7](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/39-utility-rfp-permission-requests.md) |
| 7 | **Send the utility RFP permission letters** (twelve issuers), in the order the doc gives; each draft names its address. | 1–2 h | More utility RFPs on RFPs & funding | [`docs/39` §0–§3](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/39-utility-rfp-permission-requests.md) |
| 8 | **Send the PJM data licence request.** | 15 min | PJM rows on the public site | [`docs/37`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/37-pjm-data-licence-request.md) |

## When convenient

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 9 | **Set up the email provider**: [sign up for Resend](https://resend.com/signup), verify the sending domain, and [add its API key as a secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new) as `docs/64` §2 names it. | 30 min | Email alerts; invitation emails later | [`docs/64`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/64-private-beta.md) |
| 10 | **Free API keys:** [EIA v2](https://www.eia.gov/opendata/register.php); [SAM.gov](https://sam.gov/content/home) (sign in → Account Details → Public API Key; [about the API](https://open.gsa.gov/api/get-opportunities-public-api/)); [NRC ADAMS](https://adams-api-developer.nrc.gov); regulations.gov via [api.data.gov signup](https://api.data.gov/signup/) ([about the API](https://open.gsa.gov/api/regulationsgov/)); the [LinkedIn developer app](https://www.linkedin.com/developers/apps) (then apply for the Marketing Developer Platform); a [Bluesky account](https://bsky.app). | 45 min | Tier-2 connectors, social posting | [PLAN D8](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/00-PLAN.md) |
| 11 | **Browser look-ups** the fetcher cannot do (DOE OCED portfolio, Wyoming DEQ Class VI, Illinois ICC and others); each item in the doc has its link. | ~20 min each | Carbon-storage and project coverage | [`docs/62`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md) |
| 12 | **Ask Elexon** through their [contact page](https://www.elexon.co.uk/contact/) whether their market index data (MID) is inside the open BMRS licence. | 5 min | GB market index prices | [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md) |
| 13 | **A lawyer's read** of the open questions, when budget allows: the noncommercial posture with a free deal workspace, CAISO's "competitive purposes" clause, the RRC copying conditions, OpenStreetMap share-alike for the derived database. | — | Confidence before charging or promoting My pipeline beyond the beta | [`docs/19` §7](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/19-build-a-deal.md), [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md), [`docs/13` §6.4](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/13-legal-data-rights.md) |
| 14 | **Optional, Bankable (Lovable) app:** in your [Lovable dashboard](https://lovable.dev/dashboard), unpublish it, or switch off its open email-sending and AI functions, so nobody can misuse them. You said not to spend time on Bankable, so this is only if it stays online. | 10 min | Removes two abuse risks | Private findings file sent 2026-10-22 |

## Every January

- **AEP's copyright year.** AEP's terms page prints the current year, so the RFP reader's terms check pauses the
  four AEP issuers each January until the year is updated in `data/sources.yaml` and the issuer registry. An
  agent can make the edit; the owner only needs to know why AEP RFPs pause ([`docs/13` §6.4](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/13-legal-data-rights.md)).

## Done

- 2026-10-21: site made public (`SITE_ACCESS=open`).
- 2026-10-22: deploy-beta re-run after the failed deploy (#22, live on 7238820).
