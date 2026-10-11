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
| Backups | nightly `pg_dump` to R2 | the same, plus Hetzner's daily VM backups |

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

**Sizing.** The Compose limits sum to 4.25 GB (Postgres 1, api 1, web 0.75, scheduler 0.25,
worker 1, Caddy 0.25). The measured api peak is 606–638 MB (`docs/60` §2). That fits the `cx32`
(4 vCPU, 8 GB) that `infra/terraform` already defaults to.

**Cost** (inferred from `docs/60` §4's table, prices retrieved 2026-09-13, not re-verified): one `cx32`
at 7.69 USD plus 20 % for Hetzner backups, R2 at 3–6 USD, no managed Postgres (35–55 USD saved).
That is about 15–35 USD a month before email, against 80–170 USD for the three-VM core.

## 2. Owner actions, in order

The coordinator's recommendations, which the owner accepted on 2026-10-10. Allow under an hour. Each step names
what to create and where its value goes:
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
  `infra/sops/secrets.example.plain.yaml`. Add the R2 values from step 4, `SENTRY_DSN` from step 8, and these beta
  values:

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
- [ ] **9. After the first deploy (2 min).** Check that both monitors read "up", and that a report reaches the CSP
  endpoint from Chrome (§5).

## 3. Deploy and seed

```bash
cd infra/terraform && tofu apply -var single_host=true       # one VM, Hetzner backups on
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
bucket next ticked: up to a month for EIA-860M. It runs five steps:
1. Ship `runs/`, `snapshots/`, `normalized/` (including `normalized/context/`) and `held/` into the
   volume.
2. Queue one load per source (`python -m infra.scheduler.bootstrap load`).
3. Wait until every load, resolve, enrich and match job has finished. Each load's chain ends in
   `match_tick`, so matches need no step of their own.
4. Queue the context load (`bootstrap context`: the monthly tick's `context_load`, §5) and wait for
   it. It loads the shipped asset layers, the EIA-860M retirements onto the plants, owner shares
   and features. It runs after the loads rather than beside them, because both write organisations.
5. Queue one fetch per source (`bootstrap fetch`). This gives every source its `source_run` row
   now rather than at its next tick.

Every step runs in the worker image. A server seeded with no local data can run `bootstrap fetch`
alone, then `bootstrap context --build` once the EIA-860M, EIA-860 and GHGRP fetches have stored
their snapshots: the build reads them for the plant, owner and GHGRP layers.

**Testers.** Give them the one login out of band. Rotate it by changing `SITE_ACCESS_HASH` and
redeploying the same tag.

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

Set `SITE_ACCESS=open` (or remove the three keys) and redeploy the same tag. Before that, the
launch checklist in `docs/40` applies unchanged, counsel items included. Moving to three VMs later
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

## 8. Assumptions

- **[A-64-1]** The `cx32` type is offered in the chosen Hetzner location. `docs/60` §4 assumes it
  for `ash`; check it in the console before `tofu apply`.
- **[A-64-2]** One shared login is enough access control for an invited beta. It identifies no
  tester; per-tester accounts are the site's own sign-in once the gate opens.
- **[A-64-3]** Data loss up to the last nightly dump is acceptable for the beta. The store is
  rebuilt from public sources; accounts, alerts and privacy requests since the last dump are not.
