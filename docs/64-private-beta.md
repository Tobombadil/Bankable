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
policy, `frame-ancestors 'none'` and a report-only CSP (`docs/60` §2).

**Sizing.** The Compose limits sum to 4.25 GB (Postgres 1, api 1, web 0.75, scheduler 0.25,
worker 1, Caddy 0.25). The measured api peak is 606–638 MB (`docs/60` §2). That fits the `cx32`
(4 vCPU, 8 GB) that `infra/terraform` already defaults to.

**Cost** (inferred from `docs/60` §4's table, prices retrieved 2026-09-13, not re-verified): one `cx32`
at 7.69 USD plus 20 % for Hetzner backups, R2 at 3–6 USD, no managed Postgres (35–55 USD saved).
That is about 15–35 USD a month before email, against 80–170 USD for the three-VM core.

## 2. Owner actions, in order

1. **Hetzner Cloud project:** an API token (`HCLOUD_TOKEN`) and the operator's SSH public key. Set
   `ssh_source_cidrs` in `terraform.tfvars` to the operator's address (it defaults to anyone, key auth
   only). If Cloudflare will proxy the site, set `web_source_cidrs = ["cloudflare"]` once the records are
   proxied, and not before: the origin then answers Cloudflare's edge only (`docs/60` §2).
2. **Domain and DNS:** `A` records for the apex, `admin.`, `api.` and `www.` pointing at the VM. With
   Cloudflare in front (`cloudflare_zone_id` set), the gated answers are `private, no-store`, so
   the edge caches none of them.
3. **Cloudflare R2:** the backup bucket (`R2_*`) and the basemap tiles (`MAP_TILE_URL`, `docs/40` §2.7).
4. **A GHCR read token** while the packages are private (`GHCR_USER`, `GHCR_READ_TOKEN`).
5. **The production age key and secrets file** (`infra/sops/README.md`), from
   `infra/sops/secrets.example.plain.yaml`, plus these beta values:

   ```
   POSTGRES_PASSWORD=<openssl rand -hex 24>
   DATABASE_URL=postgresql+psycopg://infraque:<same password>@postgres:5432/infraque
   SNAPSHOT_STORE=local
   SITE_ACCESS=basic
   SITE_ACCESS_USER=<one shared user name>
   SITE_ACCESS_HASH=<caddy hash-password --plaintext '<password>' | base64 -w0>
   ```

   The hash is base64-encoded so that no `$` reaches Compose's interpolation. `deploy.sh` refuses a
   raw bcrypt hash, a missing user and any `SITE_ACCESS` other than `open` or `basic`. Caddy itself
   refuses to start on an empty or misspelt value.
6. **An external uptime check** (2026-10-10). Nothing else turns an outage into an alert, because Docker
   does not restart an unhealthy container (`docs/60` §7). Use a free UptimeRobot account or Grafana Cloud
   synthetic monitoring. Check `https://api.{DOMAIN}/v1/health` (outside the gate) every 5 minutes, expect
   200, alert the owner's email after 2 failures. It answers 503 when the database is down. Add
   `https://{DOMAIN}/health` the same way.
7. **A Sentry project** (free tier) for error reports. Put its DSN in the secrets file as `SENTRY_DSN`
   (optional in `infra/sops/secrets.example.plain.yaml`). Every process reads it at start
   (`infra/observability.py`) and reports nothing without it.

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
- **Rollback:** `SINGLE_HOST=1 infra/scripts/rollback.sh production <previous tag>`.
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
- **Admin is off the public hosts.**
- **The firewall sources are variables** (`ssh_source_cidrs`, `web_source_cidrs`).

Still open:
- **Nothing alerts a human yet.** The uptime check and the Sentry project (§2 items 6 and 7) are the
  owner's to create; until then a failure is visible only to whoever looks.
- **The CSP only reports.** There is no report endpoint, so violations show in a browser console only.
  Enforcing it needs that endpoint and the inline `style` attributes moved into classes (`docs/60` §2).
- **A timed-out in-process job releases its lock while its thread runs on** (`docs/60` §6.4). This is
  bounded, not fixed.
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
