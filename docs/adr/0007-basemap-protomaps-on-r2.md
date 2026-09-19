# ADR 0007 — Basemap tiles: self-hosted Protomaps PMTiles on Cloudflare R2

**Status:** Accepted · 2026-09-15 · devops-engineer; decided by the owner in `docs/00-PLAN.md`
(2026-09-14/15 entries)
**Supersedes:** none · **Superseded by:** none
**Related:** `docs/40-launch-runbook.md` §2.7 (the owner's zoom report and the found gap);
`docs/04-standards.md` D-13 (basemap tiles "self-hosted or from a provider whose terms permit
commercial use, with attribution"); `docs/adr/0005-hosting-and-iac.md` (hosting/IaC posture this
follows: Cloudflare R2 for zero-egress object storage, already the account fronting
`docs/60-deployment.md`'s R2 usage); `docs/20-architecture.md` §14 (cost estimate).
**Implemented by:** `infra/terraform/storage.tf` (tiles bucket), `infra/scripts/build_basemap.sh`,
`.github/workflows/basemap.yml`, `infra/compose/.env.example`/`docker-compose.yml`
(`MAP_TILE_URL`).

## Context

The prototype's map loads raster tiles directly from `tile.openstreetmap.org`. That host's usage
policy explicitly forbids production use at any real traffic volume and already rate-limits or
blocks the app during testing (`docs/40` §2.7's own words: "when the tiles are blocked the map is
a beige outline with clusters on it and zooming looks like it does nothing"). `docs/04` D-13 sets
the actual requirement: tiles must be "self-hosted or from a provider whose terms permit commercial
use, with attribution." The owner decided the specific answer on 2026-09-14/15 (`docs/00-PLAN.md`):
Protomaps PMTiles, self-hosted on Cloudflare R2, served at `https://tiles.infraque.com/basemap.pmtiles`,
with the web app reading a `MAP_TILE_URL` environment variable. This ADR is that decision's record
and the implementation it authorises, per `docs/adr/0001` rule 3 (hosting/infra choices need an
ADR) — this one is narrow (a single asset pipeline, not a new hosting posture) but material because
it is expensive to reverse in the same sense a "beige outline" launch-day failure would be.

## Options considered

| Option | Cost at MVP | For | Against |
|---|---|---|---|
| **Protomaps PMTiles, self-hosted on Cloudflare R2** (chosen) | ~$0.06/mo storage (measured, see Consequences), $0 per-view egress | No per-view fee, no third-party API key or rate limit on the critical path, fits ADR 0005's "own the Cloudflare account already fronting bankablehq.com" posture; PMTiles serves tiles via plain HTTP range requests, so R2 (already used for snapshots/exports) needs no new infrastructure kind, only a second bucket; one static file to refresh, not a running tile server | The operator owns the monthly refresh job instead of a vendor; a stale basemap is not a data-quality bug like a stale connector row, but is still worth monitoring (`docs/40` §2.7's done-check: `map.basemap_failed` counter stays at zero) |
| MapTiler hosted tiles | Free tier, then usage-based (~$0-50/mo range at MVP traffic) | Zero build/refresh pipeline; managed | A vendor API key on the page-load critical path; free tier request caps are a real risk at any traffic spike (the exact failure mode this ADR is trying to avoid, just a different host than OSM's) |
| Stadia Maps hosted tiles | Free tier (non-commercial) then paid | Same appeal as MapTiler | Same dependency risk; Stadia's free tier explicitly excludes commercial use, so MVP traffic needs a paid plan sooner than MapTiler's |
| CARTO free basemaps | Free tier | Attractive default style | CARTO's free basemap tiles are documented for use with CARTO's own platform/APIs; their terms need a specific commercial-use check before relying on them for an unrelated product's public map — not verified here, flagged rather than assumed clear |
| Keep `tile.openstreetmap.org` | $0 | No work | Explicitly against the OSM Foundation's tile usage policy for a production app; this is the bug being fixed, not an option |

## Decision

Self-host a Protomaps PMTiles basemap on a dedicated, public Cloudflare R2 bucket
(`infra/terraform/storage.tf`'s `cloudflare_r2_bucket.tiles`, separate from the private
raw-snapshot bucket — public and licence-gated content must never share a bucket). Extract one
archive covering the contiguous US plus Great Britain (`--bbox=-125,24,1.85,60.9 --maxzoom=12`,
sourced from the weekly Protomaps planet build at `https://build.protomaps.com/<date>.pmtiles`) via
`infra/scripts/build_basemap.sh`, upload it to a dated key, then promote that key to the canonical
`basemap.pmtiles` name the web app's `MAP_TILE_URL` points at. Refresh monthly via
`.github/workflows/basemap.yml` (plus on-demand `workflow_dispatch`). Serve it at
`https://tiles.infraque.com/basemap.pmtiles` once the R2 custom-domain step (a manual, one-time
dashboard action — see `storage.tf`'s comment block; the pinned Cloudflare provider does not
support that resource, `versions.tf`) is done.

One file, not a per-region split. The build brief allowed either a single covering bbox or two
files (`basemap-us.pmtiles`, `basemap-gb.pmtiles`) if one bbox proved too large. Measured (see
Consequences): a single bbox spanning both regions at `--maxzoom=12` is 3.9 GB, extracted from the
remote planet file via HTTP range requests in ~28 seconds of wall-clock time in this task's
sandbox. That is not "too large" against a storage cost of ~$0.015/GB-month — the two-file split
would only save ~$0.03/month while adding a second `MAP_TILE_URL`-shaped question for the frontend
lane (which region's file to load, or a multi-source map style) that the owner's decision text does
not ask for. PMTiles' whole design point — the browser fetches only the byte ranges for tiles in
its current viewport, never the whole archive — means the extra ~1.7 GB the combined bbox carries
over the sum of two separate regional extracts (empty ocean/Canada/Greenland coverage inside the
bounding rectangle) costs nothing at request time, only a little extra monthly storage and refresh
time.

## Licence note

Protomaps basemap tiles are built from OpenStreetMap data (plus Natural Earth for some
low-zoom layers). Under OSM's ODbL §4.6, a rendered map image (or, as here, a vector tile set
consumed only for rendering) is a **produced work**: distributing it requires attribution but does
not itself make the *consuming* database (Bankable's own proposal/opportunity records) a
derivative of OSM's database. The map must show "© OpenStreetMap contributors" (`docs/04` D-13's
citation, `www.openstreetmap.org/copyright`) wherever the basemap renders — this is a UI
requirement for whichever lane wires `MAP_TILE_URL` into `web/`, not something this task's
infrastructure change can enforce in code.

This does **not** reopen the owner's separate, already-settled decision to keep OSM *data* out of
the platform's own context/geocoding layer (`CLAUDE.md`'s guardrails do not name OSM, but
`docs/00-PLAN.md`'s prior decision on this stands independent of this ADR): rendering a basemap
image from OSM-derived tiles is a different act, under a different clause of the same licence, than
ingesting OSM's raw geographic database as a data source. If that data-layer decision is ever
revisited, it does not need to touch this ADR, and vice versa.

## Consequences

- **Cost:** the tiles bucket stores ~3.9 GB (measured: a real, non-dry-run `pmtiles extract` of
  `--bbox=-125,24,1.85,60.9 --maxzoom=12` against the 2026-09-14 planet build produced a
  3,892,675,048-byte archive) — at R2's $0.015/GB-month, ~$0.06/month, plus negligible Class A/B
  operation costs for one upload and one copy per month. This is inside `docs/20` §14's existing
  object-storage line (`docs/60` §4: "$3-6/mo... ~200 GB"), not a new budget item. No per-view fee:
  the whole point of self-hosting on R2 versus a hosted tile API.
- **Monthly refresh is now an operational duty.** `.github/workflows/basemap.yml` runs on a cron
  against the `production` GitHub Environment's R2 secrets; a failed run leaves `basemap.pmtiles`
  unchanged (the dated-key-then-promote upload order means a bad build never partially overwrites
  the live file), so a missed or failing refresh degrades to "the map is a few weeks stale," not
  "the map is broken." `docs/40` §2.7's done-check ("the `map.basemap_failed` counter on the admin
  ops page stays at zero after the deploy") is the signal to watch for a worse failure mode; the
  counter itself is `web/`'s to implement, out of this task's write scope.
- **No per-view API key or rate limit on the page-load critical path** — removes the failure mode
  this ADR exists to fix (a third-party host blocking or throttling the app), whether that host is
  OSM's own tile server or a commercial provider's free tier.
- **The custom domain and CORS policy are manual, one-time dashboard/API steps per environment**
  (`infra/terraform/storage.tf`'s comment block), because the pinned Cloudflare Terraform provider
  (`~> 4.36`, `versions.tf`) does not expose `cloudflare_r2_custom_domain` or R2 bucket CORS as
  resources — both are v5-provider-only, confirmed against the v4.52.0 (latest v4) schema docs.
  Upgrading the provider pin to replace these manual steps with code is a separate decision
  (`versions.tf`'s pin is itself an ADR 0005 artefact), not bundled into this one.
- **Reversal cost: low.** Switching to a hosted provider (MapTiler/Stadia) later is a `MAP_TILE_URL`
  value change plus retiring the refresh workflow — no schema or data-model impact, since the
  platform's own database never stores OSM data (see the licence note above).
