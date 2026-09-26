# Operations: SLAs, support, data QA, compliance calendar, cadences, checklists

**Status:** Phase 6 deliverable (the phases table in `docs/00-PLAN.md` had no document for it), v1 · 2026-09-26 ·
owner: product-manager · reviewed by: owner
**Reads:** `docs/04-standards.md` §6–§9 (S-7…S-10, O-1…O-9, R-4), `docs/10-prd-mvp.md` §5 and US-502/US-904/
US-907/US-910, `docs/13-legal-data-rights.md` §5.4/§6/§7, `docs/13-legal-outreach-and-social.md` §0/§8,
`docs/26-platform-posture.md` §5, `docs/32-social-operating-playbook.md` §1, §4–§7, `docs/40-launch-runbook.md`
§5–§7, `docs/60-deployment.md` §6–§11, `data/sources.yaml`, `infra/scheduler/cadence.py`, `infra/scheduler/
jobs.py`, `infra/compose/docker-compose.yml`, `.github/workflows/connectors-nightly.yml`,
`pipeline/connectors/dq.py`, `pipeline/connectors/runner.py`, `web/admin/sources.py`.
**Companion:** `docs/15-risk-register.md` (each SLA gap below is a risk there; each calendar row services a
mitigation there).

## 0. The one fact that governs this document

**Nothing is deployed.** No cloud account, no managed Postgres, no image has been built, no runbook has been
executed (`docs/60` §10 "Last executed: not yet" ×5; §11 items 1–9). Every number below is therefore one of
three kinds, and each is labelled:

- **configured** — a value in the repo that will take effect on the first deploy (a cron, a threshold, a
  timeout, a retention count);
- **measured** — a number produced by running code in the sandbox on SQLite, or a live source fetch, with its
  date;
- **target** — a figure from a standard or a story that has not been observed anywhere yet.

An SLA "the product can actually meet today" is one whose mechanism is configured and whose bound follows from
the configuration; where the bound depends on a production measurement that does not exist, this document says
"unmeasured" rather than quoting the target as a commitment.

## 1. What runs today, and where

| Thing | Runs | Where | Evidence |
|---|---|---|---|
| Connector fixture suite (parse/normalise only, never live) | daily 06:17 UTC + manual dispatch | GitHub Actions | `.github/workflows/connectors-nightly.yml` |
| CI gates (ruff, mypy strict, pytest with coverage floor, import-linter, gitleaks, pip-audit, spec checks) | on PRs and `main` pushes | GitHub Actions | `.github/workflows/ci.yml`; `docs/60` §9 gate status |
| Image build and push | on `main` pushes and `v*` tags | GitHub Actions → GHCR | `.github/workflows/release.yml` (never run against a real registry that a VM has pulled from) |
| Basemap tile refresh | monthly, 05:23 UTC on the 1st | GitHub Actions | `.github/workflows/basemap.yml`; needs the `production` environment's R2 secrets, which do not exist — the next run (2026-10-01) will not upload anything |
| Scheduler, workers, API, web, alerts, social drafts | not running anywhere | — | `docs/60` §6, §6.1 describe what will run |
| Backups | not running anywhere | — | the systemd timer is written by cloud-init that has never executed (`docs/60` §8) |

Local development runs the whole stack against SQLite (`docs/61-run-the-latest-locally.md`); that is where the
measured numbers come from.

## 2. SLAs

### 2.1 Data freshness per source

**Mechanism (configured).** `infra/scheduler/cadence.py` buckets each connector's `cadence` string into one of
six cron schedules, always rounding to the more frequent bucket, and never less often than weekly. A fetch job
has a 10-minute timeout (5 for browser sources); the load that follows has a 30-minute timeout, resolve 60,
enrich 30 (`infra/scheduler/app.py` `LOAD_TIMEOUT_S`, `RESOLVE_TIMEOUT_S`, `ENRICH_TIMEOUT_S`). Transient
failures retry five times with exponential waits summing to ≈ 65 minutes before dead-lettering
(`FETCH_RETRY`); a block, corrupt payload or gate refusal is recorded once and left for the next tick.

The sixteen connectors that exist, with the bucket the cadence string resolves to:

