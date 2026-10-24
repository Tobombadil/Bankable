# One-server private beta

Written 2026-10-09. How to run the whole platform for invited testers on one VM, behind one shared
login, and what was measured when it was rehearsed. The three-VM design in `docs/60` is unchanged
and stays the path once the beta outgrows one host (`deploy.sh` without `SINGLE_HOST`).

## 1. What runs, and what is different from `docs/60`

| | Three-VM production (`docs/60` §2) | One-server beta |
|---|---|---|
| Hosts | app, worker, browser-worker VMs | one VM (`single_host = true` in `infra/terraform`) |
| Database | managed Postgres (ADR 0003) | the base Compose file's PostGIS container, on a named volume |
| Compose files | base + `compose.prod.yml` | base + `compose.prod.yml` + `compose.single.yml` |
| Replicas | api ×2, worker ×2, browser-worker ×1 | api ×1, worker ×1, no browser-worker |
| Connector data | `SNAPSHOT_STORE=s3` (R2) | `local` on the `connector_data` volume, or `s3` |
| Access | open | `SITE_ACCESS=basic`: one shared login on every host |
| Backups | nightly `pg_dump` to R2 | the same; Hetzner's daily VM backups off in the Actions deploy (owner, 2026-10-19; `vm_backups`) |

`ENVIRONMENT` is `production`: every fail-closed configuration check applies (session secret,
platform posture, internal token). The beta runs on the production host names, so opening it to
the public is one setting (§6), not a migration.

**The gate** (`infra/compose/Caddyfile` `gate_basic`). Every request to the site, admin and API
hosts needs the shared login, using the browser's own password prompt, except `/webhooks/*` and the
two health checks. Stripe and Attio sign their webhooks and cannot answer a prompt. Gated responses
carry `Cache-Control: private, no-store` and `X-Robots-Tag: noindex, nofollow`, so neither an edge
cache nor a search engine keeps a copy. The login never reaches the API (Caddy strips the
`Authorization` header), so it cannot be read as an API credential. The cost: API keys cannot be
used on the API host during the beta, because a key also travels in `Authorization`; testers use
the site.

**Admin is on `admin.{DOMAIN}` only** (2026-10-10). The site and API hosts answer 404 for `/admin`,
`/admin/*` and `/admin/v1/*`, after the login. The operator's URL is `https://admin.{DOMAIN}/admin`, as
before; `https://{DOMAIN}/admin` no longer works. Every host also sends HSTS, `nosniff`, a referrer
policy and a content security policy, report-only by default (`CSP_MODE`, §5; `docs/60` §2). `/csp-report`, where
browsers send violation reports, is outside the login like the health checks.

**Sizing.** The Compose limits sum to 6.25 GB (Postgres 1, api 1, web 0.75, scheduler 0.25,
worker 3, Caddy 0.25; the worker was 1 GB until 2026-10-23, raised for the site finder's Texas run, which
peaks at 1.2 GB inside one job, `docs/25` §7). The measured api peak is 606–638 MB (`docs/60` §2). That fits the `cx32`
(4 vCPU, 8 GB) that `infra/terraform` already defaults to.

