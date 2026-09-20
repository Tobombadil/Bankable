# `services/ingest/data/` — vendored reference gazetteers

All three files here are inputs to `services/ingest/geocode.py`, never to a connector's `raw_place`.
None is a substitute for a real address-level geocoder (docs/21-data-model.md §3.7); all are
categorically `county_centroid` / `state_centroid` precision, never `exact`.

## `us_county_centroids.tsv`

**Source:** US Census Bureau, *2024 Gazetteer Files*, county national file —
`https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2024_Gazetteer/2024_Gaz_counties_national.zip`
(`2024_Gaz_counties_national.txt`). Columns kept: `USPS` (state), `NAME` (county/parish/borough/
municipio), `INTPTLAT`, `INTPTLONG` — a county's internal point (Census's own representative-point
field, not a bounding-box centroid) — and, since 2026-09-15, `GEOID` (renamed `county_fips` in the
vendored TSV): the source file's own 5-digit county FIPS (2-digit state + 3-digit county), used
unmodified as the standard national county key.

**2026-09-15 addition (`county_fips`):** the table as originally vendored had already dropped
`GEOID` — only `USPS`/`NAME`/`INTPTLAT`/`INTPTLONG` were kept (see "Columns kept" above, unchanged
since this README was written). The county-permit pilot (docs/00-PLAN.md decision 2026-09-15)
needs counties keyed by FIPS, not name, so `GEOID` was re-derived by re-downloading the same 2024
Gazetteer file linked above and joining each vendored row back to its source row: 3,205 of 3,222
rows joined on an exact `(USPS, NAME)` match; the other 17 (16 Puerto Rico municipios plus NM's
Doña Ana County) joined on `normalize_county_name()` instead, because the vendored `NAME` for
those rows carries mojibake (UTF-8 bytes re-decoded as Latin-1, e.g. `BayamÃ³n Municipio` for
`Bayamón Municipio`) predating this README — `normalize_county_name()` strips all non-ASCII
characters from both sides so the mangled and correct spellings still normalise to the same key,
and each of those 17 keys had exactly one candidate `GEOID` in the source file, so the join is
unambiguous. The mojibake itself was left as found (out of scope for this addition: `NAME` values
were not touched, only appended to). All 3,222 rows now carry a `GEOID`, including the six
state/independent-city pairs that collide under `normalize_county_name()` (`Baltimore city`
24510 vs `Baltimore County` 24005 (MD); `St. Louis city` 29510 vs `St. Louis County` 29189 (MO);
`Fairfax`/`Franklin`/`Richmond`/`Roanoke` city vs county (VA), each pair keeping its own distinct,
correct FIPS since the join used the row's exact name, not the collapsed key). Licence unchanged
(same public-domain source file; no new attribution or reuse term).

**Licence:** US federal government work, public domain (17 U.S.C. §105). No attribution required,
no reuse restriction.

**Provenance note:** this file was vendored by an earlier agent (`web/build_data.py`'s original
prototype table, per that module's docstring and `web/README.md`) before this README existed, and
that agent's retrieval date was not recorded. Verified today (2026-09-13) by re-downloading the
live 2024 county Gazetteer file above and diffing: all 3,222 rows match this vendored TSV exactly,
spot-checked field-by-field on Autauga County AL, Bibb County AL, Kings County NY and Yauco
Municipio PR (`INTPTLAT`/`INTPTLONG` identical to the six decimal places both files carry). Row
count: 3,222 US counties/county-equivalents/PR municipios + 1 header line = 3,223 lines.

**Format:** UTF-8 TSV, header `state\tcounty_name\tlat\tlon\tcounty_fips`, one row per county.

## `gb_substations.tsv`

**Source:** National Energy System Operator (NESO) Data Portal, dataset *"Regional breakdown of
FES data (Electricity)"* (`regional-breakdown-of-fes-data-electricity`,
`https://www.neso.energy/data-portal/regional-breakdown-fes-data-electricity`), resource
*"FES 2024 Grid Supply Point Info"* —
`https://www.neso.energy/data-portal/regional-breakdown-fes-data-electricity/fes_2024_grid_supply_point_info`,
CSV download
`https://api.neso.energy/dataset/963525d6-5d83-4448-a99c-663f1c76330a/resource/21c2b09c-24ff-4837-a3b1-b6aea88f8124/download/fes2024_regional_breakdown_gsp_info.csv`.
Retrieved 2026-09-13 (HTTP 200, 373 data rows, columns `GSP ID, GSP Group, Minor FLOP, Name,
Latitude, Longitude, Comments`). Only `Name`, `Latitude`, `Longitude` are vendored here — a
transmission Grid Supply Point (GSP) is a real, named, fixed substation, not the TEC register
project itself, so nothing but the site's identity and location is needed.

**Why this dataset and not the TEC register's own portal listing:** the candidates named in the
task, checked in order —

1. NESO's GIS boundary datasets (`gis-boundaries-for-gb-grid-supply-points`,
   `gis-boundaries-for-gb-generation-charging-zones`, ETYS boundary shapefiles): all are *Thiessen
   polygons* around GSPs or study-boundary paths, not substation points, and boundaries share the
   same underlying GSP names as the FES dataset below — no additional coverage, more parsing (GIS
   formats), so not used.