| `source_id` | `cadence` in `data/sources.yaml` | Bucket → cron (UTC) | Freshness bound this yields | M-10 target | Last verified live |
|---|---|---|---|---|---|
| `us.ferc.elibrary` | `realtime` | 15min → `*/15 * * * *` | ≤ 15 min + fetch/load (≤ 55 min worst case) | ≤ 24 h | 2026-09-12 |
| `us.grants_gov.search2` | `realtime` | 15min | same | ≤ 24 h | 2026-09-12 |
| `gb.find_a_tender` | `realtime` | 15min | same | ≤ 24 h | 2026-09-12 |
| `eu.ted.api` | `daily` | daily → `7 3 * * *` | ≤ 24 h + fetch/load | ≤ 24 h | 2026-09-12 |
| `mdb.worldbank.procnotices` | `daily` | daily | ≤ 24 h + fetch/load | ≤ 24 h | 2026-09-12 |
| `us.iso.caiso.gen_queue` | `weekly` | weekly → `13 4 * * 1` (Monday) | ≤ 7 d + fetch/load | ≤ 7 d | 2026-09-12 |
| `us.iso.nyiso.gen_queue` | `weekly` | weekly | ≤ 7 d | ≤ 7 d | 2026-09-12 |
| `us.permits_dashboard` | `weekly` | weekly | ≤ 7 d | ≤ 7 d | 2026-09-12 |
| `gb.neso.tec_register` | `twice weekly` | weekly (keyword `twice weekly`) | ≤ 7 d — **coarser than the source's own cadence**; a mid-week NESO update waits for Monday | ≤ 7 d | 2026-09-12 |
| `us.epa.class_vi` | `biweekly-ish (no published schedule …)` | weekly (substring `weekly`) | ≤ 7 d | — (tier 3) | 2026-09-22 |
| `us.tx.rrc.class_vi` | `weekly poll; the RRC rebuilt the list … (irregular, months apart)` | weekly | ≤ 7 d; publishable only under `PLATFORM_POSTURE=noncommercial` (`docs/26`) | — (tier 3) | 2026-09-25 |
| `us.iso.ercot.gen_queue` | `monthly` | monthly → `21 5 1 * *` | ≤ 31 d | — | 2026-09-12 |
| `us.iso.ercot.large_load_queue` | `monthly` | monthly | ≤ 31 d; the connector watches the EMIL catalogue and emits an event when the product appears (it did not exist on 2026-09-12) | — | 2026-09-12 (0 rows) |
| `us.eia.860m` | `monthly` | monthly | ≤ 31 d; loaded vintage 2026-07 on a 2026-09-13 fetch (`docs/00-PLAN.md` 2026-09-21) — EIA's own lag is about two months on top | — | 2026-09-12 |
| `us.eia.860` | `annual (final in Q3/Q4 of the following year; early release earlier)` | annual → `37 7 2 1 *` | ≤ 1 y; the January tick will usually re-fetch an unchanged file | — | 2026-09-19 |
| `us.epa.ghgrp` | `annual (… published the following October), with revisions` | annual | ≤ 1 y; **the January tick misses the October release by nine months** — run-now (`POST /admin/v1/sources/{id}/run`) each October until the bucket is per-source | — | 2026-09-25 |

**What can be stated as an SLA today:** for a source whose fetch succeeds, a change is visible on every tier
within *bucket period + 55 minutes* (configured bound), because nothing is time-delayed on any tier
(`services/ingest/lag.py`, `tests/test_publication_is_never_time_delayed.py`). **What cannot:** the observed
success rate (M-9 ≥ 90 % of published sources inside 2× cadence) — every connector has exactly one live
verified run, on the date in the last column; DA-14 step 5's three baseline runs do not exist for any source.

### 2.2 Alert latency (US-502)

| Mode | Configured mechanism | Bound | Status |
|---|---|---|---|
| Immediate | `alert_tick` every 15 minutes (`*/15 * * * *`), one tick per firing, 10-minute timeout, `queueing_lock` so an overlapping tick is refused not doubled (`docs/60` §6.1; `infra/scheduler/app.py`) | mean ≈ 7.5 min, worst case 15 min + tick run time (≤ 25 min) after the event is **published**; add the §2.1 fetch/load bound for time since the **source** changed | configured; dry-run email by default (`services/alerts/worker.py`, `RESEND_API_KEY` unset) |
| Daily / weekly digest | same tick, digest grouping per saved search (`services/alerts/evaluate.py`) | next digest window | configured |
| Webhooks | delivered in the same tick; exponential retry; endpoint paused after 24 consecutive failures (`services/alerts/webhooks.py` `PAUSE_AFTER_CONSECUTIVE_FAILURES`) | as immediate | configured |
| Unsubscribe | `POST|GET /v1/alerts/unsubscribe`; the next cycle sends nothing for that search (`tests/test_api_unsubscribe.py`) | ≤ one delivery cycle (US-502 AC3) | tested |

US-502 AC1 says "within 15 minutes of the event being published". The configured worst case is up to 25
minutes; the honest published figure until measured is **"usually within 15 minutes, always within 30"**.
Email-provider delivery time is on top and outside the platform's control.

### 2.3 Availability and health

| Item | Configured | Bound | Status |
|---|---|---|---|
| Process liveness | Compose healthchecks on `api` (`GET /v1/health`) and `web` (`GET /health`): `interval: 15s`, `timeout: 5s`, `retries: 5`, `start_period: 15s`; `restart: unless-stopped` (`infra/compose/docker-compose.yml`) | a dead process is marked unhealthy within ≈ 75–100 s and restarted by Docker | configured, never observed in production |
| Deploy health gate | `deploy.sh` waits up to 180 s for every replica healthy and `/v1/health` answering, else auto-rollback once (`docs/60` §10.1) | a bad deploy self-reverts within ≈ 3 min of the health wait starting | proven against shims only (`infra/test_scripts.py`) |
| External uptime check | none exists (`docs/60` §7: UptimeRobot or Grafana synthetic, "alerts on 2 consecutive failures") | — | not operable |
| Monthly availability | — | **no figure offered**; a single VM per role with no failover cannot promise one, and there is no monitor to measure it | target only |
| API latency | measured 2026-09-13 on the full 10,409-row load, SQLite, in-process: list page 0.127 s, geo 0.35–0.53 s, detail ≈ 0.02 s; plants geo warm pan 11–37 ms (`docs/00-PLAN.md` 2026-09-13, 2026-09-15) | E-16 p95 budgets are targets until Postgres numbers exist | measured (sandbox) |
| Rate limits per hour (`services/api/ratelimit.py` `TIER_LIMITS`, `WINDOW_SECONDS = 3600`) | public 60 · free account 300 · pro 600 · api 6,000 · admin 1,200 | in-memory token bucket; resets on process restart | configured, tested (`tests/test_api_pro_ratelimit.py`) |

