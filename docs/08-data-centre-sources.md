# 08 — Data-centre sources: a survey of US public primary sources (2026-10-23)

Lane dc-survey, data engineer, 2026-10-23. Asked by the coordinator after the owner's remark "They have way more
datacenters than we do" (comparing us with Cleanview). Everything below was measured from this environment on
2026-10-23 through the platform User-Agent (`pipeline/connectors/http.py::user_agent()`, rendered for
inframation.io), with robots.txt read first and each host's rate limit honoured. No private aggregator was opened
(CLAUDE.md list, plus DataCenterHawk, Baxtel, DCByte, datacentermap, Regrid, LandGate, Acres, Land id). No
challenge page was bypassed. Counts are sample counts from a few requests per source, not bulk loads, except where a
federal bulk file is the only route (ECHO NPDES and FAA archives, both public-domain downloads published for that
purpose).

This doc builds on `docs/25` §3 (the 2026-09-28 air-permit survey and the two built connectors) and does not repeat
it.

## 1. Answer

Two federal filings that large data-centre builds make, and that this repo does not yet read, would roughly triple
our active pipeline:

1. **EPA ECHO ICIS-NPDES construction stormwater coverage.** Any construction that disturbs an acre or more needs
   it, and the notice of intent comes before grading. 74 new active sites, national, public domain, weekly, no text
   extraction.
2. **FAA obstruction evaluation (OE/AAA) case archives.** A building or construction crane that is tall or close to
   an airport needs a Part 77 notice, filed with an exact point. 73 new sites in 2026 so far, public domain, no text
   extraction. Sponsor names must be dropped, because about a third of them are people.

Together they add about **135 distinct active sites** we do not hold today: 74 + 73, less about 12 found by both. We
have 73 active-pipeline loads now (`GET https://inframation.io/v1/proposals?kind=load`, 582 records, read at
2026-10-23T20:33Z). Texas gains the most: 27 NPDES sites plus 20 FAA sites, where ICIS-Air finds 12 facilities.

| Rank | Source (proposed id) | New active records (measured) | Effort | Public display | Text extraction |
|---|---|---|---|---|---|
| 1 | ECHO ICIS-NPDES construction stormwater (`us.epa.echo.icis_npdes_construction`) | 74 sites (95 permits); also 89 new facilities whose construction coverage ended, and 53 expired | M | yes, public domain | no |
| 2 | FAA OE/AAA Part 77 case archives (`us.faa.oeaaa.part77_cases`) | 73 sites (2026-01-01 to 2026-10-16) | M | yes, public domain; drop personal names | no |
| 3 | Loudoun County data-centre pipeline layers (`us.va.loudoun.data_center_pipeline`) | 18 central (6 to 40, depending on match radius) of 85 | S | yes, attribution (legal to confirm) | no |
| 4 | SEC EDGAR 8-Ks naming a data-centre campus (`us.sec.edgar_fts`, new scope) | 20 to 40 campuses (estimate from 143 hits) | M | derived facts and link only | yes |
| 5 | Georgia EPD air permit advisories (`us.ga.epd.air_permit_advisories`, exists, gated) | about 3 a month, the earliest signal | M | not yet: terms not found | yes (PDF) |
| 6 | Prince William County data-centre campuses (`us.va.pwc.data_center_campuses`) | 12 central (8 to 33) of 78 | S | no: copyrighted, licence needed | no |
| 7 | Granicus Legistar Web API (`us.legistar.webapi`) | 20 to 25 projects across 33 governments | L | not yet: per-client terms | yes |
| 8–17 | FAST-41 scope, IL and MN incentive lists, city building permits, FERC query, state air layers, state air notices, PUC dockets, newsrooms and GDELT, OSM tags | small each (§4) | S–L | mixed | mixed |

Ranking metric: new active-pipeline records per unit of effort (S = 1, M = 2, L = 4), among sources whose terms
allow public display. Gated sources (5–7) rank after the publishable ones with similar yield. The per-source JSON is
in the lane's scratchpad (`dc-survey.json`). Build lanes are in §6.

## 2. What we hold, and the gap

The coordinator's API check on 2026-10-23 found 582 `kind=load` proposals: 487 built, 65 filed, 8 under
construction, 22 unknown, so 73 active. All of them come from EPA ECHO ICIS-Air (`us.epa.echo.icis_air`, 517) and
Virginia DEQ (`us.va.deq.data_center_air_sites`, 206). This lane re-read the same 582 records on 2026-10-23 for the
overlap tests below (`/v1/proposals?kind=load`, 6 pages, cursor paging).

Both current sources are air registers. They are blind where a state does not report minor sources to ICIS-Air, or
where engines need no registration (Texas permit-by-rule, `docs/25` §3.5). The two new federal routes do not depend on
air permitting at all.

## 3. Method

- **Selection.** A record counts as a data centre when its name or description says "data center", "data centre",
  "datacenter", "hyperscale" or "DC campus". That is a lower bound: code-named projects ("Project Borealis",
  "Orla, LLC") are missed unless a description names the use.
- **Overlap with our store.** A candidate is "held" when it lies within 1.5 km of one of our 582 load points in the
  same state, or shares at least half its distinctive name tokens with one (`dc-survey` scratch script
  `overlap.py`). In dense markets (Loudoun, Prince William) proximity is not identity, so §4.5 gives three radii.
- **Sites, not filings.** NPDES permits and FAA cases are clustered to sites: same state, within 1.5 km. FAA clusters
  made only of power-line towers and poles are removed (17 of 126), because they are lines serving a data centre,
  not the data centre.
- **Politeness.** echo.epa.gov at 0.1 rps (its `Crawl-delay: 10`); oeaaa.faa.gov at 0.1 rps; webapi.legistar.com at
  0.5 rps; ArcGIS and Socrata hosts at 0.5 rps; every other host at 1 rps or less; one attempt per request, no retry
  on 429 or challenge.

## 4. Sources, by family

### 4.1 Federal permits that every site needs (two new routes)

#### EPA ECHO ICIS-NPDES: construction stormwater coverage (rank 1)

- **Access.** `https://echo.epa.gov/files/echodownloads/npdes_downloads.zip`, 357,154,247 bytes, Last-Modified
  2026-10-18. Columns are documented at
  https://echo.epa.gov/tools/data-downloads/icis-npdes-download-summary. Read by HTTP range, exactly as the ICIS-Air
  connector reads its zip (`central_directory`, `inflate_member` in
  `pipeline/connectors/us_epa_echo_icis_air/connector.py`): `NPDES_PERM_COMPONENTS.csv` (3.0 MB compressed),
  `ICIS_FACILITIES.csv` (73.1 MB) and `ICIS_PERMITS.csv` (101.5 MB). `echo.epa.gov/robots.txt` allows `/files/`
  with `Crawl-delay: 10`.
