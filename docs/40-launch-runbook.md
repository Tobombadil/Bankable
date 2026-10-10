# Launch runbook

**Status:** Sprint 3 item 6, v1 · 2026-09-13 · product-manager · reviewed by: owner (`docs/04` R-2 row
"product-manager → owner")
**Inputs:** `docs/00-PLAN.md` Sprint 3 kickoff (S3-4, "Owner actions still open"), 2026-09-13 decisions rows
"Sprint 3 first wave landed", "Admin backend landed", "Admin panel UI landed", "Workers landed";
`docs/10-prd-mvp.md` US-908, §3.3; `docs/11-market-and-competition.md` §3; `docs/13-legal-data-rights.md` §6, §7;
`docs/32-social-operating-playbook.md` §2; `docs/60-deployment.md` §10, §11; `docs/04-standards.md` §9;
`docs/34-crm-system-of-record.md` §6; `services/billing/README.md`, `services/crm/README.md` (environment
variables); `infra/compose/.env.example`.
**Scope:** what ships at launch, what the owner must still create, the exact deploy sequence, the release
gate, day-one operations, and the gaps this runbook does not paper over.

Placeholders `Infraque` and `infraque.com` stand for the product name and domain until `docs/00-PLAN.md`
decision S3-5 is closed; "Infraque" is the working placeholder name, not yet permanent.

---

## 0. Owner's morning checklist (written 2026-09-15 evening for 2026-09-16)

Everything the coordinator could do without the owner is done and pushed (`docs/00-PLAN.md` decisions log,
2026-09-14/15). What remains needs a human: a decision, an account, a signature, or a browser that can pass a
bot challenge. Ordered by what each item unblocks. Times are estimates, not measurements.

### 0.1 Decisions (reply in chat; about 5 minutes each)

| # | Decision | Why now | Recommendation |
|---|---|---|---|
| D1 | ~~"Open the PR"~~ **Done 2026-09-15 evening:** PR #1 open, 13/13 CI jobs green after seven runs, mergeable; remaining decision is merge | `ci.yml` runs only on pushes to `main` and PRs against `main`, so real CI has never run on this branch; every red mark you saw was a misfiled worker script, now moved (`infra/cloudflare/README.md`). The coordinator opens the PR only on your word and then watches CI | Yes, open it; merge after CI is green |
| D2 | ~~Cost ceiling~~ **Done:** US$2 per accepted permit event, escalation to $5 only by owner ask for the county pilot (`docs/00-PLAN.md` 2026-09-15 "Permits are stages", item 3) | The kill criterion is half a criterion without a number | Pick a dollar figure per extracted permit event; the pilot reports measured cost against it |
| D3 | ~~D-13 budget~~ **Done:** re-based to 350 KB gzipped (docs/04 D-13) | MapLibre GL 5.24 alone is 276 KB gzipped; measured, pre-existing, recorded 2026-09-15 | Re-base; a renderer swap costs a sprint for no user-visible gain |
| D4 | ~~Product name~~ **Done 2026-09-15 evening: Infraque at infraque.com** (docs/12 §11). Still yours this morning: register infraque.com and infraqueue.com; book counsel's knock-out search | Domain registration, Stripe product names, Attio workspace name and the wordmark all wait on it | Decide before creating any account below, or every account gets renamed later |

### 0.2 See it yourself (15 minutes, at your desk)

```
git pull
python -m pipeline.context.eia_plants --latest-snapshot
python -m web.dev_up --preview
```
Then open http://127.0.0.1:8000/?layers=plants and: tick **Existing plants**, pick **Plant type → Biomass /
waste**, zoom past level 9 for labels, tap a square, open **Attribution** in the footer. Done-check: the first
command prints `"plants": 14659`; the biomass filter reports 582 plants. For the admin Engagement page, run
`python -m services.api.bootstrap owner --email you@example.com --db web/.data/dev.db`, sign in, open
`/admin/engagement`: the basemap-failure counter should read the number of times you loaded the map with
tiles blocked (the OpenStreetMap dev raster is often blocked; that is the launch reason for §2.7).

### 0.3 Accounts and secrets, in the order they unblock each other (§2 has every done-check)

1. **Cloudflare** — zone for the domain, API token, R2 enabled (§2.6). Unblocks: `tofu apply`, the tiles
   bucket, DNS. About 30 minutes.
2. **`tofu apply`** from `infra/terraform` with `HCLOUD_TOKEN` and `CLOUDFLARE_API_TOKEN` (§3 step 1). Creates
   the app VM and both R2 buckets. Then the two dashboard steps for the tiles bucket (custom domain, CORS with
   `Range`) in `infra/terraform/storage.tf`'s comment block (§2.7). About 30 minutes.
3. **Basemap** — add the four R2 secrets to the `production` GitHub environment and run
   `.github/workflows/basemap.yml` once (`workflow_dispatch`); it extracts the 3.9 GB file (measured 28 s in the
   sandbox, longer on Actions) and promotes it to `basemap.pmtiles`. Set `MAP_TILE_URL`. Done-check: §2.7.
4. **Managed Postgres** (Neon or Crunchy Bridge) with the four extensions; run the migrations (§2.6, §3 step 2).
   First-ever run outside SQLite: expect surprises and report them. About 30 minutes plus fixes.
5. **Resend** (§2.4), **Stripe** (§2.3), **Attio** (§2.2, `docs/34` §6 has the exact objects and slugs).
   Attio is the longest: about 90 minutes to build the two custom objects, five lists, key and webhooks.
6. **Sentry, Grafana Cloud, SOPS age keys** (§2.6). About 30 minutes together.

### 0.4 Browser tasks no script can do (save a PDF of each page; paste the operative clause into `docs/13`, or send it to the coordinator to record)

