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
**Closed 2026-10-24, no owner action needed:** the terms sweep found the layer through ArcGIS Online's catalogue
(item `ae0b0c44c6214112a0612ee66cf493dc`, owner `NSTA_GIS`) and read the NSTA User Agreement it links: noncommercial
use with a fixed attribution statement (`docs/07-terms-sweep-2026-10-24.md` §1.7). The register change is
proposed there for the coordinator.

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

### 8. MISO legal and reuse terms

**URL:** <https://www.misoenergy.org/meet-miso/legal-and-privacy/> and
<https://www.misoenergy.org/meet-miso/contact-us/terms-and-conditions/> (the two terms pages `docs/13` §1.7 tried);
the data they must cover is the GI queue, <https://www.misoenergy.org/planning/resource-utilization/GI_Queue/gi-interactive-queue/>
and its JSON endpoint `https://www.misoenergy.org/api/giqueue/getprojects`
**Why blocked:** Cloudflare challenge. `data/sources.yaml` `us.iso.miso.gen_queue`, verified 2026-09-12:
"403 'Just a moment...' Cloudflare challenge from datacentre IP"; its `license:` reads "page returns 403 to
non-browser clients — terms not retrievable", and `docs/13` §1.7 found no Wayback copy. The manifest says "Do
not attempt challenge bypass." `robots.txt` (retrieved 2026-09-12) does load, and its
`Content-Signal: search=yes,ai-train=no,use=reference` is stated as an express reservation of rights under
Article 4 of EU Directive 2019/790: **`ai-train=no`** means MISO content is never used for training, whatever
the terms page says.
**Look for:** save each page as a PDF. Then quote the operative clauses on: reuse of the queue data (not only
written content, photographs and logos); redistribution and republication on a public site; commercial use;
attribution wording; any separate terms for the data portal or API. Also note whether the terms say anything
that bears on `use=reference` or on non-training uses, since `docs/13` §7 item 2 asks this.
**Paste back:** `data/sources.yaml` `us.iso.miso.gen_queue` `license:`, `reuse:`, `publication:` and a dated
`verified:` note (leave `reuse: unknown` / `publication: none` if the text does not settle it), and
`docs/13-legal-data-rights.md` §6, the `us.iso.miso.gen_queue` row (Class, Publication rule, Evidence,
Confidence), with the quoted clauses added to §1.7. Until both are updated no MISO row is published on any tier.
**Time:** ~15 min.

---

## Added by the terms sweep, 2026-10-24

Sections 9 to 14 come from `docs/07-terms-sweep-2026-10-24.md`, which read everything else it could without a
person. The same rules apply to each: open the page in your own browser; **do not submit a form, create an
account, tick a box or accept terms** unless the section says so, because each of those is an acceptance the
legal lane has to record first; save the page as a PDF (browser Print, then "Save as PDF"); and paste the clauses
back verbatim with the date. "The clauses" always means the sentences on **use, copying, redistribution or
republication, commercial use and attribution**, and any separate terms for downloads, data or an API. If a page
says nothing on one of them, say "nothing on commercial use" (for example) rather than leaving it out: silence is a
finding. Send the PDFs and the pasted text to the coordinator; the legal lane records them.

### 9. Our Grid Future (planned transmission)

**URL:** <https://ourgridfuture.org/>, then its terms, about, methodology or data-sources page and the
data-download or data-request page (look in the footer and next to the download button).
**Why blocked:** Cloudflare challenge on every path since 2026-09-15 (`data/sources.yaml`
`us.ourgridfuture.transmission_projects`; `docs/13` §2.9).
**Who publishes it, as far as can be told without passing the challenge** (`docs/07` §1.9): Horizon Energy
Systems, with the Great Plains Institute. A public ArcGIS copy of the data gives the citation "Abramson, E.,
Ramsay, E., McFarlane, D., Prorok, M., Our Grid Future Planned Transmission Projects National Database, Horizon
Energy Systems, March 2026"; another copy says "Data were provided by the Horizon Climate Group". Secondary text
(a Substack post, unverified) says it compiles more than 30 public sources and that downloads need contact details
and an affirmation of citation. A search summary mentions a permitting layer built with Grid Strategies. Nothing
read so far names Interconnection.fyi, Cleanview, Energy Adepto, BidNet, Halcyon or Enverus.
**Save:** a PDF of the home page footer, the terms or licence page, the methodology or sources page, and the
download page **without submitting its form**. Paste the clauses, the citation the download form asks for, and the
**full list of data sources** the site gives.
**Stop rule:** if the source list or methodology names any of Interconnection.fyi, Cleanview, Energy Adepto, BidNet,
Halcyon or Enverus, write that down and stop: we do not use Our Grid Future, whatever its terms say (`CLAUDE.md`).
Say so too if the list is vague ("commercial data", "proprietary sources").
**If the terms allow our use and no private aggregator is named:** tell the coordinator before submitting the
download form. The legal lane decides first whether the form's affirmation is acceptable, and which address to use
(a role address such as data@inframation.io, as for GEM in `docs/06` §1.7).
**Unlocks:** a national planned-transmission layer for the map and the site finder, or a recorded "not usable".
**Paste back:** `data/sources.yaml` `us.ourgridfuture.transmission_projects` (`license`, `reuse`, `publication`,
`verified`) and `docs/13` §2.9 and its §6 row.
**Time:** ~20 min.

### 10. IEA CCUS Projects Database

