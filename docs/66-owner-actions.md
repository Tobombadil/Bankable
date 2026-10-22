# 66 — Owner actions: the desk list

**Status:** live list, 2026-10-22. Only the owner can do these: each needs a person's account, signature, browser
or judgement. Agents keep this list current and never do the items themselves. Tick an item by telling the
coordinator; it is then moved to "Done" with the date. Secrets go into GitHub (Tobombadil/Bankable → Settings →
Secrets and variables → Actions → New repository secret), never into chat.

Detail lives elsewhere and is linked; this page is the short version.

## Now (unlocks work already being built)

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 1 | **Register the ERCOT Public API account.** At <https://apiexplorer.ercot.com>: Sign In/Sign Up → verify email → Products → subscribe to the public API product → Profile → Show the Primary key. Use a password used nowhere else. Add three secrets: `ERCOT_API_SUBSCRIPTION_KEY`, `ERCOT_API_USERNAME`, `ERCOT_API_PASSWORD`. ERCOT's terms bar accounts registered by bots, so this must be you. | 10 min | ERCOT live and node prices beyond the public downloads | `docs/29` §1 (ERCOT) |
| 2 | **Read MISO's terms in a browser** (a security challenge blocks our fetcher): <https://www.misoenergy.org/meet-miso/legal-and-privacy/> and <https://www.misoenergy.org/meet-miso/contact-us/terms-and-conditions/>. Save each as a PDF and paste the clauses on using, copying, redistributing or republishing market data (prices and the queue), commercial use and attribution. | 15 min | MISO hub prices and queue rows, public or internal | `docs/62` §8 |
| 3 | **Deploy when told.** When the coordinator says a release is ready: GitHub → Actions → deploy-beta → Run workflow (action `deploy`, tag empty). | 2 min | Each finished feature reaching inframation.io | `docs/60` |

## Soon (internal tier and wider coverage)

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 4 | **Register a PJM Data Miner 2 account in your company's name** (<https://dataminer2.pjm.com>), create an API key, add it as secret `PJM_API_KEY`. For the internal tier only: PJM's terms bar redistribution without a licence. | 15 min | PJM prices for your company's named accounts | `docs/29` §1 (PJM), PLAN 2026-10-22 internal tier |
| 5 | **Register an ENTSO-E Transparency Platform account** (<https://transparency.entsoe.eu>, Login → Register), then email transparency@entsoe.eu with the subject "Restful API access" and your account email to get an API token; add it as secret `ENTSOE_API_TOKEN`. Internal tier only. | 15 min + their reply | European day-ahead prices for your company's named accounts | `docs/29` §1 (ENTSO-E) |
| 6 | **Send the commercial-reuse letters** to AEP (by post, then their web form) and the Texas Railroad Commission (by email). Not urgent while the site stays noncommercial. SPP becomes a seventh source to ask before any commercial switch (letter to be drafted). | 30 min | Keeping those sources if Inframation ever charges | `docs/39` §7 |
| 7 | **Send the utility RFP permission letters** (twelve issuers), in the order `docs/39` §1 gives. | 1–2 h | More utility RFPs on RFPs & funding | `docs/39` §0–§3 |
| 8 | **Send the PJM data licence request.** | 15 min | PJM rows on the public site | `docs/37` |

## When convenient

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 9 | **Choose and set up the email provider** (Resend recommended) so alert emails can send. | 30 min | Email alerts; invitation emails later | `docs/64` §2 |
| 10 | **Free API keys:** EIA v2, SAM.gov, NRC ADAMS, regulations.gov; the LinkedIn Marketing Developer Platform application; a Bluesky account. | 45 min | Tier-2 connectors, social posting | PLAN D8 |
| 11 | **Browser look-ups** the fetcher cannot do (DOE OCED portfolio, Wyoming DEQ Class VI, Illinois ICC, and others). | ~20 min each | Carbon-storage and project coverage | `docs/62` |
| 12 | **Email Elexon** to ask whether their market index data (MID) is inside the open BMRS licence. | 5 min | GB market index prices | `docs/29` §1 (Elexon) |
| 13 | **A lawyer's read** of the open questions, when budget allows: the noncommercial posture with a free deal workspace, CAISO's "competitive purposes" clause, the RRC copying conditions, OpenStreetMap share-alike for the derived database. | — | Confidence before charging or promoting My pipeline beyond the beta | `docs/19` §7, `docs/29`, `docs/13` §6.4 |
| 14 | **Optional, Bankable (Lovable) app:** unpublish it, or switch off its open email-sending and AI functions, so nobody can misuse them. You said not to spend time on Bankable, so this is only if it stays online. | 10 min | Removes two abuse risks | Private findings file sent 2026-10-22 |

## Every January

- **AEP's copyright year.** AEP's terms page prints the current year, so the RFP reader's terms check pauses the
  four AEP issuers each January until the year is updated in `data/sources.yaml` and the issuer registry. An
  agent can make the edit; the owner only needs to know why AEP RFPs pause (`docs/13` §6.4).

## Done

- 2026-10-21: site made public (`SITE_ACCESS=open`).
- 2026-10-22: deploy-beta re-run after the failed deploy (#22, live on 7238820).