| Site | Why it needs you | Record in |
|---|---|---|
| https://ourgridfuture.org/ terms/licence | Cloudflare challenge blocks scripted clients (HTTP 403 on ten paths, 2026-09-15) | `docs/13` §2.9, `data/sources.yaml` `us.ourgridfuture.transmission_projects` |
| EIA Energy Atlas pipelines dataset, its own licence field | The Hub catalogue API returned no row for the slug | `data/sources.yaml` `us.eia.atlas.gas_pipelines` |
| EPA permit-search dashboard terms (RBLC) | New dashboard replaced the old pages; terms not retrieved | `data/sources.yaml` `us.epa.rblc` |
| EIA Atlas dataset-level `licenseInfo` for the six context layers (pipelines, processing plants, storage, LNG terminals, ethanol plants, biodiesel plants) | About pages are a JS app; the field is not readable by script (2026-09-15, 2026-09-18) | `data/sources.yaml` `us.eia.atlas.*` |
| Argonne RNG Database terms | Cloudflare challenge (HTTP 403) to the script probe, 2026-09-18; Argonne is contractor-operated so §105 does not apply automatically; `reuse: unknown` until read | `docs/13` §2.14, `data/sources.yaml` `us.anl.rng_database` |
| PHMSA pipeline annual-report data page | Cloudflare challenge (HTTP 403), 2026-09-18; confirm the file layout and any data-page notice | `data/sources.yaml` `us.phmsa.pipeline_operator_reports` |
| EPA RFS public-data file links | Page renders 200 but the xlsx/csv links are JS-rendered; open in a browser and record the file URLs | `data/sources.yaml` `us.epa.rfs_public_data` |
| FERC major pipeline projects pending page | WAF blocks scripted fetch; a headless-browser connector is the plan | `data/sources.yaml` `us.ferc.pipelines_pending` (verified note) |

### 0.5 With counsel (§2.1; not this morning, but book it)

PJM planning-page question and redistribution licence; MISO terms; and open question 7(b): whether the paid
tier may sit on an OpenStreetMap-derived *data* layer. Take `docs/13` §2.10 with you: rendering Protomaps
tiles is a produced work and is already comfortable; ingesting OSM data is the separate question, and the
ODbL text and the OSM Foundation guideline disagree on one point that counsel should see.

### 0.6 What the coordinator does without you, and what waits

Without you: opens the PR on D1 and drives CI; keeps the tree green; records whatever you paste from 0.4.
Waits on 0.3 items 1–4: the first real deploy, the first Postgres migration run, and the basemap done-check.
Waits on D2: nothing before launch. Waits on D3: one line in `docs/04`. Waits on D4: every account name.

## 1. Coverage statement at launch (S3-4)

Per-source licence basis is `docs/13-legal-data-rights.md` §6's publication matrix; the launch subset is
`docs/00-PLAN.md` decision S3-4.

| Source | Tier shown | Licence basis (`docs/13` §6) | Confidence |
|---|---|---|---|
| ERCOT generation queue, ERCOT large-load queue | Public, raw | permissive; raw-ok | high |
| CAISO generation queue | Public, derived only, credited "California ISO" | attribution-restricted; derived-only | mod-high |
| NYISO generation queue | Public, derived only | attribution-restricted; derived-only | mod-high |
| EIA-860M, EIA API | Public, raw | public-domain (17 U.S.C. §105); raw-ok | high |
| NESO TEC register (GB) | Public, raw, credited "Supported by National Energy SO Open Data" | open-attribution; raw-ok + exact string | high |
| US federal funding & procurement (grants.gov, SAM.gov, USDA RD, FERC eLibrary, USASpending, DOE exchange portals) | Public, raw | public-domain (17 U.S.C. §105); raw-ok | high |
| International tenders (EU TED, UK Find a Tender, World Bank procurement notices) | Public, raw, credited per source | open-attribution (OGL v3 / CC BY 4.0); raw-ok + credit | high |

**Sources that show nothing until cleared, and why** (`docs/00-PLAN.md` S3-4; `docs/13` §6 matrix and §7):

| Source | Publish rule today | Blocking item |
|---|---|---|
| PJM generation queue | link-out-only; no rows in the store are publishable | `docs/13` §7 item 1 — counsel must decide whether the planning-page New Services Queue sits outside the Data Miner 2 Terms of Use; if not, the PJM Redistribution Licence enquiry (`docs/00-PLAN.md` "Owner actions still open") is a hard gate |
| MISO generation queue | link-out-only; **do not publish** | `docs/13` §7 item 2 — terms have not been retrieved at all; someone must open `misoenergy.org`'s legal page, save it, and record the operative clauses in `data/sources.yaml` before any row is even API-only |
| SPP generation queue | link-out-only | `docs/13` §6 row (`restricted`); terms recorded but bar commercial use of materials — change-event alerts over this source are themselves counsel item 6 |
| ISO-NE generation queue | link-out-only | `docs/13` §6 row (`restricted`); same posture as SPP |

These four sources ingest into the store today (so entity resolution and the internal graph are not blocked)
but the admin publish gate (US-905) refuses to set any of them to `public` or `api_only` until the licence
class in `data/sources.yaml` changes and a legal evidence entry exists — this is enforced in code, not by
convention (`docs/10` §3.3, US-905 AC1).

**Exact sentence for the About page** (`docs/13-legal-data-rights.md` §6, "Tier-1 MVP consequence"):

> Every queue tracked; ERCOT, CAISO and NYISO published; PJM, MISO, SPP and ISO-NE linked pending licence.

`web/app.py`'s `/about` route exists today; confirm this sentence (or the current equivalent) is the one
rendered there before launch, not an earlier draft that claims broader coverage.

---

## 2. Owner account checklist

Consolidated from `docs/32-social-operating-playbook.md` §2 (social/email), `docs/34-crm-system-of-record.md`
§6 (Attio), `docs/60-deployment.md` §11 (cloud/infra) and `docs/00-PLAN.md`'s "Owner actions still open" /
Sprint 3 kickoff list. Agents cannot create any of these — CAPTCHAs, identity checks, and contracts require a
human (`docs/32` §2 preamble). "Done-check" is a concrete, checkable fact, not "created".

### 2.1 Legal (blocks §1's gated sources)

| Item | Who | Done-check |
|---|---|---|
| PJM planning-page question answered by counsel | Owner + counsel | `docs/13` §7 item 1 has a written answer; `data/sources.yaml`'s `us.iso.pjm.gen_queue` licence field updated accordingly |
| PJM Redistribution Licence (if item above says "no") | Owner (signatory) | Signed licence on file; `data/sources.yaml` licence reference, date, permitted uses recorded (`docs/10` §3.3 G-PJM) |
| MISO terms retrieved and read | Owner or counsel | `misoenergy.org/meet-miso/legal-and-privacy/` saved as PDF; operative clauses quoted into `docs/13` §7 item 2; `data/sources.yaml` classification set |
| SPP / NYISO / ISO-NE terms confirmed in `data/sources.yaml` | legal-compliance | `docs/04` §10 conflict row 6: `data/sources.yaml` `reuse` updated from `unknown` to match `docs/13` §6 (`restricted` / `attribution-restricted`) |
| Counsel brief on the remaining 22 items in `docs/13` §7 | Owner + counsel | Each of the 12 numbered items in `docs/13` §7 has a written answer or an explicit "not yet" with a date |
| Domain registration: `infraque.com`, `infrafeed.com` | Owner | WHOIS shows registrant; both registered per `docs/00-PLAN.md` S3-5 default |
| Name made permanent (Infraque vs Infrafeed) | Owner | Decision logged in `docs/00-PLAN.md`; `infraque.com`/`Infraque` placeholders scheduled for replacement |

