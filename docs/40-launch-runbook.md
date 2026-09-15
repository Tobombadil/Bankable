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
| Sentry project | `SENTRY_DSN` | Owner | A test exception appears in Sentry |
| Grafana Cloud | `GRAFANA_CLOUD_API_KEY` | Owner | A dashboard shows real metrics |
| Model gateway provider key | `MODEL_PROVIDER_API_KEY` | Owner | **Blocked**: `services/modelgw` does not exist yet (`docs/60` §11 item 5) — this key has nothing to authenticate to until that component is built; do not treat "key obtained" as "done" here |
| Free API keys: EIA v2, SAM.gov, NRC, regulations.gov | per-connector, not yet named in an env file | Owner | Each connector's live run returns 200, not 401 |

**Note on where secrets actually live:** `infra/compose/.env.example` (`ENVIRONMENT`, `DOMAIN`, `LOG_LEVEL`,
`POSTGRES_PASSWORD`, `DATABASE_URL`, `R2_*`, `SENTRY_DSN`, `GRAFANA_CLOUD_API_KEY`, `MODEL_PROVIDER_API_KEY`)
is the local-Compose template only, explicitly git-ignored when filled in. None of the CRM, billing, email or
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
   `requirements.txt` has no Postgres driver at the root lockfile level — `infra/docker/Dockerfile` and
   `Dockerfile.browser-worker` install `psycopg[binary]` as an image-layer stopgap (`docs/60` §11 item 4).
   This is a known gap outside this runbook's write scope; confirm the image build still installs it before
   relying on this step.
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
   repo.
4. **Object storage.** Create the R2 bucket; there is no lifecycle rule pruning it yet (`docs/60` §11 item 8) —
   accept unbounded growth at launch or add one via `infra/terraform/storage.tf` first.
5. **Observability accounts.** Create the Sentry project and the Grafana Cloud account; their keys are wiring
   points already named in `infra/compose/.env.example` and now also belong in the secrets file from step 3.
6. **Verify secrets decrypt** before the first deploy: `sops -d infra/sops/secrets.<env>.enc.yaml` with only
   the intended age key present.
