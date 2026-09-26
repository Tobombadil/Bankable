# Platform posture: noncommercial for now, and the machinery that makes the switch a flip

Phase 2 architecture/data doc. Owner: backend-developer (posture lane). Status: v2, 2026-09-26 (§3 precondition
(i) landed; (ii) withdrawn). Companion to `13-legal-data-rights.md` §0/§6 (the `noncommercial` class),
`21-data-model.md` §3.19/§8 (the vocabulary and the gating table) and the `docs/00-PLAN.md` decisions log (two
rows dated 2026-09-25: the Texas lane's records the owner's decision and what it settles for
`us.tx.rrc.class_vi`; this lane's records the machinery below and the three preconditions — plus the 2026-09-26
row recording the owner's "Mark inactive, then flip" decision on precondition (i) and the withdrawal of (ii)).

## 1. What the posture is

**The owner's decision, 2026-09-25, verbatim:** "Let's move forward as a noncommercial platform for now for
maximum and best data access. Then decide how to proceed once we're done."

The posture is a single setting, **`PLATFORM_POSTURE`**, with two values:

| Value | Reuse classes the gates admit | What that means |
|---|---|---|
| `commercial` (**default**) | `open`, `attribution` | Exactly the behaviour before 2026-09-25. What every deployment gets when the variable is unset, empty, or anything other than the two values below (a typo fails closed here). |
| `noncommercial` | `open`, `attribution`, **`noncommercial`** | Sources whose terms permit noncommercial reuse only (CC BY-NC; state-site grants such as the RRC's) are ingested and published, with attribution and a link back, as `attribution` sources are. |

`restricted` and `unknown` are gated under both. Nothing about a posture is a tier: the free tier still sees
every record live; the posture only changes *which licences may leave the building at all* (docs/21 §8).

It is read the way `SENDER_LEGAL_NAME` and `MAP_TILE_URL` are read — `os.environ.get` at the point of use,
stripped and case-folded (`services/posture.py`; there is no settings object in this codebase and this change
does not add one) — **once per process, at import**. `services/api/visibility.py` (the API predicate),
`pipeline/connectors/registry.py` (the connector and loader gate), `services/api/coverage.py` (the withheld-source
statement), `services/social/editorial.py` (the posting rule) and `services/api/app.py` (health) each compute
their class set from it on start-up, so a running process never has half its queries under one posture. Flipping
the value therefore means: set it, restart the API, the ingest runner and the workers.

**The default stays `commercial` until the owner flips it, after precondition (i) below is met.** Merging this
change alters no behaviour anywhere.

Where it shows: `GET /v1/health` carries `posture` and `posture_statement` (read-only; there is no endpoint
that changes it), and `/about#tiers` and `/methodology` print that sentence — the API's sentence, not the
template's, so a page can never claim a posture the gate is not applying. The sentence under `noncommercial`:

> This platform operates under a noncommercial posture: sources that permit noncommercial reuse are published
> with attribution; they will be withdrawn if the posture changes.

## 2. What it unlocks

The `noncommercial` reuse class exists as of migration `0020` (vocabulary + a CHECK that a `noncommercial`
licence has `allows_commercial_use = false`), in `data/sources.yaml`'s `reuse` vocabulary
(`scripts/check_manifest_licences.py` accepts it against a `noncommercial` register row in docs/13 §6), and in
every gate. **No source carries it yet.** Assigning it is per-source work with the terms in front of the
reviewer — the row in docs/13 §6 moves first, the manifest second — and is not part of this change.

Measured 2026-09-25 against the committed manifest: of the **24** entries recorded `reuse: unknown`, **none**
has a `license` text containing "noncommercial", "non-commercial", "NC" or "research"; the terms of most of
them were never retrieved (`docs/13` §6 "not retrieved"), which is why they are `unknown` rather than
misfiled. The class will be applied source by source as terms are read, starting with:

- `us.tx.rrc.class_vi` — RRC Site Policies grant "permission to copy and distribute the information on its
  website for noncommercial use, as long as the content remains unaltered and is not presented in a misleading
  way" (read 2026-09-25, `docs/00-PLAN.md` decisions log, Texas lane). Connector built (`02e05fc`), gated as
  `unknown` until its docs/13 row and manifest entry are moved to `noncommercial`.
- *Placeholder for the source-map lane's §11 findings:* the other five Class VI primacy states
  (`us.az.adeq`, `us.la.dce`, `us.nd.dmr`, `us.wv.dep`, `us.wy.deq`) and any further register whose terms turn
  out to grant noncommercial reuse. Each gets its own docs/13 §2 quote and §6 row before the manifest changes.
- `global.gem.trackers` TZ-ID rows (CC BY-NC 4.0, docs/13 §2.2), currently dropped at ingest; a candidate for
  a separate `noncommercial` source id under this posture, not a reclassification of the CC BY parent.

## 3. The three preconditions the coordinator is putting to the owner (open items)

Written here in the coordinator's words; none is implemented by this change and none should be assumed met.

**(i) The posture must be true, not a label.** The pricing page, checkout and paid tiers must be suspended or
plainly marked inactive while the posture is `noncommercial`, because a platform with a live "Get the plan"
button claiming noncommercial use is a false claim, and worse than the current position.

*Status, 2026-09-26: satisfied.* Owner decision (`docs/00-PLAN.md` 2026-09-26 decisions log), verbatim: "Mark
inactive, then flip — keep the pricing page visible with a 'paid tiers not currently offered' notice; disable
checkout; flip the posture once ready." Gated on this precondition being read from the posture itself, not a
second setting, so a deployment can never have the posture flipped without the paid surfaces following:

- `GET /pricing` (`web/pricing.py`) reads the posture the way `/about` and `/methodology` do —
  `web/page.py::get_platform_posture`, which asks `GET /v1/health`'s `posture`/`posture_statement` fields rather
  than the environment. Under `noncommercial` it prints a notice near the top (the API's own sentence plus one
  page-owned sentence, never the template's own claim about the posture) and every tier card stays visible but
  marked inactive, with no checkout button or form. Under `commercial`, the default, nothing changes.
- `POST /pricing/checkout` re-renders the same page, `200`, instead of posting to the API — a courtesy backstop,
  not the gate itself, since the button that would reach it is already gone from the page.
- `POST /v1/billing/checkout` (`services/billing/router.py`) is the actual gate: a module-level constant,
  `PAID_TIERS_ACTIVE = platform_posture() != "noncommercial"`, computed once at import exactly the way
  `services/api/visibility.py`'s `PUBLISHABLE_REUSE_CLASSES` is, refuses every request with a new
  `403 paid_tiers_inactive` problem response while it is `noncommercial`, before the plan or seats are even
  parsed. `api/openapi.yaml` documents the response on the existing operation (no operation added or removed).
- `POST /pricing/portal` and `POST /v1/billing/portal` are deliberately untouched by all of the above: managing
  a subscription an account already holds is not selling a new one, and the owner's decision was to stop
  *offering* paid tiers, not to strand anyone already on one. Both routes' docstrings say so.

`tests/test_platform_posture.py`+`web/test_pricing.py`+`services/billing/test_router.py` between them pin: the
`commercial` behaviour is unchanged (checkout still succeeds, no notice, no inactive marking); the `noncommercial`
notice, inactive marking and refusal; an existing subscriber keeps the "manage billing" branch on the tier they
hold under either posture; `POST /pricing/portal`/`POST /v1/billing/portal` succeed under both.

**(ii) Counsel must confirm the entity can hold the status.** A pre-revenue LLC (Compass International Trading
Group LLC, `docs/00-PLAN.md` 2026-09-18) whose stated purpose is to feed a commercial deal workflow must be
confirmed able to hold noncommercial status under CC BY-NC's definition — "not primarily intended for or
directed towards commercial advantage or monetary compensation" — and under undefined state-site grants like the
RRC's, which define neither the purpose nor the user.

*Status: withdrawn by the owner, 2026-09-26 (`docs/00-PLAN.md` decisions log).* No longer a precondition to
flipping the posture; nothing in this document's runbook or tests depends on it having been satisfied.

**(iii) A firewall.** No `noncommercial` row may flow to the Bankable deal workflow or any downstream commercial
use. What enforces that today, and what does not:

| Path | Enforced today? | By what |
|---|---|---|
| Public site, `GET /v1/*` records, events, assets, feeds | yes | the visibility predicate (`services/api/visibility.py`): under `commercial` the class is gated on every non-admin tier; under `noncommercial` it publishes to every non-admin tier alike, which is the point — and the reason (i) must hold first |
| Alerts, saved searches, webhooks | yes | `services/alerts/*` compose the same predicate (`services/alerts/visibility.py`, `feed.py`) |
| Social posts | yes | `services/social/editorial.py`'s class set is the posture's, and the worker reads events through the `api` predicate |
| Bulk export, CSV | yes, vacuously | no export or bulk route exists (`docs/41`, `web/pricing.py`); at load a `noncommercial` licence is written `allows_bulk_export = false` and `allows_api_redistribution = false` so the day one is built the flags are already against it — but **nothing reads those flags yet**, so they are a statement, not a gate |
| CRM (Attio) lead signals | yes, vacuously | `services/crm/signals.py` maps events to lead signals but the worker that would send them "is a later wave"; when it lands it must compose the predicate or filter on `licence.reuse_class`, and this row is where that requirement is recorded |
| Admin surfaces | **no** | admin reads bypass the predicate by design (docs/21 §5.4); an operator can see and copy a `noncommercial` row. Acceptable for operations; not a commercial channel, but not a firewall either |
| The Bankable deal workflow at bankablehq.com | **no mechanism** | no integration exists (`CLAUDE.md`: a later consideration, not a foundation). If one is built, it must consume the `api` entitlement through the predicate, never the store — and under the `noncommercial` posture it must not be built at all |
| `Licence.allows_commercial_use` as a gate | **not load-bearing** | measured 2026-09-25: the field is serialised (`services/api/serialize.py`) and shown on `/about`, and nothing filters on it. The loader writes it `true` only for `open`, `false` for every `attribution` licence (because manifest `attribution` covers `attribution-restricted` register rows too), so it cannot be the gate: it would hide NESO and CC BY sources. The posture gates on the **class**; the CHECK from 0020 keeps the field consistent with the class in the one direction that is a definition |

## 4. Why the class, and why the CHECK, rather than a flag on the row

Every row ingested under a noncommercial grant is a liability at the switch back unless it is tagged now, and
the tag has to be the thing the gates already read: `licence.reuse_class`, copied onto every observation row at
fetch time (docs/21 invariant L2), and the `min_reuse_class` each record carries. A boolean beside it would be a
second thing to keep in step. `allows_commercial_use` already existed on `licence`; a `noncommercial` grant is,
by definition, a licence with that flag false, so migration 0020 pins `reuse_class = 'noncommercial' ⇒
NOT allows_commercial_use` as a CHECK, the loader writes it so, and the admin reclassification to
`noncommercial` clears the flag on the new licence row rather than tripping the constraint. The converse
(`allows_commercial_use = false ⇒ noncommercial`) is deliberately not constrained — see the last row of the
table above — and no admin route accepts the field, which `tests/test_platform_posture.py` pins.

## 5. Runbook: switching back to `commercial`

1. **Flip the setting.** Set `PLATFORM_POSTURE=commercial` (or unset it) on the API, ingest runner and worker
   hosts; restart them. The change is a single environment variable per host; the compose `.env` is where it
   lives (`infra/compose/.env.example` should carry the key with its default — a one-line follow-up outside this
   lane's write scope).
2. **Every `noncommercial` row is now invisible on every non-admin surface.** That is the predicate:
   `PUBLISHABLE_REUSE_CLASSES` is recomputed on start-up as `("open", "attribution")`, so records, events,
   assets, feeds, alerts and posts all drop the class at once. No data changes; `GET /v1/health` reports
   `posture: commercial` and the `/about` and `/methodology` sentence changes with it. Verify with
   `curl /v1/health` and one `GET /v1/proposals?source_id=<a noncommercial source>` returning an empty page.
3. **The connector gate closes.** `GATED_REUSE` now contains `noncommercial`; the next scheduled run of any
   such source refuses (`GateViolation`) and writes nothing publishable. Disable the schedule or leave it to
   refuse; either is safe.
4. **Then the purge-or-relicence decision, per source.** List what is held:

   ```
   python scripts/posture_report.py --database-url "$DATABASE_URL"
   ```

   which prints, read-only, for every table carrying a `licence_id` (`proposal_source`, `opportunity_source`,
   `event`, `asset`, `asset_owner`, `location`, `organization_alias`, `document`, `extraction`, `snapshot`):

   ```
   SELECT source_id, count(*) FROM <table> JOIN licence ON licence.id = <table>.licence_id
    WHERE licence.reuse_class = 'noncommercial' GROUP BY source_id
   ```

   plus the `noncommercial` licences themselves and the sources pointing at them. For each source the owner
   decides: **relicence** (a commercial licence is obtained; the reviewer writes a new `licence` row per invariant
   L2 and the admin reclassification repoints the source — historical rows keep the licence they were fetched
   under and stay invisible; a re-ingest writes new rows under the new licence), or **purge** (the source's rows
   are deleted through the redaction procedure; `docs/21` §1 grants no `DELETE` outside it). Nothing here is
   automated on purpose.
5. **Only then, optionally, `alembic downgrade 0019`.** Migration 0020's downgrade **refuses** while any row in
   `licence`, `proposal` or `opportunity` carries the class, naming the counts, so a rollback cannot drop the
   tag from rows that are still held. There is no need to downgrade at all: the class in the vocabulary is
   harmless under `commercial`.

Switching *to* `noncommercial` is the same first step in the other direction, taken only after precondition (i)
is met and recorded in the decisions log; steps 3–5 have no forward counterpart, because widening admits rows
that were never ingested rather than un-hiding held ones (a `noncommercial` source loads on its next run).

## 6. Tests that pin this document

- `services/test_posture.py` — the helper at 100%: default, both values, garbage fails closed, the sentence.
- `tests/test_visibility_predicate.py` — the gate module executed under both postures and a typo; the licence
  clause byte-identical across tiers under `noncommercial`; the drift guard.
- `tests/test_platform_posture.py` — registry and predicate cannot disagree under any setting; a
  `noncommercial` row invisible on every tier by default, visible on every tier under the widened set, gone
  again when narrowed; `restricted`/`unknown` (PJM named) stay gated under `noncommercial`; the class↔flag CHECK,
  the loader's licence row, the admin reclassification; health.
- `tests/test_migration_0020.py` — the SQLite round trip including the refused downgrade; the PostGIS round
  trip is recorded in `docs/CHANGELOG.md` for this revision.
- `tests/test_posture_report.py`, `tests/test_manifest_licences.py` (the register class), `web/test_platform_posture.py`
  (the sentence is the API's).
- §3 precondition (i), 2026-09-26: `web/test_pricing.py` (the notice, the inactive marking, no checkout
  button/form, `commercial` unchanged, an existing subscriber keeps "manage billing", `POST /pricing/portal`
  unaffected) and `services/billing/test_router.py` (`PAID_TIERS_ACTIVE` computed at import under every
  `PLATFORM_POSTURE` value; `POST /v1/billing/checkout` refuses `403 paid_tiers_inactive` under `noncommercial`,
  still requires a session first, still succeeds under `commercial`; `POST /v1/billing/portal` unaffected).
