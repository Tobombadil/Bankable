# `services/ingest/data/` — vendored reference gazetteers

Both files here are inputs to `services/ingest/geocode.py`, never to a connector's `raw_place`.
Neither is a substitute for a real address-level geocoder (docs/21-data-model.md §3.7); both are
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

**Format:** UTF-8 TSV, header `name\tlat\tlon`, one row per named GSP (no comment/provenance lines
in the data file itself, per this README).

## Not done / left to the coordinator

`docs/21-data-model.md` §3.7's `geocoder` vocabulary (`source_provided | census_tiger | manual`)
has no value for this tier. `geocode()` cannot report which tier produced a point today — it
returns the same 2-tuple `(point, precision)` it always has, because `services/ingest/loader.py`
unpacks it positionally and this task does not touch `loader.py`. When the coordinator wires the
`country` argument through at `loader.py`'s `geocode(state, county, gaz=gaz)` call site (see the
note left in `geocode.py`'s module docstring), it should also set `Location.geocoder =
"gb_substation"` there for GB rows that resolved (`precision == "county_centroid"`), and extend
`docs/21` §3.7's `geocoder` vocabulary to name that value.
