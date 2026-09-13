# CRM system of record: the Attio contract

**Status:** Sprint 3 input · 2026-09-13 · owner builds the Attio workspace; backend-developer builds
`services/crm/attio.py` against this document. Supersedes the HubSpot assumptions in `docs/33` §7.4 and
`docs/20` §9's comparison; the port design in `docs/20` §9 and ADR 0006 is unchanged.

## 1. Who owns what (the split that keeps three systems from fighting)

| Record | System of record | Who writes it | Mirrored to |
|---|---|---|---|
| Proposals, opportunities, organisations as *subjects*, events, matches | **Platform** (`services/db`) | Pipeline, resolver | Attio gets a *link*, never a copy |
| Accounts, contacts, deals, activities, pipeline stage, consent | **Attio** | Humans in Attio; the adapter for the automatable subset in §5 | Platform caches `account` display fields only |
| Subscriptions, entitlements, invoices, seats | **Stripe** | Stripe (checkout, portal, webhooks) | Attio gets a read-only Subscription record; platform sets `account.entitlement` from the Stripe webhook |

Rule: the platform never treats Attio as the source of a project fact, and Attio never treats the platform
as the source of a commercial fact. Entitlement comes from Stripe, not from a field a human can edit in Attio.

## 2. Objects to build in Attio

Attio's standard objects cover most of it. Two custom objects carry what the platform emits.

### 2.1 Companies (standard) — one per prospect or customer organisation

| Attribute | Type | Set by | Meaning |
|---|---|---|---|
| `domains` | domain (standard) | human / adapter | Match key; the adapter upserts by primary domain |
| `platform_org_id` | text | adapter | `organization.id` in the platform, when the company is also a subject |
| `platform_account_id` | text | adapter | `account.id` once they sign up; empty for prospects |
| `segment` | select: S1 developer/IPP · S2 lender/investor · S3 utility/co-op/CCA · S4 EPC/OEM · S5 advisory/law · S6 data-centre | human | ICP per `docs/33` §1 |
| `owner_relationship` | select: warm · second-degree · cold | human | |
| `lead_score` / `lead_band` | number / select: hot · warm · watch | adapter | From the scoring rubric in `docs/33` §2 |
| `top_event`, `top_event_url`, `top_event_at` | text, text, timestamp | adapter | Latest lead signal, denormalised for the list view |
| `tools_in_use` | multi-select | human | Enverus, Cleanview, Halcyon, GridStatus, none, other |
| `do_not_contact` | checkbox | human | Hard stop the adapter and every draft respect |
| `consent_basis` | select: legitimate-interest · consent · customer · none | human | Per `docs/13-legal-outreach-and-social.md` |
| `partner_source` | record-reference → Companies | human | Channel partner that introduced them |
| `stage` | status: the ten stages in `docs/33` §7.2 | human (agent may move 0→1 only) | |
| `next_action`, `next_action_at` | text, timestamp | human | |
| `footprint` | multi-select: ERCOT · CAISO · NYISO · ISO-NE · SPP · MISO · PJM · non-ISO SE · non-ISO West · GB · EU | human | Which markets they care about; drives which events are relevant |

### 2.2 People (standard) — business contacts only

`name`, `email_addresses` (company email), `job_title`, `phone_numbers` (optional), plus custom:
`source_of_contact` (select: public filing · company site · conference · referral · inbound),
`opt_out` (checkbox) and `opt_out_at` (timestamp), `linkedin_company_page_url` (text; the company page, never
a personal profile). No personal data beyond this; the adapter never creates People, only humans do.

### 2.3 Deals (standard) — one per commercial motion