### 2.2 CRM — Attio (`docs/34` §6)

| Secret / setup | Environment variable | Who creates | Done-check |
|---|---|---|---|
| Workspace + two custom objects (Lead Signals, Subscriptions), five lists | — | Owner | Objects and lists visible in the Attio workspace with the exact slugs `services/crm/attio.py` expects (`lead_signals`, `subscriptions`, `unmatched_signals`) |
| API key (read/write on records, lists, webhooks) | `ATTIO_API_KEY` | Owner | `build_crm_port()` (`services/crm/README.md` D-1) returns the real adapter (`sor_kind = "attio"`), not the in-memory fake (`sor_kind = "attio_fake"`) |
| Two outbound webhooks → `https://api.infraque.com/webhooks/attio` | — | Owner | Attio's webhook admin page shows both registered and enabled |
| Webhook signing secret | `ATTIO_WEBHOOK_SECRET` | Owner | A test delivery from Attio verifies (`hmac.compare_digest` match, no `WebhookRejected`, `services/crm/README.md` "Webhook verification") |
| Object-id → slug map | `ATTIO_OBJECT_IDS` (JSON) | Owner, after objects exist | A logged `company.updated`/`lead_signal.updated` event resolves to its real slug, not the `"unknown"` sentinel (`services/crm/README.md` A-34-1) |
| Curated RFP-issuer list + 20 discovery targets imported as Companies | Owner | Companies exist with `segment` set (`docs/34` §6 step 5) |

### 2.3 Billing — Stripe

| Secret / setup | Environment variable | Who creates | Done-check |
|---|---|---|---|
| Stripe account | — | Owner | Account active, bank details attached |
| Secret key | `STRIPE_SECRET_KEY` | Owner | `build_billing_port()` returns `StripeBillingAdapter`, not the dry-run fallback (`services/billing/README.md` item 1) |
| Products/Prices for the four tiers in `docs/11` §3 (Pro, Team, Team+API, Enterprise) | `STRIPE_PRICE_PRO`, `STRIPE_PRICE_TEAM`, `STRIPE_PRICE_API`, `STRIPE_PRICE_ENTERPRISE` | Owner | A checkout session can be created against each price without error |
| Webhook endpoint registered in the Stripe dashboard | — | Owner | Endpoint shows "Enabled" against the deployed `/webhooks/stripe`-equivalent route |
| Webhook signing secret | `STRIPE_WEBHOOK_SECRET` | Owner | A Stripe CLI test event verifies against the 300-second tolerance (`services/billing/README.md`) |

### 2.4 Email — Resend

| Secret / setup | Environment variable | Who creates | Done-check |
|---|---|---|---|
| Domain added, DNS records (SPF/DKIM/DMARC, `p=quarantine` minimum) | — | Owner | `docs/32` §2.3 verification: seed test lands in Gmail, Outlook and Apple Mail |
| Sending-scoped API key | `RESEND_API_KEY` | Owner | `services/alerts/README.md`'s dry-run mode turns off (it is dry-run whenever this key is unset) |
| Webhook for bounces/complaints/unsubscribes | `EMAIL_WEBHOOK_SECRET` | Owner | Endpoint receives a test bounce event |
| From/reply-to and postal address for the footer | `EMAIL_FROM`, `EMAIL_POSTAL_ADDRESS` | Owner | Footer renders `docs/32` §2.2's verbatim text with the real address, not a placeholder |

### 2.5 Social channels (`docs/32` §2.1–§2.5)

| Secret / setup | Environment variable | Who creates | Done-check |
|---|---|---|---|
| Bluesky domain handle (`_atproto` TXT record) + app password | `BSKY_HANDLE`, `BSKY_APP_PASSWORD` | Owner | Handle resolves; a test post succeeds |
| LinkedIn developer app, Community Management API (Development Tier), org URN | `LINKEDIN_CLIENT_ID`, `LINKEDIN_CLIENT_SECRET`, `LINKEDIN_ACCESS_TOKEN`, `LINKEDIN_REFRESH_TOKEN`, `LINKEDIN_ORG_URN` | Owner | Marketing Developer Platform application approved; one authorisation-code flow run as page admin; a test post succeeds |
| X developer project/app, pay-per-use billing + spend alerts at $150/$250 | `X_CLIENT_ID`, `X_CLIENT_SECRET`, `X_ACCESS_TOKEN`, `X_REFRESH_TOKEN`, `X_ACCOUNT_ID` | Owner | A test post succeeds under the automated account, not the owner's personal account |
| X monthly spend cap | `SOCIAL_BUDGET_X_MONTHLY_USD` (default 250) | Owner | Set in config; `docs/04` §10 conflict row 8 — this is the authoritative cap, not `docs/20` §14's estimate |
| Written confirmation of which channels are enabled for review-mode posting | — | Owner | A line in `docs/00-PLAN.md` decisions log (`docs/32` §2.5) |

### 2.6 Cloud, database, observability (`docs/60` §11)

