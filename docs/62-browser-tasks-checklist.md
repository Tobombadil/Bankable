# 62 — Browser tasks: what the sandbox cannot do

**Status:** operator checklist. Every item here is blocked for the same reason: it needs a JavaScript-executing
browser, a human to click through a gate, or a challenge the polite scripted fetch (`pipeline/connectors/http.py`'s
`PoliteSession` — robots honoured, no retries, challenge pages reported as blocked and never bypassed) will not
pass. None of these was attempted a second way; each quoted reason is the recorded `verified` note.
**Reads:** `docs/02-data-sources.md` §11 (CCS source map); `data/sources.yaml` `verified` blocks named per row.

For each: the URL, why the sandbox is stuck, what to look for, and where the finding goes.

---

### 1. DOE OCED project portfolio

**URL:** <https://www.energy.gov/cmei/oced/portfolio> (landing: <https://www.energy.gov/cmei/oced> — "Project
Portfolio" and "Project Map" links)
**Why blocked:** client-rendered. `data/sources.yaml` `us.doe.oced.portfolio`, verified 2026-09-25:
"portfolio page 200, 120,589 bytes with no project content in the served HTML"; DAC Hubs and Carbon Capture
Demonstration pages likewise "list funding announcements only, no selections."
**Look for:** the actual list of funded/selected projects (not just FOA announcements) — award name,
recipient, amount, status — especially anything post-dating the 2025-10-01 "Termination of 223 Projects"
notice, which named the offices and "321 financial awards" but linked no list.
**Paste back:** `docs/02-data-sources.md` §11.1 ("Planned storage" and "Capture" tables, the OCED rows) and
`data/sources.yaml` `us.doe.oced.portfolio` `notes:`.
**Time:** ~20 min.

### 2. Wyoming DEQ — UIC Class VI list

**URL:** <https://deq.wyoming.gov/water-quality/groundwater/uic/class-vi/>
**Why blocked:** popup gate. `data/sources.yaml` `us.wy.deq.class_vi`, verified 2026-09-22: "the permit and
application list is an Elementor popup (anchor `#eae-pupup-item-235152391`) rendered client-side," plus
"intermittent bot mitigation" (one of three attempts served a JS-required redirect interstitial).
**Look for:** the Class VI permit/application list behind the popup — project name, operator, status, county.
**Paste back:** `docs/02-data-sources.md` §11.1 (Class VI table, Wyoming row) and `data/sources.yaml`
`us.wy.deq.class_vi` `verified.note`.
**Time:** ~15 min.

### 3. Illinois ICC e-Docket (SAFE CCS Act certificates)

**URL:** <https://www.icc.illinois.gov/programs/carbon-dioxide-pipelines>
**Why blocked:** single-page app. `data/sources.yaml` `us.il.icc.edocket`, verified 2026-09-25: "programme
page 200, 21,397 bytes but the extracted text is 'Home Programs and Initiatives' only — client-rendered. One
guessed API path (`/api/docket/search?query=carbon%20dioxide`) answered HTTP 500."
**Look for:** the Navigator, Wolf and One Earth CO₂-pipeline dockets in e-Docket — docket number, filer,
filing type, status, date.
**Paste back:** `docs/02-data-sources.md` §11.1 ("CO₂ pipelines" table, Illinois row) and `data/sources.yaml`
`us.il.icc.edocket` `verified.note`.
**Time:** ~20 min.

### 4. NSTA carbon-storage licence terms

**URL:** <https://www.nstauthority.co.uk/data-and-insights/data/> (ArcGIS Hub; carbon-storage licence areas
layer)
**Why blocked:** not findable by fetch. `data/sources.yaml` `gb.nsta.carbon_storage_licences`, verified
2026-09-25, result `ok_terms_not_found`: the Hub's own dataset-search API "is the global ArcGIS Hub search
(195,678 results, unrelated sites) rather than an NSTA-scoped listing; the carbon-storage licensing page URL
guessed from memory 404s. Nothing usable was retrieved."
**Look for:** the carbon-storage licence-areas layer itself inside the Hub, and its own `licenseInfo` field
(NSTA is inferred, not confirmed, to publish under the UK Open Government Licence — this is unread, not
assumed).
**Paste back:** `docs/02-data-sources.md` §11.1 ("Planned storage" table, NSTA row) and `data/sources.yaml`
`gb.nsta.carbon_storage_licences` `license:`/`reuse:` fields.
**Time:** ~25 min.

### 5. Sodir NLOD clause

**URL:** <https://factpages.sodir.no/> ("Use of content" page) and <https://data.norge.no/nlod/en> (the NLOD
text itself)
**Why blocked:** not findable by fetch. `data/sources.yaml` `no.sodir.factpages_co2_storage`, verified
2026-09-25: FactPages quotes the NLOD reference ("may be used in accordance with... NLOD... there may be
limitations with respect to... third party rights, such as reports, core images and logs"), but
"`www.sodir.no/en/about-us/use-of-content/` answered a challenge page (403)" and "NLOD text itself... not yet
read."
**Look for:** NLOD's attribution requirement, and whether the "third party rights" carve-out reaches the CO₂
storage licence and wellbore pages we'd use.
**Paste back:** `docs/13-legal-data-rights.md` (new NLOD subsection near §2.2/§2.18) and `data/sources.yaml`
`no.sodir.factpages_co2_storage`, moving `publication` off `derived_only` only if the text supports it.
**Time:** ~15 min.

### 6. GEM's in-file attribution notice

**URL:** a Global Energy Monitor tracker download (e.g. <https://globalenergymonitor.org/projects/global-solar-power-tracker/download-data/>),
gated behind an email-registration form
**Why blocked:** GEM's own clause puts the notice inside the file, not the page: `docs/13-legal-data-rights.md`
§2.2 quotes it verbatim — "Necessary attribution elements are included in our data download files" —
unreadable without the gated download.
**Look for:** open the downloaded spreadsheet for its actual attribution text (header row, hidden sheet,
README tab). Does it require the citation **next to every derived fact shown**, or does one citation on a
methodology page (what `docs/13` §2.2 assumes) satisfy it?
**Paste back:** `docs/13-legal-data-rights.md` §2.2, dated note — decides if per-record attribution rendering
must change for GEM facts.
**Time:** ~15 min plus the download-form wait.

### 7. Argonne's privacy-and-security notice

**URL:** <https://www.anl.gov/privacy-security-notice>
**Why blocked:** Cloudflare challenge. Re-confirmed 2026-09-26 via `PoliteSession`: both
`www.anl.gov/robots.txt` and this page answer `HttpBlocked — challenge page (HTTP 403)`. `data/sources.yaml`
`us.anl.rng_database` (Argonne's RNG Database) is gated the same way: "403 'Just a moment...'... Argonne is
contractor-operated (UChicago Argonne, LLC), so 17 U.S.C. §105 does not apply automatically; ANL data terms
must be read in a browser."
**Look for:** what this notice permits for reuse of ANL-published data — it's a privacy/security notice, not
a reuse licence, so check whether it addresses data reuse at all.
**Paste back:** `data/sources.yaml` `us.anl.rng_database` `license:`/`reuse:` — state plainly if it settles
nothing and the RNG database page still needs its own visit.
**Time:** ~15 min.