### 2.4 Data durability

| Item | Configured | Bound | Status |
|---|---|---|---|
| Primary backup | managed Postgres daily snapshot + PITR (a provider property, ADR 0003) | RPO ≤ 1 h (`docs/04` O-7) | no provider account exists |
| Secondary backup | `infraque-backup.timer` nightly 03:17 UTC + ≤ 10 min jitter, `pg_dump` to Cloudflare R2; local retention 35 days; R2 keeps 14 daily + 8 Sunday dumps (`docs/60` §8) | RPO 24 h on the secondary copy | script tested against shims; timer never installed |
| Restore | `restore_drill.sh` monthly by hand; real restore via provider PITR to a new branch then `DATABASE_URL` cut-over (`docs/60` §10.3) | RTO ≤ 4 h is a **target** | never executed |
| Backup-age alert | > 26 h (`docs/04` O-7) | — | not wired (`docs/60` §7) |
| Retention of stored data | DA-10: raw snapshots 24 months then monthly samples; `model_call` prompts 90 days; sessions 30 days; alerts 12 months | — | **no retention job exists** (grep: `retention_class` is a column with no scheduled consumer); nothing is old enough to matter before 2026-10-12 (sessions) |

### 2.5 Privacy and rights requests

| Obligation | Legal maximum | Platform commitment | Mechanism | Status |
|---|---|---|---|---|
| Deletion or objection from a named filer or a member | GDPR Art. 17: one month; CCPA: 45 days | **30 days** (`docs/13` §5.4 rule 6; `docs/04` S-8) | `deletion_request` task type; CRM asked first, local redaction only if that succeeds; identifiers hashed in the audit log; `suppression` table survives re-ingest (`services/alerts/suppression.py`, migration `0012`; `docs/CHANGELOG.md` 2026-09-19) | admin-triggered only; no self-service route (`docs/40` §4 row 6) |
| Email opt-out (alerts, digests) | CAN-SPAM 10 business days | ≤ one delivery cycle, i.e. ≤ 24 h | §2.2 unsubscribe | tested |
| Outreach-contact objection (Art. 21) | immediately | 24 h, confirmed in writing within 72 h (`docs/13-legal-outreach-and-social.md` §8.2) | CRM suppression, human | no channel active |
| Privacy notice (Art. 14) | within one month of indirect collection | before the first public page | `/privacy` (`web/legal.py`) | built, not deployed |

### 2.6 Licence and posture

| Item | Commitment | Mechanism | Status |
|---|---|---|---|
| No `restricted`/`unknown` row on any non-admin surface (M-11) | 0, always | `services/api/visibility.py`; loader gate; `scripts/check_manifest_licences.py` (113 sources PASS 2026-09-26, `docs/26` §6) | enforced in code and tests; the nightly audit that would prove it in production is **in build 2026-09-26** |
| Attribution on every surface | every record carries `source_id`, `source_url`, `retrieved_at`, `licence` (`CLAUDE.md`) | provenance not-null in the schema; renders from data | built; `docs/40` §4 row 4 "not independently re-verified this sprint" |
| Posture truthfulness | the `/about`, `/methodology` and `/pricing` sentence is the API's own `posture_statement`, never template prose | `services/posture.py`; `web/page.py::get_platform_posture` | tested |
| Switch-back from `noncommercial` | every `noncommercial` row invisible on every non-admin surface after one restart | `docs/26` §5 runbook; `scripts/posture_report.py` | tested; never run against a populated database |
| S1 incident (gated data visible, credential exposure, unhuman send, personal-data exposure) | unpublish/revoke within 1 h, notify affected parties within 24 h, postmortem in `docs/6x-incidents-YYYY.md` within 5 business days (`docs/04` S-9) | admin unpublish (US-905 AC2), `rotate_secret.sh` | see §3.3 for what a solo operator can honestly promise |

## 3. Support with a solo owner

### 3.1 Channels

| Channel | Who reads it | For |
|---|---|---|
| Support mailbox on the product domain (`infraque.com` — **not yet registered**, `docs/00-PLAN.md` owner actions) | owner | account, billing (inactive under `noncommercial`), data questions, corrections |
| In-product report (`POST /v1/reports` → `report` task) | owner via the admin task queue (`web/admin/tasks.py`) | wrong fact, wrong merge, takedown, personal-data request |
| Intake forms (`intake_proposal`, `intake_opportunity` tasks) | owner | user-submitted records; human review before anything publishes (US-1002) |
| Social replies and DMs | owner; drafts from the review queue, sent by a human (`docs/32` §5) | never automated |
| Press, legal wording, a party named in a post | owner within 4 working hours, red-flagged at the top of the queue (`docs/32` §5) | escalation |
| Status page | none | — |

There is no phone, chat, or ticketing product. The task queue **is** the ticketing system; `task.status`
(`open`, `in progress`, `done`, `rejected`) and the audit event on every action (`docs/04` S-4) are the record.

### 3.2 Response targets

Stated in the owner's time zone (America/Denver). **Proposed here; ratify or amend** (`docs/15` §4 question 3).