| Item | Environment variable | Who creates | Done-check |
|---|---|---|---|
| Hetzner Cloud project | `HCLOUD_TOKEN` | Owner | `tofu plan -var-file=terraform.tfvars` runs against real infrastructure |
| Cloudflare account/zone | `CLOUDFLARE_API_TOKEN` | Owner | Zone visible in the Cloudflare dashboard for `infraque.com` |
| Managed Postgres (Neon or Crunchy Bridge, ADR 0003) with `postgis`/`pg_trgm`/`btree_gin`/`pgcrypto` | `DATABASE_URL` | Owner | `alembic -c services/db/migrations/alembic.ini upgrade head` succeeds against it (never run against real Postgres before this — `docs/60` §11 item 2) |
| Per-environment SOPS age keys | `SOPS_AGE_KEY` (held by deploy operator, not committed) | Owner/devops | `infra/scripts/bootstrap_age_key.sh <env>` run; `.sops.yaml`'s `REPLACE_WITH_*` filled in; `sops -d` decrypts the real `secrets.<env>.enc.yaml` |
| Object storage (R2) | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET` | Owner | `infra/scripts/restore_drill.sh` downloads a real backup and restores it |
| Sentry project | `SENTRY_DSN` | Owner | A test exception appears in Sentry (every process initialises the SDK when the DSN is set, `infra/observability.py`; nothing happens without it) |
| Session signing secret, one per environment, ≥ 32 chars | `SESSION_SECRET` | Owner/devops (`python -c "import secrets; print(secrets.token_urlsafe(48))"`) | The api container starts with `ENVIRONMENT=production`; it exits with `SESSION_SECRET must be set...` when the value is missing or short (`services/api/auth.py`) |
| Service identity shared by api and web | `API_INTERNAL_TOKEN` | Owner/devops (random) | The site's own API calls are not rate-limited as one anonymous client (`services/api/app.py`) |
| Registry read access for the VMs while the GHCR packages are private | `GHCR_USER`, `GHCR_READ_TOKEN` (read-only) | Owner | `docker compose pull` succeeds on a VM; or make the four `ghcr.io/tobombadil/bankable-*` packages public and skip this |
| Grafana Cloud | `GRAFANA_CLOUD_API_KEY` | Owner | A dashboard shows real metrics |
| Model gateway provider key | `MODEL_PROVIDER_API_KEY` | Owner | **Blocked**: `services/modelgw` does not exist yet (`docs/60` §11 item 5) — this key has nothing to authenticate to until that component is built; do not treat "key obtained" as "done" here |
| Free API keys: EIA v2, SAM.gov, NRC, regulations.gov | per-connector, not yet named in an env file | Owner | Each connector's live run returns 200, not 401 |

**Note on where secrets actually live:** `infra/compose/.env.example` (`ENVIRONMENT`, `DOMAIN`, `LOG_LEVEL`,
`IMAGE_TAG`, `SESSION_SECRET`, `POSTGRES_PASSWORD`, `DATABASE_URL`, `R2_*`, `SENTRY_DSN`,
`GRAFANA_CLOUD_API_KEY`, `MODEL_PROVIDER_API_KEY`, `MAP_TILE_URL`, `API_INTERNAL_TOKEN`,
`BACKUP_RETENTION_DAYS`) is the local-Compose template only, explicitly git-ignored when filled in. Every key
in the decrypted per-environment file reaches every container (`docs/60` §5, fixed 2026-09-19). None of the CRM, billing, email or
social secrets above appear in it — they are staging/production values that go into
`infra/sops/secrets.<env>.enc.yaml` (`docs/60` §10.1 access line: "the environment's `SOPS_AGE_KEY`"), which
do not exist yet (`docs/60` §11 item 3). Creating that file is itself a first-deploy step (§3 below).

---

### 2.7 Map basemap tiles (found while the owner tested the prototype, 2026-09-14; decided 2026-09-15)

**Decided** (`docs/00-PLAN.md` 2026-09-14/15; `docs/adr/0007-basemap-protomaps-on-r2.md`): Protomaps
PMTiles, self-hosted on Cloudflare R2, served at `https://tiles.infraque.com/basemap.pmtiles`. This
replaces the prototype's `tile.openstreetmap.org` raster load, whose usage policy forbids
production apps and rate-limits or blocks them (when blocked, the map is a beige outline with
clusters on it and zooming looks like it does nothing). The two hosted-provider options
(MapTiler/Stadia) considered alongside it are recorded, with why they were not chosen, in the ADR.