`name`, `value`, `stage` (use Attio's deal stage for: pilot-proposed · pilot-running · committed · won · lost),
`associated_company`, plus custom: `tier` (select: pro · team · api · enterprise), `seats` (number),
`pilot_scope`, `pilot_start` (date), `pilot_metric`, `pilot_result`, `commitment_type` (select: card · LOI ·
contract), `close_reason` (select per `docs/33` §7.2 reason codes), `stripe_subscription_id` (text, adapter),
`originating_signal` (record-reference → Lead Signals).

### 2.4 Lead Signals (custom object) — what the platform emits

One record per platform event that makes an account timely (`docs/10` US-403; `docs/33` §2 triggers).

| Attribute | Type | Meaning |
|---|---|---|
| `signal_id` | text, unique | Platform `event.id`; idempotency key for the adapter |
| `company` | record-reference → Companies | Resolved by domain or `platform_org_id`; unresolved signals go to the "Unmatched signals" list |
| `event_type` | select: proposal.new · proposal.status_changed · proposal.withdrawn · opportunity.rfp_opened · opportunity.rfp_closing · opportunity.awarded · funding.cancelled · funding.reinstated · match.new |
| `subject_kind`, `subject_name`, `subject_url` | select, text, text | Link back into the platform; the CRM never copies the record |
| `jurisdiction`, `technology`, `capacity_mw` | text, select, number | Enough to read the signal in a list without clicking |
| `score`, `rationale` | number, text | From the rubric; rationale is one sentence |
| `observed_at` | timestamp | Event time, not insert time |
| `handled` | checkbox | Human tick; agents never set it |

### 2.5 Subscriptions (custom object, read-only mirror of Stripe)

`stripe_subscription_id` (unique), `company` (reference), `plan` (select: pro · team · api · enterprise),
`seats`, `status` (select: trialing · active · past_due · cancelled), `current_period_end`, `mrr`. Written only
by the adapter from Stripe webhooks; humans read it. If a human edits it, the next webhook overwrites it.

## 3. Relationships

```
Companies 1─* People            (standard)
Companies 1─* Deals             (standard)
Companies 1─* Lead Signals      (custom reference; the core link from platform to CRM)
Companies 1─* Subscriptions     (custom reference; from Stripe)
Deals     *─1 Lead Signals      (originating_signal: which event started the conversation)
Companies *─1 Companies         (partner_source)
```

## 4. Lists to create

| List | Object | Purpose | Stages / list attributes |
|---|---|---|---|
| Discovery interviews | Companies | The 20 conversations in `docs/33` §4 | scheduled · held · synthesised; `notes_ref` |
| Founding Pro pre-sale | Companies | The 10-commitment target in `docs/01` §6 | offered · committed · declined |
| Unmatched signals | Lead Signals | Signals the adapter could not attach to a company | resolve · discard |
| Data partners | Companies | GridTracker, Cleanview, Halcyon, Catalyst Cooperative | contacted · term-sheet · signed |
| Channel partners | Companies | Law firms, lenders, co-op G&Ts, trade press | |

## 5. What the adapter does, exactly

**Writes (idempotent on the keys named):**
- Upsert **Company** by primary domain; set only `platform_org_id`, `platform_account_id`, `lead_score`,
  `lead_band`, `top_event*`. Never touches human-owned fields.
- Create **Lead Signal** on qualifying events (idempotent on `signal_id`), attach to the company or the
  Unmatched list.
- Create **Deal** on the US-403 hand-off with `originating_signal` set; never moves a deal stage.
- Upsert **Subscription** from Stripe `customer.subscription.*` webhooks; set `Deal.stripe_subscription_id`
  when the customer id matches.
- Log an **activity note** on the company when an alert email is delivered (type `alert_open` only when the
  tracking pixel or link fires; never infers engagement).

**Reads:**
- `do_not_contact`, `opt_out`, `consent_basis` before any draft is generated for that company or person.
- `stage` to filter which companies the weekly pipeline report (`docs/33` §7.3) covers.
- Nothing else. Entitlement is read from Stripe.

**Never:** sends email, LinkedIn messages or calls from Attio; creates People; edits human-owned fields;
moves stages above 1; deletes anything.

**Inbound webhooks from Attio → platform:** `record.updated` on Companies (to refresh the cached display
fields) and on Lead Signals (`handled` → close the platform task). Verified with the Attio webhook secret.

## 6. What the owner builds, in order

1. Create the two custom objects (Lead Signals, Subscriptions) with the attributes above; add the custom
   attributes to Companies, People and Deals. Use the exact slugs in §2 so the adapter's mapping is a
   constant, not a config file.
2. Create the five lists with their stages.
3. Create an API key scoped to read/write on records, lists and webhooks; hand it back as an environment
   secret (`ATTIO_API_KEY`), never in the repo.
4. Register the two outbound webhooks (§5) pointing at `https://api.{{DOMAIN}}/webhooks/attio`; record the
   signing secret as `ATTIO_WEBHOOK_SECRET`.
5. Import the curated RFP-issuer list and the 20 discovery targets from `docs/33` §4 as Companies with
   `segment` set; that gives the adapter something to attach signals to on day one.

## 7. Things to decide while building

- **One company or several for a utility holding?** Recommend one Company per operating entity (the one that
  issues RFPs or holds queue positions), parent as `partner_source`-style reference if needed later.
- **Segment on Company or on Deal?** Company. A lender that also develops is rare enough to handle by hand.
- **Should Lead Signals expire?** Yes: the adapter marks signals older than 90 days `handled=false, stale=true`
  (add a `stale` checkbox) so lists stay readable; nothing is deleted.
- **Attio's built-in email sync:** fine for humans; the human-sends rule (`docs/13` §7) still governs — the
  adapter never triggers a send.