| Class | First response | Resolution | Notes |
|---|---|---|---|
| Correction of a published fact (wrong status, capacity, date, merge) | 1 business day | 3 business days, or unpublish the record meanwhile | corrections policy `docs/32` §3.4; unmerge is exact and audited |
| Takedown or licence complaint from a source or sponsor | same business day | unpublish within 1 h of reading it (S1/S2 per S-9); resolution per counsel | legal-compliance on every S1/S2 |
| Personal-data deletion or objection | 3 business days | 30 days (§2.5) | task type `deletion_request` |
| Account, login, alert delivery | 1 business day | 3 business days | |
| Feature requests, coverage requests | acknowledge within 5 business days | logged in `docs/00-PLAN.md` open questions or the connector backlog | a named user job or it does not enter the plan (`docs/10` §3) |
| Press enquiry | 4 working hours (`docs/32` §5) | owner decides | never answered by an agent |

**Hours:** business days 09:00–17:00 Denver. Outside hours there is nobody on call.

### 3.3 What a solo operator can and cannot promise on S1

`docs/04` S-9 asks for unpublish within 1 hour. One person cannot hold that around the clock. The honest
position, recorded as an assumption pending the owner's answer (`docs/15` §4):

- **In hours:** best effort within 1 hour.
- **Out of hours:** within 12 hours, because the compensating control is that the classes of S1 that matter
  most are prevented, not responded to — `restricted`/`unknown` rows cannot reach a non-admin surface by
  configuration, and no outbound message can be sent without a human. What remains for a fast human response
  is a credential exposure or a defect in the predicate itself, and the nightly M-11 audit (in build) is the
  detector for the second.
- **Before any paying customer:** a second person with the `operator` role and a written hand-over, or the
  1-hour figure is not published anywhere customers can read it.

## 4. Data QA operations

### 4.1 The gates that run on every source run

`pipeline/connectors/dq.py::run_gates` executes at the end of every run and writes `source_run.dq`
(`pipeline/connectors/runner.py` lines 229–247). Defaults, per source overridable (`DEFAULT_THRESHOLDS`):

| Check | warn | hold | Notes |
|---|---|---|---|
| Row-count drift vs previous run | ≥ 10 % | > 30 % either way | needs one previous run; the first run is `info` |
| Vocabulary: unmapped status (`status_rule` ends `.unmapped`) | any | > 5 % of rows | `status_map.yaml` per DA-5 |
| Vocabulary drift: new `status_raw`/`technology_raw`/`kind` values | any new value | — | vocabulary stored per run (≤ 500 values per column) |
| Null spike on a required field | +5 pp vs the median of the last 5 runs | +10 pp | required fields per kind: proposal `name_canonical, capacity_mw, technology_raw, state`; opportunity `title, issuer, jurisdiction, due_at`; document `title, accession_number, docket_refs, published_date` |
| Duplicate `record_id` | suffixed duplicates (declared) | any remaining | |
| Provenance quartet | — | any missing cell | `PROVENANCE` from `pipeline/connectors/base.py` |
| Schema drift of source columns | any added/removed | a removed column that feeds a field (`key_source_columns`) | |

**What a hold does (configured):** the raw snapshot is kept as evidence, the normalised frame is written to
`data/held/<source_id>/…` (`runner.py` `st.held_path`), nothing publishable is written, `source_run.dq_status
= fail`, `hold_reasons` are recorded, and the scheduled path records the run as `partial`, which sets
`source.health = degraded` without counting as a failure (`infra/scheduler/jobs.py::_update_health`).