| Item | Environment variable | Who creates | Done-check |
|---|---|---|---|
| Apply the Terraform: the tiles R2 bucket (`infra/terraform/storage.tf`'s `cloudflare_r2_bucket.tiles`) | — | Owner (`tofu apply`, part of §3 step 1's normal `tofu apply`) | `tofu output tiles_bucket_name` returns a real bucket name |
| Connect the custom domain — a manual dashboard step; the pinned Cloudflare provider does not support `cloudflare_r2_custom_domain` (`infra/terraform/storage.tf`'s comment block has the exact steps) | — | Owner | Cloudflare dashboard shows `tiles.infraque.com` connected and enabled on the tiles bucket |
| Set the bucket's CORS policy (`GET, HEAD` with `Range`, from the site origin) — also manual, same reason (`storage.tf`'s comment block has the exact `curl` call) | — | Owner | The `curl` call in `storage.tf` returns 200; a browser map load does not fail cross-origin |
| Run the refresh workflow once (`.github/workflows/basemap.yml`, `workflow_dispatch`) to populate `basemap.pmtiles` for the first time | `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_TILES_BUCKET` (GitHub Actions `production` environment secrets — the first three already exist per §2.6; `R2_TILES_BUCKET` is new, set to `tofu output tiles_bucket_name`) | Owner (triggers the run) | The workflow's own verification step passes (`accept-ranges: bytes` on the uploaded object); `infra/scripts/build_basemap.sh`'s log names the build date used |
| Set `MAP_TILE_URL=https://tiles.infraque.com/basemap.pmtiles` in the environment's secrets/config | `MAP_TILE_URL` | Owner | frontend-developer's `web/` change (out of this task's scope) reads it and renders the map |

**Done-check for the whole item:** street-level detail renders at zoom 10 on the deployed site; the
attribution line names Protomaps/OpenStreetMap (`docs/adr/0007`'s licence note: "© OpenStreetMap
contributors" must render wherever the basemap does); and the `map.basemap_failed` counter on the
admin ops page stays at zero after the deploy (that counter is `web/`'s to implement — flagged here
as a gap this task's write scope does not cover, not assumed done).

## 3. First deploy, step by step

Order: accounts and secrets, then infrastructure, then the application. Steps 1–6 are `docs/60-deployment.md`
§11's unvalidated-items list, in the order the owner must act; step 7 is `docs/60` §10.1's Deploy runbook,
which only makes sense once 1–6 exist.

1. **Cloud accounts and tokens.** Create the Hetzner Cloud project and the Cloudflare account/zone (or confirm
   the zone already fronting bankablehq.com can host a new one); issue `HCLOUD_TOKEN` and
   `CLOUDFLARE_API_TOKEN`; store both as GitHub Actions environment secrets for `staging` and `production`.
   ```
   cd infra/terraform && tofu init && tofu plan -var-file=terraform.tfvars
   ```
   (copy `terraform.tfvars.example` first). Review the plan before `tofu apply`.
2. **Managed Postgres.** Create a Neon (or Crunchy Bridge) project; enable `postgis`, `pg_trgm`, `btree_gin`,
   `pgcrypto`. Run the migrations against it for the first time ever outside SQLite:
   ```
   alembic -c services/db/migrations/alembic.ini upgrade head
   ```
   The Postgres driver (`psycopg[binary]`) comes from `requirements.txt`; the images no longer install it
   a second time (`docs/60` §11 item 4, rebuilt and checked 2026-09-27: psycopg 3.3.6 in all four images).
   The deploy (step 7) runs the migrations itself; running them by hand here is only a first check that the
   managed provider accepts them.
3. **Per-environment secrets file.** Generate real age keys and create the encrypted secrets files that do not
   exist yet:
   ```
   infra/scripts/bootstrap_age_key.sh dev
   infra/scripts/bootstrap_age_key.sh staging
   infra/scripts/bootstrap_age_key.sh production
   ```
   Fill in `infra/sops/.sops.yaml`'s three `REPLACE_WITH_*` placeholders with the new public keys, then create
   `secrets.<env>.enc.yaml` for each environment (`infra/sops/secrets.dev.example.enc.yaml` is the only
   template that exists today). This file is where every secret in §2 above (`ATTIO_*`, `STRIPE_*`,
   `RESEND_API_KEY`, `EMAIL_*`, `BSKY_*`, `LINKEDIN_*`, `X_*`, `SOCIAL_BUDGET_X_MONTHLY_USD`, `DATABASE_URL`,
   `R2_*`, `SENTRY_DSN`, `GRAFANA_CLOUD_API_KEY`) is written — never into `infra/compose/.env.example` or the
   repo. **Start from `infra/sops/secrets.example.plain.yaml`** (added 2026-09-30): it lists every key, the
   non-secret configuration included (`DOMAIN`, `PLATFORM_POSTURE`, `MAP_TILE_URL`, `SNAPSHOT_STORE`,
   `AUDIT_HASH_PEPPER`, the `SENDER_*` and `PRODUCT_*` identity), and marks which ones `deploy.sh` requires.
   The list above was missing those, and a file built from it took Caddy down and switched the posture to
   `commercial` without an error (devops audit 2026-09-30 F3; `docs/60` §5). Do not add `ENVIRONMENT`;
   `deploy.sh` writes it.
4. **Object storage.** Create the R2 bucket; there is no lifecycle rule pruning it yet (`docs/60` §11 item 8) —
   accept unbounded growth at launch or add one via `infra/terraform/storage.tf` first.
5. **Observability accounts.** Create the Sentry project and the Grafana Cloud account; their keys are wiring
   points already named in `infra/compose/.env.example` and now also belong in the secrets file from step 3.
6. **Verify secrets decrypt** before the first deploy: `sops -d infra/sops/secrets.<env>.enc.yaml` with only
   the intended age key present.
7. **Deploy** (`docs/60` §10.1), secrets and infrastructure before services, workers stopped before
   migrations, migrations before app restart. First make sure an image exists: merging to `main` runs
   `.github/workflows/release.yml`, which pushes `ghcr.io/tobombadil/bankable-{api,web,worker,browser-worker}`
   tagged `sha-<7-char sha>` and `latest`; take the `sha-` tag from that run's log.
   ```
   export APP_HOST=... WORKER_HOSTS="..." BROWSER_WORKER_HOST=... SOPS_AGE_KEY="$(cat path/to/key)"
   infra/scripts/deploy.sh <staging|production> sha-<short sha>
   ```
   The script (`docs/60` §10.1 step 3 has the detail):
   1. It decrypts the secrets and refuses to go on if `API_INTERNAL_TOKEN` is empty or missing. Without the
      token every call `web` makes shares the anonymous 60-per-hour bucket, so pages fail after a few
      views. Nothing has been touched at that point.
   2. It syncs compose files, `backup.sh` and the decrypted secrets to every VM, pulls the images, and stops
      `worker`/`browser-worker`/`scheduler`.
   3. It runs migrations once in a one-off container of the same api image (`alembic upgrade head`, expand
      phase only).
   4. It installs or upgrades Procrastinate's job-queue schema once (`python -m infra.scheduler.queue_schema
      ensure` in a one-off `scheduler` container of the same tag). The first deploy gets the schema
      installed here; later deploys are a no-op, or apply the Procrastinate migrations when the pin moved.
      Nothing needs running by hand, and the one-off `procrastinate schema --apply` this step used to ask for
      is no longer needed. A schema applied that way earlier is recognised and recorded, not re-applied.
   5. It starts `caddy`/`api`/`web` on the app VM (public pages keep serving from the Cloudflare edge cache
      throughout) and waits, bounded, until every replica is healthy and `/v1/health` answers with
      `checks.queue: true`.
   6. It starts the workers, then the scheduler last.

   A failed health or queue check rolls back to the previously deployed tag automatically. The script
   appends a row to `infra/deploy-log.md`. The nightly backup timer (`docs/60` §8) starts working after this
   first deploy ships `backup.sh` and the secrets file to the app VM. All of this except SSH to real hosts
   was run on a local Compose stack on 2026-09-27 (`docs/60` §11 item 10).
   **Verify:** `curl https://infraque.com/health` and `curl https://infraque.com/v1/health` return 200, and
   `/v1/health`'s `build.commit` and the page footer show the deployed commit, not "unknown". On each worker host,
   `docker compose ps` shows `worker`/`browser-worker`/`scheduler` up with no restarts; `deploy.sh` does not
   check them, because they have no healthcheck. The E-10 Playwright smoke suite passes: it passed 19/19 on
   2026-09-26 against a local Compose stack of these images (`docs/60` §11 item 9), but has not yet run against
   staging or production. The new `infra/deploy-log.md` row looks right.
   **If migrations fail:** do not proceed to the restart step; fix forward or roll back the migration per its
   own reversibility docstring; page the owner if a production deploy has been mid-rollout more than 15
   minutes (`docs/60` §10.1).

---

## 4. Go/no-go checklist

From `docs/04-standards.md` §9 R-4 (the qa-engineer's US-908 release sign-off) and `docs/10-prd-mvp.md` US-908
AC1. Each row needs an actual pass, not an assertion; "sign-off" is the qa-engineer named in `docs/04` R-2.

| # | Check | How to prove it | Status today | Sign-off (name/date) |
|---|---|---|---|---|
| 1 | Tier test (US-601) | `pytest tests/test_api_pro_tier.py -q` | Passing per Sprint 3 wave (`services/api/pro.py` tier split) | |
| 2 | Gate test (US-906) | `pytest tests/test_api_admin_records.py -k gate_unmet -q` | Passing (`422 gate_unmet` enforced, "Admin backend landed") | |
| 3 | E-9 integration fixtures (tier + gate across web/RSS/API/export/webhook/post-draft) | `pytest -k "publish_state or visibility" -q` across `tests/` | Not confirmed as one named suite this sprint; verify before sign-off | |
| 4 | Attribution renders on all surfaces | Manual check: web page footer, RSS item, API envelope `licence_summary`, exported CSV row all carry `source_id`/`source_url`/`retrieved_at`/`licence` | Measured 2026-09-26 on a local Compose stack of the release images (`docs/60` §11 item 9). **Web:** the detail page shows `.provenance-panel` and `.attribution-line` at 1440 and 400 px (E-10 smoke). **API:** list and detail rows carry `provenance[]` with `source_id`, `source_url`, `retrieved_at`, `licence_id` and `attribution_text`, plus envelope `licence_summary`. **RSS:** `/feeds/proposals.rss` items carry only the source name (`dc:creator`) and a link to the record page, not `source_url`/`retrieved_at`/licence per item. **CSV:** no export route exists (`docs/41` dropped export) | Gap: RSS items; decide whether the linked page's attribution suffices |
| 5 | Privacy notice live | A `/privacy` page exists on the public site | Built: `web/legal.py` serves `/privacy`, linked from the footer of every public page. `pytest web/test_legal.py` 7 passed (2026-09-26), and `GET /privacy` returned 200 from the containerised `web` image | |
| 6 | Deletion route live | A self-service or admin-triggered path that redacts personal data and requests CRM deletion | **Public request route:** `POST /v1/privacy/requests` (`services/api/privacy_routes.py`) and its form at `/privacy/request` (`web/legal.py`) feed the admin queue `/admin/v1/privacy-requests`. `pytest tests/test_api_privacy_requests.py tests/test_web_privacy_request.py` 20 passed; the containerised API answered a request with 202. **Admin deletion:** `pytest tests/test_api_admin_people.py -k deletion_task` 6 passed; `services/crm/test_attio.py::test_request_personal_data_deletion_opens_a_task_with_a_30_day_deadline` passed (all 2026-09-26). **Self-service deletion (2026-10-09):** a signed-in member deletes their own account from `/account` → `/account/delete` (`web/auth.py`), which says what is deleted and kept and asks for the password; it calls `DELETE /v1/me` (`services/api/auth_routes.py`). That runs the same erasure as a completed operator task (`services/api/account_erasure.py`): CRM deletion requested first, user row anonymised, sessions deleted, own keys revoked, alerts stopped and their recipient redacted, a personal account closed, the address suppressed as a hash, an audit event with hashes and counts, and a `deletion_request` task as the record. Staff roles are refused in-app. `pytest tests/test_api_account_deletion.py tests/test_api_admin_people.py web/test_auth.py` passed (2026-10-09). **Remaining:** `BillingPort` has no cancel operation, so a personal account's live subscription is cancelled by an operator from the open task; a person named in a record (not a member) still uses the request route | Met for members; state the billing hand-off at sign-off |
| 7 | Automated-account labels set | Bios/disclosure text from `docs/32` §2.2 live on each channel profile; `services/social/editorial.py` enforces the disclosure label on post templates (`pytest -k disclosure -q` covers `test_admin_update_post_rejects_removing_disclosure_label` in `tests/test_api_admin_posts.py`) | Code enforces it; the actual profile bios depend on §2's account checklist being done first | |
| 8 | Rate limits active | `pytest tests/test_api_pro_ratelimit.py -q`; confirm `TIER_LIMITS` in `services/api/ratelimit.py` matches `docs/23` §6 | Implemented (in-memory token bucket); `test_tier_limits_match_docs_23_defaults` is the exact check | |
| 9 | Alert unsubscribe works | Click the unsubscribe link in a delivered alert email and confirm no further alerts send | Built: `GET\|POST /v1/alerts/unsubscribe` (`services/api/unsubscribe_routes.py`) and `/unsubscribe` (`web/legal.py`). `pytest tests/test_api_unsubscribe.py` 9 passed (2026-09-26), including the next alert cycle sending nothing for that search. Live on the containerised stack: `/unsubscribe` 200; an unknown token gets a 404 problem response. Not yet proven with a delivered email (needs the Resend key, §2.4) | |
| 10 | Backups verified | `infra/scripts/restore_drill.sh`, then row-count sanity checks against `proposal`/`opportunity`/`organization`/`event` | Run 2026-09-26 with an `aws` shim serving a `pg_dump` of the local stack (R2 not reached). The committed script failed 2 of 2 runs at the restore step; it is fixed (`docs/60` §11 item 9) and now exits 0 in 16 s with counts equal to the source (`proposal` 10,409, `opportunity` 707, `organization` 8,374, `event` 0). Never run against R2 or a real environment (`docs/60` §10.3 "Last executed: not yet") | |
| 11 | M-11 = 0 in the nightly audit | `python -m services.visibility_audit.run` (exit 1 when M-11 > 0), or read the last nightly `visibility_audit_tick` run at `GET /admin/v1/visibility-audits/latest`; tests: `pytest tests/test_visibility_audit.py -q` | Job exists (2026-09-26): nightly at 04:52 UTC on the `audit` queue (`docs/60` §6.1). Measured on a copy of the 2026-09-26 dev store (SQLite, commercial posture), 3.2 s wall: **M-11 = 0** — shown 10,409 proposals, 707 opportunities, 0 events, 8,374 organisations, 17,871 assets, 11,504 source links, 0 breaches on each; 52 sources gated by the register, none of them present in that store; served pass probed 0 rows because the store holds no taken-down, pending or gated-source rows to probe. That proves the job runs and the store is clean; it does not yet prove the served pass against production-shaped data (R-4 "E-9/E-10 green on production-shaped data") — re-run against the production database before sign-off | |
| 12 | ADRs match the running system | Manual review of `docs/adr/000*.md` against what is actually deployed | Not checked this sprint | |
| 13 | No expired exception (`docs/04` §9.4) | `docs/04-standards.md` §9.4 register | Empty register today — nothing to expire | |

**Rows 5 and 9 were hard blockers in the 2026-09-13 pass; both are now built and their tests pass
(2026-09-26).** US-908 AC1 still asks for row 9 to be proven with a delivered alert email. Row 6's account self-delete gap
is closed (2026-10-09); its one manual step, cancelling a personal account's subscription in the billing
provider, must be stated at sign-off.

---

## 5. Day-one operations

**Scheduled jobs that must be running** (`docs/60-deployment.md` §6, §6.1):

| Job | Schedule | Queue | What it does |
|---|---|---|---|
| Per-source fetch ticks | 15-min/daily/weekly/monthly/quarterly/annual buckets from `data/sources.yaml`'s `cadence` | `fetch` / `fetch_browser` | Connector runs, routed by `access` (soon `egress`) |
| `alert_tick` | every 15 minutes | `alert` | Alert cycle + webhook delivery, one tick per firing (US-502 AC1: alerts fire within 15 minutes) |
| `post_draft_tick` | hourly, offset 7 minutes past the hour | `post_draft` | Drafts posts from the event log behind the four `docs/32` §4.3 gates |

Both alert and social ticks carry `retry=0` and a `queueing_lock`; an overlapping tick is refused
(`AlreadyEnqueued`), not double-run. To run either by hand (backfill/debug):
```
python -m services.alerts.worker
python -m services.social.worker
```

**Admin screens to watch daily:** source health (failed-run flag after 3 consecutive failures, per US-904
AC3), the social post review queue (nothing auto-publishes without the owner-only channel switch and
disclosure confirmation), the task queue (intake submissions, resolution disputes, deletion requests), the
cost log (model-call cost per source per day, US-909).

**Manual overrides that exist today:**

- **Interim entitlement grant** — `PUT /admin/v1/accounts/{account_id}/entitlement`
  (`services/api/pro.py`). Sets `account.entitlement` directly with `entitlement_source = "manual_grant"` and
  an audited reason. This is the deliberate stand-in until Stripe checkout is the only path to an entitlement
  change (`docs/00-PLAN.md` kickoff: "the interim admin entitlement endpoint stays as the manual override").
  Use it for one-off grants (e.g. a pre-sale customer before Stripe is wired) and expect it to be superseded,
  not removed casually.
- **Run-now** — `POST /admin/v1/sources/{source_id}/run` (`services/api/admin_sources.py`, US-904 AC2). Always
  enqueues; never runs inline, so a run-now click behaves exactly like the scheduled tick it substitutes for.

---

## 6. Known gaps at launch

Taken from the Sprint 3 decisions-log rows and this runbook's own verification pass. Stated plainly, not
softened.

1. **Intake captcha is verified only when `TURNSTILE_SECRET_KEY` is set** (closed on the API side 2026-09-26:
   `services/api/captcha.py` checks every `captcha_token` against Cloudflare Turnstile's `siteverify`, fails
   closed on a rejected token or an unreachable Cloudflare, sends the client IP only as `remoteip` and stores
   none of it; `infra/compose/.env.example`, `docs/60` §5). With the key unset — every environment today — the
   token is still accepted unverified and the api logs one warning per process; the honeypot and the per-IP
   5/hour bucket remain the only controls. One piece is still missing: the owner's Turnstile site and secret
   keys. The public form exists since 2026-10-09: `GET`/`POST /submit` (`web/submit.py`,
   `web/templates/submit.html`, `docs/30` §4.5 "As built"), linked from the `/proposals` and `/opportunities`
   page headers and listed in the sitemap. It relays to `POST /v1/intake/proposals` through `web/api_client.py`,
   sends the `website` honeypot as typed, and renders the Turnstile widget only when `TURNSTILE_SITE_KEY` is set
   ("Report a problem" has read the same key since 2026-10-06), relaying its token as `captcha_token`.
   **Set both keys or neither.** The API requires a non-empty `captcha_token` even with verification off, so a
   page with no site key sends the fixed placeholder `no-widget`: with `TURNSTILE_SECRET_KEY` set and
   `TURNSTILE_SITE_KEY` unset, the API rejects that placeholder and every submission is refused (fail closed);
   with only the site key set, the widget runs and nothing verifies its token. No Content-Security-Policy is
   sent anywhere today (no header in `web/app.py`'s middleware, the Caddyfile or the templates), so none was
   changed. Whoever adds one must allow `https://challenges.cloudflare.com` in `script-src` and `frame-src`
   on `/submit` and on the record pages that carry the report form, and nothing wider
   (https://developers.cloudflare.com/turnstile/reference/content-security-policy/). Still open around intake:
   US-1001 AC3's confirmation email (nothing sends one, so the success page says so and shows the task
   reference instead); US-1002 AC3's private status link (the API's `status_url` is `/status/{task_id}`, which
   no web route serves, so the page does not print it); and US-1003's opportunity intake form. (The API's
   `privacy_notice_url` and the report form's hint pointed at `/legal/privacy`, which the site does not serve;
   both now link `/privacy`, 2026-10-10.)
2. **Organisation takedown works; it is not yet proven against Postgres.** Migration `0022` gave `organization`
   a `publish_state` (default `public`, so nothing visible changed on deploy), and the admin panel's
   organisation page now has the same publish-state form, reason and audit event as proposals. A taken-down
   organisation is `404` on every `/v1/organizations` route, gone from lists, search and the sitemap, and
   dropped (not name-withheld) from sponsor/issuer embeds, asset owner tables and the ownership tree
   (`docs/21` §3.5). What remains: the migration has been round-tripped on SQLite only; CI's Postgres
   `migrations` job is the first real `upgrade head`. `downgrade` refuses while any organisation is not
   `public`, so rolling back past `0022` needs those organisations republished first (§7).
3. **`opportunity.awarded` and `funding.*` posts never draft.** No award or funding-programme field exists in
   the store or any connector's event payload yet — this is a schema gap, not a worker bug ("Workers landed").
4. **Playwright/axe run once, against a local stack, not a real environment** (2026-09-26, `docs/60` §11
   item 9). Against the containerised `web` on :8001: the E-10 smoke passed 19/19 checks (map → list → detail
   with attribution at 1440 and 400 px; pricing from the nav at 400 px). `npx axe` (axe-core 4.13.0, the
   `a11y-and-performance` job's command) found 0 violations on `/`, `/proposals` and `/about`. The sandbox
   needed a chromedriver matched to its Chromium (141), and `--no-sandbox` because it runs as root. A
   Playwright-driven axe pass, with the map actually rendered and a detail, an asset and `/pricing` added,
   found 1 moderate violation. On `/assets/<slug>`, `landmark-unique` fires on `section[aria-label="Map"]`
   (`web/templates/asset_detail.html`), probably colliding with the region MapLibre adds. The CI job's step
   says "map, list, detail" but scans `/about`, not a detail page. Lighthouse was not run. Still open: all of
   it against staging or production.
5. **Privacy notice and deletion request route: built** (§4 rows 5–6). `/privacy` and the request form
   `/privacy/request` are served by `web/legal.py`; `POST /v1/privacy/requests` and the admin queue are in
   `services/api/privacy_routes.py`. Tests: `web/test_legal.py` 7 passed, `tests/test_api_privacy_requests.py` +
   `tests/test_web_privacy_request.py` 20 passed (2026-09-26). Since 2026-10-09 a member deletes their own
   account in-app (`/account/delete`, `DELETE /v1/me`; password required; staff roles refused), through the same
   erasure as an operator's deletion task (`services/api/account_erasure.py`). Remaining: a personal account's live
   subscription is cancelled by hand, because `BillingPort` has no cancel operation (the deletion task stays open
   naming it); a request about someone named in a record is still carried out by an operator.
6. **Alert unsubscribe: built** (§4 row 9). `GET|POST /v1/alerts/unsubscribe` is in
   `services/api/unsubscribe_routes.py`, and the `/unsubscribe` page in `web/legal.py`.
   `tests/test_api_unsubscribe.py` 9 passed (2026-09-26). Not yet proven with a delivered email.
7. **`services/modelgw` does not exist.** The `worker-model` pool has no code to run against
   `MODEL_PROVIDER_API_KEY` (`docs/60` §11 item 5).
8. **No R2 lifecycle rule.** Backups accumulate unbounded in object storage until one is added
   (`docs/60` §11 item 8).
9. **Preview-per-PR is stubbed, not wired** (`docs/60` §11 item 6) — not a launch blocker, a development-loop
   gap.
10. **Four ISO queues (PJM, MISO, SPP, ISO-NE) show nothing** until the legal items in §1/§2.1 clear.
11. **`requirements.txt` now lists the Postgres driver** (`psycopg[binary]>=3.1,<4`, added 2026-09-26; resolves
    to psycopg 3.3.6 and passes `pip-audit` with CI's ignore list). The Dockerfiles' duplicate install went on
    2026-09-27; the rebuilt images carry psycopg 3.3.6 (`docs/60` §11 item 4). The unit suites still run on
    SQLite (`services/README.md`); the driver is exercised by the Compose stack and the Postgres-gated tests.
12. **The job-queue schema is applied by the deploy** (closed 2026-09-27, `docs/60` §11 item 10). `deploy.sh`
    runs `infra/scheduler/queue_schema.py ensure` after Alembic and before any worker starts. The step
    installs, upgrades or does nothing, and records the Procrastinate version in the database.
    `/v1/health`'s `checks.queue` reports whether the schema exists, and the deploy will not start workers
    unless it says `true`. On a local stack, all four worker processes ran with 0 restarts. Not yet run
    against the managed Postgres provider. The schema needs only PL/pgSQL, which every PostgreSQL database
    has by default, but that is unverified until §3 step 2 runs.
13. **Connector output: a named volume on one host, object storage across hosts** (`docs/60` §11 items 9–10). On a single host, `worker` and
    `browser-worker` write to the named volume `connector_data`, mounted at `INFRAQUE_DATA_DIR`
    (`/var/lib/infraque/data`, owned by the container user). Every replica on that host shares it, so a fetch
    and its load can run on different replicas. Measured 2026-09-27: a fixture-input run in one worker, and
    its load, resolve and enrich in the other, all succeeded. The scheduled loop can refresh data on a
    one-host staging. It cannot on the production topology (`docs/60` §2), where fetch and load can land on
    different worker VMs that share no filesystem. For that topology use the object-storage mode below
    (`docs/20` §2: stages talk only through the database and object storage).
    **Object-storage mode (added 2026-09-26, merged 2026-09-27):** `SNAPSHOT_STORE=s3` routes every connector-store read and write to
    the environment's R2 bucket (`docs/60` §5 lists the variables; the R2 ones are those `backup.sh` already
    reads). That closes the cross-host half: the load reads what the fetch wrote without sharing its host. Runs
    fail closed if the bucket is unreachable. It passed its store contract against a local S3-compatible server
    (`docs/60` §11 item 9) but has not touched a real R2 bucket. For staging, set it with the other R2 values in
    `secrets.staging.enc.yaml`. Before trusting the scheduled loop, run one source by hand in a `worker` container
    (`python -m pipeline.connectors run us.eia.860m`) and check that its `runs/` object appears under
    `data/` in the bucket.

---

## 7. Rollback

Per `docs/60-deployment.md` §10.2.

**Trigger:** a deploy is bad (5xx spike, failing health check, a source silently breaking that correlates with
the deploy time). **Severity:** S2 by default; S1 if gated/restricted data is now visible — unpublish first
(the S-9 runbook), before rolling back the deploy itself.

1. `infra/scripts/rollback.sh <staging|production> <previous-image-tag>` — re-runs the deploy script against
   the older tag (every `sha-` tag stays in GHCR; the last deployed one is in `/opt/infraque/current-tag` on
   the app VM). `deploy.sh` already does this on its own when the post-deploy health check fails.
2. Only pass `--downgrade-migration` if the migration being rolled back from documents itself as reversible
   (E-11); otherwise the old image runs against the new-but-compatible schema (expand/contract).
3. If entity tables need repair after a bad merge/write during the bad window, run the `docs/21` §6.5 `replay`
   procedure next.

**Verify:** same checks as the deploy runbook (§3 step 7), against the restored tag.
**Escalation:** owner immediately if a rollback does not clear the symptom within 15 minutes.
**Last executed:** not yet — no `staging` environment exists yet to rehearse against (`docs/60` §10.2).

---

## 8. Assumptions

| Id | Assumption | Depends on | Effect if wrong |
|---|---|---|---|
| L-1 | The `/about` page's coverage sentence has not drifted from `docs/13` §6's wording since 2026-09-12 | A frontend-developer re-check before launch | Update the live copy to match §1's sentence before publishing |
| L-2 | No E-9-named integration-fixture test file exists as a single target this sprint (row 3, §4) | A search limited to this task's read scope; a broader test file may cover it under a different name | Update the "how to prove it" command once the actual file is confirmed |
| L-3 | A staff member (`operator`, `legal`, `owner`) never needs to delete their own account in-app: an owner demotes them first or deletes them from the admin panel (§4 row 6, 2026-10-09). Refusing it prevents the last owner locking every operator out of `/admin` | The owner agreeing; `services/api/auth_routes.py::STAFF_ROLES` | Drop the role check in `delete_me`, keeping a guard that at least one owner remains |
| L-4 | Alert-log rows (`alert`) are kept after a deletion with the recipient cleared, and saved searches are paused rather than deleted, because docs/21 §3.16 keeps the alert log 12 months for metric M-5 (§4 row 6) | docs/21 §3.16 retention | `services/api/account_erasure.py` deletes both instead (alerts first; they reference the searches) |
