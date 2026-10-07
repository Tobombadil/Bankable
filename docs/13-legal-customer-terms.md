# Customer terms and API licence — DRAFT, counsel review required

Phase 1 legal deliverable. Owner: legal-compliance agent. Status: **DRAFT v0.1, 2026-10-07. Not published.
Not in force. Counsel review required before any part of it is shown to a customer or accepted by anyone.**

**I am not a lawyer and this is not legal advice.** This is a working draft for counsel to correct, written so
the code has something true to point at. §6 lists the questions counsel must answer; nothing below is settled
until they are.

**Why it exists now (2026-09-30 legal audit L-3).** Every API response, CSV and bulk file cited
`/legal/api-licence` as its terms, and `POST /v1/keys` recorded each key as having accepted `api-licence-1.0`.
No such page existed (it answered 404), so the acceptance recorded nothing, and nothing bound API, CSV and bulk
recipients to the duties the source licences pass on (`docs/13-legal-data-rights.md` §6, §6.3). Customer terms
are already a named launch blocker (`docs/00-PLAN.md` decisions log, 2026-09-18 (4)).

**What the code does until counsel approves this draft** (2026-10-07):

- `meta.terms_url`, the CSV `# terms=` line and the bulk meta point at `/legal/reuse`, a factual page generated
  from `data/sources.yaml`: per source, the licence link, the required credit (exact strings verbatim), the
  statement of changes for CC BY sources, the RRC's noncommercial-and-unaltered condition, and the
  derived-only rule. The page says it is a summary of the sources' licences and not a contract
  (`web/legal.py::reuse_conditions`). `/legal/api-licence` answers a 301 to it.
- `services/api/pro.py::API_LICENCE_PUBLISHED = False`. `GET /v1/me` reports `api_licence.status:
  not_published` and no current version. `POST /v1/keys` records no acceptance (`licence_accepted_version` and
  `licence_accepted_at` are NULL, migration 0034) and refuses a request that claims to accept a version.
- Registration records no terms acceptance (`user.tos_version` is never written; there is no checkbox). That
  was already true; it stays true until terms exist.
- **Not changed, outside this lane's files:** the operator route `POST /admin/v1/keys`
  (`services/api/admin_posts.py`) still requires and stores `api-licence-1.0`, with a free-text
  `licence_acceptance_ref` that is meant to cite a separately signed agreement; `web/admin/ops.py` sends that
  value. Until a signed agreement exists, an operator-issued key's stored version is no more evidence than the
  self-serve one was. Recommended fix: the same `current_api_licence_version()` check, so operator keys also
  record nothing while nothing is published.
- Keys created before 2026-10-07 keep `api-licence-1.0` in their row. That records what the code asserted, not
  an acceptance of any document (counsel question 6.7).

**Parties and law (from the decisions log, not from counsel).** The contracting entity is Compass International
Trading Group LLC, 1029 E 8th Ave, Suite 806, Denver, CO 80216 (`docs/00-PLAN.md`, 2026-09-18 (5)), rendered from
`SENDER_LEGAL_NAME`/`SENDER_POSTAL_ADDRESS` on any page that shows it. Governing law: Colorado, "unless counsel
advises otherwise" (`docs/00-PLAN.md` open question 3, answered 2026-09-18). The product name is a placeholder
(`docs/04` §0.6); the draft says "the Service".

---

## 1. Customer terms (site, accounts, alerts, exports)

Drafting notes in *italics* are for counsel and are not proposed text.

**1.1 Who these terms are between.** These terms are between you and Compass International Trading Group LLC
("we"). They apply when you create an account or use the Service's site, alerts, exports or API. If you use the
Service for an organisation, you accept them for it and confirm you may.

*Acceptance mechanics to build when approved: a required, unticked checkbox on `/register` naming the version,
`user.tos_version` written with the version and time, and re-acceptance on a material change (the account page
blocks paid features until re-accepted). Browsewrap is not proposed (`docs/13-legal-data-rights.md` §3.2).*