**Releasing a hold (not operable as designed):** `docs/04` DA-6 and `docs/60` §10.4 describe release "from the
task queue (US-907) with a reason". `TASK_TYPES` (`services/db/models.py`) is `report, intake_proposal,
intake_opportunity, deletion_request, resolution_dispute` — there is no hold task type and no admin release
action. Today the procedure is manual: inspect `data/held/<source_id>/`, fix the cause (parser, threshold or
the source), and run-now; the frame is re-fetched, not released. `docs/04` O-9 lists "DQ-hold release" among
the runbooks required at launch; it does not exist yet.

**Baselines (measured):** every connector has one verified live run (§2.1 last column). Row-drift and
null-spike gates only bite from the second and second-to-sixth runs respectively, so the first production week
is `info`, not protection. Sandbox results on 2026-09-12: 9 pass, NYISO warn (2 duplicated queue positions,
1,350 padding rows dropped), 0 held (`docs/00-PLAN.md` connector row).

### 4.2 Source health and the admin screen

- **Vocabulary** (`docs/21` §4.1, `infra/scheduler/jobs.py::_update_health`): `ok | degraded | failing |
  blocked | paused`. `ok`/`unchanged` reset the counter; `partial` (a DQ hold) degrades; `failed`/`budget`
  increment `consecutive_failures` and flip to `failing` at `FAILING_AFTER = 3` (US-904 AC3); `blocked` is
  its own state; `paused` is never overwritten by a run.
- **Screen** (`web/admin/sources.py`, `/admin/sources`): one row per registered source with the health chip
  (icon plus the health word, never the icon alone), the **GATED** chip read off the licence class under the
  running posture (`gated_reuse_classes(platform_posture())`) or a `gate_flag`, publish state, last run and
  snapshots; filters on `health`, `publish_state`, `implemented`; the gate form is rendered only for the `legal`
  role (US-905 AC1); **run-now** (`POST /admin/v1/sources/{id}/run`, 202) always enqueues and never runs
  inline, so it behaves exactly like the scheduled tick.
- **What the screen cannot show yet:** model cost per record (no `model_call` writer, §8), events emitted per
  run is on the run row only if the loader ran (it does on the scheduled path since the 2026-09-18 audit's
  closed-loop fix, `infra/scheduler/jobs.py` "the closed loop").
- **Freshness alert** (`docs/04` O-6: failing > 2 cycles, queue age > 2× cadence, DQ hold) — the data is in
  `source_run`; the alert has no delivery channel until Grafana exists (§8).

### 4.3 The nightly connector fixture workflow

`.github/workflows/connectors-nightly.yml`, 06:17 UTC daily and on manual dispatch: `pytest pipeline -v`
(every connector's `parse()`/`normalize()` against recorded fixtures; `fetch()` never runs in CI, `docs/20`
§3.1) then `pytest infra/scheduler -v`, whose `test_sources_yaml_cadences_are_all_covered` fails when a new
cadence string in `data/sources.yaml` is not in `cadence.py`'s keyword table.

What it catches: a parser change that breaks on the recorded sample; a cadence string that would silently fall
to the weekly default. What it does **not** catch: the source changing under the fixture — that is `source.health`
and DQ's job in production. **Failure routing:** the workflow header says a red run "should page the owner the
same way `ci.yml`'s `security` job would"; nothing pages anyone today — the Actions tab and GitHub's email on a
failed scheduled workflow (to the workflow's last committer, not necessarily the owner) are the only signals.
Put "check the nightly run" on the weekly list until an alert route exists (§7.2).

### 4.4 Resolution and coverage QA

- Merges are reversible events; `resolution_decision` rows short-circuit later automation; clusters the resolver
  cannot decide route to human review (3 on the 2026-09-13 load). The measured bar is precision/recall
  0.946/0.946 on the usable labels; the 600-label held-out set of the M-1 recalibration and `data/eval/reports/
  <version>.md` (DA-9) do not exist. **Weekly:** count of open `resolution_dispute` tasks and unmerge events.
- Coverage is derived, not written: `GET /v1/coverage` and the `/coverage` page (86 registered / 16 with rows /
  19 withheld on the 2026-09-21 load; 119 manifest entries today). **Weekly:** read the three counts and the
  vintage table; a source whose `vintage` moves is a source whose rows moved.
- Slippage: `scripts/measure_slippage.py` reproduces the `docs/22` §18 distribution behind the 90-day grace
  period in `services/api/slippage.py`. **Per load** that changes a register's date granularity.

## 5. Licence renewals and the compliance calendar

Every dated or recurring obligation the repo names, with its next date from 2026-09-26. "Overdue" means the
repo has carried it as an open action past its own stated timing. Where the repo names no cadence the row says
**proposed** and gives one; the owner ratifies by leaving it in.

### 5.1 Overdue owner actions (all open since 2026-09-12/13, `docs/00-PLAN.md` "Owner actions still open")

| Item | Blocks | Where recorded |
|---|---|---|
| PJM Redistribution Licence enquiry; counsel question 1 | R-01 | `docs/13` §7 item 1; `docs/40` §2.1 |
| MISO terms read in a browser | R-02 | `docs/13` §7 item 2 |
| SPP written-authorisation request | R-03 | `docs/40` §2.1 |
| Counsel brief (15 items doc A §7, 11 items doc B §11); the three launch blockers first | R-05 | PLAN 2026-09-18 decision 4 |
| Register `infraque.com` (and `infraqueue.com`, typo variants counsel suggests) | every sender address and handle | PLAN 2026-09-15 |
| Free API keys: EIA v2, SAM.gov, NRC ADAMS, regulations.gov | tier-2 connectors | PLAN "Immediate next actions" |
| LinkedIn Marketing Developer Platform application (2–8 weeks) | LinkedIn API posting | `docs/32` §1.4 |
| Bluesky account; X developer project; email provider | first post | `docs/32` §2 |
| Attio workspace and API key; Stripe account; Hetzner, Cloudflare, Neon accounts and tokens | first deploy | `docs/40` §2.2–§2.6 |
| Browser tasks: OCED portfolio, WY DEQ popup, IL ICC e-docket, NSTA terms, Sodir NLOD, GEM in-file notice, Argonne notice | CCS sources; GEM attribution | `docs/62` items 1–7; `docs/40` §0.4 |
| Commit-history rewrite (139 commits) | repo going public | PLAN 2026-09-18 decision 1 |

### 5.2 Dated and recurring

| Next date | Obligation | Cadence | Source | Owner |
|---|---|---|---|---|
| every Monday | Weekly social report 08:00 ET; weekly pipeline report; gridstatus release check; opt-out verification and CRM dedupe | weekly | `docs/32` §6.2; `docs/04` G-7, G-11; PLAN 2026-09-15 | coordinator drafts, owner reads |
| daily 06:17 UTC | Connector fixture suite | daily | `connectors-nightly.yml` | GitHub Actions; owner checks weekly |
| 2026-10-01, then the 1st | Basemap refresh workflow fires (uploads nothing until R2 secrets exist) | monthly | `basemap.yml` | devops-engineer |
| 2026-10-12 | First sessions reach the 30-day retention age (DA-10); no retention job exists — note only | rolling | `docs/04` DA-10 | backend-developer |
| **2026-10-15** | gridstatus decision: if no release relaxes the `cryptography<47`/`lxml~=5.3`/`setuptools<79` pins, vendor the two parsers and drop the six `pip-audit` ignores | once | PLAN 2026-09-15; `ci.yml` 349–352 | coordinator |
| 2026-10 (October) | EPA GHGRP reporting-year release — run-now, because the annual bucket fires in January (§2.1) | annual | `data/sources.yaml` `us.epa.ghgrp` | data-engineer |
| 2026-10/11 (Q3/Q4) | EIA-860 final release for the prior year — run-now for the same reason | annual | `us.eia.860` | data-engineer |
| **2026-10-26** | First monthly operations review (§7.3): cost vs `docs/60` §4, the risk register, the owner-action queue, this calendar | monthly | this document | owner + coordinator |
| **2026-12-12** | Re-verify X pricing and limits, LinkedIn API version header, Bluesky limits | quarterly | `docs/32` §7 day 30 | content-social |
| 2026-12-12 (**proposed**, aligned to the row above) | Re-retrieve every quoted terms URL in `data/sources.yaml` (`license` field) and diff against the stored quote; record any change in `docs/13` and re-classify before the next run | quarterly | R-07 in `docs/15`; no cadence exists in the repo | legal-compliance |
| 2026-12 | GEM tracker release check (semi-annual/quarterly); update the ownership-tracker citation string, which names "August 2026 release" | per release | `docs/13` §2.18, §6 | legal-compliance |
| 2027-03-13 | `docs/60` §10 runbooks turn stale if still never executed (6 months, `docs/04` O-9) | once, then rolling | `docs/04` O-9 | devops-engineer |
| 2027-05/06 | LBNL "Queued Up" annual release (licence still unverified, `docs/13` §2.1) | annual | `us.lbnl.queued_up` | legal-compliance |
| 2027-09-12 | First annual review of retained personal fields (12 months from the first ingest) | annual | `docs/13` §5.4 rule 7 | legal-compliance |
| 2028-09 | 24-month purge of personal fields on withdrawn/cancelled projects; 24-month raw-snapshot retention begins to bite | rolling | `docs/13` §5.4 rule 7; DA-10 | backend-developer |
| from account creation | Stripe API version pin (account default until set); Attio object ids `ATTIO_OBJECT_IDS` (A-34-1) | once | PLAN 2026-09-13 first-wave row | owner, backend-developer |
| first deploy + 30 days | Re-price the Neon line from real usage (B-1) | once | `docs/60` §4 | devops-engineer |
| first `staging` month, then monthly | Restore drill; paste the result into `docs/60` §10.3 | monthly | `docs/04` O-7 | devops-engineer |
| first real secret + 90 days | First rotation of every platform secret; session signing keys quarterly; log in `infra/secret-rotation-log.md` | ≤ 90 days | `docs/04` S-7; `docs/60` §10.5 | devops-engineer |
| first deploy | Let's Encrypt via Caddy, 90-day certificates auto-renewed; alert if no renewal in 60 days | rolling | `docs/60` §7 | devops-engineer |
| first deploy + 26 h | Backup-age alert threshold (not wired) | daily | `docs/04` O-7 | devops-engineer |
| LinkedIn app creation | Pin the current monthly API version; versions sunset after ≈ 12 months (the `202508` example in `docs/32` §1.4 already sunset on 2026-08-17); quarterly bump | quarterly | `docs/32` §1.4 item 8 | content-social |
| LinkedIn OAuth + 350 days | Owner re-authorises (refresh token 365 days; access token 60 days auto-refreshes) | annual | `docs/32` §1.4 item 7, §2.5 | owner |
| account creation + 1 year | Bluesky app password and email API key re-issued | annual | `docs/32` §2.5 | owner |
| each sprint end | M-1…M-13 against targets and kill signals; cost ceilings; rollback rehearsal on `staging` | per sprint | `docs/04` G-11, O-5, O-8 | coordinator |
| monthly | Lead-scoring weights | monthly | `docs/04` G-11 | sales-bd |
| quarterly | ICP priority; pricing ladder (moot while paid tiers are inactive) | quarterly | `docs/04` G-11 | owner |
| per request | Deletion/objection within 30 days; email opt-out within one cycle (≤ 24 h) | per request | §2.5 | owner |
| per new source | DA-14 steps 1–8, including legal review before the connector and three baseline runs | per source | `docs/04` DA-14 | data-engineer, legal-compliance |
| per exception | `docs/04` §9.4 register entries expire after ≤ 1 sprint; an expired exception blocks release (R-4) | per entry | `docs/04` R-6 | product-manager |
| per breaking API change | `Sunset` header, old version kept ≥ 6 months, email to every key that used it in the prior 90 days | per change | `docs/04` §5 | backend-developer |

**No signed data licence exists**, so there is nothing to renew in the contractual sense. When one is signed
(PJM, SPP, or a partnership under `docs/04` G-8) it becomes a `licence` row with `contract_ref` and
`expires_at` (`docs/21` §3.19) and a row in this table at `expires_at − 90 days`.

## 6. Content and social cadence

From `docs/32`; nothing below is live — every publisher is a dry-run adapter that never sends, no account
exists, and the review queue has no owner-set `auto_publish` for any pair.

| Channel | Role | Cadence at target | Slots | Review | Draft expiry |
|---|---|---|---|---|---|
| Email + RSS (owned) | primary; alerts and digests | event-driven alerts (paid tier — inactive under `noncommercial`); weekly digests Monday 07:00 region-local; RSS immediate | — | digest reviewed; alerts automatic with disclosure footer | — |
| Bluesky | public-tier firehose | up to 30/day, ramped 3 → 10 → 20 → 30 over the first three weeks | 06:00–20:00 ET, ≥ 10 min apart | reviewed; may graduate (§4.6) | 48 h |
| LinkedIn Company Page | curated, highest-value audience | 1–3/day + weekly digest | 07:30, 12:00, 16:30 ET weekdays | **always** reviewed; never auto | 24 h |
| X | journalists, analysts; metered | ≤ 30 link posts/day; credit cap USD 250/month, never auto top-up | as Bluesky | reviewed; may graduate; "Automated" label mandatory | 24 h |
| Threads, Mastodon | month-6 review | — | — | — | — |
| Reddit | never automated; owner posts manually if at all | ≤ weekly | — | manual only | — |

**Pipeline cadence (configured):** `post_draft_tick` hourly at :07 drafts from the platform's own event log
behind the four hard gates (api-tier visibility, published event, licence allows derived publication, not
unpublished) with duplicate suppression per (subject, channel) and a stored watermark (`services/social/
worker.py`; `docs/60` §6.1). Bursts are smoothed to the daily cap plus one cluster post (`docs/32` §4.5).

**Graduation to auto-publish** (`docs/32` §4.6): ≥ 200 posts of the (channel, event_type) pair in review mode
over ≥ 30 days, edit rate ≤ 5 %, `wrong_fact` = 0 over the trailing 100, zero platform incidents in 30 days,
adapter errors ≤ 1 %, channel disclosed, owner's name and date in `config/social.yaml`; revoked automatically
on any `wrong_fact`, enforcement action or budget hold. Earliest possible pair: Bluesky `proposal.new` around
day 40 of the launch calendar.

**Weekly report:** Monday 08:00 ET, template `docs/32` §6.2, stored under `reports/social/YYYY-WW.md`; the
first with cost per sign-up on day 14. **Retro** on day 30 with decisions into `docs/00-PLAN.md`.

**Engagement:** every reply, comment and DM is drafted by the pipeline and sent by a human; never follows,
likes, reposts, @-mentions or DMs from the automated account; escalations to the owner within 4 working hours
(`docs/32` §5).

**Channel register** (required by `docs/13-legal-outreach-and-social.md` §0 before any channel is active; it
named this document's phase as its home). Empty until the owner activates a channel; a row with a blank cell
does not ship.

| Channel | Sending identity | Recipient jurisdictions | Legal basis per jurisdiction (doc B §1–§4) | Platform rules checked (§5) | AI-disclosure position (§6) | Automation level and disclosure text (§7) | Suppression / unsubscribe / records (§8) | Activated (owner, date) |
|---|---|---|---|---|---|---|---|---|
| — | — | — | — | — | — | — | — | — |

## 7. Operator checklists

Written for one person. Times are estimates, not measurements. Until the first deploy only the items marked
† are possible.

### 7.1 Daily (≈ 15 minutes; `docs/40` §5)

1. Admin source health: any `failing`/`blocked` chip → `docs/60` §10.4; any `degraded` with a DQ `fail` →
   §4.1 manual hold procedure.
2. Task queue oldest-first: `deletion_request` age against 30 days; `report` tasks about published facts;
   red-flagged escalations (named party, press, legal wording) answered within 4 working hours.
3. Social review queue: approve, edit or reject with a reason code; drafts past 24 h (LinkedIn/X) or 48 h
   (Bluesky) have already expired to `withdrawn`.
4. Nightly M-11 audit result = 0 (once the job exists; in build 2026-09-26). Any other number is S1: unpublish
   first, then everything else.
5. Cost log (US-909) — empty until `services/modelgw` exists; skip.

### 7.2 Weekly, Monday (≈ 60 minutes)

1. † `connectors-nightly` runs for the last seven days green; a red run is a parser fix PR this week.
2. † gridstatus releases: has a release relaxed a pin? If yes, drop the matching `--ignore-vuln` in `ci.yml`.
   Decision date 2026-10-15 stands regardless.
3. Read the weekly social report (§6) and the pipeline report; note every `wrong_fact`.
4. Coverage counts (`/coverage`): registered / with rows / withheld; any `vintage` that moved; the `load`
   technology still at zero rows.
5. Resolution: open `resolution_dispute` tasks, unmerge events this week, clusters in human review.
6. DQ `warn` runs this week — new vocabulary values need a `status_map.yaml` entry before they become
   `unmapped` holds.
7. Backup `last-success` age (once deployed) < 26 h; certificate renewal not older than 60 days.
8. CRM: opt-outs processed and verified, dedupe run (`docs/04` G-7).
9. † Owner-action queue (§5.1): anything closable this week; record closures in `docs/40` §2 done-checks.
10. † Rate-limit and 5xx counts (once a monitor exists); until then, `docker compose logs` for `5xx` on the
    app VM.

### 7.3 Monthly, last Monday (first: 2026-10-26; ≈ 2–3 hours)

1. † `docs/15-risk-register.md`: re-score every risk whose review date has passed; append a change-history
   line; move anything that changed the plan into `docs/00-PLAN.md`.
2. † This calendar (§5): everything due in the next 60 days assigned; anything overdue named in the review.
3. Cost: invoices against the `docs/60` §4 table; anything over 80 % of a ceiling is a `docs/00-PLAN.md`
   decision, not a quiet overrun (`docs/04` O-8).
4. Restore drill (`infra/scripts/restore_drill.sh`), result pasted into `docs/60` §10.3.
5. Secrets: rotation log; anything approaching 90 days rotated this month (`docs/60` §10.5).
6. Runbooks: "Last executed" lines; any procedure not run in the last six months is rehearsed on `staging`
   or marked stale.
7. † `pip-audit` ignore list and `requirements.txt` pins reviewed; Sentry (once it exists) top errors.
8. Retention (manual until a job exists): sessions > 30 days, `model_call` prompts > 90 days, alerts > 12
   months — count, then delete through the documented procedure only (`docs/21` §1 grants no `DELETE`
   outside it).
9. Lead-scoring weights (`docs/33` §8); social graduation dashboard; month-end X spend vs cap.
10. † `docs/04` §9.4 exceptions register: nothing expired (empty today).
11. † Decisions log: every decision taken this month is a row; every open question has an owner.

### 7.4 Quarterly (first: 2026-12-12)

Terms re-retrieval and diff for every quoted licence; X/LinkedIn/Bluesky limits and pricing; LinkedIn API
version bump; session signing-key rotation; ICP priority and pricing ladder review; `docs/13` §7 item list
re-read against what has been answered.

## 8. Not yet operable, and why

Stated so that nothing above is read as running.

| Capability | Why not | Where it is tracked |
|---|---|---|
| Any production environment | No Hetzner, Cloudflare zone, Neon or R2 account; no tokens; migrations never run on real Postgres; images never pulled by a VM | `docs/60` §11 items 1–3, 9; `docs/40` §2.6, §3 |
| **Nightly M-11 audit** | **In build 2026-09-26** by another lane; `docs/40` §4 row 11 still reads "not located". Until it runs, M-11 = 0 is proven by tests on fixtures, not observed on production data | `docs/04` R-4; `docs/15` R-06 |
| Alerts to a human on failing sources, DQ holds, 5xx, backup age, cost 80 % | No Grafana Cloud or Sentry account; logs not shipped; the `source_run` data exists, the delivery channel does not | `docs/60` §7, §11 item 7 |
| DQ-hold release from the task queue | No hold task type, no admin release action (§4.1) | `docs/04` O-9 required runbook list |
| Restore, rollback, secret rotation, deploy | Never executed; no environment to execute in | `docs/60` §10 |
| Supervision Routine that drafts fixture and parser fixes on a red nightly run | Does not exist; a human does it | `docs/03` §1; `connectors-nightly.yml` header |
| Model cost log (US-909), extraction, adjudication, the county-permit pilot's US$2 kill criterion | `services/modelgw` does not exist; `model_call` has no writer | `docs/60` §11 item 5; `docs/15` R-16 |
| Retention job (DA-10) | No scheduled consumer of `retention_class` or the age rules | §2.4 |
| Terms-change detection on published sources | No job re-reads a terms URL; proposed quarterly in §5.2 | `docs/15` R-07 |
| Self-service personal-data deletion for a member | Admin-triggered only | `docs/40` §4 row 6 |
| Any social account, any post, any email to a real person | No accounts; all adapters dry-run; the channel register (§6) is empty; the human-sends rule stands | `docs/32` §2; `docs/13-legal-outreach-and-social.md` §0 |
| LinkedIn via API | Self-serve API terms prohibit automated posting; MDP application not submitted; bridge scheduler until approval | `docs/13-legal-outreach-and-social.md` §5.1; `docs/32` §1.4 |
| Paid tiers, checkout, revenue | Inactive by design while `PLATFORM_POSTURE=noncommercial` (`.env.example` default since 2026-09-26; code default stays `commercial`) | `docs/26` §3(i), §6 |
| PJM, MISO, SPP, ISO-NE on any tier | `restricted`/`unknown`; owner legal actions open (§5.1) | `docs/13` §6; `docs/40` §1 |
| External uptime monitoring, status page | Not set up | `docs/60` §7 |
| Preview-per-PR | CI job is a stub behind `PREVIEW_DEPLOY_TOKEN` | `docs/60` §11 item 6 |

## 9. Assumptions

| Id | Assumption | Depends on | Effect if wrong |
|---|---|---|---|
| OP-1 | Support hours and the out-of-hours S1 window in §3.2–§3.3 are acceptable to the owner | `docs/15` §4 question 3 | Rewrite §3 to the owner's figures; if the 1-hour clock is kept, a second operator precedes any paid customer |
| OP-2 | The quarterly terms re-retrieval (§5.2) is the right cadence; the repo names none | Owner leaving the row in | A different cadence is one cell |
| OP-3 | The nightly M-11 audit is "in build 2026-09-26" per the coordinator's brief and not yet running; this document does not describe its output format | The building lane's handback | §7.1 item 4 and §8 row 2 are updated with the job's name and where its result is read |
| OP-4 | ~~`biweekly-ish` and `weekly poll; …` resolve to the weekly bucket by substring match~~ **Verified 2026-09-26** by calling `bucket_for_cadence` on all sixteen connector cadences: the buckets in §2.1 are the ones the code returns | — | — |
| OP-5 | No retention job exists (grep for a consumer of `retention_class` or the DA-10 ages found none) | A search of `services/`, `infra/`, `pipeline/` | §2.4 and §8 rows are struck; §7.3 item 8 becomes a check, not a manual purge |
| OP-6 | The weekly pipeline report (`docs/33` §7.3, G-11) has no generator yet; it is an agent-drafted document until one exists | `docs/33` | None on this document |

## 10. Change history

| Date | Change |
|---|---|
| 2026-09-26 | v1: SLAs labelled configured/measured/target; support model for a solo owner with proposed hours; DQ operations from `dq.py`, the scheduler's health writer and the admin screen; overdue owner actions and the dated calendar; social cadence and an empty channel register; daily/weekly/monthly/quarterly checklists; the not-yet-operable table |