7. **Deploy** (`docs/60` §10.1), secrets and infrastructure before services, workers stopped before
   migrations, migrations before app restart:
   ```
   export APP_HOST=... WORKER_HOSTS="..." BROWSER_WORKER_HOST=... SOPS_AGE_KEY="$(cat path/to/key)"
   infra/scripts/deploy.sh <staging|production> <image-tag>
   ```
   The script: stops `worker`/`browser-worker`/`scheduler` on the worker VMs → runs migrations once
   (`alembic upgrade head`, expand phase only) → restarts `caddy`/`api`/`web` on the app VM (public pages
   keep serving from the Cloudflare edge cache throughout) → restarts the workers → restarts the scheduler
   last. It appends a row to `infra/deploy-log.md`.
   **Verify:** `curl https://infraque.com/health` and `curl https://infraque.com/v1/health` return 200; the E-10
   Playwright smoke suite passes (not yet run against any real environment — flagged in §6 below); the new
   `infra/deploy-log.md` row looks right.
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
| 4 | Attribution renders on all surfaces | Manual check: web page footer, RSS item, API envelope `licence_summary`, exported CSV row all carry `source_id`/`source_url`/`retrieved_at`/`licence` | Attribution renders automatically per `CLAUDE.md`; not independently re-verified this sprint | |
| 5 | Privacy notice live | A `/privacy` page exists on the public site | Built 2026-09-13: `web/legal.py` serves `/privacy` (footer link on every public page); `web/test_legal.py` proves it renders | **NOT MET** |
| 6 | Deletion route live | A self-service or admin-triggered path that redacts personal data and requests CRM deletion | Admin-triggered deletion exists and is tested (`pytest tests/test_api_admin_people.py -k deletion_task -q`; also `services/crm/test_attio.py::test_request_personal_data_deletion_opens_a_task_with_a_30_day_deadline`) — this is an **admin** action (US-901 AC1's "delete" user action), not a self-service route a member can use on their own account | Partially met — flag the gap explicitly at sign-off |
| 7 | Automated-account labels set | Bios/disclosure text from `docs/32` §2.2 live on each channel profile; `services/social/editorial.py` enforces the disclosure label on post templates (`pytest -k disclosure -q` covers `test_admin_update_post_rejects_removing_disclosure_label` in `tests/test_api_admin_posts.py`) | Code enforces it; the actual profile bios depend on §2's account checklist being done first | |
| 8 | Rate limits active | `pytest tests/test_api_pro_ratelimit.py -q`; confirm `TIER_LIMITS` in `services/api/ratelimit.py` matches `docs/23` §6 | Implemented (in-memory token bucket); `test_tier_limits_match_docs_23_defaults` is the exact check | |
| 9 | Alert unsubscribe works | Click the unsubscribe link in a delivered alert email and confirm no further alerts send | Built 2026-09-13: `POST|GET /v1/alerts/unsubscribe` and `/unsubscribe`; `tests/test_api_unsubscribe.py` proves the next alert cycle sends nothing for that search | **NOT MET** |
| 10 | Backups verified | `infra/scripts/restore_drill.sh`, then row-count sanity checks against `proposal`/`opportunity`/`organization`/`event` | Script exists; never run against a real environment (`docs/60` §10.3 "Last executed: not yet") | |
| 11 | M-11 = 0 in the nightly audit | Whatever job computes `docs/04` metric M-11 | Not located in this sprint's read scope; confirm the job exists and runs before sign-off | |
| 12 | ADRs match the running system | Manual review of `docs/adr/000*.md` against what is actually deployed | Not checked this sprint | |
| 13 | No expired exception (`docs/04` §9.4) | `docs/04-standards.md` §9.4 register | Empty register today — nothing to expire | |

**Two items are hard blockers, not soft findings:** row 5 (no privacy notice page) and row 9 (no working
unsubscribe route) are both named explicitly in US-908 AC1 and both fail on inspection of the current code.
Release should not be signed off against US-908 with these unresolved; they are also listed in §6.

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

1. **Intake captcha is accepted but not verified.** A honeypot field and a per-IP 5/hour bucket stand in
   ("Admin backend landed"). A determined submitter can automate intake past both.
2. **Organisation takedown is a 400.** `organization` has no `publish_state` column; a migration is needed
   before an organisation-level takedown can be actioned through the admin panel ("Admin backend landed").
3. **`opportunity.awarded` and `funding.*` posts never draft.** No award or funding-programme field exists in
   the store or any connector's event payload yet — this is a schema gap, not a worker bug ("Workers landed").
4. **Playwright/axe checks have not been run.** Neither the E-10 end-to-end smoke suite nor the 400px/axe
   accessibility checks (`docs/04` D-30–D-32) have executed against a real preview or production environment
   this sprint ("Admin panel UI landed": "Not verified here").
5. **No self-service privacy notice or deletion route** for an end user (§4 rows 5–6). Deletion exists only as
   an admin action.
6. **No alert unsubscribe endpoint** (§4 row 9). The token exists in the database; nothing consumes it.
7. **`services/modelgw` does not exist.** The `worker-model` pool has no code to run against
   `MODEL_PROVIDER_API_KEY` (`docs/60` §11 item 5).
8. **No R2 lifecycle rule.** Backups accumulate unbounded in object storage until one is added
   (`docs/60` §11 item 8).
9. **Preview-per-PR is stubbed, not wired** (`docs/60` §11 item 6) — not a launch blocker, a development-loop
   gap.
10. **Four ISO queues (PJM, MISO, SPP, ISO-NE) show nothing** until the legal items in §1/§2.1 clear.
11. **`requirements.txt` has no Postgres driver at the root lockfile**; the Docker image installs it as a
    stopgap (§3 step 2).

---

## 7. Rollback

Per `docs/60-deployment.md` §10.2.

**Trigger:** a deploy is bad (5xx spike, failing health check, a source silently breaking that correlates with
the deploy time). **Severity:** S2 by default; S1 if gated/restricted data is now visible — unpublish first
(the S-9 runbook), before rolling back the deploy itself.

1. `infra/scripts/rollback.sh <staging|production> <previous-image-tag>` — re-runs the deploy script against
   the older tag (kept ≥ 5 back).
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
