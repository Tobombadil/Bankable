# 66 — Owner actions: the desk list

**Status:** live list, 2026-10-22. Only the owner can do these: each needs a person's account, signature, browser
or judgement. Agents keep this list current and never do the items themselves. Tick an item by telling the
coordinator; it is then moved to "Done" with the date.

**Secrets** go into GitHub, never into chat: [add a repository secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new) (Tobombadil/Bankable → Settings →
Secrets and variables → Actions → New repository secret). Detail lives in the linked docs; this page is the short
version. Doc links point at the working branch until it merges to `main`.

## Run sheet: Saturday evening and Sunday morning (2026-10-24 and 25)

Slow replies first (every day they wait is a day lost), then the instant ones. **Send back** says what the
coordinator needs from each; paste text in chat, never a key or password.

**Evening, about two hours**

| Order | Item | Links | Send back |
|---|---|---|---|
| 0a | ~~Decide: SPP's queue under the noncommercial reading~~ Done 2026-10-24: yes | [`docs/07` §3.3](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/07-terms-sweep-2026-10-24.md) | "yes" or "no" (about 3,000 SPP queue rows, derived fields only, while the site is noncommercial; a connector is then built) |
| 0c | GitHub: limit the `production` environment to `main` (2 min). Without this, anything that can push a branch could run a workflow that reads every production secret (`docs/44` §6) | [Environments settings](https://github.com/Tobombadil/Bankable/settings/environments) → `production` → Deployment branches and tags → Selected branches and tags → add `main`; also check Settings → Actions → General → fork pull request workflows: leave "Send write tokens" and "Send secrets" unticked | "done" |
| 0b | Site finder step (a): Geofabrik's page and robots.txt, **before 2 November** (the first national grid build runs then; our fetcher cannot reach the site) | [download.geofabrik.de](https://download.geofabrik.de/), [robots.txt](https://download.geofabrik.de/robots.txt); what to compare is in step (a) below | one line, e.g. "Geofabrik checked: page and robots match", or what differs |
| 1 | #8 PJM licence request | draft in [`docs/37` §2](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/37-pjm-data-licence-request.md); email custsvc@pjm.com, signed copy by post to General Counsel, PJM Interconnection, 2750 Monroe Blvd., Audubon, PA 19403 | "sent", with the date |
| 2 | #2 MISO terms | [Data Exchange](https://data-exchange.misoenergy.org) (pricing API → Terms of Use), [Legal and privacy](https://www.misoenergy.org/meet-miso/legal-and-privacy/), [Terms and conditions](https://www.misoenergy.org/meet-miso/contact-us/terms-and-conditions/) | the PDFs, or the clauses pasted |
| 3 | #23 SPP and ISO-NE permission letters | drafts in [`docs/37` §5](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/37-pjm-data-licence-request.md): SPP by post to SPP Communications, 201 Worthen Drive, Little Rock, AR 72223-4936, copy to communication@spp.org; ISO-NE to info@iso-ne.com | "sent", with the date |
| 3b | #24 Louisiana DCE letter (Class VI applications, the largest primacy state) | draft in [`docs/35`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/35-louisiana-dce-data-request.md) | "sent", with the date |
| 4 | #16 GEM download, #17 GEM email | [GIPT page](https://globalenergymonitor.org/projects/global-integrated-power-tracker) ("Download data"); email info@globalenergymonitor.org ([contact page](https://globalenergymonitor.org/contact-us)), draft in [`docs/06` §1.8](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/06-terms-gem-and-parcels.md) | the downloaded file (attach or say where it is), "sent" |
| 5 | #18 Parcel states | Wisconsin wlip@wisconsin.gov or help@sco.wisc.edu, and read [SCO's parcels page](https://www.sco.wisc.edu/parcels/data/) (our fetcher fails on it); Florida FloridaGIO@floridadep.gov; Ohio gis.support@das.ohio.gov; Virginia VBMP@VDEM.Virginia.gov; wording in [`docs/06` §2.8](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/06-terms-gem-and-parcels.md) | "sent"; later, each reply |
| 6 | #19 to #21 and #11 Terms pages only a browser can read | in order of value: [Our Grid Future](https://ourgridfuture.org/) (`docs/62` §9, with its stop rule), [Wyoming DEQ Class VI](https://deq.wyoming.gov/water-quality/groundwater/uic/class-vi/), [Illinois ICC CO2 pipelines](https://www.icc.illinois.gov/programs/carbon-dioxide-pipelines), Minnesota eDockets and South Dakota's disclaimer (`docs/62` §13 and §14), [DOE OCED portfolio](https://www.energy.gov/cmei/oced/portfolio), [Argonne RNG database](https://www.anl.gov/esia/reference/renewable-natural-gas-database) and [notice](https://www.anl.gov/privacy-security-notice), [IEA CCUS database](https://www.iea.org/data-and-statistics/data-product/ccus-projects-database) and [terms](https://www.iea.org/terms) (read signed out; do not create an IEA account yet, registering accepts their terms), [Sodir FactPages](https://factpages.sodir.no/), [Arizona DEQ UIC](https://azdeq.gov/UIC) (low value); every link and what to save is in [`docs/62`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md) | per page: a PDF, or the clauses on use, copying, redistribution, commercial use and credit pasted |
| 7 | #7 Utility RFP letters | twelve drafts in [`docs/39`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/39-utility-rfp-permission-requests.md), send kit §0 first | "sent" per issuer |

**Morning, about an hour**

| Order | Item | Links | Send back |
|---|---|---|---|
| 8 | #10 Free API keys | [EIA](https://www.eia.gov/opendata/register.php), [SAM.gov](https://sam.gov/content/home) (Account Details → Public API Key), [NRC ADAMS](https://adams-api-developer.nrc.gov), [api.data.gov](https://api.data.gov/signup/), [Ember](https://ember-energy.org/data/api/) (secret `EMBER_API_KEY`; global figures, then read the first run's World and US figures once, `docs/26` §10) | each key as a [GitHub secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new), then the secret names in chat |
| 9 | #1 ERCOT account | [ERCOT API explorer](https://apiexplorer.ercot.com) | "added the three ERCOT secrets" |
| 10 | #15 TxGIO licence question | [TxGIO contact](https://www.geographic.texas.gov/contact.html) | "sent"; later, the reply |
| 11 | #12 Elexon question | [Elexon contact](https://www.elexon.co.uk/contact/) | "sent"; later, the reply |
| 12 | Decisions: the three from the filter audit; the map's faint infrastructure contrast (`docs/31-brand-identity.md` §7 M-2); the terms sweep's rule for state sites that publish no terms (attribution-restricted, derived-only, held before paid use, as NYISO); whether to read EIA-860M's 2015 to 2026 archive from Catalyst Cooperative's public Zenodo copy (about 1.16 GB once; trend lines then start in 2015); and whether ERCOT's five "MWH" fuel-code projects read as storage (all are named "... Storage") | [`docs/28` §11](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/28-filter-and-tag-audit.md), [`docs/07` §2](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/07-terms-sweep-2026-10-24.md) | one line each in chat |
| 13 | #3 Deploy, if the coordinator says the next release is ready | [deploy-beta](https://github.com/Tobombadil/Bankable/actions/workflows/deploy-beta.yml) | "deployed" |

## Now (unlocks work already being built)

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 1 | **Register the ERCOT Public API account** at [apiexplorer.ercot.com](https://apiexplorer.ercot.com): Sign In/Sign Up → verify email → **Products** → subscribe to the public API product → **Profile** → Show the **Primary key**. Use a password used nowhere else. Then [add three secrets](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new): `ERCOT_API_SUBSCRIPTION_KEY` (the primary key), `ERCOT_API_USERNAME` (your email), `ERCOT_API_PASSWORD`. ERCOT's terms bar accounts registered by bots, so this must be you. ([ERCOT's registration guide](https://developer.ercot.com/applications/pubapi/user-guide/registration-and-authentication/)) | 10 min | ERCOT live and node prices beyond the public downloads | [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md) |
| 2 | **Get MISO's two sets of terms** (a security challenge blocks our fetcher). (a) **The API terms**, which govern prices: sign in to [MISO Data Exchange](https://data-exchange.misoenergy.org) with a MISO website account (Sign up links the two), open **Products**, pick the pricing API (real-time and day-ahead LMP), and open the **Terms of Use** linked beside the "I agree" box; copy the whole text. Don't subscribe yet. ([MISO's Data Exchange user guide](https://cdn.misoenergy.org/MISO%20Data%20Exchange%20User%20Guide674561.pdf?v=20250124142839) shows the screens.) (b) **The website terms**, which govern report downloads and the queue: [Legal and privacy](https://www.misoenergy.org/meet-miso/legal-and-privacy/) and [Terms and conditions](https://www.misoenergy.org/meet-miso/contact-us/terms-and-conditions/). Save each as a PDF and paste the clauses on using, copying, redistributing or republishing market data, commercial use and attribution. | 20 min | MISO hub prices and queue rows, public or internal | [`docs/62` §8](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md#8-miso-legal-and-reuse-terms) |
| 3 | **Deploy when told.** When the coordinator says a release is ready: open [deploy-beta](https://github.com/Tobombadil/Bankable/actions/workflows/deploy-beta.yml) → **Run workflow** → action `deploy`, tag empty → **Run workflow**. | 2 min | Each finished feature reaching [inframation.io](https://inframation.io) | [`docs/60`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/60-deployment.md) |

## Site finder go-live (Texas first)

Added 2026-10-23 (lane sf-golive; corrected the same day after review). The site finder needs three data builds
that run on GitHub's machines, then one deploy. About 30 minutes of clicks, up to about 3 hours of waiting.
**Start when the coordinator says the release is ready**, except step (a), which comes first. Detail:
[`docs/25` §2.7, §5, §7](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/25-grid-interconnection-and-siting.md).

**The worker's memory.** The Texas run peaked at 1.2 GB, and the server's worker is limited to 1 GB until the
coordinator raises it to 3 GB (`infra/compose/compose.single.yml`). Until that change is deployed, the server
pulls and loads the data but does not start the site finder run: it logs `site_finder_refresh: not run: the
worker's memory limit is 1024 MB, below the 2048 MB a run needs`, and step (e) stays `false`. Nothing else is
affected. Ask the coordinator whether the release holds the memory change before you start (d).

**No new secret.** Every workflow below runs in the GitHub Environment `production` and reads only names that are
already there: deploy-beta deployed the site with them (Done, 2026-10-22), and the buckets are the two it created.

| Workflow | Secrets it reads | Variables it reads | Already set? |
|---|---|---|---|
| grid-layers.yml | `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`; optional `R2_ACCOUNT_ID`, `R2_TILES_BUCKET` | `DOMAIN`, `CLOUDFLARE_ACCOUNT_ID`; optional `R2_ACCOUNT_ID`, `R2_TILES_BUCKET` | Yes. Unset, the optional ones fall back to `CLOUDFLARE_ACCOUNT_ID` and the bucket `beta-object-storage-tiles-production` |
| flood-layer.yml | `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`; optional `R2_ACCOUNT_ID`, `R2_BUCKET` | `CLOUDFLARE_ACCOUNT_ID`, `DOMAIN` (default inframation.io); optional `R2_ACCOUNT_ID`, `R2_BUCKET` | Yes. Unset, `R2_BUCKET` falls back to `beta-object-storage-production` |
| prospect-layers.yml | as flood-layer.yml | `CLOUDFLARE_ACCOUNT_ID`; optional `R2_ACCOUNT_ID`, `R2_BUCKET` | Yes, same fallbacks |
| parcels-layer.yml | as flood-layer.yml (the dry run reads none) | `DOMAIN` (default inframation.io), `CLOUDFLARE_ACCOUNT_ID`; optional `R2_ACCOUNT_ID`, `R2_BUCKET` | Yes, same fallbacks |
| deploy-beta.yml | the `docs/64` §2 list | the `docs/64` §2 list, plus the new optional variables `GRID_LAYERS_REGIONS` and `PARCELS_COUNTIES` | Yes. Leave `GRID_LAYERS_REGIONS` **unset**: the deploy then sets `US-TX`. `all` is accepted with a warning: national does not fit this server. Leave `PARCELS_COUNTIES` unset too: the deploy then sets `all` (step (f)) |

- [ ] **(a) Confirm Geofabrik's page and robots.txt (5 min). Do this before the coordinator merges the release to
  `main`:** from then on grid-layers.yml also runs a national build by itself, on the 2nd of each month at 06:41
  UTC, and `docs/25` §2.7 limit 4 asks for this check before the first national run. In a browser:
  1. Open [download.geofabrik.de](https://download.geofabrik.de/) and find, word for word, "This open data
     download service is offered free of charge by Geofabrik GmbH." and "License: ODbL 1.0". Compare them with
     [`data/sources.yaml`, row `id: us.osm.power`, field `verified`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/data/sources.yaml#L4149)
     (line 4149 today; search the file for `id: us.osm.power` if lines have moved): the passage that starts
     "Fix round 2: read 2026-10-22 about 23:50 UTC through a fetch service" quotes both. On the same page, note
     whether it says the files do not contain user names, user IDs or changeset IDs (`docs/25` §2.7 limit 4
     reports that sentence on the front page; the register holds only the similar sentence from another page,
     step 3).
  2. Open [download.geofabrik.de/robots.txt](https://download.geofabrik.de/robots.txt). The same passage of
     `verified` says "https://download.geofabrik.de/robots.txt answered HTTP 404 (no robots file)". If the browser
     shows a file instead, copy it whole: what matters is any `Disallow:` line under `User-agent: *` that covers
     `/north-america/` or `.osm.pbf` files.
  3. Optional: open [www.geofabrik.de/data/download.html](https://www.geofabrik.de/data/download.html) and compare
     the two sentences quoted at the end of the same row's `license` field
     ([line 4140](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/data/sources.yaml#L4140)):
     "All OpenStreetMap derived data on the download server is licensed under the Open Database License 1.0 ..."
     and "The raw data files ( .osm.pbf , .osc.gz , .osm.bz2 ) on our public download server ( download.geofabrik.de )
     do not contain these fields".
  4. **Record it** by telling the coordinator one line, for example "Geofabrik checked 2026-10-24: front page and
     robots.txt match", or paste what differs. The coordinator adds it to that `verified` field (date, result,
     "confirmed by the owner in a browser"). The build does not read the field, so (b) need not wait for that
     commit.
- [ ] **(b) Run the three data builds (10 min of clicks; up to about 3 hours of waiting).** They do not depend on
  each other (the flood build reads the LBNL lines, not the OpenStreetMap build), so start all three together. For
  each: open the link, **Run workflow**, keep the branch the coordinator names (`main` once merged), set the inputs,
  **Run workflow**.
  1. [grid-layers](https://github.com/Tobombadil/Bankable/actions/workflows/grid-layers.yml): `states` = `all`
     (the default). **It cannot publish one state**: a subset is uploaded but never promoted to `latest.json`, which
     is all the server reads, so Texas comes from the national build (the server keeps Texas only). Not yet run in
     Actions; the job may take up to its 3-hour limit. Done when the run is green and
     [tiles.inframation.io/grid/osm-power/latest.json](https://tiles.inframation.io/grid/osm-power/latest.json)
     opens.
  2. [flood-layer](https://github.com/Tobombadil/Bankable/actions/workflows/flood-layer.yml): `states` = `TX`,
     `allow_dropped_states` empty. One state builds on its own (a partial build); about 15 to 30 minutes (Texas took
     12 in development). Done when the job "manifest and latest.json" is green. National later: `states` empty,
     about 1.3 hours (estimated). Until the coordinator's change that buffers the OpenStreetMap lines lands (`docs/25`
     §5.8 item 2), this build covers the floodplain near the LBNL lines only, and the finder leaves the other bands
     out as "flood data not computed"; re-run this step once the coordinator says that change is in.
  3. [prospect-layers](https://github.com/Tobombadil/Bankable/actions/workflows/prospect-layers.yml): `region` =
     `texas`, `sources` empty. About 10 minutes. **Expect a red run** while PAD-US and Aqueduct have no file in the
     inbox (step (c)): the other layers still publish, and the step "verify the pointer and its manifest are
     readable" is then skipped (it runs only after a green build step), so do not wait for it. Done when the step
     "build and upload (infra/scripts/build_prospect_layers.sh)" (open the run, then the job, then that step) shows
     the line `[prospect-layers] published prospect/builds/texas/<build_id>/ and moved prospect/current/texas.json to it`,
     or when the Cloudflare dashboard (**R2 Object Storage** > `beta-object-storage-production` > **Objects** >
     `prospect` > `current`) lists `texas.json`. A Texas server reads only this `texas` build, never the national
     one, and only this dispatch moves it (the monthly scheduled run builds `conus`): run it again whenever you
     want fresher prospect layers. National later: `region` = `conus` (it then needs the national PAD-US zip,
     1.5 GB).
- [ ] **(c) Optional: the PAD-US and Aqueduct files (15 min).** Both hosts refuse automated download, so without
  this the site finder shows no protected-land or water-stress values.
  1. PAD-US for Texas: open [the ScienceBase item](https://www.sciencebase.gov/catalog/item/6759abcfd34edfeb8710a004)
     and download `PADUS4_1_State_TX_GDB_KMZ.zip` (261 MB).
  2. Aqueduct: open [WRI's Aqueduct 4.0 data page](https://www.wri.org/data/aqueduct-global-maps-40-data) and
     download `aqueduct-4-0-water-risk-data.zip` (261 MB).
  3. In the [Cloudflare dashboard](https://dash.cloudflare.com), **R2 Object Storage** > bucket
     `beta-object-storage-production` > **Objects**: open or create the folders `prospect` > `inbox` > `padus` and
     upload the PAD-US zip there; then `prospect` > `inbox` > `wri_aqueduct` for the Aqueduct zip (the dashboard
     takes files up to 300 MB). Optional: beside each, a text file named like the zip with `.json` in place of
     `.zip`, that is `PADUS4_1_State_TX_GDB_KMZ.json` and `aqueduct-4-0-water-risk-data.json` (not `.zip.json`),
     holding `{"downloaded_at": "2026-10-24T15:00:00Z"}` (when you downloaded it); without it the file's time is
     used and the record says so.
  4. Run [prospect-layers](https://github.com/Tobombadil/Bankable/actions/workflows/prospect-layers.yml) again,
     `region` = `texas`. It should now be green.
- [ ] **(d) Deploy (5 min), once (b) is done.** Open [deploy-beta](https://github.com/Tobombadil/Bankable/actions/workflows/deploy-beta.yml)
  > **Run workflow** > action `deploy`, tag empty > **Run workflow**. When it is green, run it again with action
  `seed`: that queues the context build, which ends with the four pulls, then the load, then the site finder run
  (about 35 minutes for Texas). Without `seed` the server pulls on its own on the 4th of the month at 02:43 UTC.
- [ ] **(e) Check it worked (about 1 to 3 hours after `seed`).**
  1. Open [api.inframation.io/v1/health](https://api.inframation.io/v1/health): it reads
     `"site_finder": {"computed": true, "segments": ...}` (Texas had 120,699 segments in development). While it
     reads `"computed": false` the run has not finished.
  2. Open [inframation.io](https://inframation.io): the top menu shows **Site finder** after **Map** (it appears
     only once the run has finished), and [inframation.io/site-finder](https://inframation.io/site-finder) lists
     bands along Texas lines.
  3. Still `false` 4 hours after `seed`: run deploy-beta with action `status` and send the coordinator the run's
     summary (queue depths and the seed log). If the worker's log says `site_finder_refresh: not run: the worker's
     memory limit`, the release did not hold the memory change (above): the coordinator ships it, then you run
     (d) again.
- [ ] **(f) Texas land parcels (15 min of clicks; up to several hours of waiting), once (e) shows the site finder.**
  [parcels-layer](https://github.com/Tobombadil/Bankable/actions/workflows/parcels-layer.yml) builds TxGIO's
  county parcel files on GitHub's machines into the private bucket; the server's monthly context build pulls the
  counties `PARCELS_COUNTIES` names (`all` unless you set it) and the site finder run measures them. Until the first
  build the server's log says the parcels are "not published", and nothing fails. The parcel files stay in the
  private bucket `beta-object-storage-production`: never copy them to the tiles bucket or share them (the source is
  derived only, `docs/25-siting-parcels.md` §8.1). The licence email to TxGIO is item 15 under "Soon"; it does not
  block these steps, but Site finder Pro is not sold before the answer.
  1. **Dry run.** Run the workflow with `counties` = `all` and `dry_run` ticked. It downloads nothing and reads no
     secret. Done when the job "dry run (catalogue and a HEAD per county zip)" is green; its summary says how many
     county files answered as catalogued and lists any that did not (warnings, not a stop). Send the coordinator
     that count.
  2. **Four counties.** Run it again with `counties` = `48351,48241,48199,48371` (Newton, Jasper, Hardin, Pecos)
     and `dry_run` unticked: about 10 minutes (estimated). Done when the job "manifest, per-county report and latest.json" is
     green; its summary lists the four with their parcels and holdings, and names the build's version
     (`v1/...`). Then run [deploy-beta](https://github.com/Tobombadil/Bankable/actions/workflows/deploy-beta.yml)
     with action `seed` (or wait for the 4th), so the server pulls them and the site finder measures them.
  3. **Read the counts.** In the [Cloudflare dashboard](https://dash.cloudflare.com), **R2 Object Storage** >
     `beta-object-storage-production` > **Objects** > `data` > `normalized` > `context` > `us.tx.txgio.parcels` >
     `v1` > the version from step 2 > `counties`, open `48351.json`, `48241.json`, `48199.json` and `48371.json`.
     Copy each file's `outlines` block (how often the county's roll stacks accounts on one outline, and how many
     outlines overlap) and `owners` block (counts of named, blank, withheld and placeholder owners, and
     `suspect_kept`: kept names that look like a placeholder) to the coordinator. These files hold counts only, no
     name or address.
  4. **Open three record links per county by hand.** On [inframation.io/site-finder](https://inframation.io/site-finder),
     pick a county, open a band's drawer and, under "Land beside the line", open the appraisal link of three
     holdings (or zoom the map in to a parcel and use its card). Newton, Jasper and Hardin link to the parcel's own
     record on the district's `esearch` site: check that each opens a record, and that its acres and place match
     the parcel. Pecos links to the district's search page with the property id to copy: search three of them. Tell
     the coordinator which counties' links failed; those switch to search-page links (`docs/25-siting-parcels.md`
     §8.4).
  5. **All of Texas.** Run the workflow with `counties` = `all`, `dry_run` unticked. Not yet run in Actions: allow
     several hours (each batch job may take up to its 4-hour limit). Done when the job "manifest, per-county report
     and latest.json" has run; a county that failed is listed in its summary with the reason, keeps any version
     already published, and turns the run red, while the others are published. The server pulls them at the next
     `seed` or on the 4th: about 1.6 GB of files for the whole state. The Houston counties are the densest; the
     map reads them in bounded chunks, measured only on synthetic data so far (`docs/25-siting-parcels.md` §8.9).
  6. **Optional: fewer counties.** To have the server pull only some counties, add the Environment variable
     `PARCELS_COUNTIES` (GitHub > Settings > Environments > `production` > Variables), for example
     `48351,48241,48199,48371`: Texas county FIPS codes, comma separated, or `all`. A variable, not a secret; the
     deploy refuses anything else (`351` alone, a county name). It takes effect at the next `deploy`, then the next
     `seed` or the 4th. Fewer counties also means less disk: about 1.6 GB for all of Texas,
     up to twice that while the previous version is kept (`docs/25-siting-parcels.md` §8.9).

## Soon (internal tier and wider coverage)

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 4 | **Register a PJM account in your company's name** for [Data Miner 2](https://dataminer2.pjm.com), then get an API key from the [PJM API portal](https://apiportal.pjm.com) and [add it as secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new) `PJM_API_KEY`. Internal tier only: PJM's terms bar redistribution without a licence. | 15 min | PJM prices for your company's named accounts | [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md) |
| 5 | **Register an ENTSO-E account** at the [Transparency Platform](https://transparency.entsoe.eu) (Login → Register), then [email transparency@entsoe.eu](mailto:transparency@entsoe.eu?subject=Restful%20API%20access) with the subject "Restful API access" and your account email to get an API token; [add it as secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new) `ENTSOE_API_TOKEN`. Internal tier only. | 15 min + their reply | European day-ahead prices for your company's named accounts | [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md) |
| 6 | **Send the commercial-reuse letters**: AEP by post (address in the letter), then the [AEP contact form](https://www.aep.com/contact/) after 15 business days; the Texas Railroad Commission by [email to Publicassist@rrc.texas.gov](mailto:Publicassist@rrc.texas.gov). Not urgent while the site stays noncommercial. SPP becomes a seventh source to ask before any commercial switch (letter to be drafted). | 30 min | Keeping those sources if Inframation ever charges | [`docs/39` §7](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/39-utility-rfp-permission-requests.md) |
| 7 | **Send the utility RFP permission letters** (twelve issuers), in the order the doc gives; each draft names its address. | 1–2 h | More utility RFPs on RFPs & funding | [`docs/39` §0–§3](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/39-utility-rfp-permission-requests.md) |
| 8 | **Send the PJM data licence request.** | 15 min | PJM rows on the public site | [`docs/37`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/37-pjm-data-licence-request.md) |
| 15 | **Email TxGIO to confirm the Texas parcels licence** through their [contact page](https://www.geographic.texas.gov/contact.html): ask whether the "Creative Commons Zero v1.0 Universal" label on the StratMap Land Parcels collections ([catalogue record](https://api.tnris.org/api/v1/collections/?search=parcels)) covers showing parcel facts to paying subscribers of a commercial product. Every TxGIO collection carries the same label, so a written yes closes the question. | 10 min + their reply | Texas parcels in Site finder Pro | [`docs/25-siting-parcels.md` §1](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/25-siting-parcels.md) |
| 16 | **Download Global Energy Monitor's Global Integrated Power Tracker** yourself (GEM's form has a licence box, so no agent submits it): the "Download data" block on the [GIPT page](https://globalenergymonitor.org/projects/global-integrated-power-tracker), with the field values in the doc (use a role address such as data@inframation.io). Hand the file to the coordinator, who records its licence notice and its TransitionZero rows before anything loads. | 10 min | Global pipeline figures beside Ember's operating figures | [`docs/06` §1.7](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/06-terms-gem-and-parcels.md) |
| 17 | **Send the GEM email** (drafted; five questions, including paid use and the summary sheets). | 5 min + their reply | GEM in paid tiers; GEM's summary tables | [`docs/06` §1.8](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/06-terms-gem-and-parcels.md) |
| 18 | **Ask the next parcel states about their terms**: Wisconsin, Florida, Ohio and Virginia (one email each, addresses and wording in the doc; North Carolina needs none). Save each reply with its date. | 20 min + their replies | Site finder parcels beyond Texas (NC first) | [`docs/06` §2.8](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/06-terms-gem-and-parcels.md) |
| 19 | **Read Our Grid Future's terms** in a browser (Cloudflare blocks our fetcher): open [ourgridfuture.org](https://ourgridfuture.org/), find Terms, About or Data in the footer, save each as a PDF, and note who publishes the site and where its data comes from. If it is built from a private aggregator we do not use it, whatever the terms say. | 10 min | US transmission projects (the Transmission filter has no records today) | [`docs/62`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md) |
| 20 | **Read IEA's CCUS projects database terms**: the [product page](https://www.iea.org/data-and-statistics/data-product/ccus-projects-database) and [IEA's terms](https://www.iea.org/terms); a free IEA account may be needed to see the licence. | 10 min | Carbon capture and storage projects worldwide | [`docs/62`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md) |
| 21 | **Read Arizona DEQ's site policies** from the footer of [azdeq.gov/UIC](https://azdeq.gov/UIC) (the site blocks non-browser clients). | 5 min | Arizona Class VI carbon-storage wells | [`docs/62`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md) |
| 22 | **Read Argonne's RNG database page** ([database](https://www.anl.gov/esia/reference/renewable-natural-gas-database), [notice](https://www.anl.gov/privacy-security-notice)); Argonne is contractor-run, so its data is not public domain by default. | 10 min | Renewable natural gas and biogas projects | [`docs/62` §7](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md) |
| 23 | **Send the SPP and ISO-NE permission requests** (drafts in `docs/37` §5.1 and §5.2). Both terms are read: SPP needs written authorisation for commercial publication, ISO-NE says duplication or non-personal use may violate copyright. | 15 min | Two more US queues, withheld today (SPP about 3,000 rows, ISO-NE about 1,750) | [`docs/37` §5](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/37-pjm-data-licence-request.md) |
| 24 | **Send the Louisiana DCE letter** (drafted, was missing from this list). | 10 min + their reply | SONRIS Class VI applications, the largest primacy state | [`docs/35`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/35-louisiana-dce-data-request.md) |
| 25 | ~~Trademark check on "Inframation"~~ **Done 2026-10-24 by the owner:** the only live mark is INFRAMATION, Serial 75741290, Teledyne FLIR, LLC, class 41 (conducting conferences, its thermography conference). Optional next: file Inframation in classes 42 and 35 (about $350 a class), ideally with counsel. Do not brand an event or conference "Inframation". | — | — | PLAN row 2026-10-24 |

## When convenient

| # | Action | Time | Unlocks | Detail |
|---|---|---|---|---|
| 9 | **Set up the email provider**: [sign up for Resend](https://resend.com/signup), verify the sending domain, and [add its API key as a secret](https://github.com/Tobombadil/Bankable/settings/secrets/actions/new) as `docs/64` §2 names it. | 30 min | Email alerts; invitation emails later | [`docs/64`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/64-private-beta.md) |
| 10 | **Free API keys:** [EIA v2](https://www.eia.gov/opendata/register.php); [SAM.gov](https://sam.gov/content/home) (sign in → Account Details → Public API Key; [about the API](https://open.gsa.gov/api/get-opportunities-public-api/)); [NRC ADAMS](https://adams-api-developer.nrc.gov); regulations.gov via [api.data.gov signup](https://api.data.gov/signup/) ([about the API](https://open.gsa.gov/api/regulationsgov/)); the [LinkedIn developer app](https://www.linkedin.com/developers/apps) (then apply for the Marketing Developer Platform); a [Bluesky account](https://bsky.app). | 45 min | Tier-2 connectors, social posting | [PLAN D8](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/00-PLAN.md) |
| 11 | **Browser look-ups** the fetcher cannot do (DOE OCED portfolio, Wyoming DEQ Class VI, Illinois ICC and others); each item in the doc has its link. | ~20 min each | Carbon-storage and project coverage | [`docs/62`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/62-browser-tasks-checklist.md) |
| 12 | **Ask Elexon** through their [contact page](https://www.elexon.co.uk/contact/) whether their market index data (MID) is inside the open BMRS licence. | 5 min | GB market index prices | [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md) |
| 13 | **A lawyer's read** of the open questions, when budget allows: the noncommercial posture with a free deal workspace, CAISO's "competitive purposes" clause, the RRC copying conditions, OpenStreetMap share-alike for the derived database. | — | Confidence before charging or promoting My pipeline beyond the beta | [`docs/19` §7](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/19-build-a-deal.md), [`docs/29`](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/29-energy-prices.md), [`docs/13` §6.4](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/13-legal-data-rights.md) |
| 14 | **Optional, Bankable (Lovable) app:** in your [Lovable dashboard](https://lovable.dev/dashboard), unpublish it, or switch off its open email-sending and AI functions, so nobody can misuse them. You said not to spend time on Bankable, so this is only if it stays online. | 10 min | Removes two abuse risks | Private findings file sent 2026-10-22 |

## Every January

- **AEP's copyright year.** AEP's terms page prints the current year, so the RFP reader's terms check pauses the
  four AEP issuers each January until the year is updated in `data/sources.yaml` and the issuer registry. An
  agent can make the edit; the owner only needs to know why AEP RFPs pause ([`docs/13` §6.4](https://github.com/Tobombadil/Bankable/blob/claude/sprint-3-attio-adapter-sbxbvy/docs/13-legal-data-rights.md)).

## Done

- 2026-10-21: site made public (`SITE_ACCESS=open`).
- 2026-10-22: deploy-beta re-run after the failed deploy (#22, live on 7238820).