2. National Grid Electricity Transmission's own "Transmission Network Route Map Shapefiles" (Open
   Net Zero): its own documentation states substation geocoordinates are **not** included ("can be
   estimated from maps" only) and its licence restricts use to "emergency and land use planning...
   not for commercial purposes" — fails both the coordinate requirement and Bankable's commercial
   reuse need. Not used.
3. This FES dataset (chosen): NESO's own `package_show` for
   `963525d6-5d83-4448-a99c-663f1c76330a` returns `license_title: "NESO Open Data Licence"`,
   `license_id: "ESO"`, `license_url: "https://www.neso.energy/data-portal/ngeso-open-licence"`
   (redirects 200 to `https://www.neso.energy/data-portal/neso-open-licence`) — the identical
   licence already quoted in full in `docs/13-legal-data-rights.md` §2.5 for the TEC register
   itself. Per that licence's own per-dataset caveat ("each dataset... is licenced on an individual
   basis"), this is that dataset's own licence record, not inherited by assumption. Required
   attribution (from the quoted text): **"Supported by National Energy SO Open Data"**.
4. OS Open Names and OpenStreetMap were not fetched: NESO/OGL-family coverage above was sufficient
   to answer the task and the source note above already has a real, on-portal, GSP-level dataset
   under the same licence as the register it enriches — per the task's own instruction to prefer
   NESO/OGL when in reach, a share-alike (ODbL) source was not needed and its terms were not
   evaluated.

**Coverage measured (2026-09-13):** matching every distinct NESO TEC register "Connection Site"
string against this gazetteer, via `SubstationGazetteer`/`normalize_substation_name` in
`services/ingest/geocode.py` —

| Register | Distinct Connection Site strings resolved | Rows resolved |
|---|---|---|
| `tests/fixtures/neso_tec_register.csv` (38 rows, 30 distinct sites) | 6 / 30 (20.0%) | 9 / 38 (23.7%) |
| Live TEC register, `tec-register-11-september-2026.csv` (2,198 rows, fetched via the connector's own `package_show` -> CSV path), 1,237 distinct sites | 346 / 1,237 (28.0%) | 739 / 2,198 (33.6%) |

The gap is structural, not a normalisation shortfall: a large share of TEC "Connection Site"
strings name a substation built new, specifically for that generator/BESS project (e.g. "Aberarder
Extension Wind Farm 132/33kV Substation", "New Connagill 275/132kV Substation") or an internal
connection node ("North Humber Connection Node C 132kV Substation") — neither exists in any
general-purpose gazetteer, because it is private, project-specific infrastructure, sometimes not
yet built. Only rows connecting into the existing, named public Grid Supply Point network resolve.
Unmatched sites are correctly `unknown`, not guessed.

**Deduplication:** the live GSP list has 373 rows and 10 cases of two rows sharing a normalised
name after this module's tolerant matching (typically the same physical site split by voltage
level or busbar, e.g. `Iver 132kV` / `Iver 66kV`, coordinates identical or within ~0.002° — see the
`Comments` column in the source CSV, e.g. `GSP "DUMF" modelled as 3 parts (split busbar)`). Two
rows were byte-identical (`Name`, `Latitude`, `Longitude` all equal) and were dropped when
vendoring; the other 8 collisions are kept as separate rows (both are real, distinctly-named GSPs)
and `SubstationGazetteer.load()` keeps the first coordinate seen for a given normalised key — safe
at this module's `county_centroid`-scale precision, never `exact`. 371 rows vendored.

**2026-09-20 addition (`region`):** the settlement tier below needed a GB transmission geography to
constrain itself with, and no licence-area polygon is published under terms this repository has
read. The source CSV's own `GSP Group` column — dropped when this table was first vendored, see
"Only `Name`, `Latitude`, `Longitude` are vendored here" above — is exactly that geography, and it
comes under the licence already recorded for this dataset. The same CSV linked above was
re-downloaded (2026-09-20, HTTP 200, 16,416 bytes, still 373 rows and the same seven columns) and
each vendored row joined back to its source row on the exact `(Name, Latitude, Longitude)` triple:
all 371 joined, the only textual difference being a trailing space in `Dunbar B ` that the original
vendoring had already stripped. Group letters were collapsed to the three transmission areas:
`_P` → `north_scotland` (72 rows), `_N` → `south_scotland` (94), the other twelve groups →
`england_wales` (205).

The collapse to three areas also resolves the only two ambiguities in the join: `East Claydon`
appears under both `_B` and `_H`, and `Ferrybridge B` under both `_M` and `_F` — in each case two
groups that are both England and Wales, so the region is unambiguous even though the group is not.
No row's `name`, `lat` or `lon` was touched; the diff is one added column. The mapping is not
asserted from the group letters' published names alone — it is corroborated by the data: sorting
the 14 groups by mean latitude puts `_P` first (55.59–60.28, mean 57.19, includes Shetland) and
`_N` second (54.85–56.34, mean 55.77), with every other group at or below `_F`'s 55.14 maximum.
Note that `_P` and `_N` overlap in latitude by three quarters of a degree, which is why a latitude
band cannot separate north from south Scotland and `region_of_point()` does not use one.

**Format:** UTF-8 TSV, header `name\tlat\tlon\tregion`, one row per named GSP (no comment/provenance
lines in the data file itself, per this README).

## `gb_settlements.tsv`

**Source:** Office for National Statistics, *Index of Place Names in Great Britain (July 2024)*,
published on the ONS Open Geography Portal —
`https://geoportal.statistics.gov.uk/datasets/208d9884575647c29f0dd5a1184e711a/about`, downloaded
2026-09-20 from `https://www.arcgis.com/sharing/rest/content/items/208d9884575647c29f0dd5a1184e711a/data`
(HTTP 200, 7,991,422-byte ZIP containing `IPN_GB_2024.csv`, 104,395 data rows, plus the user
guide in `.pdf` and `.odt`). Vintage: the index as at December 2023, published July 2024 — the
latest IPN on the portal on the retrieval date. The CSV is cp1252-encoded, not UTF-8 (161 place
names carry Gaelic/Welsh accents that fail a UTF-8 decode); the vendored TSV is UTF-8.

**Licence:** Open Government Licence v3.0. The portal item's own `licenseInfo` field points at
`https://www.ons.gov.uk/methodology/geography/licences`, which states (retrieved 2026-09-20):
"Under the terms of the Open Government Licence and UK Government Licensing Framework …, if you
wish to use or re-use ONS material, **whether commercially or privately**, you may do so freely
without a specific application for a licence". The IPN's own user guide (in the download) gives
the copyright block: "© Crown copyright 2024. Contains Ordnance Survey data © Crown copyright and
database right 2016. You may re-use this information (not including logos) free of charge in any
format or medium, under the terms of the Open Government Licence" with the OGL v3 URL. OGL v3
itself (retrieved 2026-09-20, `https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/`,
already quoted in `docs/13-legal-data-rights.md` §2.4) grants the right to "exploit the Information
commercially and non-commercially … by including it in your own product or application", against
an attribution condition. Required attribution for this file, per the ONS page and the user guide:
**"Source: Office for National Statistics licensed under the Open Government Licence v3.0"** and
**"Contains OS data © Crown copyright and database right 2024"**. (The ONS page gives that second
statement as a template with a bracketed `[year]`; the user guide's own copy of it reads "database
right 20167", an evident typo for 2016 in a 2024 publication. 2024 is used here, matching the
edition vendored — the discrepancy is recorded rather than silently corrected.)

Two third-party elements in the source were checked and are *not* in the vendored table: the
user guide credits the Historic County Borders Project (`http://www.county-borders.co.uk`) for the
historic-county columns, which are dropped; and ONS's Royal Mail condition applies to its postcode
and UPRN products, which the IPN is not (it carries no postcode or UPRN field).

**Why this dataset and not the other candidate**, checked in order —

1. **Ordnance Survey OS Open Names** (`https://api.os.uk/downloads/v1/products/OpenNames`, version
   2026-07, open download, no API key). Terms read at source: the `Doc/licence.txt` inside
   `opname_csv_gb.zip` (read by a ranged request for the file's own bytes, not by downloading the
   103 MB archive) says "Your use of OS OpenData is subject to the terms at
   http://os.uk/opendata/licence", which redirects 200 to the same National Archives OGL v3 page,
   and gives the attribution statements "Contains OS data © Crown Copyright and database rights
   2026", "Contains Royal Mail data © Royal Mail copyright and database right 2026", "Contains
   National Statistics data © Crown copyright and database right 2026". So its terms are
   **equally suitable** — OGL v3, commercial reuse permitted. It was not chosen on fit, not on
   licence: the CSV distribution is a 103 MB zip of 820 per-grid-square files covering roads and
   postcodes as well as settlements, and its coordinates are British National Grid eastings and
   northings (EPSG:27700), which would need an OSGB36→WGS84 transform — a new dependency and a new
   class of error — before anything here could use them. The IPN is one 47 MB CSV with `lat`/`long`
   already in WGS84 and one row per named place. OS Open Names remains the better source if this
   tier ever needs road or postcode names.
2. **This ONS IPN (chosen).** OGL v3 as above, WGS84 coordinates, a `descnm` place-type column that
   separates localities from administrative areas, and local-authority columns that make an
   individual placement auditable by eye.
3. **OpenStreetMap / Nominatim was not fetched and its terms were not relied on.** ODbL 1.0 puts a
   share-alike obligation on a derived database (`docs/13-legal-data-rights.md` §2.10 quotes §4.4);
   whether extracting settlement points into this table would trigger it is open question 7(b) for
   counsel, not a lane decision, and this product is commercial. An OGL source answers the same
   question with no such question attached.

**How it was trimmed.** From `IPN_GB_2024.csv`, rows of the settlement-ish place types only —
`LOC` (locality, 61,569 rows), `BUA` (built-up area), `PAR` (parish), `COM` (Welsh community);
89,689 rows in all. Administrative-area types (`WD` ward, `CED`, `UA`, `MD`, `LONB`, `CA`, `CTY`,
`CTYLT`, `CTYHIST`, `NPARK`, `RGN`) are excluded because their centroids are not settlement points.
Rows are grouped by the normalised name key (upper-case, punctuation to spaces, whitespace
collapsed — `normalize_settlement_name()` in `services/ingest/geocode.py`). One output row per key
that has at least one `LOC` record; a key with none can never be placed and is dropped. Columns
kept: `place23nm` (the `LOC` spelling, sorted first where several), `lat`, `long` rounded to four
decimals (~11 m, far finer than this tier's honest accuracy), `lad23nm` (local authority district)
and a derived `places` count. **52,110 rows vendored** (2.0 MB), of which 47,027 carry coordinates.

`places` is the number of distinct real places the name denotes: more than one distinct `LOC`
point makes it that count directly, and otherwise each non-`LOC` record further than 10 km from
the single `LOC` point adds one. A row with `places > 1` is vendored with **empty** `lat`, `lon`
and `district` — it exists only to record that the name is ambiguous, so the module can tell "this
is several real places" from "this is not a place at all". The 10 km rule is what separates one
place recorded twice (a village's `LOC` and its `PAR` record, metres apart) from two places
sharing a name; it is never used to choose between two places, because two `LOC` points always
make the name ambiguous regardless of distance. Without the cross-type check, "Coddington" would
have resolved: it has a single `LOC` record (Amber Valley, Derbyshire) but `BUA`/`PAR` records for
three other Coddingtons, including the Newark one the TEC register actually means, 50 km away.

`district` is not used at runtime. It is the audit column: it lets a reviewer check a placement
("Navenby → North Kesteven") without re-downloading 8 MB from ONS. It costs about 0.7 MB of the
file.

**Word boundaries are kept**, unlike `gb_substations.tsv`'s key. That table is 371 hand-checked
names where folding "Upperboat" onto "Upper Boat" was verified to introduce no collision. Folding
them across 52,110 settlement names does introduce collisions: it put the TEC register's "Whitelee
275/33kV" — the Whitelee wind farm south of Glasgow — onto "White Lee" in Kirklees, 220 km away.

**The region precondition.** The settlement tier places nothing unless the caller names the
transmission owner the register carries in its `HOST TO` column (`NGET`, `SPT`, `SHET`, `OFTO`),
and refuses any match whose point falls in another owner's transmission area. This is a
precondition, not a filter applied afterwards: wiring `country` through at `loader.py`'s call site
therefore cannot, by itself, switch on unconstrained settlement placement — a caller has to supply
the region as well, deliberately. `OFTO` is absent from the mapping on purpose: an offshore
transmission owner has no onshore area, so its 15 rows in the stored snapshot can never place.

The area test is `SubstationGazetteer.region_of_point()`: a nearest-neighbour classifier over the
371 vendored GSPs and their `region` column. It is not a boundary polygon, because none is
published under terms this repository has read, and it is not a latitude band, because `_P` and
`_N` overlap by three quarters of a degree (above) so bands cannot separate them — the earlier
band proxy flagged Carradale in Kintyre and Cousland in Midlothian as impossible when both are
fine. The GSP network *is* the transmission system, so "whose network is this point in the
catchment of" is the same question `HOST TO` answers, and it needs no tuned radius. Branxton in
Northumberland classifies as `south_scotland` because its four nearest GSPs are Eccles, Berwick,
Galashiels and Hawick — which is the intended semantics, not an error.

**The mapping is validated where both sides are known independently.** For every Connection Site
the GSP tier already places, the register gives an owner and the gazetteer row gives a
NESO-sourced region. **345 of 347 site/owner pairs agree (99.4%).** The two that do not are
`Westfield 132/33kV` and `Whitehouse 275kV Substation`, both recorded `SHET` while sitting in
south-Scotland territory — register-side inconsistencies, not classifier errors.

**Coverage measured (2026-09-20)** against the stored TEC register snapshot
`data/snapshots/gb.neso.tec_register/20260913T202618Z.csv` (2,198 rows, 1,237 distinct Connection
Site strings), through `geocode(..., country="GB", region=<HOST TO>)`:

| | Rows placed | Distinct sites placed |
|---|---|---|
| GSP tier only (before) | 739 / 2,198 (33.6%) | 346 / 1,237 (28.0%) |
| + settlement tier, **no region constraint** | 1,068 / 2,198 (48.6%) | 512 / 1,237 (41.4%) |
| + settlement tier, **region-constrained (shipped)** | **1,049 / 2,198 (47.7%)** | **499 / 1,237 (40.3%)** |
| added by the settlement tier | +310 rows | +153 sites |

Full outcome accounting over the 1,459 rows the GSP tier leaves, which sums exactly to 1,459:

| Outcome | Rows | Sites |
|---|---|---|
| `placed` | 310 | 153 |
| `outside_region` — resolved, refused by the constraint | 19 | 13 |
| `unknown_region` — `OFTO`, no onshore area | 15 | 15 |
| `ambiguous` — the name is several real places | 105 | 56 |
| `not_a_settlement` | 1,010 | 654 |

**What the constraint cost and what it bought.** It refused 19 rows / 13 sites that would otherwise
have been placed. Inspected one by one:

* **13 rows / 8 sites correctly refused** — every wrong placement the unconstrained tier was known
  to make: `Greens` (4 rows, a SHET site for the Caledonia and Stromar offshore farms, onto a
  Greens in Rossendale), `Torness` (2, the East Lothian station onto a Torness in Highland),
  `Blacklaw` and `Blacklaw Extension` (3, South Lanarkshire onto an Aberdeenshire Blacklaw),
  `Griffin` (1, Perthshire onto Lancashire), `Nant` (1, Argyll onto Wrexham), `Fairburn Extension`
  (1, Highland onto North Yorkshire), `Blackcraig` (1, a Dumfries and Galloway wind farm onto a
  Perth and Kinross Blackcraig).
* **3 rows / 3 sites lost that were right.** `Cousland 400kV` and `Dunlop 132kV Substation` are
  both recorded `NGET` while standing in Midlothian and East Ayrshire — the same register-side
  inconsistency as Westfield and Whitehouse above, so the classifier is right and the field is
  odd. `Gretna 400/132kV` is the one true classifier artefact: Gretna is a kilometre from the
  border and its nearest GSP is Harker near Carlisle at 9 km (`england_wales`) against Chapelcross
  at 10 km (`south_scotland`), so a `SPT` row is refused on a 1 km margin.
* **3 rows / 2 sites uncertain.** `Limekilns` (`SHET`, a Fife place) and `Middlemuir` (`SPT`, an
  Aberdeenshire place) have the same shape as the register-side disagreements and were not resolved
  either way.

**Residual error, re-measured.** The independent latitude-band cross-check that originally found
11 impossible placements in 166 now finds **0 in 153** (bands retuned to the measured group
extents above: SHET ≥ 55.5, SPT 54.6–57.0, NGET ≤ 55.9). That check is weaker than the constraint
by construction, so it is a floor, not a proof.

**What the constraint cannot catch.** A collision *within* one owner's area. `Norton East 400kV
Substation` is an `NGET` site whose projects (Grindon Grange, Letch Beck Energy Park, Zenobe
Norton) are in County Durham and Teesside; it still places on "Norton East" in Cannock Chase,
Staffordshire, because both are England. Nothing in the register distinguishes them at this
resolution. It is the one known-wrong placement that survives.

**Spot check (2026-09-20, re-run over the constrained set).** The thirty highest-row-count
placements were checked by name against their real locations; **29 of 30 agree** — Cilfynydd
(Rhondda Cynon Taf), Navenby (North Kesteven), High Marnham (Bassetlaw), Necton (Breckland),
Biggleswade, Penwortham (South Ribble), Chirk (Wrexham), Longside (Aberdeenshire), Monk Fryston
(North Yorkshire), Langage (South Hams), Carradale (Argyll and Bute), Branxton (Northumberland),
Rhigos (Rhondda Cynon Taf), Fiddlers Ferry (Warrington), Peterhead (Aberdeenshire), Didcot (South
Oxfordshire), New Deer (Aberdeenshire, two sites), Hams Hall (North Warwickshire), Whitson
(Newport), Market Harborough (Harborough), Dalkeith (Midlothian), Dumfries, Gwyddelwern
(Denbighshire), Keith (Moray), Edinbane (Highland/Skye), Eggborough (North Yorkshire), Ferrybridge
(Wakefield). `Greens` has dropped out of the set entirely, refused by the constraint. `Norton
East` is the one that is still wrong, for the reason above.

**Format:** UTF-8 TSV, header `name\tlat\tlon\tdistrict\tplaces`, one row per normalised place
name; `lat`, `lon` and `district` are empty exactly when `places` is greater than 1.

## Not done / left to the coordinator

`docs/21-data-model.md` §3.7's `geocoder` vocabulary now names both GB tiers (`gb_substation`
2026-09-13, `gb_settlement` 2026-09-20), but nothing sets either yet. `geocode()` cannot report
which tier produced a point: it returns the same 2-tuple `(point, precision)` it always has,
because `services/ingest/loader.py` unpacks it positionally and neither task touched `loader.py`.
When the coordinator wires the `country` argument through at `loader.py`'s
`geocode(state, county, gaz=gaz)` call site (see the note in `geocode.py`'s module docstring), it
should also set `Location.geocoder` there for GB rows that resolved — `gb_substation` when
`default_substation_gazetteer().substation_point(site)` returns a point, `gb_settlement`
otherwise, which is exactly the order `geocode()` itself tries them in.

Three further items are the coordinator's, not this file's:

* **The connector must pass `HOST TO`.** The region precondition is built and enforced, but
  nothing supplies it yet: `geocode()` takes `region` as an explicit keyword, and
  `pipeline/connectors/gb_neso_tec_register/connector.py` keeps the Connection Site in the
  `county` field without carrying the owner anywhere. Wiring `country` through at `loader.py`'s
  call site without also passing `region` leaves the settlement tier placing nothing at all —
  which is the intended failure direction, but it does mean the two changes belong together.
* **A collision inside one owner's area is still unguarded.** `Norton East` (above) is the known
  instance. A finer region key would catch it — the register carries no such field today, so this
  would need a new signal (the project's own name, the customer, a DNO licence area) rather than a
  tighter use of what is there.
* **Re-vendoring cadence.** Both GB tables are annual publications (NESO FES, ONS IPN). Neither is
  a live feed; re-vendor when the next vintage appears and re-run the coverage measurement above.
  `gb_substations.tsv`'s `region` column comes from the same CSV as its coordinates, so a
  re-vendor must keep it.