- **Terms.** A US federal work, public domain under 17 U.S.C. §105
  (https://www.law.cornell.edu/uscode/text/17/105). The `docs/13` §2.12 EPA hedge applies, as for ICIS-Air.
- **Why it works.** Under the NPDES electronic reporting rule, states report general-permit coverage to EPA. The
  permit component `SWC` marks stormwater construction. Construction disturbing one acre or more needs NPDES
  stormwater coverage (https://www.epa.gov/npdes/stormwater-discharges-construction-activities), so a data-centre
  campus files a notice of intent before grading starts, and a notice of termination when the site is stabilised.
- **Fields.** NPDES id, facility (project) name, address, city, county FIPS, state, ZIP, geocoded latitude and
  longitude (on 411 of 419 data-centre rows; precision not stated in the file, so placement needs the ICIS-Air rule
  or a county-centroid fallback), permit type (`GPC`, general-permit coverage), status (`EFF` effective, `ADC`
  administratively continued, `TRM` terminated, `EXP` expired), original issue, issue, effective, expiration and
  termination dates, issuing agency, permittee name. No MW, no floor area.
- **Stage.** Active coverage is `under_construction`; termination is construction complete (usually `built`, but not
  proof of operation).
- **Measured yield.** 419 facilities named as data centres (TX 93, VA 51, AZ 50, IL 23, CO 21, GA 20, CA 15, OR 15,
  UT 14, FL 11). Components: `SWC` 284, none 133. Status: EFF 156, TRM 127, EXP 68, ADC 66. Active (EFF, ADC or
  PND, no termination date): 223. Active and issued 2024 or later: 142 (TX 34, AZ 23, IL 15, VA 14, NC 8, IN 6,
  NJ 6). Examples: "VANTAGE DATA CENTERS AZ21 - MAIN PROJECT" (Goodyear), "NTT GLOBAL DATA CENTER VA10 EARLY GRADING
  PLAN", "COREWEAVE DATA CENTER / APPLICATION 2026-7731" (NJ, effective 2026-09-17), "APLD JAMESTOWN DATACENTER" (ND).
- **Overlap.** 142 of 419 (34%) are near or name-matched to a record we hold (129 by point, 13 by name). New: 277,
  of which 134 are active, 89 terminated (construction coverage ended) and 53 expired (outcome unknown). New, active
  and issued 2024 or later: 95 permits, clustering to **74 sites** (TX 27, AZ 6, NC 5, FL 3, NJ 3, IN 3, IL 3, CA 3).
- **Precision.** In a random 40 of the 419 names, 35 are the data-centre site itself and 5 are infrastructure built
  for one (a substation, a road, a concrete batch plant). All 40 belong to a data-centre project.
- **Coverage gap, measured.** Which states report construction coverage now: `SWC` records issued since 2025 exist
  for 59 jurisdictions. Texas reports 16,701, Utah 9,621, Florida 8,714, New York 6,859, Alabama 5,392, South
  Carolina 4,372, Tennessee 4,350, Indiana 3,990, California 3,363, Illinois 3,163, Washington 2,979, Pennsylvania
  2,894. Georgia (187,896 records, none since 2025), Virginia (13,340, one since 2025), Ohio, Iowa, Nevada, Nebraska,
  North Carolina (7) and Mississippi (5) report little or nothing since 2025. Those states need their own NOI
  registers, or the air and county routes in this doc.
- **Personal data.** `PERMIT_NAME` is the permittee: 64 of 419 carry no corporate token, and a 30-row sample showed
  four individuals' names. The connector must not store that column. Facility names are project names; run
  `services.personal_names.classify` over them anyway.
- **Effort.** M. The ranged-zip reader and conditional GET already exist. New work: the selection rule, the status
  map, and site clustering for multi-permit campuses. About 4 requests a week at 0.1 rps.
- **Built (2026-10-23, lane dc-npdes; review fixes 2026-10-24).**
  `pipeline/connectors/us_epa_echo_icis_npdes_construction/`. On the 2026-10-18 file: 5 requests, 52 s (an
  unchanged week is one 304); 387 data-centre construction coverages (14 cryptocurrency mines excluded: names that
  declare mining, and King Mountain, Alborz, Bear, Chief and Helios from their operators' SEC filings) group into
  295 sites; 19 made only of works (substations, switching stations, duct packages, batch plants, laydown yards,
  utilities' permits) are dropped and 47 are held, so 229 are published. Precision 45 of 50 sampled published
  sites, crypto counted (2 works for one, 3 possible mines); recall 23 of 54 (43 %), lowest in Northern Virginia,
  where NOIs carry campus codes. Lifecycle, judged at the file's date: under_construction 105, built 77, unknown 47.
  Coverage still in force more than a year after its own expiry, in force in a state that stopped reporting to
  ICIS (California since October 2025, Wyoming, Oklahoma, Guam), or ended on its expiry date proves nothing current
  and is unknown; the most recent filing decides a site, except that a coverage filed within a year before its
  latest termination (a handover, renewal or next phase) keeps it under construction. Georgia and Virginia do
  report (6,196 and 944 construction filings in the year to the file): "none since 2025" above counted only
  coverages carrying their own `SWC`, and theirs carry it on the master permit. Against the store (ICIS-Air and
  DEQ, 2026-10-09): the resolver merges 11, all checked correct, 9 by a shared FRS id. Citing every permit's FRS id
  stops the wrong merges of the first build (17 merges then, 6 of them joining distinct campuses: Vantage GA1 and
  GA2, 12.7 km apart; TX 11 and TX2; Aligned ORD03 into its Northlake campus; Vantage VA16 into four Virginia
  campuses). The resolver compared names, counties and registry ids, not points, so `held.yaml` held the 46 sites
  that duplicate a held record (within 300 m, or farther under the held record's own building code) and the one
  bridge, Vantage VA16, which DEQ rows (no `frs` id) still let chain. *Holds, 2026-10-24 (lane resolver-veto):*
  with the location veto and point evidence in `pipeline/resolve.py` (docs/22 §28), 12 duplicates now merge with
  their held record and nothing else, each checked with and without FAA, and are released (H5 Ashburn, Compark,
  ASH2 Lockridge, Prime ORD-1, One Neck Denver, Vantage V6, ATL East, MNZ03, Sharka, Vantage GA 21, Allstate
  Rochelle, Aligned Phoenix): 241 sites publish. 35 stay held: 32 whose names score under 70 against their held
  record (nearness never merges alone), LYH10 and LYH03 (709 m and 1,122 m, named differently), and the VA16
  bridge, whose cluster still takes in Vantage VA2 (1.15 km). 218 sites are new: TX 53, AZ
  20, VA 14, OR 13, GA 12, CA 10; 98 under construction, 66 of them first covered since 2024. About 3 campuses
  still show twice inside this source (Nexus at Hubbard, 1.4 km apart; Stargate Abilene buildings 9 and 10 filed
  twice, 1.6 km; NTT TX-4 and RagingWire). Lifecycle mapping and its limits: the connector's `status_map.yaml`;
  counts and terms: `data/sources.yaml`.

#### FAA OE/AAA: obstruction evaluation case archives (rank 2)

- **Access.** The public OE3A application (https://oeaaa.faa.gov/oeaaa/oe3a/main/#/home) offers "Download Archives".
  Its own controller
  (https://oeaaa.faa.gov/oeaaa/oe3a/main/components/download/downloadController.js) builds
  `/oeaaa/oe3a-external-api/downloadArchives.do?fname=Part77{REGION}{YEAR}List.gzip`. The file is a plain CSV, one
  per FAA region per year: AEA, ASO, AGL, ASW, ANM, AWP, ACE, ANE, AAL. `oeaaa.faa.gov/robots.txt` answers 404 (no
  rule).
- **Terms.** A US federal work, public domain (17 U.S.C. §105). The faa.gov Web Policies page
  (https://www.faa.gov/web_policies) answered an Akamai 403 to this environment, so no FAA clause is quoted. The
  legal lane should read it from another route.
- **Fields.** Study number (ASN), status, determination, entered, received, completion and expiration dates,
  latitude and longitude with a survey-accuracy code, structure name, city, county, state, proposal description,
  "notice of" (new construction or alteration), duration and work schedule, structure type (building, crane, tower),
  heights above ground and sea level, sponsor and representative names. No MW.
- **Why it works.** 14 CFR Part 77 (https://www.ecfr.gov/current/title-14/chapter-I/subchapter-E/part-77) requires
  notice for structures, cranes included, over 200 ft above ground or within the slopes around public airports.
  Data-centre builds use tall cranes and often sit near airports (Dulles, Phoenix, Dallas), so many file. Sites far
  from airports with low cranes do not, which is why this complements NPDES rather than replacing it.
- **Stage.** A building filing is `filed`, typically months before construction. A crane filing is
  `under_construction`. A determination expires after 18 months, which gives a natural "no longer active" signal.
- **Measured yield.** The nine 2026 archives total 104 MB and 116,354 cases entered from 2026-01-01 to 2026-10-16.
  423 cases name a data centre in the structure name, description or sponsor. 415 are new construction. Statuses:
  Determined – No Hazard 387, Evaluating 18, Studying 18. Structure types: transmission tower 109, crane 82,
  building 61, mobile crane 56, pole 54, commercial building 50. They cluster to 126 sites; removing 17 that are
  power-line structures only leaves **109 data-centre sites**, of which **73 are new** to us (TX 20, AZ 7, GA 6, NV 4,
  OH 4, CA 3, IA 3, NE 3, OR 3, PA 3). Examples: "CloudHQ MDC2/MDC3" (Prince William), "QTS SLC2 DATA CENTER"
  (Utah County), "Project Miner – Dona Ana County NM" (Stack Americas), "AUS01 Temple TX".
- **Overlap.** 36 of 109 (33%) are within 1.5 km of a record we hold. About 12 of the 73 new sites are also new
  NPDES sites.
- **Operational note.** On 2026-10-23 the OE3A home page reads: "Due to the government shutdown the Federal Aviation
  Administration (FAA) is unable to accept any new notices under 14 CFR Part 77." The 2026 archive will grow again
  when filing resumes.
- **Personal data.** Yes. 38 of 127 distinct sponsor strings look like a natural person (a first and last name with
  no company token); representative names are mostly people. Store neither. Keep a sponsor only when
  `services.personal_names.classify` says it is an organisation. Drop `SIGNATURE CONTROL NUMBER`. Structure names are
  place or project names.
- **Effort.** M: nine yearly files (current and previous year, 18 files, about 230 MB, once a week, conditional
  where the server allows), the keyword rule, a deterministic site cluster key, and an expiry rule. No text extraction.
- **Built (2026-10-23, lane dc-faa; fix round 2026-10-24).** Connector `us.faa.oeaaa.part77_cases`
  (`pipeline/connectors/us_faa_oeaaa_part77_cases/`), 0.1 rps, tests on recorded fixtures. **Scheduled weekly
  from 2026-10-24** (lane resolver-veto): the row had no `cadence` until `pipeline/resolve.py` had a location
  veto (last item); with it, none of the 14 far merges remains (docs/22 §28.5).
  - *Run.* The 20 requests a run makes (robots.txt, 18 archives, the furlough flag) downloaded live on 2026-10-24 in
    218 s and read offline in 19 s: 240,960 cases (data as of 2026-10-17), 922 data-centre cases, 276 sites, 268
    records (filed 246, built 14, under_construction 8); 8 sites hold no active case. No ETag or Last-Modified, so
    every run downloads the archives. The furlough flag was off: the operational note above is out of date.
  - *Selection.* Precision 45 of 45 on a random sample of selected cases (2026-10-24; 95% lower bound about 94%).
    Recall on 60 cases within 0.5 km of a held data-centre point: 21 of 34 cases, 32 of 34 sites, 0 of 24
    non-data-centre cases selected; both samples lean to Loudoun and Prince William. Labels and cases are fixtures
    (`selection_sample_*`) and a test re-checks the figures. A campus's generator buildings and energy centre stay.
  - *Sites.* A 1 km link, nearest first; a site is never wider than 1.2 km, never spans two named operators, and an
    AWS-coded group never joins an operator other than Amazon or Digital Realty. The first rule (1 km chain, operator
    guard only) made four Loudoun and Prince William records of several campuses each, 2.5 km across (Yondr NOV2
    inside an AWS-coded record; Iron Mountain named after another building's crane; QTS MAN1 published as
    "NVA05E"); a fixture of those 77 cases pins the fix. Limits: a campus wider than 1.2 km splits (QTS Cedar Rapids,
    about 3 km); two AWS-coded campuses under 1 km apart (IAD 12-13 and IAD 170) stay one record.
  - *Stage.* A crane or an unexpired Determination of No Hazard is `filed`, not `under_construction`: it clears a
    height for a work window, it does not show work started. `under_construction` needs a date built on a permanent
    structure under a notice of new construction (Form 7460-2); a date built on a notice of an existing structure,
    or work at an existing data centre, is `built`. A site whose determinations have all expired is not a record (a
    loaded one becomes a `removed` event). Cranes stay active until their determination expires, not their work
    window: 58 sites are `filed` only on cranes past their window, where the building is likely under way.
  - *Expiry.* The FAA's own expiration date, else 18 months: 14 CFR 77.33(b) sets 18 months, so the survey's figure
    stands.
  - *Personal data.* Representative and signature columns are never stored. A sponsor is kept only when
    `services/personal_names.py` proves an organisation and no part of it (brackets included) and no run of
    capitalised words reads as a person; a bracketed suffix ("(CE)") is cut. Structure names lose any person's name.
    On the 2026-10-24 archives this refused 15 sponsor cells the first rule kept: 1 with a person's name in brackets
    (2026-AEA-922-OE), 13 with one beside the organisation's, 1 other. Pinned by tests with made-up names.
  - *Against held loads* (ICIS-Air and Virginia DEQ frames of 2026-10-09, the NPDES construction lane's frame of
    2026-10-24; Loudoun's layer was not measured here, its lane counts 6 overlaps). 129 records are held (a held
    exact point within 1 km of a case, or merged), 139 new (filed 131). New of total by state: VA 2/58, TX 28/41,
    AZ 8/27, IL 4/18, OH 13/14, CA 7/10, GA 9/10, OR 2/9, IA 8/8, NV 7/8, CO 5/7, PA 5/5, UT 4/5, 21 other states
    37/48. The resolver (`pipeline.resolve.run`, threshold 75) accepted 42 FAA-to-held fuzzy pairs: 15 within 0.5
    km, 8 at 0.5 to 2 km, 14 more than 2 km apart (wrong: same operator, another campus, up to 32 km), 5 against a
    record with no exact point; 8 clusters join held records the held-only run keeps apart. A simulated veto (two
    exact points more than 2 km apart never merge) removes all 14 far merges and 3 of the 8 joins. 93 records have a
    held point within 0.5 km and stay apart: duplicates until a same-place rule exists. The first load is not news;
    the trends rule handles it. *Veto built, 2026-10-24 (lane resolver-veto, docs/22 §28):* FAA-to-held accepted
    pairs 41 → 31, more than 2 km apart 14 → 0; clusters wider than 2 km 8 → 1 (a shared registry id). Of the 268
    records, 26 merge and 242 stand alone: 139 more than 1 km from every held point, 103 within 1 km (92 within
    0.5 km) kept apart because their names do not match.

### 4.2 State air permits for backup generators (family 1)

Machine-readable state registers add little beyond ICIS-Air. The pre-construction signal is in the application and
draft-permit notices, which are PDFs or HTML pages and often name a code-named LLC. Measured per state:

| State | Route found | robots / access | Terms | Measured | Verdict |
|---|---|---|---|---|---|
| Texas (TCEQ) | NSR permits database https://www2.tceq.texas.gov/airperm/index.cfm; Central Registry | `Disallow: /` on www2 and www15 (`docs/25` §3.2) | not retrieved | not fetched. The TCEQ water general-permits dataset on data.texas.gov (https://data.texas.gov/d/6pm5-am5m) excludes the construction permit and holds 2 data-centre rows | **Use ECHO NPDES** (93 Texas facilities named as data centres, mostly construction coverage) |
| Georgia (EPD) | public advisories https://epd.georgia.gov/permitting-public-advisories-and-public-notices (PDF) | robots allows `/document/` | no reuse clause found; https://gta-psg.georgia.gov/node/171 governs agency posting, not reuse | 7 advisories (pa0926-2 to pa1026-3): 54 applications, 6 data centres, e.g. Amazon Data Services ATL 114 Campus, Douglas County, app. 30374 | rank 5, gated on terms. EPD's ArcGIS "air source coordinates" layer holds 10 data centres of 1,084 rows |
| Ohio (EPA) | eDocument portal https://edocpub.epa.ohio.gov/publicportal/edochome.aspx; draft-permit hearings, e.g. Amazon Hilliard, 158 generators (https://www.abc6onyourside.com/news/local/ohio-epa-host-public-meeting-proposed-amazon-data-center-permit-hilliard) | eDocs robots 404 | not read | not counted. Ohio EPA drafted a general NPDES permit for data-centre discharges (https://www.bricker.com/insights/publications/ohio-epa-issues-draft-general-npdes-permit-for-data-centers) | gap: Ohio reports no construction coverage to ICIS since 2025. Later lane |
| Arizona (ADEQ, Maricopa AQD) | ADEQ ArcGIS layers `AQD_Permits` (416 rows) and `AIR_COMPLIANCE_PERMITS_AGOL` (40); Maricopa issues an emergency-engine general permit (https://trinityconsultants.com/resources/maricopa-county-generator-engine-considerations) | azdeq.gov answered a Cloudflare challenge (not bypassed); maricopa.gov robots allows | not read | ADEQ layers: 0 data centres | **use ECHO NPDES** (50 Arizona data-centre facilities) and FAA |
| Illinois (IEPA) | Bureau of Air public notices | epa.illinois.gov robots allows | not read | not counted | ECHO NPDES covers Illinois (23) |
| Indiana (IDEM) | notice PDFs (e.g. https://www.in.gov/idem/files/notice_20261024_air_50413pn-cm.pdf, Amazon, Jasper County, T 073-50413-00077) and https://permits.air.idem.in.gov/ | permits host robots.txt declares the content-signal framework with no signal lines | not read | not counted; applicants are code-named LLCs (Orla, Woodland Caribou, Lavender Fields) | text extraction needed; NPDES covers Indiana |
| North Carolina (DAQ) | hearing notices, e.g. Amazon Energy Way Tech Campus, Richmond County (https://www.deq.nc.gov/news/events/public-hearing-preliminary-determination-air-quality-permit-applications-amazon-data-services-and) | deq.nc.gov robots allows | not read | not counted | later lane |
| Pennsylvania (DEP eFACTS) | https://www.ahs.dep.pa.gov/eFACTSWeb/ | TLS verification failed from this environment (unable to get local issuer certificate) | not read | not reached | NPDES covers Pennsylvania (5) |
| Washington (Ecology) | publications, e.g. Microsoft Oxford draft permit (https://apps.ecology.wa.gov/publications/SummaryPages/1402014.html); a draft general order for data centres in Quincy, East Wenatchee and Malaga (https://ecology.wa.gov/events/aq/general-orders/data-centers-general-order-comment-ends-9-28-25) | apps.ecology.wa.gov robots allows (disallows document download paths) | not read | not counted | when the general order is final, its coverage list is the register |
| Oregon (DEQ) | public notices | oregon.gov robots allows | not read | not counted | NPDES covers Oregon (15) |
| Minnesota (MPCA) | MN Geospatial Commons "MPCA Air Quality Permits" (https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_pca/env_air_quality_permits/FeatureServer/0) | ArcGIS API | not read | 4 data centres of 1,929 rows (e.g. Iron Gate Eagan, registration permit 2026) | low |
| Iowa (DNR) | not found | iowadnr.gov robots allows | — | not counted | gap (no ICIS construction coverage) |
| Nevada (NDEP, Clark County) | NDEP proposed-action PDFs (e.g. https://ndep.nv.gov/uploads/documents/ap7374-4749-Proposed-Action.pdf) | ndep.nv.gov robots 404; clarkcountynv.gov robots allows | not read | not counted | FAA covers Nevada (4 new sites) |
| Nebraska (NDEE) | not found | dee.nebraska.gov robots allows | — | not counted | FAA (3 new) |
| South Carolina (DES) | not found | des.sc.gov robots 404 | — | not counted | NPDES covers South Carolina |
| Louisiana (LDEQ) | EDMS https://edms.deq.louisiana.gov/ | **EDMS robots disallows our token** | — | not fetched | NPDES covers Louisiana (4 new) |
| Mississippi (MDEQ) | not found | mdeq.ms.gov robots allows | — | not counted | gap |
| Wisconsin (DNR) | WARP permit tracking, e.g. Vantage Port Washington 25-MIN-094 (https://apps.dnr.wi.gov/warp_ext/AM_PermitTracking2.aspx?id=32201) | apps.dnr.wi.gov robots allows all | not read | not counted | later lane |

Missouri DNR's air facility layer (https://gis.dnr.mo.gov/host/rest/services/air/air_facility_emissions/MapServer/0)
carries 28 data-centre emission rows (for example Oracle Lee's Summit); New Mexico's air facilities layer holds none;
Florida ARMS holds none. "Not counted" means no route was found that would count without text extraction in this
lane.

### 4.3 Can EPA find permit applications nationally?

Not for air permits. ICIS-Air carries a facility's operating status ("Planned", "Under Construction") where the state
reports minor sources, which the built connector already reads (`docs/25` §3.7). It carries no applications. RBLC
holds BACT/LAER determinations from issued major-source permits; data centres are mostly synthetic minor sources, and
the legacy RBLC URLs answer 404 while EPA migrates to a new dashboard (`data/sources.yaml` `us.epa.rblc`). The
national pre-construction signal EPA does hold is the **construction stormwater notice of intent**, in ICIS-NPDES
(§4.1), for the states that report it.

### 4.4 State incentive approvals naming projects (family 2)

| State | Source | Measured | Terms | Verdict |
|---|---|---|---|---|
| Illinois | DCEO Data Center Investment Program annual report (https://dceo.illinois.gov/content/dam/soi/en/web/dceo/aboutdceo/reportsrequiredbystatute/2025-data-centers-annual-report.pdf) | 34 MOUs to 2025-12-31: company, year, city, commitment ($10.67 bn total), jobs. About 17 have a same-company record in our Illinois loads | no reuse clause on illinois.gov (https://www.illinois.gov/about/privacy covers disclaimers) | rank 9. Mostly operating or under construction |
| Minnesota | DEED list (https://mn.gov/deed/assets/data-center-project-list_tcm1045-531119.pdf, dated 2026-07-15), from https://mn.gov/deed/business/financing-business/tax-credits/data-center-credit/ | 42 certified (16 new, 26 refurbished), name, company, city; about 8 match a record we hold | no mn.gov reuse clause found; several state agencies reserve commercial reuse | rank 10. Mostly enterprise and operating |
| Texas | Comptroller data-centre exemption (https://comptroller.texas.gov/taxes/data-centers/) | "Information provided on and with the application is confidential." No list | — | excluded |
| Texas | JETI (https://comptroller.texas.gov/economy/development/prop-tax/jeti/) | eligibility by NAICS category; data centres reported excluded (https://grist.org/?p=717178); the applications table loads by script (0 rows in the HTML) | — | excluded; it shows the gas plants built for data centres |
| Ohio | Tax Credit Authority data-centre sales-tax exemptions | about 19 active agreements, via shell LLCs (https://news.bloomberglaw.com/daily-tax-report/data-centers-won-billions-in-tax-breaks-some-states-are-balking) | not read | low |
| Georgia | DOR high-technology data-centre exemption | no public list. The state audit could not publish DOR data below the IRS aggregation threshold (https://theaugustapress.com/report-30-of-georgias-data-centers-traced-to-2018-tax-break/) | — | excluded |
| Arizona | Commerce Authority computer data-centre program (https://www.azcommerce.com/incentives/computer-data-center-program/) | Cloudflare challenge, not bypassed | — | not reached |
| Indiana | IEDC data-centre exemption (https://iedc.in.gov/incentives/data-center-sales-tax-exemption) | approvals by code name (https://www.ibj.com/articles/iedc-board-approves-incentives-for-four-planned-data-center-projects) | not read | low |
| Pennsylvania | Revenue annual certification report (https://www.revenue.pa.gov/News%20and%20Statistics/ReportsStats/TaxCredits/ComputerDataCtrEquipProgram/Documents/2023_computer_data_ctr_equip_pgm.pdf) | names beneficiaries (Iron Mountain, Oracle, TierPoint, Expedient) | not read | low |
| Mississippi | MDA | no list found | — | — |

Incentive lists name companies (no natural persons) but mostly describe operating enterprise sites, at city
precision, once a year. They add built inventory more than pipeline.

### 4.5 Local land-use filings (family 3)

#### County GIS layers

ArcGIS Online search (`title:"data center"`, feature and map services, 730 hits) found layers published by county
governments in the top market. The rest were student, consultant or advocacy layers (§4.9).

| Layer | URL | Rows | Status vocabulary | New to us at 0.5 / 1.0 / 1.5 km | Terms |
|---|---|---|---|---|---|
| Loudoun pipeline parcels | https://services1.arcgis.com/MxjRokvPm7bjslyR/arcgis/rest/services/Pipeline_Data_Center_Areas/FeatureServer/1 | 85 | Active 51, Approved: Conditionally 18, Approved 16 | 40 / 18 / 6 | "available to the public ... Acknowledgement of Loudoun County would be appreciated" (item https://www.arcgis.com/home/item.html?id=2a084dcc039249f286685205f1f4b33b) |
| Loudoun existing parcels | …/Existing_Data_Center_Parcel/FeatureServer/1 | 139 | Built 103, Under construction 29, mixed 7 | 9 (0.5 km) | same |
| Loudoun permitted buildings | …/Existing_Permitted_Data_Center_Buildings/FeatureServer/1 | 239 | Built 187, Under construction 52 | 27 (0.5 km) | same |
| Prince William campuses | https://gisweb.pwcva.gov/arcgis/rest/services/Planning/Build_Out_Analysis/MapServer/10 | 78 | Planned 53, Pending 15, Completed 10 | 33 / 12 / 8 | "The Prince William County GIS database is copyrighted and digital map data will be released under a license-to-use agreement." (layer metadata) |
| Prince William buildings | …/Build_Out_Analysis/MapServer/9 | 247 | Planned 91, Pending 68, Under Construction 29, Completed 59 | 113 (0.5 km) | same |
| Stafford project outlines | https://services1.arcgis.com/qKiA6JuCrE2l72iL/arcgis/rest/services/Data_Center_Project_Outlines_view_V2/FeatureServer | 16 | id only | — | none stated |

- **Loudoun (rank 3).** It is the county's own data-centre pipeline, behind its "Existing & Potential Data Centers"
  web experience (https://experience.arcgis.com/experience/0c2021535c3f47589d1a10d6d0e2948c). Fields: application
  number, status, zoning case, overall square feet, acreage. Square feet is the only size measure a public source
  here gives for pipeline sites. The layers are owned by a staff account and named as snapshots
  ("Proposed_DC_Parcels_March1"), so the cadence is irregular and the layer could be renamed; the connector must alarm
  on a missing layer. The existing-parcel and by-right layers have an `Owner` column (by-right covers 2,291 parcels,
  where natural persons are likely): never read it. Effort S: it is the Virginia DEQ connector's pattern.
- **Built (2026-10-24, lane dc-local).** `us.va.loudoun.data_centers`
  (`pipeline/connectors/us_va_loudoun_data_centers/`), weekly at 0.5 rps, credit "Source: Loudoun County,
  Virginia". 85 applications make 81 sites (4 same-land pairs): filed 47, permitted 34 ("Approved" proves a county
  land-use approval, nothing more). Through the resolver, 5 fold into held records (DEQ's own point lies inside
  their land); the other 76 are new, and 23 / 8 / 6 of them have no held point within 0.5 / 1.0 / 1.5 km. Two
  possible duplicates are reported, not merged (LEGI-2023-0097, STPL-2022-0051). 6 hold a Lane B (FAA) site:
  known duplicates until the resolver compares places. *Settled 2026-10-24 (lane resolver-veto, docs/22 §28.5):*
  on the live layer 8 sites hold an FAA site's point; 4 are one site (IAD121, Equinix DC17, ARA4A, Yondr NOV2), 4
  cannot be told (the FAA site mixes several buildings' structures). The resolver merges none, because the names
  share nothing and nearness never merges alone; they stay two records each until this connector cites the FAA
  site whose point lies in its land, as it cites DEQ's air sites. Nearness is not identity here: ICIS-Air's and DEQ's points
  for one Loudoun facility are a median 190 m apart. Rows, counts and terms: `data/sources.yaml`.
- **Prince William (rank 6).** It is the richest layer found: status, case number and planned floor area per campus
  and per building. It is restricted by its own metadata. The owner should ask the county GIS office
  (`pwcmaps@pwcgov.org`, per the metadata) for a licence that permits republication. Until then: no connector.
  Staff user names (`CreateUser`, `LastEditUser`) must be dropped either way.
- **Stafford.** The parcel layer carries `OwnerName1`, `OwnerName2` and owners' mailing addresses: personal data,
  never read. The project-outline layer has only an id.

Proximity understates novelty in a market this dense: at 1.5 km almost every parcel in Ashburn is "near" something
we hold. The 1.0 km figure is the central estimate; a parcel-to-record join by zoning case or permit number (both
layers carry them; DEQ does not) would settle it.

#### Granicus Legistar Web API (rank 7)

- **Access.** `https://webapi.legistar.com/v1/{client}/matters` with OData filters (examples page
  https://webapi.legistar.com/Home/Examples). Replies are capped at 1,000 rows. "Items returned to GET requests are
  limited to those items marked as public and available for view on InSite"; "Some clients require use of API
  tokens". `webapi.legistar.com/robots.txt` answers 404.
- **Terms.** None published on the API site, and none on a client InSite page checked (mesa.legistar.com). The
  content is each government's public record. Per-client terms must be read before publication; until then the
  matters are leads that link out.
- **Measured.** 93 candidate client names were tried, one request each. 33 answered; the rest are not clients (HTTP
  500), and Philadelphia answered 403. One filtered query per client (`substringof('data center', MatterTitle)` or
  `MatterName`) found **177 matters since 2024**. 43 are project-specific: rezonings, site plans, design reviews,
  special-permit waivers and abatements (Mesa 16, e.g. ZON25-00626 "NTT Mesa PH10", 170 acres; Fort Worth 10, e.g.
  ZC-26-039, 68.49 acres to "PD/I"; Phoenix 7, waivers for Aligned and 5C). 58 are policy (moratoria in DeKalb,
  Madison, Seattle and Milwaukee; ordinances), 19 are the government's own IT, and 57 are other. That is an estimated
  20 to 25 distinct projects. Maricopa County's Legistar holds one test-like 2017 matter, Prince William's (`pwcgov`)
  none, and Loudoun is not a client.
- **Personal data.** Yes. Fort Worth zoning titles list individual landowners beside the applicant (ZC-26-041 names several);
  a Phoenix title names an individual owner; Mesa design-review items name staff planners; Columbus public notices
  carry a contact's name, phone number and email. An extractor must keep only the case number, address or
  intersection, acreage, zoning change, applicant organisation and dates.
- **Effort.** L: a per-client registry, title parsing, geocoding of addresses and intersections, and person
  redaction. Other agenda platforms (CivicClerk, BoardDocs, CivicPlus AgendaCenter) cover the counties Legistar does
  not, one platform at a time.

#### City building permits (rank 11)

Socrata catalog search (`building permits`, 474 datasets). Rows whose description names a data centre:

| Dataset | All time | Since 2024 | Notes |
|---|---|---|---|
| Mesa (https://citydata.mesaaz.gov/d/dzpk-hxfb) | 77 | 33 | NTT, EdgeCore and MSA campuses; "See Terms of Use" |
| Chicago (https://data.cityofchicago.org/d/ydr8-5enu) | 178 | 19 | mostly interior fit-outs; "See Terms of Use"; contact columns |
| Collin CAD (https://data.texas.gov/d/82ee-gbj5) | 13 | 7 | Plano, Aligned: valuation $190.9 M, 280,000 sq ft; owner names column |
| Dallas (https://www.dallasopendata.com/d/e7gq-4sah) | 54 | — | no issue-date column found |
| Austin (https://datahub.austintexas.gov/d/quv8-5ckq) | 0 | 0 | public domain |
| Seattle (https://cos-data.seattle.gov/d/76t5-zqzr) | 26 | 0 | public domain |

They give floor area and valuation, but only a few campuses per city: hyperscale campuses mostly sit in
unincorporated county land. Contractor and owner columns must be dropped.

### 4.6 Utility and PUC filings (family 4)

Document-level and mostly aggregate. Customer names are redacted.

- **Georgia PSC.** Georgia Power's quarterly Large Load Economic Development Report, Docket 55378 (Q1 2026 filing,
  https://services.psc.ga.gov/api/v1/External/Public/Get/Document/DownloadFile/226607/107887): 76,200 MW pipeline,
  31 projects with an executed request for electric service (21 broken ground, 10 online). Values and names are
  marked trade secret and redacted. The PSC has a public document API (`services.psc.ga.gov/api/v1/External/Public`);
  terms not read.
- **Louisiana PSC.** U-37425 (Entergy, Meta Richland Parish) is listed as service "to a single customer for a project
  in North Louisiana" (https://lpsc.louisiana.gov/docs/agenda/August%2020%202025.pdf).
- **Virginia SCC.** `www.scc.virginia.gov/robots.txt` disallows our token; not fetched.
- **North Carolina NCUC.** `starw1.ncuc.gov` answered a challenge page; not bypassed.
- **Ohio PUCO.** `dis.puc.state.oh.us/robots.txt` disallows named AI crawlers only; not searched.
- **Texas PUCT Interchange.** robots 404; not searched in this lane.

Named per-project yield is near zero and needs extraction. Better used as aggregate context (pipeline MW by utility)
than as project records. Rank 15.

### 4.7 Sponsor announcements and SEC (family 5)

- **SEC EDGAR full-text search (rank 4).** The existing row `us.sec.edgar_fts` (attribution, derived_only; SEC
  "Accessing EDGAR Data", https://www.sec.gov/os/accessing-edgar-data). `q="data center campus"`, 8-K, 2026-01-01 to
  2026-10-23: **143 hits**. The first 100 come from 45 registrants: Applied Digital 11, Hyperscale Data 8, Galaxy
  Digital 8, New Era Energy & Digital 6, Digi Power X 5, Hut 8 5, TeraWulf 4, Soluna 4. 8-Ks usually name the
  campus, county, MW and tenant, which no permit source gives. Extraction is needed (M), and only derived facts with
  a link may be published. Distinct campuses were not counted; the estimate is 20 to 40 new, mostly converted mining
  sites and neocloud campuses in Texas, North Dakota, Pennsylvania and the Midwest.
- **Company newsrooms.** RSS checked: Microsoft (https://news.microsoft.com/source/feed/) 0 of 10 items name a data
  centre; Meta (https://about.fb.com/news/feed/) 1 of 10; Google (https://blog.google/rss/) 0 of 20; Amazon
  (https://www.aboutamazon.com/news/rss) 1 of 10, a community update; Oracle 403. QTS, Vantage, Aligned, Stack and
  Equinix `/feed` URLs answer HTML, not RSS; CyrusOne and Digital Realty 404. Each company's terms are unread. Leads
  only (rank 16).
- **GDELT** (`news.gdelt.doc`, derived_only). It answered HTTP 429 to two paced requests; not measured.

### 4.8 FERC, EIA and federal data (family 6)

- **FERC eLibrary** (`us.ferc.elibrary`, public domain). A description search for "data center" returns 131
  documents, 36 filed in 2026 (dockets ER 42, EL 20, QF 15, RM 7). Most are Data Center Coalition interventions in
  policy dockets (EL25-49 co-location, RM26-4 large-load interconnection, ER26-3515 PJM interim service). A few name a
  site: QF18-306 Form 556 of EXUS NM Data Center ("Facebook 3"); EC26-58 TeraWulf at Morgantown; ER26-1563 Vantage.
  Commenters are named individuals. Adding the query to the existing connector is S effort for about five site
  mentions a year; these are documents, not projects (rank 12).
- **FAST-41 Permitting Dashboard** (`us.permits_dashboard`). Two data-centre projects are known: QTS Richmond
  Technology Park Data Center 5, the first data centre covered
  (https://www.permitting.gov/newsroom/press-releases/first-data-center-project-gains-permitting-councils-fast-41-coverage),
  and Portsmouth Data Center
  (https://www.permits.performance.gov/permitting-project/fast-41-covered-projects/portsmouth-data-center). The
  connector's sector filter excludes both; widening it is a one-line change (rank 8). The dataset host failed TLS
  verification from this environment on 2026-10-23 and was not retried.
- **EIA.** EIA-860 and 860M list generators, not loads; EIA-861 has no customer names. No federal dataset names large
  loads per project. ERCOT still publishes no per-request rows (`data/sources.yaml`, `us.iso.ercot.large_load_queue`).

### 4.9 Found and rejected

- **WSDOT data-centre layers** (https://www.arcgis.com/home/item.html?id=a1daacf1d7c3486eb7e4dfcb9547ccfa) credit
  Business Insider and DataCenterMap: aggregator-derived, never used.
- **NVRC "Data Centers"** (265 rows, "Existing or Permitted",
  https://services5.arcgis.com/6MUPhDX27Ne3DNOw/arcgis/rest/services/Data_Centers/FeatureServer/0): a regional
  commission layer that states no source, with a liability disclaimer only. Not used.
- **PNNL Data Center Atlas**: built from "numerous data sources, some of them previously proprietary"
  (https://www.tricitiesbusinessnews.com/articles/pnnl-publishes-interactive-data-center-map;
  https://www.pnnl.gov/publications/mapping-future-data-centers-new-public-tool-illuminates-whats-next). Not used
  until its licence and provenance are read.
- Student, consultant and advocacy ArcGIS layers (PEC, university accounts, NAI Horizon): not primary sources.
- **OpenStreetMap `telecom=data_center` / `building=data_center`** (rank 17, ODbL, operating inventory). Not
  measured: overpass-api.de reset the connection and taginfo.openstreetmap.org's robots.txt disallows our token. The
  grid-osm lane already reads US Geofabrik extracts and can count these tags in the same pass.

## 5. Ranking, with the arithmetic

| Rank | Source | New active (central) | Effort units | Per unit | Display now |
|---|---|---|---|---|---|
| 1 | ECHO NPDES construction | 74 | 2 | 37 | yes |
| 2 | FAA OE/AAA | 73 (61 net of NPDES) | 2 | 36 (30) | yes |
| 3 | Loudoun pipeline | 18 | 1 | 18 | yes (attribution) |
| 4 | SEC 8-K campuses | 30 (estimate) | 2 | 15 | derived facts |
| 5 | Georgia EPD advisories | about 3 a month (36 a year) | 2 | 18 a year | no: terms |
| 6 | Prince William campuses | 12 | 1 | 12 | no: licence |
| 7 | Legistar | 20 (estimate) | 4 | 5 | no: per-client terms |
| 8 | FAST-41 sector scope | 1 to 2 | 1 | 1–2 | yes |
| 9 | Illinois DCEO | about 17 (mostly built) | 2 | low for pipeline | no: terms |
| 10 | Minnesota DEED | about 34 (mostly built) | 1 | low for pipeline | no: terms |

Gated sources rank after publishable ones of similar yield. Georgia EPD has the highest per-unit flow among them and
is the earliest signal in a top-three market, so it is first to clear. A facts-only, link-out record (county,
application number, reason, date) may be publishable even without a reuse grant; that is the legal lane's call, not
this lane's.

## 6. Proposed build lanes

One agent per file area. Each lane appends its line to `docs/CHANGELOG.md`. The coordinator wires the web lists
(`web/build_data.py` `SOURCE_POLICY` and `PROPOSAL_SOURCE_IDS`, `web/viewmodels.py` `PROPOSAL_SOURCE_LABELS`,
`services/api/my_projects.py` `AIR_PERMIT_SOURCES` or a sibling set for construction permits) after review, as for
the two existing connectors.

**Lane A — dc-npdes (data engineer, M).** Owns `pipeline/connectors/us_epa_echo_icis_npdes_construction/`
(`__init__.py`, `connector.py`, `status_map.yaml`, `test_connector.py`, recorded fixtures trimmed from the three CSV
members), and one new row in the data-centre block of `data/sources.yaml`. It imports, does not edit, the ranged-zip
helpers from `pipeline/connectors/us_epa_echo_icis_air/connector.py`. It owns the selection rule (name keywords, `SWC`
or `GPC`), the status map (EFF/ADC/PND → `under_construction`; TRM → `built` with "construction complete" as status
text; EXP → `unknown`), site clustering of phased permits, placement (exact only where the point passes the ICIS-Air
checks), and dropping `PERMIT_NAME`. Done: DQ pass on the 2026-10-18 file, about 220 active rows, tests on fixtures.

**Lane B — dc-faa (data engineer, M).** Owns `pipeline/connectors/us_faa_oeaaa_part77_cases/` (same file set) and one
new `data/sources.yaml` row. It owns the archive fetch (regions × current and previous year, 0.1 rps), the keyword
rule, the line-structure exclusion, a deterministic site key (state plus a 1 km grid cell, merged with neighbours),
the expiry rule (18 months after determination), `classify`-based sponsor filtering, and tests that prove no person
name reaches `raw`. Done: about 110 data-centre sites from the 2026 archive; the shutdown banner recorded as a
source-health note, not a failure.

**Lane C — dc-local (data engineer, S, with owner actions).** Owns `pipeline/connectors/us_va_loudoun_data_centers/`
(pipeline, existing-parcel and building layers; `Owner` never requested in `outFields`), and these
`data/sources.yaml` rows: the Loudoun row (built); `us.va.pwc.data_center_campuses` as `status: pending`,
`reuse: restricted`, `publication: none`; a refreshed `verified` block on `us.ga.epd.air_permit_advisories` (new
advisories URL, 2026-10-23 counts); and `us.legistar.webapi` as `status: pending`. It also drafts two owner actions
for `docs/66-owner-actions.md`: the Prince William licence request and the Georgia EPD reuse-terms request. The
Georgia advisory parser and Legistar extraction wait for those answers.

*Done 2026-10-24:* the Loudoun connector (§4.5); `us.va.pwc.data_center_campuses` and `us.legistar.webapi` as
pending rows; the Georgia EPD row refreshed (no connector yet; its terms are now read under the silent-site rule
of `docs/07` §2, derived facts only, held before paid use); the two letters as `docs/66` items 26 and 27.

## 7. Cleanview: what we can and cannot say

What Cleanview says publicly, as quoted in the coordinator's brief (2026-10-23; this lane did not open Cleanview's
site, under CLAUDE.md): it processes "local permits, interconnection queues, EIA filings, FERC documents, SEC filings
and dozens of other public sources", plus satellite imagery, and tracks "thousands of data center buildings",
including operating ones; its API calls records "audit-approved". We cannot verify the count, its date, or how a
building is defined, and we will not use their data to find out. What we can say is that the units differ. They
count **buildings**, operating ones included. We count **projects**: one record per real-world site, from a public
filing, and today mostly operating (487 of 582). Where a county publishes both units, one site holds several
buildings: Prince William has 247 data-centre buildings on 78 campuses (3.2 per campus), and Loudoun 239 permitted
buildings on 139 existing parcels (1.7 per parcel). A building count is therefore two to three times a site count for
the same ground, and the gap the owner sees is partly a unit difference. The rest is real: Texas, Arizona, Georgia,
Ohio and Nevada are thin in our store, and Lanes A and B address that.

The fair figure to quote is in our own unit and says where it comes from: "N US data-centre sites, each traced to a
named public filing, of which M are in active development (filed, approved or under construction, with a filing in
the last 24 months)". Today that is 582 sites, 73 active. After Lanes A and B it would be about 800 sites, roughly
210 active: 582, plus about 135 new active sites, plus 89 new NPDES facilities whose construction coverage ended,
before resolver merges. The 53 expired permits, whose outcome is unknown, are left out.
Do not set our sites against their buildings as if they were the same unit, and do not convert one into the other in
public copy. A like-for-like building count would need a building inventory (OpenStreetMap tags, §4.9; county
building layers in Loudoun and Prince William) and should be labelled as such.

## 8. Limits, assumptions and open items

- **Keyword selection is a lower bound.** Code-named projects are missed by NPDES and FAA names. An operator allowlist
  (hyperscaler campus codes such as IAD, PHX, CLT and DFW) would raise recall but needs its own precision check.
- **Placement precision of ICIS-NPDES geocodes is unstated** in the download. Assumption: treat points like ICIS-Air
  FRS points, exact only when they pass the same checks, otherwise county centroid. Lane A measures this.
- **Overlap is a proximity estimate.** The resolver, not these counts, decides identity. In dense counties, true
  novelty sits between the 1.0 km and 0.5 km figures.
- **Coverage holes after Lanes A–B:** construction coverage in Georgia, Virginia (since 2025), Ohio, Iowa, Nebraska,
  Nevada, North Carolina and Mississippi is not reported to ICIS. FAA partly fills these (Georgia 6, Ohio 4, Nevada 4,
  Iowa 3, Nebraska 3 new sites). Each state's own NOI register is a later candidate.
- **Federal shutdown.** FAA is not accepting new Part 77 notices on 2026-10-23, and the permitting-dashboard host
  failed TLS. Both are source-health facts to watch, not reasons to work around anything.
- **Not reached, and why:** Virginia SCC and LDEQ EDMS (robots disallow our token); azdeq.gov, azcommerce.com and
  starw1.ncuc.gov (challenge pages); www.ahs.dep.pa.gov and data.permits.performance.gov (TLS verification failed);
  faa.gov web policies (Akamai 403); GDELT (429); Overpass (connection reset); taginfo (robots).
- **Spotted in passing.** `docs/25` §3.10.4 cites datacenterHawk, baxtel and datacentermap pages as evidence for
  hand-checked rows. Those are on the private-aggregator list this lane was given. The citations should be replaced
  with primary sources by the doc's owner. *Done 2026-10-24 (lane dc-local):* two replaced (the MPCA permit record;
  Digital Realty's own ORD23 page), one claim removed (the World Bank row's floor area and power).

## 9. Requests made (2026-10-23)

| Host | Requests | What |
|---|---|---|
| echo.epa.gov | 10, archive reads at 0.1 rps | download summary page, robots.txt, one HEAD, four zip tails, three ranged members (178 MB compressed in all) |
| oeaaa.faa.gov | 15, archives at 0.1 rps | home, download page, four app scripts, nine 2026 archives (104 MB) |
| webapi.legistar.com, mesa.legistar.com | about 130 at 0.5 rps | home and examples pages; 93 `bodies?$top=1` probes; 33 filtered matter queries; one InSite page and its robots.txt |
| arcgis.com and county/state ArcGIS hosts | about 60 at 0.5 rps | three catalog searches; item JSON; layer metadata, counts and samples; full reads of six small county layers |
| Socrata (api.us.socrata.com, six city portals, data.texas.gov) | about 30 at 0.5 rps | catalog searches, metadata, counts, samples |
| epd.georgia.gov | 12 | pages, terms probes, seven advisory PDFs |
| efts.sec.gov, elibrary.ferc.gov | 1 and 3 | one full-text search; three eLibrary searches |
| inframation.io | 8 | our own load records (6 pages) and a search |
| others (state agencies, newsrooms, robots files) | about 70 | robots.txt and one page each |

Scratch scripts and derived counts stay in the lane's scratchpad; nothing was written under `data/`. Some survey reads
took whole rows (`outFields=*`, full CSV members), so they included the Loudoun `Owner` column, Prince William staff
user names, NPDES permittee names, FAA sponsor and representative names, Legistar titles and FERC commenter names.
Those scratch copies were deleted after counting on 2026-10-23. No person's name appears in this doc.