**Cost** (inferred from `docs/60` §4's table, prices retrieved 2026-09-13, not re-verified): one `cx32`
at 7.69 USD plus 20 % for Hetzner backups, R2 at 3–6 USD, no managed Postgres (35–55 USD saved).
**Superseded 2026-10-19:** cx32 is no longer sold, and Hetzner's API priced the cheapest qualifying type in
`ash`, cpx31, at 73.49 EUR a month gross. With VM backups off, the beta costs about 75 EUR a month plus R2
(decisions log, 2026-10-19). Hetzner bills by the hour up to that cap.
That is about 15–35 USD a month before email, against 80–170 USD for the three-VM core.

## 2. Owner actions, in order

### One-click route (GitHub Actions)

Added 2026-10-15. The owner creates three accounts and pastes their values into GitHub once. The coordinator
then runs `.github/workflows/deploy-beta.yml` on GitHub's own machines, which can reach the server over SSH.
Nothing is installed locally: no OpenTofu, sops, age, Caddy or SSH key. Allow about 40 minutes of work, plus up
to a day if the domain's nameservers have to move (step A).

Email (Resend), Sentry and UptimeRobot are not part of this first version. Email is a dry run, no error reaches
anyone, and preflight check 9 reads FAIL until manual steps 7 to 9 below are done. Secret values go only into
GitHub's secret fields: never into chat, an email or the repository.

- [ ] **A. The domain on Cloudflare (10 min).**
  - Register `<your domain>` in the Cloudflare dashboard under **Domain Registration > Register Domains**. Its
    zone then exists on Cloudflare already.
  - **If Cloudflare does not sell that ending** (for example `.io`), register it elsewhere. Then, in Cloudflare,
    choose **Add a domain**, enter `<your domain>`, pick the **Free** plan, and at your registrar replace the
    nameservers with the two Cloudflare shows. Continue once the domain's Overview page says **Active**.
  - The domain's **Overview** page shows, in its **API** box, the **Zone ID** and the **Account ID**.
- [ ] **B. R2 storage and the state bucket (5 min).**
  - Open **R2 Object Storage** in the left menu and enable it. Cloudflare asks for a payment method; the beta
    costs about 3 to 6 USD a month there (§1).
  - **Create bucket**, named exactly `beta-tfstate`, location Automatic. It holds OpenTofu's state, which
    includes the generated passwords and the server's SSH key: never make it public.
- [ ] **C. The R2 storage token (3 min).** On the R2 Object Storage overview page, **Manage API tokens >
  Create Account API token**: permission **Object Read & Write**, **Apply to all buckets in this account**, no
  expiry. Copy the **Access Key ID** and the **Secret Access Key**; the secret is shown once.
- [ ] **D. The Cloudflare API token (3 min).** **My Profile > API Tokens > Create Token > Create Custom Token**:
  - three permission rows: **Zone · DNS · Edit**, **Zone · Zone · Read**, **Account · Workers R2 Storage ·
    Edit**;
  - Zone Resources **Include · Specific zone · `<your domain>`**; Account Resources **Include · your account**.
- [ ] **E. Hetzner Cloud (10 min).** Sign up at https://console.hetzner.com and verify the account. Create a
  project (any name). In it, **Security > API tokens > Generate API token**, permission **Read & Write**. Create
  no server and no SSH key: the workflow makes both.
- [ ] **F. The GitHub Environment (10 min).** In the repository, **Settings > Environments > `production`**
  (create it if it is not there). Add each value under **Environment secrets** or **Environment variables**,
  named exactly as here:

  | Name | Kind | Value |
  |---|---|---|
  | `HCLOUD_TOKEN` | secret | step E |
  | `CLOUDFLARE_API_TOKEN` | secret | step D |
  | `R2_ACCESS_KEY_ID` | secret | step C, Access Key ID |
  | `R2_SECRET_ACCESS_KEY` | secret | step C, Secret Access Key |
  | `SITE_ACCESS_PASSWORD` | secret | the testers' shared password, 12 characters or more and at most 72 bytes. The workflow hashes it. Give it to testers out of band |
  | `DOMAIN` | variable | `<your domain>`: lowercase, without `https://`. It must equal `services.api.common.DOMAIN` in the code being deployed, or the run stops (preflight check 10's rule) |
  | `CLOUDFLARE_ACCOUNT_ID` | variable | step A, Account ID |
  | `CLOUDFLARE_ZONE_ID` | variable | step A, Zone ID |
  | `SITE_ACCESS_USER` | variable | the shared user name: letters, digits, `.`, `_`, `@` or `-` |
  | `SITE_ACCESS` | variable, optional | `basic` (the default: the shared login) or `open` (public, no login; §6) |
  | `MY_PIPELINE` | variable, optional | `off` (the default: `/my` and `/v1/my` answer 404) or `on` (My pipeline, the signed-in user's private Tracker, `docs/19` §11). Takes effect at the next `deploy` |
  | `GRID_LAYERS_REGIONS` | variable, optional | the states whose site-finder inputs the server pulls each month (the OpenStreetMap lines and substations, the FEMA flood files, the prospect build; `docs/25` §2.7 limit 3): ISO 3166-2 codes, comma separated, such as `US-TX` or `US-TX,US-OK`, or `all`. Unset: `US-TX`, the beta's value. `all` passes with a warning: the national line index needs about 1.3 GB more memory than this server has. A variable, not a secret. The inputs check refuses anything else (`TX` alone, a state name). Takes effect at the next `deploy`, then the next context build (§3 `seed`, or the 4th of the month) |
  | `PARCELS_COUNTIES` | variable, optional | the Texas counties whose land parcels the server pulls each month from the private bucket for the site finder (`docs/25-siting-parcels.md` §8): `all`, or county FIPS codes, comma separated, such as `48351,48241`. Unset: `all`, every county `parcels-layer.yml` has published (until its first build the pull reports "not published" and nothing fails). A variable, not a secret. The inputs check refuses anything else (`351` alone, a county name). Takes effect at the next `deploy`, then the next context build (§3 `seed`, or the 4th of the month) |
  | `SENDER_POSTAL_ADDRESS` | variable | the legal sender's real postal address, on one line (manual step 9 says why) |
  | `SENDER_LEGAL_NAME` | variable, optional | default: Compass International Trading Group, LLC (decided 2026-09-18) |
  | `PRODUCT_CONTACT_EMAIL` | variable, optional | default: `hello@<your domain>` |
  | `PRODUCT_NAME` | variable, optional | default: the product name in the code |
  | `ERCOT_API_SUBSCRIPTION_KEY` | secret, optional | the ERCOT Public API subscription key, once a person has registered the account at apiexplorer.ercot.com (ERCOT bars accounts "registered by bots"). All three ERCOT values or none; without them ERCOT prices use the public downloads (docs/29 §2.1) |
  | `ERCOT_API_USERNAME` | secret, optional | that account's user name |
  | `ERCOT_API_PASSWORD` | secret, optional | that account's password |
- [ ] **G. Tell the coordinator.** The coordinator runs the workflow with `deploy`, then `seed`, then
  `preflight`, checking with `status` in between. Each run's summary page says what it did. A run with a missing
  or malformed value stops before it touches any account and lists every value to fix.

**What the first `deploy` creates.** It creates:
- one Hetzner VM in Ashburn, the cheapest type offered there with 4 vCPU and 8 GB, with Hetzner's backups;
- two R2 buckets: `beta-object-storage-production` for the nightly database dumps, and
  `beta-object-storage-tiles-production` for the map;
- the `A` and `CNAME` records for `<your domain>`, `admin.`, `api.` and `www.`, not proxied for now (so
  `web_source_cidrs` stays open), and `tiles.<your domain>` on the tiles bucket;
- the database password, the session secret, the internal token, the audit pepper and the server's SSH key,
  generated once and kept in the `beta-tfstate` state.

Later runs reuse all of it, and a plan that would delete or replace any of it is refused. A separate job of the
first run builds the map's basemap (about 3.9 GB); the site works while it builds.

**The trade-off.** The generated secrets live in the state, in the private bucket `beta-tfstate`, which the R2
token above can read. Whoever holds that token, or may run the workflow, can read them. That is the same set of
people who can already read the deployed env file through the workflow.

### Manual route (the alternative)

The coordinator's recommendations, which the owner accepted on 2026-10-10. Allow about 75 minutes of work, plus
waiting time for DNS: **start step 9 (email) first**, because its domain check can take up to 72 hours. Each step
names what to create and where its value goes:
- **secret** values go only into the encrypted secrets file (`infra/sops/README.md`), never into chat or the
  repository;
- **config** values may go in the file in the clear.

- [ ] **1. Hetzner Cloud (10 min).**
  - Create a project and an API token with read and write access. Set it as `HCLOUD_TOKEN` in the shell that runs
    `tofu`.
  - Put the operator's SSH public key in `infra/terraform/terraform.tfvars` as `ssh_public_key`.
- [ ] **2. Limit SSH to your address (2 min).**
  - On the machine you deploy from, run `curl -4 https://ifconfig.me`. It prints your public IPv4 address.
  - In `terraform.tfvars`, set `ssh_source_cidrs = ["<that address>/32"]`.
  - If `curl -6 https://ifconfig.me` also prints an address, add that address's /64 to the list as well.
  - Until you do this, keep the default: anyone may connect, with key authentication only.
  - The rule is applied through the Hetzner API, not over SSH. So if your address changes (home connections
    do), edit the value and run `tofu apply` again, from anywhere.
  - Do not set `web_source_cidrs = ["cloudflare"]` until the DNS records are proxied (`docs/60` §2).
- [ ] **3. Domain and DNS (5 min).** Create `A` records for the apex, `admin.`, `api.` and `www.`, pointing at the
  VM. With Cloudflare in front (`cloudflare_zone_id` set), the gated answers are `private, no-store`, so the edge
  caches none of them.
- [ ] **4. Cloudflare R2 for the backups (10 min).** Every deploy dumps the database there before migrating, so R2
  must work before the first deploy (§3).
  - **The bucket.** Let `tofu apply` create it: set `cloudflare_account_id` in `terraform.tfvars` and
    `CLOUDFLARE_API_TOKEN` (DNS edit and R2 edit) in the shell. It is then named
    `infraque-object-storage-production` (`tofu output r2_bucket_name`). Or create one bucket by hand in the
    Cloudflare dashboard under R2.
  - **The token.** Under R2, create an API token with object read and write permission, limited to that one
    bucket. The secret is shown once.
  - **The four values `infra/scripts/backup.sh` reads:**
    - `R2_ACCOUNT_ID` (config): the account id shown on the R2 page;
    - `R2_BUCKET` (config): the bucket name;
    - `R2_ACCESS_KEY_ID` (secret);
    - `R2_SECRET_ACCESS_KEY` (secret).

  It uploads to `https://<R2_ACCOUNT_ID>.r2.cloudflarestorage.com` under `postgres/`. The basemap tiles live in a
  second bucket (`MAP_TILE_URL`, `docs/40` §2.7), as before.
- [ ] **5. A GHCR read token (2 min)** while the packages are private: `GHCR_USER` and `GHCR_READ_TOKEN`, in the
  shell that runs `deploy.sh`.
- [ ] **6. The production age key and secrets file (8 min)** (`infra/sops/README.md`), from
  `infra/sops/secrets.example.plain.yaml`. Add the R2 values from step 4, `SENTRY_DSN` from step 8, the three email values
  from step 9, and these beta values:

   ```
   POSTGRES_PASSWORD=<openssl rand -hex 24>
   DATABASE_URL=postgresql+psycopg://infraque:<same password>@postgres:5432/infraque
   SNAPSHOT_STORE=local
   SITE_ACCESS=basic
   SITE_ACCESS_USER=<one shared user name>
   SITE_ACCESS_HASH=<caddy hash-password --plaintext '<password>' | base64 -w0>
   CSP_MODE=report
   ```

  - The hash is base64-encoded so that no `$` reaches Compose's interpolation.
  - `deploy.sh` refuses a raw bcrypt hash, a missing user, any `SITE_ACCESS` other than `open` or `basic`, and any
    `CSP_MODE` other than `report` or `enforce`. Caddy itself refuses to start on an empty or misspelt value of
    either.
  - `CSP_MODE=report` is also the default when the line is absent. §5 says when to switch.
- [ ] **7. Uptime alerts: UptimeRobot, free plan (10 min).** Nothing else turns an outage into an alert: Docker does
  not restart an unhealthy container (`docs/60` §7).
  - Sign up, and check that the alert contact is the owner's email.
  - Create two monitors, each with a 5-minute interval (the free plan's interval):

    | Monitor | Type | URL | Down when |
    |---|---|---|---|
    | API | **Keyword** | `https://api.{DOMAIN}/v1/health` | the keyword `"status":"ok"` is not present |
    | Site | HTTP(s) | `https://{DOMAIN}/health` | it does not answer 2xx |

  - **Why the API monitor is a keyword monitor.** The free plan's HTTP(s) monitors send `HEAD` requests and cannot
    be switched to `GET` (method selection is a paid feature:
    https://uptimerobot.com/blog/introducing-http-method-selection-headgetpostputpatchdelete). The API answers
    `HEAD /v1/health` with 405. Keyword monitors send `GET`. When the database is down, the API answers 503 with
    `"status":"degraded"`, which the keyword check catches. The site's `/health` answers `HEAD`.
  - **No login needed.** The beta gate exempts `/health` and `/v1/health` on every host (Caddyfile `gate_basic`;
    proved through real Caddy by `infra/test_caddyfile.py`).
  - Once `web_source_cidrs = ["cloudflare"]` is set, the probes still work, because they use the public host names
    and reach the VM through Cloudflare.
- [ ] **8. Errors: Sentry, free Developer plan (10 min).**
  - Create one project for Python. All services report to it, each tagged with its service name.
  - Copy the project's DSN (in the project settings, under client keys) into the secrets file as `SENTRY_DSN`
    (secret).
  - Every process reads it at start (`infra/observability.py`). Without it, nothing is reported. The release is the
    image tag; personal data sending is off.
- [ ] **9. Email: Resend (15 min, then up to 72 hours of DNS; start this first).** Without it the beta cannot
  verify a sign-up, reset a password or send an alert. Nothing fails loudly: with no key, every email is a dry run
  that goes nowhere (`services/api/auth.py` `ResendEmailAdapter`).
  - **The domain is fixed in code.** Every email's links and From address are built from `inframation.io`
    (`services/api/common.py` `DOMAIN`: From `alerts@inframation.io`, reply-to `hello@inframation.io`). So `DOMAIN` in
    the secrets file must be `inframation.io`, and that is the domain to verify. The preflight (§3) fails if they
    differ.
  - **Verify the domain.** Create a Resend account, add `inframation.io` under Domains, and copy the DKIM and SPF
    records it generates (`TXT`, plus `MX` for the return path, which defaults to `send.inframation.io`) into
    Cloudflare exactly as shown, with the proxy **off** (grey cloud). Resend says most domains verify within 15
    minutes and DNS can take up to 72 hours (https://resend.com/docs/add-a-domain). Cloudflare specifics:
    https://resend.com/docs/knowledge-base/cloudflare.
  - **Add DMARC.** Resend does not require DMARC to verify the domain, but `docs/32` §2.3 asks for it before
    launch: a `TXT` record at `_dmarc` with `p=quarantine` at minimum
    (https://resend.com/docs/dashboard/domains/dmarc). Then send one test to a Gmail, an Outlook and an Apple
    Mail address, and check that none lands in spam.
  - **Receive replies.** Alerts set reply-to `hello@` and a `mailto:unsubscribe@` unsubscribe header
    (`services/alerts/mail.py`). Forward both addresses to the owner's inbox, for example with Cloudflare Email
    Routing on the apex (free). The one-click unsubscribe works over HTTPS without it; replies and mailed
    unsubscribes bounce without it.
  - **The three values:**
    - `RESEND_API_KEY` (secret): an API key with sending access only, limited to `inframation.io` (`docs/32` §2.4);
    - `SENDER_LEGAL_NAME` (config): the legal sender, decided 2026-09-18 as Compass International Trading Group
      LLC (`docs/00-PLAN.md` open question 3);
    - `SENDER_POSTAL_ADDRESS` (config): a real postal address where that entity receives mail. A street address,
      a USPS-registered PO box or a registered private mailbox all qualify
      (https://www.ftc.gov/business-guidance/resources/can-spam-act-compliance-guide-business).
  - **Why the address matters.** `deploy.sh` only checks that the two sender values are present, so the template's
    `Replace Me LLC` would pass it. Production refuses to send an alert when either value is missing
    (`services/alerts/mail.py` `SenderIdentityMissing`), and the preflight (§3) fails on the placeholders. The same
    two values name the controller on the privacy notice (`web/legal.py`).
  - **Since 2026-10-11, a server without the key does not show the link instead.** Before then, a dry-run
    sign-up or reset answered with the link the email would have carried. That is convenient on a laptop, and on a
    deployed server it would hand anyone a reset link for any account. Now only development environments do it
    (`services/api/auth_routes.py` `_dev_links`).
- [ ] **10. After the first deploy (5 min).**
  - Check that both monitors read "up".
  - Check that a report reaches the CSP endpoint from Chrome (§5).
  - Sign up with a personal address, and confirm that the verification email arrives and its link works.
  - Request a password reset for that account, and confirm that the email arrives.
  - Preflight checks 9 and 10 read PASS (§3).

## 3. Deploy and seed

```bash
cd infra/terraform && tofu apply -var single_host=true       # one VM; Hetzner backups on unless -var vm_backups=false
SINGLE_HOST=1 APP_HOST=<ip> SOPS_AGE_KEY="$(cat <key>)" \
  GHCR_USER=<user> GHCR_READ_TOKEN=<token> \
  infra/scripts/deploy.sh production sha-<commit>             # docs/60 §10.1, one host
APP_HOST=<ip> infra/scripts/seed_single_host.sh data/          # from the operator's checkout
```

**`deploy.sh` with `SINGLE_HOST=1`** runs the usual order on the one host. Its only additions:
- before the migrations, it starts the database and waits until it is healthy;
- it starts no browser-worker.

Like every deploy since 2026-10-10, it then dumps the database before migrating. It runs
`infraque-backup.service`, which is `backup.sh`: `pg_dump` over loopback, then the upload to R2. If the
dump or the upload fails, it does not migrate, restarts the stopped worker and scheduler, and exits 1.
R2 must therefore work before the first deploy. `SKIP_PRE_MIGRATION_DUMP=1` skips the dump on purpose.

It refuses before touching the host in five cases:
- `WORKER_HOSTS` is also set;
- `POSTGRES_PASSWORD` is empty;
- `DATABASE_URL` does not name the in-stack database with that password;
- `SNAPSHOT_STORE` is neither `local` nor `s3`;
- the gate's settings are incomplete.

**`seed_single_host.sh`** starts the server from the operator's local data root, the one
`docs/61` §4 builds. Without the seed, the scheduler would fill the store only as each source's
bucket next ticked: up to a month for EIA-860M. It runs six steps:
1. Ship `runs/`, `snapshots/`, `normalized/` (including `normalized/context/`) and `held/` into the
   volume.
2. Queue one load per source (`python -m infra.scheduler.bootstrap load`).
3. Wait until every load, resolve, enrich and match job has finished. Each load's chain ends in
   `match_tick`, so matches need no step of their own.
4. Restate every opportunity's value and approximate USD equivalent at the latest ECB rates
   (`python -m services.ingest.opportunity_value --refresh-rates`, the daily `value_tick` run now;
   `docs/63` §4.1). A load converts only the rows it writes, and incremental sources never reload
   the shipped rows, so without this `sort=-value` would rank only notices loaded later.
5. Queue the context load (`bootstrap context`: the monthly tick's `context_load`, §5) and wait for
   it. It loads the shipped asset layers, the EIA-860M retirements onto the plants, owner shares
   and features. It runs after the loads rather than beside them, because both write organisations.
6. Queue one fetch per source (`bootstrap fetch`). This gives every source its `source_run` row
   now rather than at its next tick.

Every step runs in the worker image. A server seeded with no local data can run `bootstrap fetch`
alone, then `bootstrap context --build` once the EIA-860M, EIA-860 and GHGRP fetches have stored
their snapshots: the build reads them for the plant, owner and GHGRP layers.

**After seeding, measure on the beta store** (owner decision 2026-10-11; these figures could not be
measured in the sandbox):
1. Records no register lists any more, active states only:
   `GET /v1/proposals?listed=false&include=count&limit=1&lifecycle_state=<the active states>`.
2. What the map's hidden-records count costs. Take the median server time of 15 requests each to `/`
   (hides and counts) and to `/?unlisted=include` (neither). If the difference is above **150 ms**,
   replace the number with a line without one (lane U's alternative, docs/30 §5.1) and record it.
3. Generate the 100-item hand-check worksheet from this store
   (`data/eval/handcheck/2026-10-10-e2e-reference/README.md` gives the command) before the owner's
   check (planned for 14–15 October; it now follows the first deploy).

**Run the preflight; invite testers only on GO** (2026-10-11, `infra/scheduler/preflight.py`). Once the
seed's jobs have finished, on the host:

```bash
cd /opt/infraque/compose && IMAGE_TAG=$(cat /opt/infraque/current-tag) INFRAQUE_ENV_FILE=/opt/infraque/secrets/.env \
  docker compose -f docker-compose.yml -f compose.prod.yml -f compose.single.yml --env-file /opt/infraque/secrets/.env \
  run --rm --no-deps -T worker python -m infra.scheduler.bootstrap preflight
```

It prints one line per check, PASS, WARN or FAIL, with the figure it measured and what to do. Then it prints
GO or NO-GO, and exits 1 on NO-GO. A WARN does not block, but read each one. The twelve checks:

1. the database is at the migration head;
2. every scheduled source has succeeded and none is stale;
3. no data-quality hold is waiting for release;
4. live and public counts are sane;
5. the M-11 visibility audit is clean;
6. sites are built, none oversize;
7. records no register lists any more (item 1 above);
8. the map's hidden-count cost, timed from inside the stack against `web:8001` (item 2 above);
9. email can be sent: key set, sender values not the placeholders;
10. the `DOMAIN` setting matches the code's;
11. the gate, CSP mode and Sentry;
12. the hand-check worksheet (item 3 above), printing the generate command when it is missing.

Record the figures from checks 7 and 8 here.

- **What it writes.** It is read-only apart from its own record, `<data root>/preflight/<ts>.json`, which
  the next run compares counts against. `--json` prints that record.
- **Time.** It takes at most about two minutes (`--budget`).
- **Seeing the worksheet.** Once the worksheet is generated, add `-v /opt/infraque/handcheck:/out:ro`
  before `worker`, so check 12 can see it.
- **Not yet run anywhere real.** It has run only on SQLite in the sandbox. Its Postgres-only paths (the
  read-only transaction, the statement timeout, the last-resolve query) and the `web:8001` timing are
  first exercised on the beta host.

**Testers.** Give them the one login out of band. Rotate it by changing `SITE_ACCESS_HASH` and
redeploying the same tag. The tester guide, the invitation draft, the debrief script and the owner's
weekly review are in [`docs/65`](65-beta-tester-kit.md). While `SITE_ACCESS=basic`, every page shows
"Tell us what you were trying to do", which files feedback into the admin review queue.

## 4. Rehearsal, 2026-10-09 (this sandbox, not a cloud VM)

**Scheduler on local data, host processes.**
- Setup: Postgres 16 + PostGIS 3.4.2 local, migrated to 0035, queue schema installed.
- Fetch: `pipeline.connectors run --all` fetched 18 sources in about 4 minutes, 110 MB. The ERCOT
  generation queue's document list answered non-JSON once; the next fetch succeeded.
- Scheduler and worker: `python -m infra.scheduler.app` with a worker on the scheduler queues.
  `bootstrap load` queued 14 loads (3 document sources have no generic load). All succeeded and
  chained into resolve and enrich. `bootstrap fetch` then recorded a `source_run` for all 18
  sources and reloaded the ones that had changed.
- Store: 11,281 proposals (10,720 public), 1,349 opportunities, all 17 loadable sources `ok`.
- Context: `--context-only` loaded 16,472 power plants (14,496 operating, 204 retiring,
  1,772 retired), 3,212 other assets and 1,993 matches in 3 minutes.

**The single host in containers.** `compose.single.yml` with images built from this branch, running
`deploy.sh`'s steps with its exact Compose commands (no SSH host here):

| Step | Result |
|---|---|
| Database up, healthy | 4 s |
| `alembic upgrade head` (0001 → 0035) | 3.3 s |
| Queue schema `ensure` | `install`, Procrastinate 3.9.0 |
| caddy/api/web healthy; in-network `/v1/health` | `"queue": true` |
| Seed: ship 181 MB | 8.7 s |
| Seed: 15 loads + resolve + enrich drained | 104 s |
| Seed: `--context-only` | 2 min 55 s; same counts as the host run |
| `/proposals` through Caddy with the login | 200 in 0.59 s; 6,085 active proposals; masthead "All 13 scheduled sources fetched on schedule" |
| `pg_dump` over the loopback URL `backup.sh` builds | 12.7 MB in 2.3 s |

Gate, through real Caddy:

| Request | Status |
|---|---|
| any page, no login or a wrong one | 401 |
| `/`, `/v1/proposals` with the login | 200, `private, no-store`, `noindex, nofollow` |
| `api.` `/v1/proposals`, no login | 401 |
| `api.` `/v1/health`, no login | 200 |
| `POST /webhooks/stripe`, no login | reaches the API, which refuses the unsigned body |
| `admin.` `/admin` with the login | 303 to sign-in |

**Three defects the rehearsal found, fixed on this branch.** All three are invisible on SQLite,
where every test runs, and the first is invisible in the containers:
1. **The documented scheduler command queued fetches no worker could import.**
   `python -m infra.scheduler.app` (the module docstring's command) registered the unnamed
   `run_connector` task as `__main__.run_connector`. Every fetch the ticks queued failed with
   `TaskNotFound`. The task is now named; the container path (`infra.entrypoint`) was not affected.
2. **Concurrent loads collided.** Two sources' loads run side by side, and on Postgres the second
   insert of one organisation fails. In the first container pass, 2 of 15 loads failed: NYISO on a
   deadlock, the Permitting Dashboard on `organization_slug_key` (`duke-energy-carolinas-llc`).
   With `load_source` at `retry=0`, a failed load waited for its source's next new run, a week for
   the weekly queues. Those two error classes are now retried twice, with 5 s and 25 s backoff.
3. **Every resolve pass on Postgres stopped at the first merge into a reloaded survivor.**
   `resolution_confidence` reads back from NUMERIC as `Decimal`, the merge event copies it into its
   JSON, and `json.dumps` refuses it. `JSONVariant` now stores a `Decimal` as a JSON number at any
   depth. `tests/test_postgres.py::test_a_second_merge_into_a_reloaded_survivor_writes_its_event`
   fails without the fix; CI runs it in the migrations job.

**Not verified here** (needs the accounts in §2):
- cloud-init on a real VM;
- Let's Encrypt issuance (Caddy used its internal CA for `localhost`);
- GHCR pulls, the `sops` decrypt and an upload to R2;
- a fetch from inside the containers, which have no egress in this sandbox (the host run did fetch).

Rehearse once against the real VM with the gate on before inviting anyone.

## 5. Day to day

- **Logs and health:** `docker compose ... logs -f worker scheduler`, `/v1/health`, and the admin
  source-health page. `/v1/health` needs no login.
  - It answers 503 when the database is down, so `docker compose ps` shows `api` unhealthy. Docker does
    not restart it: find out why with `docker compose logs postgres api`.
  - `checks.queue_age_seconds` is how long the oldest runnable job has waited. Minutes is normal while a
    load or resolve runs; hours means a stuck lock.
  - Stalled jobs are recovered every 10 minutes and logged at WARNING by
    `infra.scheduler.queue_maintenance` (`docs/60` §6.4).
- **Backups:** the host timer runs `infra/scripts/backup.sh` nightly, and every deploy runs it before
  migrating. It reaches the in-stack database on `127.0.0.1:5432`, which the base file publishes on
  loopback only. Restore with `docs/60` §10.3.
- **Stopping or redeploying** waits up to 5 minutes for the worker's running jobs
  (`stop_grace_period`). A job still running then is killed. Within about 20 minutes it is retried, or
  failed if it has run 3 times, and its lock is freed either way.
- **Rollback:** `SINGLE_HOST=1 infra/scripts/rollback.sh production <previous tag>`. To undo a reversible migration
  as well, add `--downgrade-migration`. It reads the previous image's migration head, stops the worker and the
  scheduler, dumps the database, and downgrades in the newer image. Add `--from <newer tag>` if an automatic
  rollback has already run (`docs/60` §10.2).
- **The content security policy (`CSP_MODE`).** The beta opens in `report` mode, so a violation is only reported:
  `docker compose ... logs web | grep csp_directive` lists one line per report, giving the directive, the blocked
  origin, the page's route and the mode.
  - **Once, right after the first deploy, check that Chrome's reports arrive.** Chrome sends them through the
    Reporting API and then ignores the older `report-uri`. That delivery could not be shown in the sandbox
    (`docs/60` §2).
    1. Open any page in Chrome, signed in through the gate.
    2. In the DevTools console, run
       `document.head.appendChild(Object.assign(document.createElement("script"), {src: "https://example.invalid/x.js"}))`.
    3. Within about two minutes, DevTools' Application panel should show a report with status "Success" under
       Reporting API, and the web log should have a `script-src-elem` line for `https://example.invalid`.
    4. If neither appears, delete `; report-to csp` from the policy line in `infra/compose/Caddyfile` and deploy
       again. Chrome then reports through `report-uri`, which the browser test proves.
  - **Switch to enforcing once both of these hold:**
    1. the check above has passed;
    2. there have been **7 consecutive beta days with no report** other than browser extensions
       (`csp_blocked` such as `chrome-extension:`) and the test violation.

    To switch, set `CSP_MODE=enforce` in the secrets file and redeploy the same tag: only Caddy's headers change.
  - **Reset the count whenever a deploy changes `web/templates`, `web/static` or `web/assets.py`.** For example,
    applying the style-attribute patch for `home_map.html` and `_org_pipeline.html`, which also removes
    `style-src-attr` from the policy.
  - **To go back**, set `CSP_MODE=report` and redeploy. Under `enforce`, reports keep arriving, marked `enforce`.
    Any report there is a page that broke for someone: open that page and fix it, or go back to `report`.
- **A store-wide pass that outlived its timeout** keeps the database's store-pass lock until it finishes. Passes
  queued in the meantime log `skipped: previous pass still running` and do nothing (`docs/60` §6.4). The next
  load or the 04:37 resolve tick runs the chain again. A skipped `context_load` needs
  `bootstrap context` by hand.
- **Matches** are recomputed after every resolve pass (`match_tick`, incremental), so at least daily
  after the 04:37 resolve tick.
- **Context layers** are rebuilt on the 3rd of each month at 02:43 UTC (`context_build`, then
  `context_load`; `docs/60` §6.2). To refresh now:
  `docker compose ... run --rm worker python -m infra.scheduler.bootstrap context --build`.
  The build log names each builder that failed. A failed builder keeps its previous file, and the
  load reloads it unchanged.

## 6. Opening it to the public

With the GitHub Actions deploy: set the Environment variable `SITE_ACCESS` to `open` and run `deploy`;
`basic` (or deleting the variable) and another `deploy` put the login back. By hand: set `SITE_ACCESS=open`
(or remove the three keys) and redeploy the same tag. The header's beta feedback
link and `/feedback` go with the gate (`web/beta.py`). Before that, the
launch checklist in `docs/40` applies unchanged, counsel items included. The owner opened the beta on
2026-10-20 with that checklist still open (decisions log). Moving to three VMs later
needs:
- `pg_dump` from the single host, restored into the managed database (`docs/60` §10.3);
- `SNAPSHOT_STORE=s3`;
- `deploy.sh` without `SINGLE_HOST`.

## 7. Known gaps

Closed on 2026-10-09:
- **Matches are refreshed.** The scheduler now runs the matcher (`match_tick`) at the end of every
  resolve chain.
- **Context layers are refreshed monthly** (§5).
- **Every builder writes under `INFRAQUE_DATA_DIR`.** Before this, every builder wrote its parquet
  under the checkout's `data/`, which is read-only in the image. The EIA-860, EIA-860M and GLEIF
  builders also read and stored snapshots there. Only `eia_plants --data-root` could be pointed
  elsewhere.

**Measured on the operator's local data root:** all thirteen scheduled builders, run one after
another as `context_build` runs them, took 410 s by hand.
- The slowest was LBNL's transmission lines: 199 s.
- The highest peak memory was LBNL's: 361 MB, against the worker's 1 GB limit. Each builder is its
  own process, so its memory is returned when it exits.

**Rehearsed on Postgres** (the §4 rehearsal store, with a worker running this change's code):
`bootstrap context --build` queued `context_build`.
- It ran 13 builders in 287 s; 12 succeeded and PHMSA failed (above). It then queued
  `context_load`.
- `context_load` took 190 s:
  - it reloaded the plants and the retirement run idempotently (0 inserted; 16,472 unchanged);
  - it added the 13,084 LBNL transmission lines and the GHGRP shares;
  - it reported only the GLEIF file missing.
- A deferred `enrich_tick` chained into `match_tick`, which ran incrementally in 3 s. It found
  nothing changed since the seed's match run (1,993 active matches).

Closed on 2026-10-10 (`docs/51` §2.4, §2.9; `docs/60` §2, §6.4, §7):
- **The health check tells the truth.** `/v1/health` is 503 with the database down, so `deploy.sh` and
  the Compose healthcheck fail. `queue_age_seconds` is measured. The constant fields are gone.
- **A stalled job no longer freezes the pipeline.** Jobs a dead worker left `doing` are retried every
  10 minutes, which frees their locks; old jobs are pruned daily. Workers get 300 s to finish on stop.
  The worker's database sessions have a 10-minute statement limit and a 5-minute lock-wait limit.
- **A dump precedes every migration.**
- **Security headers** on every host: HSTS, nosniff, referrer policy and `frame-ancestors 'none'`; a
  report-only CSP.

Closed later on 2026-10-10 (lane O2; `docs/60` §2, §6.4, §10.2):
- **The CSP can be enforced.** Reports reach `/csp-report` and are logged without URLs or addresses. `CSP_MODE`
  switches modes without editing the Caddyfile. In Chromium, the enforced policy blocks nothing on the public or
  admin pages. `'unsafe-inline'` is gone except `style-src-attr`, which two templates owned by other work still
  need. FastAPI's `/docs` and `/redoc` are off outside development.
- **A timed-out store-wide pass keeps the store** until its thread ends (an advisory lock held by the work).
- **`--downgrade-migration` works.** It downgrades in the newer image, to the previous image's head.
- **Admin is off the public hosts.**
- **The firewall sources are variables** (`ssh_source_cidrs`, `web_source_cidrs`).

Still open:
- **Nothing alerts a human until §2 steps 7 and 8 are done.** Until then, a failure is visible only to whoever
  looks.
- **The CSP reports by default.** It is enforced only after the §5 checks. Whether Chrome delivers reports
  through `report-to` is unverified until the §5 check runs on the real host. Two templates still carry `style`
  attributes (`home_map.html`, `partials/_org_pipeline.html`); their classes are in `styles.css` and the
  coordinator holds the patch.
- **`/docs/api` links the API's `/redoc`,** which now answers 404 in staging and production. The page should link
  `/openapi.json` alone, or the committed `api/openapi.yaml` once it is served (`docs/51` §4).
- **PHMSA pipeline features do not build here.** archive.org reset the connection on every attempt,
  by hand and in the scheduled build. On a server it is one failed builder in the build log, and
  the other features still load.
- **GLEIF parent companies are not rebuilt.** The 500 MB entity file is kept out of the monthly
  build (`infra/scheduler/jobs.py` `CONTEXT_BUILDERS`). A file a data root ships keeps loading.
  The operator's root ships none, so the beta has no GLEIF parents until someone runs
  `python -m pipeline.context.gleif`, then `bootstrap context`.
- **The context chain assumes one data root on one host.** The four builders that read stored
  snapshots read local files, and `context_load` reads the files the build wrote. Both hold on this
  single host. With `SNAPSHOT_STORE=s3`, or with workers on several VMs (`docs/60` §2), the build
  and the load need a shared data root first.
- **Snapshots grow, by design.** Each build stores what it fetched again, whether or not it changed: 72 MB
  per build, measured (EIA-923 36 MB, LBNL 26 MB, RFS 5.5 MB), about 0.9 GB a year. DA-10 keeps raw
  snapshots whole for 24 months and then as monthly samples (`docs/04` DA-10, `docs/20` §3.2), so this is
  expected growth. Since 2026-10-10 the daily `retention_tick` enforces that rule (`docs/60` §6.3);
  nothing is old enough for it before 2028-09. It caps what is fetched more often than monthly at one
  snapshot per source, artefact and month once 24 months old. A monthly build already stores one snapshot
  a month (two for EIA-923's two workbooks), so the job removes none of it, and this growth stays about
  0.9 GB a year. Storing unchanged bytes again also departs from `docs/20` §3.2 ("the run is recorded as
  `unchanged`"); stopping that is a change to the builders.
- **API keys do not work while the gate is on** (§1).
- **No bounce or complaint webhook from Resend** (found 2026-10-11). `docs/32` §2.4 asks for one. Until it exists,
  a bounced verification or alert email is visible only in the Resend dashboard, and a complaint does not
  suppress the address. At beta scale (8–12 testers) the owner can check the dashboard by hand.
- **The email domain is a code constant, not a setting** (found 2026-10-11). `services/api/common.py` `DOMAIN`
  builds every email's links and From address. Until 2026-10-11, `services/api/auth.py` also repeated
  `alerts@infraque.com` literally; it now uses the constant. A deployment on any other domain would send links to
  the wrong host. Preflight check 10 catches the mismatch; making it a setting is post-beta work.

## 8. Assumptions

- **[A-64-1]** The `cx32` type is offered in the chosen Hetzner location. `docs/60` §4 assumes it
  for `ash`; check it in the console before `tofu apply`. **Found 2026-10-15: it is not.** Hetzner stopped
  selling `cx32` on 1 January 2026 (https://docs.hetzner.cloud/changelog). The one-click route picks the type
  from Hetzner's API instead, the cheapest with 4 vCPU and 8 GB offered in `ash`, and keeps it. The manual route
  must set `app_server_type` in `terraform.tfvars`.
- **[A-64-2]** One shared login is enough access control for an invited beta. It identifies no
  tester; per-tester accounts are the site's own sign-in once the gate opens.
- **[A-64-3]** Data loss up to the last nightly dump is acceptable for the beta. The store is
  rebuilt from public sources; accounts, alerts and privacy requests since the last dump are not.