**1.2 What the Service is.** The Service collects public records about energy and infrastructure projects from
third-party sources, normalises them and publishes them. Every record names its sources, links to them and
carries their licence.

**1.3 The data is other people's, and their terms come with it.** We do not own the source data. Each record is
available to you under the licence of each source it comes from, as summarised at `/legal/reuse` and stated in
every API response (`licence_summary`, `provenance`). When you reuse data from the Service, you must:

1. keep the credit each source requires with the data, using any exact wording the source specifies (for
   example, National Energy SO Open Data: "Supported by National Energy SO Open Data"; California ISO: "Source:
   California ISO");
2. for Creative Commons sources, keep the licence link and say that the data was changed, as our statement of
   changes does;
3. not republish the source's own table where we publish derived fields only;
4. use data from sources whose terms permit noncommercial use only (for example, the Railroad Commission of
   Texas) only for noncommercial purposes, keep it unaltered, and not present it in a misleading way;
5. comply with any other condition the source's licence states.

*Inference, confidence moderate: a contractual pass-through is the only way the Service can make the sources'
conditions bind a downstream recipient; the source licences bind us, not our customers. Whether the RRC's
"noncommercial use" can be passed through by an entity whose own noncommercial status is itself the open
question in `docs/13` §7 item 15 is counsel's.*

**1.4 People named in records.** Some records name people (an owner, a sponsor, a filer). You must not use the
Service to compile profiles of individuals, to find or contact a person named in a record for marketing, or to
re-identify information we have withheld (an ownership share, a street address, a contact detail). If we tell you
that a record has been corrected or a person's details removed, you must make the same change in any copy you
hold within 30 days. *The change feed (`updated_since`, events) carries the change; counsel to confirm whether a
contractual deletion duty on customers is enforceable and proportionate.*

**1.5 Acceptable use.** No scraping of the site outside the API; no attempts to exceed rate limits or quotas;
no sharing of credentials or keys; no use that breaks the law or a source's terms; no reverse engineering of
withheld data.

**1.6 Accuracy.** Records reflect what sources published, as we read them, and may be incomplete, late or wrong.
The Service is provided as is. *Counsel: warranty disclaimer wording under Colorado law; implied warranties.*

**1.7 Liability.** *Counsel to draft: a cap (for example, fees paid in the prior 12 months, or a fixed sum for
free accounts), exclusion of indirect and consequential loss, and the carve-outs Colorado law requires.*

**1.8 Indemnity.** You indemnify us against claims arising from your use of the data in breach of 1.3–1.5.
*Counsel: PJM's Data Miner 2 terms (clause 9, `docs/13` §1.3) would make us indemnify PJM for claims arising
from our customers' use of derivatives; no PJM row is published today, but if a PJM licence is signed the
customer indemnity must be at least as wide.*

**1.9 Suspension and termination.** We may suspend an account or key that breaches these terms. You may close
your account at any time; we then delete your account data as the privacy notice says.

**1.10 Paid plans.** *Not offered while the platform posture is noncommercial (`docs/26`). When offered: fees,
renewal, cancellation, refunds and taxes, written with the Stripe checkout flow (`docs/41`).*

**1.11 Privacy.** The privacy notice at `/privacy` explains how we handle personal data.

**1.12 Changes.** We will give notice of material changes by email and on the site at least 30 days before they
apply, and ask for acceptance again where the law requires it.

**1.13 Law and disputes.** Colorado law; courts of Denver, Colorado. *Counsel: consumer-protection overrides for
EU/UK consumers if accounts are open to individuals; arbitration is not proposed.*

**1.14 Contact.** The address in the privacy notice.

## 2. API licence

**2.1 Grant.** Subject to these terms and the customer terms, we grant you a non-exclusive, non-transferable,
revocable licence to call the API with keys issued to your account and to use the data it returns, for the
term of your account, within your plan's limits.

**2.2 The source licences prevail.** The data returned is licensed to you only to the extent each source's
licence allows (customer terms 1.3). Every response names the terms it is served under (`meta.terms_url`) and
the licence of each source in it (`licence_summary`). Where a field or record is withheld under a source's
terms (`redactions[]`), you must not attempt to recover it.

**2.3 Attribution.** Wherever you display or redistribute data from the API, render each source's
`attribution_text` verbatim, with its `licence_url` where one is given and its statement of changes where one
is given. The credit for a record follows that record; a combined display credits every source in it.

**2.4 Redistribution.** You may redistribute derived data to your own users inside your product with the
attribution in 2.3 and the conditions of 1.3. You may not resell or republish bulk extracts of the data as a
data product of your own, or publish a source's rows where we publish derived fields only. *Counsel: whether
"bulk extract" needs a numeric threshold; the Team/API tier's promised "licence pass-through for restricted
sources" (`docs/11` §3) depends on this clause.*

**2.5 Keeping copies current.** If you store data, use `updated_since` or the event feed at least every 30 days
and apply corrections, takedowns and personal-data removals (customer terms 1.4).

**2.6 Keys.** Keep keys secret; you are responsible for calls made with them; revoke a key you suspect is
exposed. Rate limits and quotas are those of your plan, published in the API reference.

**2.7 Versions.** This licence carries a version id. Creating a key records the version you accepted and the
time. A material change gets a new version; keys created under an older version keep working for at least 30
days after notice, then require re-acceptance.

**2.8 Termination.** On termination you stop calling the API; data you already received under a source's
open licence stays usable under that licence, but this licence's permissions end.

## 3. Attribution requirements (live list)

The binding list is the one generated from `data/sources.yaml` at `/legal/reuse` and carried in every API
response; it is not copied here so that it cannot drift. The register rules it implements are `docs/13` §6 and
§6.3 (`attribution`, `licence_url`, `changes_statement`, enforced by `scripts/check_manifest_licences.py` R5/R6).

## 4. What has to happen before this goes live

1. Counsel answers §6 and returns a reviewed text.
2. The owner approves the reviewed text.
3. One commit: serve `/legal/terms` and `/legal/api-licence` from `web/legal.py`; set
   `API_LICENCE_PUBLISHED = True` and `API_LICENCE_VERSION` to the approved id; point `TERMS_URL` at
   `/legal/api-licence`; add the registration checkbox writing `user.tos_version`; apply the same check to the
   operator key route.
4. Existing keys and accounts: notify and ask for acceptance (counsel 6.7).

## 5. Data-partner term sheet outline (for licensed sources such as PJM)

Not drafted. Heads for counsel when a licence conversation starts: scope of licensed data and fields;
permitted publication shapes (public derived, paid raw, API); attribution and link-back; sublicensing to
customers and the pass-through text; indemnities in both directions (PJM clause 9); fees and audit; term,
termination and what happens to published derived data on termination; governing law and forum
(`docs/13` §3.5, §7 item 9).

## 6. Questions for counsel

6.1 Is a contractual pass-through (1.3, 2.2) the right instrument for the source duties, and does 2.3's
"verbatim attribution" satisfy NESO's "include the following attribution statement" for downstream users?

6.2 The RRC's grant is for "noncommercial use" and requires content to remain "unaltered" (`docs/13` §6.2).
Can it be passed through at all to customers of an LLC, and does our normalisation count as altering?

6.3 Customer deletion duty (1.4, 2.5): enforceable, proportionate, and is 30 days right?

6.4 Liability cap and warranty disclaimer under Colorado law (1.6, 1.7); any consumer-law overrides if
individuals in the EU or UK open accounts.

6.5 Indemnity scope, including PJM clause 9 if a PJM licence is signed (1.8).

6.6 Clickwrap mechanics: is a checkbox plus stored version and timestamp sufficient evidence of acceptance for
the site and for each API key?

6.7 Remediation for keys created before 2026-10-07 with a stored `api-licence-1.0` acceptance of a document that
did not exist: is any notice or correction of the record needed beyond re-acceptance when the licence is
published?

6.8 "Bulk extract" and resale limits (2.4) for the Team/API tier's pass-through promise.