**URL:** <https://www.iea.org/data-and-statistics/data-product/ccus-projects-database> (the product page: look for
the "Licence" or "Terms" line near the download button), then <https://www.iea.org/terms> and
<https://www.iea.org/terms/terms-of-use-for-non-cc-material>.
**Why blocked:** Cloudflare challenge on the product page and on `/terms` (`data/sources.yaml`
`global.iea.ccus_projects`, verified 2026-09-25).
**Is an IEA account needed?** Not to read the terms: open the pages signed out. For the download, probably yes:
search results report that the product page tells signed-in visitors they have access "as a registered user", so
the free download is likely behind a free IEA account (`docs/07` §1.9, secondary). **Do not create the account
yet.** Registering accepts IEA's terms; read them first and send them back. The legal lane says whether to register.
**Save:** a PDF of the product page (its licence line especially) and of both terms pages. Paste: the licence named
for this product (CC BY 4.0, CC BY-NC-SA 4.0 IGO, or the Non-CC terms), the clauses, and anything on "databases" or
"datasets" in the terms. Search summaries say IEA's CC BY 4.0 excludes standalone datasets and databases; check
whether the page says so.
**Unlocks:** a global cross-check for the CCS project list (capture, transport, storage), or a recorded "not
usable".
**Paste back:** `data/sources.yaml` `global.iea.ccus_projects` and its `docs/13` §6 row.
**Time:** ~15 min.

### 11. Arizona ADEQ: UIC and Class VI

**URL:** <https://azdeq.gov/UIC>, then the site's policy, disclaimer or terms page (footer).
**Why blocked:** Cloudflare managed challenge on the page and on `robots.txt` (`data/sources.yaml`
`us.az.adeq.class_vi`, verified 2026-09-22).
**Save:** a PDF of the UIC page and of the footer's policy or disclaimer page. Paste: any Class VI permit or
application list (name, operator, county, status) or a link to one; the clauses; and **what `azdeq.gov/robots.txt`
says** (open it in the browser and copy it whole).
**Unlocks:** Arizona Class VI rows. Low value today: EPA granted Arizona primacy over all well classes in September
2025, and one law-firm note (secondary) says no Class VI application was pending then.
**Paste back:** `data/sources.yaml` `us.az.adeq.class_vi`; `docs/02-data-sources.md` §11.1, Class VI table.
**Time:** ~10 min.

### 12. Wisconsin SCO statewide parcels: the download page

**URL:** <https://www.sco.wisc.edu/parcels/data/>
**Why blocked:** TLS verification fails from the agent sandbox ("self-signed certificate in certificate chain",
2026-10-23 and 2026-10-24, `docs/06` §2.3). It is not a challenge; the sandbox will not work around a certificate
error.
**Save:** a PDF of the page. Paste the clauses, any licence, readme or "terms of use" link, and the statewide
file's **format and size**. Do not download the file unless the coordinator asks; if you do, also paste any
licence or readme inside the zip.
**Unlocks:** the read `docs/06` §2.3 requires before the Wisconsin file is used (the only terms read so far are
the ArcGIS items': "This data free for public consumption"). Paid use still waits for WLIP's or SCO's answer to
the `docs/06` §2.8 email (`docs/66` row 18), which this does not replace.
**Paste back:** `docs/06` §2.3 and the proposed `us.wi.sco.parcels` row (`docs/06` §2.9).
**Time:** ~10 min.

### 13. South Dakota: the state disclaimer behind the PUC's footer

**URL:** <https://sd.gov/disclaim.aspx> (the "Disclaimer" link in the footer of
<https://puc.sd.gov/dockets/HydrocarbonPipeline/default.aspx>).
**Why blocked:** from the sandbox the link redirects to a Microsoft sign-in page (2026-10-24, `docs/07` §1.5).
**Look for:** whether the page opens for you without signing in. If it asks you to sign in, **do not**: write
"sign-in required" and stop; the legal lane then treats the PUC site as having no published terms (`docs/07` §2).
If it opens, save a PDF and paste the clauses.
**Unlocks:** the South Dakota CO₂ pipeline docket index (Summit HP22-001, Navigator HP22-002), the cleanest state
docket list for the pipeline asset class.
**Paste back:** `data/sources.yaml` `us.sd.puc.co2_pipeline_dockets` and its `docs/13` §6 row.
**Time:** ~5 min.

### 14. Minnesota PUC eDockets

**URL:** <https://www.edockets.state.mn.us/edockets/>
**Why blocked:** it now redirects to the Department of Commerce's eApps sign-in (Keycloak, "Sign in to eApps",
2026-10-24, `docs/07` §1.5). In September it answered a bare 403.
**Look for:** whether eDockets has a **public search that works without an account** (a "Search eDockets" or
"Search documents" link on the sign-in page, or on <https://mn.gov/puc/>). If it does, open the docket for Summit's
Otter Tail to Wilkin route permit (docket family PL-9/CN-22-472) and note the address of the search page. Then the
terms: save a PDF of any terms, disclaimer or "conditions of use" page linked from eDockets or the PUC site, and
paste the clauses. If eDockets needs an account, **do not register**; save the registration page's terms (if shown
before registering) and stop.
**Unlocks:** Minnesota's Summit CO₂ pipeline record, or a recorded "account required".
**Paste back:** `data/sources.yaml` `us.mn.puc.edockets` and its `docs/13` §6 row.
**Time:** ~10 min.
