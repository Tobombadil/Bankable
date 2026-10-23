# 25 — Siting: land parcels for Site finder Pro

Owner decision (2026-10-23, `docs/00-PLAN.md` decisions log): Site finder Pro is the first paid product, and land
parcels move into it. Texas comes first, from TxGIO StratMap, filtered by acreage and contiguous same-owner holdings.
Corridors alone stay the free screening view. Owner, verbatim: "if we cant find land parcels then how can a site
finder be valuable? its in the name? isnt there a way to find these sites/parcels?" This document answers the open
items that row left for the legal lane: the TxGIO licence, other states' free layers, the licensed nationwide options,
and whether Inframation stores owner names.

**I am not a lawyer and this is not legal advice.** Section 6 lists what goes to counsel.

**Reading convention** (as in `docs/13-legal-data-rights.md`). Blockquotes are verbatim source text. Text marked
*Inference* is my reading and carries a stated confidence. Where a page could not be read I say so.

**Retrieval.** Everything below was retrieved on **2026-10-23** unless stated otherwise. Method: HTTPS GET through
the session proxy with a generic research user-agent (`Mozilla/5.0 (research; terms review)`), plus a
fetch-and-summarise tool and a web search tool. Only terms pages, catalogue records, service descriptions
(`?f=json`, `iteminfo`, `metadata`) and public pricing pages were read. **No parcel records were downloaded or
queried** from any source. One exception, disclosed: an 87-row query of Minnesota's *metadata* layer (county name,
opt-in flag, acquisition date; no parcels) to count opt-in counties (§2). Captures are in the session scratchpad,
not committed.

---

## 0. The answer

**Recommendation, Texas v1 (confidence moderate-high):** build Site finder Pro's parcel layer on the TxGIO StratMap
2026 vintage. Its catalogue record states **CC0 1.0**, it covers 253 of 254 counties (only Donley is missing), and it
carries owner, acreage, state land-use code and value fields. Publish **derived parcel facts and no owner names**:
parcel id, acreage, state land-use category, land and market value, the parcel count, acreage and owner count of
contiguous same-owner assemblages, and a deep link to the appraisal district's own record, where the user reads the
owner (option (b), §4.7). Owner identity is used only as a keyed hash, to group parcels. Before launch, get TxGIO
to confirm in writing that the CC0 label covers a paid product. All 240 DataHub collections carry the same label, so
it reads as a catalogue default, not a per-dataset rights review (§1.1).

**Next three states (confidence moderate):** Virginia (VGIN geometry plus locality tables), Ohio (OGRIP standardized
statewide) and North Carolina (NC OneMap, "free and unrestricted use", owner and acreage fields). Wisconsin, Indiana
and Minnesota follow. Georgia, Arizona, Illinois, Pennsylvania, Oklahoma, Louisiana, Nevada and New Mexico have **no
free statewide layer usable by a private company** and need a licence (§2).

**Nationwide licence (confidence high on the arithmetic, low on the price):** no vendor publishes a price for a
licence that lets a platform show parcel attributes to its subscribers. Regrid's only public nationwide prices are
**end-user** licences ($50,000 to $135,000 a year, 2023 sheet). At an assumed platform price of $100,000 a year, the
licence pays for itself at about **59 net paying seats attributable to states without a free layer**: about 117
total seats if half of all seats are attributable. That is above `docs/11`'s year-1 band of 40 to 80 Pro seats.
Buy it when you need parcel attributes in about six or more licensed states, or when seats in licensed-only states
reach about 60. Quote first (§5.3).

**The three facts that decide it:**

1. TxGIO's DataHub catalogue record for Land Parcels (2026 vintage) states `license_name: Creative Commons Zero v1.0
   Universal`, and TWDB's copyright policy says it "freely grants permission to copy and distribute its materials"
   (https://api.tnris.org/api/v1/collections/?search=parcels; https://www.geographic.texas.gov/site-policies.html).
2. Regrid's own 2023 price sheet calls its nationwide prices "end-user annual parcel data licensing prices"
   (https://regrid.com/blog/2023-parcel-data-price-change). Its standard licence forbids "otherwise making the Data
   available to third parties" and allows only map rendering "in a manner that reasonably prevents the customers from
   obtaining a copy" (https://app.regrid.com/store/license).
3. Texas Tax Code §25.025(b) makes the home address of a named protected individual (peace officers, judges,
   family-violence victims and 34 other classes) "confidential and ... available only for the official use of"
   government bodies once elected. TxGIO says of its data: "No values from the original data have been changed or
   removed" (https://tcss.legis.texas.gov/resources/TX/htm/TX.25.htm; https://api.tnris.org/api/v1/collections/?search=parcels).

---

## 1. Texas: TxGIO StratMap Land Parcels

Publisher: Texas Geographic Information Office (TxGIO), a division of the Texas Water Development Board (TWDB),
formerly TNRIS. Program page: https://www.geographic.texas.gov/stratmap/land-parcels.html. Catalogue (DataHub):
https://data.geographic.texas.gov/ . Service:
https://feature.geographic.texas.gov/arcgis/rest/services/Parcels/stratmap_land_parcels_48_most_recent/MapServer .

### 1.1 The licence: what the text says

**DataHub catalogue record.** The DataHub web page (`data.geographic.texas.gov`, CloudFront) answered `HTTP 403`
("Request blocked.") to curl and to the fetch tool, so I could not read the rendered page. The same catalogue's API,
`https://api.tnris.org/api/v1/collections/?search=parcels` (HTTP 200, 39,111 bytes, `server: nginx/1.24.0
(Ubuntu)`), returns the seven Land Parcels vintages. Each record's images and resources are served from
`data.geographic.texas.gov/<collection_id>/...`, so this is the catalogue behind the DataHub. The 2026 record
(collection `1d33dafb-6e9c-40ac-887d-f4577909ed9b`), verbatim fields:

> `"name": "Land Parcels"`, `"acquisition_date": "2026-09-25"`, `"publication_date": "2026-10-01"`,
> `"license_name": "Creative Commons Zero v1.0 Universal"`, `"license_abbreviation": "CC0-1.0"`,
> `"license_url": "https://spdx.org/licenses/CC0-1.0.html"`, `"public": true`, `"authoritative": false`,
> `"source_name": "Various Appraisal Districts"`, `"file_type": "GDB,SHP"`

From the same record's `description`:

> Data is provided by county appraisal districts or their service providers and then translated in the TxGIO common
> schema. Data collection and transformation was completed by BIS Consultants and delivered to TxGIO. Users should
> note that the data available in the StratMap Land Parcels dataset is presented ‘AS-IS’ based on the delivery by the
> local appraisal districts or their vendors. No values from the original data have been changed or removed.
> Additionally, StratMap Land Parcels are intended to be used as basemap and cartographic data and are not for
> detailed analysis.

> This dataset currently excludes the following counties: Donley

*Measured:* the same API lists 240 collections (`/api/v1/collections/?limit=500`), and **all 240 carry `CC0-1.0`**.
Every vintage of Land Parcels (2019, 2021 to 2026) carries it.

**TWDB site policies**, https://www.geographic.texas.gov/site-policies.html (HTTP 200). They apply to "all pages
beginning with ... Texas Geographic Information Office (TxGIO) (geographic.texas.gov)":

> Copyright Policy The Texas Water Development Board freely grants permission to copy and distribute its materials.
> The agency would appreciate acknowledgment.

> TWDB complies with the State Web Site Link and Privacy Policy. TWDB encourages organizations that link to this web
> site to comply with the provisions of the State Web Site Link and Privacy Policy, especially regarding protection
> of the privacy rights of individuals, and to make reasonable efforts to provide accessible sites.

> TWDB makes no representations, guarantees, or warranties as to the accuracy, completeness, currency, or
> suitability for any purpose of the information provided via this web site.

**Program page FAQ**, https://www.geographic.texas.gov/stratmap/land-parcels.html (HTTP 200):

> TxGIO collects these data to allow end users to acquire statewide land parcel data in one location at no cost.
> These geographic land parcel data are created by appraisal districts or their third party vendor to supplement the
> appraisal process with a visual representation of property. They are not survey grade and should not be used for
> legal purposes.

> Yes, this data may sometimes be acquired through appraisal districts, their 3rd party vendor, or commercial
> vendors. More current data and greater volume of information may be available from them as compared to the no cost
> TxGIO statewide version.

> TxGIO received the data from the appraisal districts or their vendors “as-is.”

**Service metadata.** MapServer `?f=json`: `"copyrightText": "Texas Geographic Information Office, Various Counties,
Various Vendors"`. Layer 0: `"copyrightText": "TxGIO, Various Counties, Various Vendors"`. `info/iteminfo`:
`"licenseInfo": " None "`. TxGIO's portal item for the Web Experience
(https://portal.geographic.texas.gov/portal/sharing/rest/search?q=parcels&f=json) carries Esri's default text: "No
special restrictions or limitations on using the item's content have been provided."

**History that bears on it.** The 2019 record's description:

> Note: The following licensed land parcel datasets are available to Texas governmental entities by special request:
> Castro, Chambers, Cottle, Crockett, Crosby, Dawson, Donley, Frio, Gonzales, Houston, Jack, Kerr, Knox, Mason,
> Menard, Oldham, Roberts, Shelby, Ward, Wichita, and Wilbarger.

From 2021 all of these except Donley are in the public set; Donley is excluded in 2024, 2025 and 2026.

**The State Website Linking and Privacy Policy's copying paragraph** (agency, web address, date copied, no-endorsement
statement) is already quoted, from the TBAE reproduction, in `docs/13` §6.4. DIR's own page was unreadable then.
TWDB says it "complies with" that policy and "encourages" linkers to comply.

### 1.2 What it means for the four uses

| Use | Text | *Inference* | Confidence |
|---|---|---|---|
| Commercial use | CC0 on the catalogue record; TWDB "freely grants permission to copy and distribute" | Permitted. CC0 waives all rights the publisher holds; neither text limits purpose | high that TxGIO permits it; moderate that TxGIO can waive everything (see the residual risk below) |
| Display to paying users | No restriction in any text read | Permitted | moderate-high |
| Derived products (assemblages, acreage filters, scores) | CC0; TWDB grant | Permitted; no share-alike, no attribution duty under CC0 | high |
| Redistribution (raw rows, exports, API) | CC0; TWDB grant | Permitted on TxGIO's terms. Owner-name and mailing-address columns are held back for privacy reasons (§4), not licence reasons | moderate-high |

**Residual risk, and why the confidence is not "high" across the board.**

1. *The label is a catalogue default.* All 240 collections carry CC0, so it does not show that anyone reviewed the
   parcel data's rights specifically. TxGIO compiles data that 253 appraisal districts or their vendors made
   ("Various Vendors" in the copyright line), and CC0 can waive only what TxGIO holds. *Inference, moderate:* a CAD
   or its vendor could claim copyright in a parcel map. How such a claim interacts with the Public Information Act
   is a question for counsel (§6 item 1). It is not a reason to wait.
2. *TxGIO honours county restrictions.* The 2019 "licensed ... by special request" list, and Donley's continued
   exclusion, show that TxGIO keeps restricted counties out of the public set. *Inference, moderate:* the counties it
   publishes are ones whose CADs did not restrict public distribution. This raises confidence in display rights but
   does not settle commercial resale.
3. *The written confirmation that closes it:* an email to TxGIO (StratMap contact,
   https://www.geographic.texas.gov/contact.html) asking whether the CC0 designation on Land Parcels covers display
   and redistribution in a paid subscription product, and whether any county has asked for restrictions. Owner
   action, §7.

**Classification proposed for the register:** class `public-domain` (CC0 dedication; manifest `reuse: open`).
Publication: `raw_ok` at licence level, **`derived_only` in practice**. The owner, care-of and mailing-address
columns are never published (§4.7), so the restriction is a privacy rule, not a licence rule. Credit and notice:
CC0 requires none, but TWDB "would appreciate acknowledgment" and the state copying paragraph asks for agency, web
address, date and a no-endorsement statement. Render the same pattern as the RRC rows (`docs/13` §6.4):

- `attribution`: "Source: Texas Geographic Information Office (TWDB), StratMap Land Parcels, https://geographic.texas.gov; data from Texas appraisal districts"
- `notice`: "Neither Inframation nor this information, as presented on Inframation, is endorsed by the State of Texas or any state agency. Not survey grade; not for legal purposes."
- date copied: each record's `retrieved_at`.

### 1.3 Schema

TxGIO Land Parcel Schema, https://www.geographic.texas.gov/_uploads/TxGIO-land-parcel-schema.pdf (HTTP 200,
515,152 bytes). The live layer's field list (MapServer layer 0 `?f=json`) matches it. Columns marked *personal*
carry personal data when the owner is a natural person.

| Field | Schema description (verbatim) | Use in Pro | Personal? |
|---|---|---|---|
| `PROP_ID`, `GEO_ID` | "Source unique identifier that can be joined with other datasets" | parcel id; CAD deep link key | no |
| `OWNER_NAME` | "Owner of the parcel" | hashed for grouping only (§4.7) | *personal* |
| `NAME_CARE` | "Owner name in care of" | not stored | *personal* |
| `LEGAL_AREA`, `LGL_AREA_UNIT` | "Legal area per deed. The non-rounded value." | acreage (deed) | no |
| `GIS_AREA`, `GIS_AREA_UNIT` | "The area calculated within GIS" | acreage (GIS); recompute from geometry as a check | no |
| `LEGAL_DESC` | "Legal description from deed" | show (abstract/survey names are historic patentees) | rarely |
| `STAT_LAND_USE` | "Legal land use utilizing State of Texas land use codes" | land-use filter | no |
| `LOC_LAND_USE` | "Legal land use utilizing local land use codes" | stored, not filtered (codes differ by county) | no |
| `LAND_VALUE`, `IMP_VALUE`, `MKT_VALUE` | "Current value of land" / "of improvements" / "Current market value" | value per acre; improvement flag | linked to a person through the owner |
| `SITUS_*` (9 fields) | "The main address of this property" | shown only for non-residential land-use categories | a home address is *personal* |
| `MAIL_ADDR`, `MAIL_LINE1/2`, `MAIL_CITY/STAT/ZIP` | "The mailing address for the owner of the property" | hashed with the owner name for grouping; not stored | *personal* |
| `SOURCE`, `DATE_ACQ`, `TAX_YEAR` | source; "The date TxGIO acquired this data."; "The year of this dataset" | provenance | no |
| `FIPS`, `COUNTY` | county | filter | no |
| `YEAR_BUILT` | "Year structure built" | improvement flag | no |

`STAT_LAND_USE` holds the Comptroller's property categories, the codes CADs report to the Property Tax Assistance
Division: for example D1 "Acres - Qualified Open-Space Land", E "Non-Ag Land and Improvements", F1 "Commercial Real".
Labels are as printed in PTAD's value tables (e.g. https://comptroller.texas.gov/auto-data/PT2/PVS/2021P/0610619021D.php);
the guide is https://comptroller.texas.gov/taxes/property-tax/docs/96-313.pdf, not read in full. *Inference:* D1,
D2 and E are the categories for undeveloped tracts. The FAQ warns that "If data are missing, the data were not shared
with the request for information", so the land-use filter must treat blanks as "unknown", not "excluded".

### 1.4 County coverage

Measured from the catalogue API's `counties` field, per vintage:

| Vintage | Acquired | Published | Counties | Missing |
|---|---|---|---|---|
| 2019 | 2019-03-01 | 2019-04-01 | 230 | 24, including Ward, Crockett, Kerr, Chambers |
| 2021 | 2021-07-07 | 2021-07-28 | 254 | none |
| 2022 | 2022-07-01 | 2022-07-27 | 254 | none (14 counties not standardized to the schema) |
| 2023 | 2023-02-01 | 2023-08-09 | 254 | none (13 not standardized, incl. Bexar, Lubbock, Webb) |
| 2024 | 2024-02-01 | 2024-08-22 | 253 | Donley |
| 2025 | 2025-06-01 | 2025-09-11 | 253 | Donley |
| **2026** | **2026-09-25** | **2026-10-01** | **253** | **Donley** |

The 2026 resource list (`/api/v1/resources/?collection_id=1d33dafb-...&limit=400`) has 254 files: 253 county zips
and one statewide zip. Donley is absent; its 2019 entry was among the "licensed" counties.

**Large-load counties missing:** none known. Donley (Panhandle, county seat Clarendon) is the only county missing,
and I found no announced large load there (not exhaustively searched). The store cannot test this. A query of the
dev store (`web/.data/dev.db`, read-only) found no Texas data-centre or large-load records. ERCOT publishes
large-load requests without county detail (`docs/25-grid-interconnection-and-siting.md` §3.10.1).

**Not measured: attribute completeness by county.** Geometry coverage is not attribute coverage. TxGIO says
attributes arrive "as-is" and some are null. The 2026 description no longer lists non-standardized counties, but that
is not evidence they are standardized. A proxy, labelled as a different dataset: Land Records (a vendor) publishes
statewide fill rates for its Texas file of 14,169,098 records
(https://landrecords.us/products/us-tx-parcels): `ownername` 97.4%, `totalvalue` 86.1%, `taxacres` 88.5%, `usecode`
67.9%. *Inference, low-moderate:* TxGIO's fill rates are of the same order, since both come from CAD exports; the
land-use code is the weakest field. The build lane's first task is to measure TxGIO's own fill rates with one
aggregate statistics query on the public MapServer (count of non-null `owner_name`, `stat_land_use`, `mkt_value`,
`legal_area` grouped by `county`; it returns 253 rows of counts, no records). That query was out of this lane's brief.

### 1.5 Vintage, cadence, format, size

- **Vintage:** 2026, acquired 2026-09-25, published 2026-10-01 (catalogue record). `TAX_YEAR` and `DATE_ACQ` are
  per record; the FAQ: "The year and month of the land parcel dataset is included in the file name as YYYYMM."
- **Cadence:** "TxGIO will attempt to refresh annually from each appraisal district or their third party vendor. The
  refresh rate will vary across the state. Some counties may refresh multiple times within one year." And: "At no one
  time will there be a completed or final version of data as each data source has varying update schedules." The
  `stratmap_land_parcels_48_most_recent` service is the rolling current view.
- **Format:** "The land parcels are in shapefile and geodatabase format". Each zip holds "File geodatabase,
  Shapefile, Metadata" (2026 record). The service supports `JSON, geoJSON, PBF` queries, `maxRecordCount` 2000,
  visible from 1:500,000 to 1:1,000, `exportTilesAllowed: false`.
- **Size (catalogue `filesize`, bytes):** statewide `stratmap26-landparcels_48_lp.zip` **2,714,312,383 (2.71 GB)**;
  the 253 county zips together 6,178,503,066; largest Harris 429,896,042, Tarrant 313,374,530, Bexar 283,881,226;
  smallest Kenedy 352,951. Parcel count: not published by TxGIO. Vendors' Texas counts are Regrid 14,148,474
  (https://app.regrid.com/store/us/tx), ReportAll 14,304,632 (https://reportallusa.com/store/texas) and Land Records
  14,169,098. *Inference:* about 14 million polygons.
- **Access from this egress:** the program page, site policies, schema PDF, service JSON and the catalogue API all
  answered 200. `data.geographic.texas.gov` (DataHub UI, the supplemental report zip and every download URL) answered
  CloudFront 403. The ingest job must run from an egress TxGIO serves, or the owner downloads the zip in a browser.
  The supplemental report (`land-parcels-supplemental-report.zip`) is unread and may hold per-county notes.

### 1.6 Proposed manifest row (for the coordinator; `data/sources.yaml` and `docs/13` §6 are not this lane's files)

```yaml
- id: us.tx.txgio.land_parcels
  name: TxGIO StratMap Land Parcels (Texas appraisal-district parcels, statewide common schema)
  jurisdiction: US-TX
  category: land
  operator: Texas Geographic Information Office (Texas Water Development Board)
  url: https://data.geographic.texas.gov/collection/?c=1d33dafb-6e9c-40ac-887d-f4577909ed9b
  access: bulk_file
  auth: none
  format: zipped file geodatabase and shapefile per county and statewide (2.71 GB statewide, 2026)
  cadence: annual vintage (2026 published 2026-10-01); counties refresh on their own schedules
  license: 'CC0 1.0 Universal — TxGIO DataHub catalogue record, https://api.tnris.org/api/v1/collections/?search=parcels (retrieved 2026-10-23): "license_name": "Creative Commons Zero v1.0 Universal"; TWDB site policies: "The Texas Water Development Board freely grants permission to copy and distribute its materials."'
  reuse: open
  publication: derived_only
  attribution: 'Source: Texas Geographic Information Office (TWDB), StratMap Land Parcels, https://geographic.texas.gov; data from Texas appraisal districts'
  notice: 'Neither Inframation nor this information, as presented on Inframation, is endorsed by the State of Texas or any state agency. Not survey grade; not for legal purposes.'
  notes: >-
    derived_only is a privacy rule, not a licence rule: OWNER_NAME, NAME_CARE and MAIL_* are never stored in
    clear or published (docs/25-siting-parcels.md §4.7). Donley County excluded by TxGIO (2024 to 2026).
    Written confirmation of commercial display requested from TxGIO (docs/25-siting-parcels.md §7).
```

---

## 2. Other states: free statewide parcel layers

Priority order is the task's (data-centre and large-load activity). "Classification" is this register's scheme
(`docs/13` header) and is my inference.

| State | Free statewide layer? | Publisher and URL | Vintage | Terms (verbatim, from the item or site) | Owner names | Classification (*inference*) |
|---|---|---|---|---|---|---|
| **VA** | Yes, geometry | VGIN (Virginia Geographic Information Network, VDEM). Item `29627d7c051a47dc8ce71b4484531ab3` "Virginia Parcels", https://www.arcgis.com/home/item.html?id=29627d7c051a47dc8ce71b4484531ab3 ; companion "Local Schema Tables" `523d89ebf23d4d84957f9fe5b9158bd9` | "2026 Q3 Product", modified 2026-09-25; GDB 991 MB, tables 373 MB | "The Virginia Base Data layers are intended for cartographic use and spatial analysis only, and not for use as a legal description. ... all warranties ... are hereby expressly disclaimed." Description: "Attributes for these Virginia parcels are limited to locality identification and parcel id." No licence stated | **Not in the statewide layer.** Local schema tables carry whatever each locality sent: "Attributes differ from locality to locality" | `unknown` until VGIN confirms reuse in writing; disclaimer only. VDEM's own server (`gismaps.vdem.virginia.gov`) answered 502 through the proxy |
| **GA** | **No** | Georgia GIO publishes no statewide parcel layer that I found; county layers only (e.g. DeKalb, Glynn) | n/a | n/a | n/a | licence needed (ReportAll statewide store price $9,000, end-user, https://reportallusa.com/store/georgia) |
| **OH** | Yes | OGRIP, "Ohio Statewide Parcels Public View", item `26ab5fad8d5d4258a7492a14de83bc0e`, service https://services2.arcgis.com/MlJ0G8iWUyC7jAmu/arcgis/rest/services/OhioStatewidePacels_full_view/FeatureServer ; hub https://ohioparcels-geohio.hub.arcgis.com | item modified 2025-09-12; snippet "Updated Jan 21, 2024" | "The State of Ohio’s geospatial data are available for public use at the user’s own risk and provided for informational purposes only. The State of Ohio provides these data “as is” ..." Description: "produced from county-maintained parcel and assessment records and combined into a single, standardized statewide dataset." | **No owner-name field** in the public view (fields include `MailAddressAll`, `LandArea`, `StateLUC`, `CAMADataSite`). ODNR's separate "Statewide Parcels" map layer has `OWNER1`, `OWNER2` (https://gis.ohiodnr.gov/arcgis/rest/services/OIT_Services/odnr_landbase_v2/MapServer/4), no licence text | `permissive` ("available for public use"), moderate; vintage needs checking |
| **AZ** | **No** (public attributes) | ADOA AZ911 publishes "Arizona Statewide Parcels" vector tiles, public, no licence text (e.g. item `3b47079164aa4211bbdd266d708322ee`, 2026-10-06). Its parcel provisioning layer is for public-safety staff only | new tile packages several times a month (items dated 2026-08-31 to 2026-10-06) | none on the tiles. County records fall under A.R.S. §39-121.03 (below) | no | licence needed. A.R.S. §39-121.03 makes commercial use of county records obtained "from anyone other than the custodian" a treble-damages risk |
| **NC** | **Yes, full attributes** | NC OneMap (NC DIT, Center for Geographic Information and Analysis). Parcels page https://www.nconemap.gov/pages/parcels ; service https://services.nconemap.gov/secure/rest/services/NC1Map_Parcels/MapServer/1 ; terms https://www.nconemap.gov/pages/terms | all 100 counties plus the Eastern Band of Cherokee Indians; refreshed weekly (NC DIT brief, https://it.nc.gov/documents/files/nc-parcels-brief/open) | "committed to offering access to current State geospatial information at no charge. By sharing their content, all partner organizations understand this free and unrestricted use policy." "Written release agreements ... are not required and will not be issued." "Any sale of this data must not violate applicable state laws or regulations." | **Yes**: `ownname`, `ownname2`, `ownfrst`, `ownlast`, `mailadd`; also `gisacres`, `parusecode`, `parval`, `landval` | `permissive`, high |
| **PA** | **No** | PASDA hosts county layers, not a merged statewide one (https://www.pasda.psu.edu/) | n/a | n/a | varies by county | licence needed |
| **IN** | Yes, no owners | Indiana GIO / IndianaMap, "Parcel Boundaries of Indiana Current", https://gisdata.in.gov/server/rest/services/Hosted/Parcel_Boundaries_of_Indiana_Current/FeatureServer ; item `70565d886f1e4528a43a86be6ce5c2f3` | Data Harvest; prezipped 2022 GDB linked from the item | "CREDIT - It is requested, but not required, that the Indiana GIS Data Harvest Program be cited in any products generated from this data set." | **No** (fields: parcel ids, property address, `dlgf_prop_class_code`, tax districts; no acreage field, so compute it from geometry) | `permissive`, moderate-high |
| **IL** | **No** | The "statewide parcel fabric" on Prairie Research Institute's server is an IDNR database (https://gis.prairie.illinois.edu/arcgis/rest/services/OMLP/OMLP_Parcel_Fabric_No_LandUse/MapServer), *inference:* state-managed lands, not tax parcels | n/a | n/a | n/a | licence needed |
| **OK** | **No** | none found from OKMaps or the Tax Commission | n/a | n/a | n/a | licence needed |
| **LA** | **No** | parish layers only (e.g. East Baton Rouge, New Orleans) | n/a | n/a | n/a | licence needed |
| **NV** | **No** (exists, not public) | NV Division of Water Resources "County Parcels in Nevada", https://arcgis.water.nv.gov/arcgis/rest/services/BaseLayers/County_Parcels_in_Nevada/MapServer | 2024-10-22 data | "This dataset cannot be downloaded per NRS 250." "The GIS data for county parcels cannot be shared outside the department as directed by NRS 250." NRS 250.087 (https://www.leg.state.nv.us/NRS/NRS-250.html): "The State Demographer shall keep confidential the information provided to him or her pursuant to this subsection, except that the State Demographer shall provide such information at no charge to a state agency" | n/a | not available; county assessors or licence |
| **WI** | **Yes, full attributes** | Wisconsin DOA / State Cartographer's Office, "Wisconsin Statewide Parcels DB" V12, item `2386813b23ea4e51a009f7d1d6b76e02`, service https://services3.arcgis.com/n6uYoouQZW75n5WI/arcgis/rest/services/Wisconsin_Statewide_Parcels_DB/FeatureServer ; project https://www.sco.wisc.edu/parcels/data/ (TLS chain failed from here; fetch tool got 503) | V12.0.0, 2026; "The next release ... V13, is tentatively scheduled for June 30, 2027" | "This data free for public consumption as of: 06/30/2026" and "No warranty, expressed or implied, is made regarding accuracy, completeness, or legality of the information herein." (licence text on the V12 county items, e.g. `68fd1b7089b84bc0a1da1ded0c72e8fc`) | **Yes**: `OWNERNME1`, `OWNERNME2`, `PSTLADRESS`; also `GISACRES`, `DEEDACRES`, `PROPCLASS`, `CNTASSDVALUE` | `permissive`, moderate ("free for public consumption" does not say commercial) |
| **MN** | Partial: 59 of 87 counties | MnGeo, "Parcels, Compiled from Opt-In Open Data Counties", https://enterprise.gisdata.mn.gov/aghost/rest/services/us_mn_state_mngeo/plan_parcels_open/MapServer (layer 1 parcels; layer 0 metadata); disclaimer https://mn.gov/mngeo/gis-data-and-maps/disclaimer.jsp | metadata 2026-09-02 | "In obtaining this data from MnGeo, it is understood that you and/or your organization have the right to use it for any purpose." Not opted in (28): Beltrami, Blue Earth, Brown, Cottonwood, Dodge, Faribault, Freeborn, Goodhue, Hubbard, Kanabec, Kandiyohi, Kittson, Le Sueur, Lincoln, Mahnomen, Martin, Meeker, Nicollet, Nobles, Pine, Redwood, Rock, Roseau, Sibley, Swift, Todd, Wadena, Watonwan | **Yes** in schema: `owner_name`, `own_add_l1..4`, `tax_name`; also `acres_poly`, `acres_deed`, `useclass1`, `emv_total` | `permissive`, high for opted-in counties |
| **FL** | Yes, full attributes | Florida DOR Property Tax Oversight via Florida GIO, "Florida Statewide Parcels", item `efa909d6b1c841d298b0a649e7f71cf2`, service https://services9.arcgis.com/Gh9awoU677aKree0/arcgis/rest/services/Florida_Statewide_Cadastral/FeatureServer ; zip https://publicfiles.dep.state.fl.us/otis/gis/data/Cadastral_Statewide.zip (HEAD: 2,768,059,632 bytes, Last-Modified 2026-09-30) | "exported from these file submissions in August 2026"; 10.8 million parcels, all 67 counties | "The Department does not generate or maintain the data used to compile this statewide parcel map. The Department does not guarantee the accuracy, or completeness, or the compliance with public records laws of this parcel GIS data." "Each county is responsible for omitting confidential records subject to redaction under Chapter 119, Florida Statutes." | **Yes**: `OWN_NAME`, `OWN_ADDR1/2`, `OWN_CITY`, `FIDU_NAME`; also `LND_SQFOOT`, `DOR_UC`, `JV`, sale prices | `unknown`, low-moderate: disclaimer only. UF GeoPlan (2025-10-08, updated 2026-04-11): "All parcel layers (including historic versions) have been removed from FGDL per a request from the State Geographic Information Office (GIO). Until such time as we receive guidance on what may be permissible ..." (https://www.geoplan.ufl.edu/parcel-layers-removed-from-fgdl-per-state-request/) |
| **NM** | **No** (exists, not public) | NM Taxation and Revenue aggregates all 33 assessors (https://gis.ose.nm.gov/server_s/rest/services/Parcels/County_Parcels_2025/MapServer) | 2025 | "Do not share the service URL without first consulting the TRD GIS Coordinator. TRD / PTD will, under no circumstances, supply these data to any private sector entity." | n/a | not available; county assessors or licence |

**Basis for "No".** For GA, PA, IL, OK and LA, "no statewide layer" rests on web searches and ArcGIS Online
catalogue searches (`/sharing/rest/search`, "<state> statewide parcels") on 2026-10-23 that returned county layers
and vendors only. That is absence of evidence from two search routes, not a statement from the state; confidence
moderate. For NV and NM the state's own service text says the layer is not shared.

**State laws on commercial use of owner lists.** These bind whatever source the data comes from, licensed vendor
data included.

- **Arizona, A.R.S. §39-121.03**, https://www.azleg.gov/ars/39/00121-03.htm:

  > C. A person who obtains a public record for a commercial purpose without indicating the commercial purpose ... or
  > who obtains a public record from anyone other than the custodian of such records and uses it for a commercial
  > purpose shall in addition to other penalties be liable to the state or the political subdivision from which the
  > public record was obtained for damages in the amount of three times the amount which would have been charged for
  > the public record had the commercial purpose been stated ...

  > D. ... "commercial purpose" means the use of a public record for the purpose of sale or resale ... or the obtaining
  > of names and addresses from public records for the purpose of solicitation or the sale of names and addresses to
  > another for the purpose of solicitation ...

  *Inference, moderate:* an Arizona parcel layer in a paid product needs each assessor's commercial-purpose terms
  (or a vendor that has them) **before** launch. A vendor licence alone may not cure (C)'s "from anyone other than
  the custodian".
- **Idaho, Kansas, South Carolina.** Land Records' data terms (https://landrecords.us/terms-of-service) bar using
  owner information "as a mailing list, or to contact land owners for the purpose of selling or offering for sale any
  property or service", citing "Idaho Title 9, Chapter 3, Statute 948", "Kansas KORA Ch. 45, Art 2, Statute 230",
  "South Carolina Code Section 30-2-50". Statutes not read; secondary.

**Not covered in this pass:** other states with statewide layers (for example UT, CO, MT, AR, MD, NJ, NY, MA, VT,
WA, OR, ND). A follow-up pass should read their terms the same way before any is promised.

---

## 3. Licensed nationwide options

### 3.1 The distinction that decides feasibility

- **End-user licence:** the licensee's own staff use the data internally (GIS, analysis, their own decisions).
  Showing attributes to the licensee's customers is outside it.
- **Platform, display or redistribution licence** (vendors say "OEM", "reseller", "embedded", "per user module"): the
  licensee shows the data, or products derived from it, to its own subscribers. Pro needs this for every attribute it
  shows, and for geometry unless the licence carves out map rendering.

### 3.2 Vendors (public pages only)

| Vendor | Public pricing | Licence shape (verbatim where possible) | Per-record API price | Platform licence? |
|---|---|---|---|---|
| **Regrid** (Loveland Technologies) | 2023 sheet (https://regrid.com/blog/2023-parcel-data-price-change, posted 2022-12-01): "starting Jan 1st, 2023, our end-user annual parcel data licensing prices will be starting at": Nationwide Standard **$50,000**, Premium **$80,000**, Premium + Matched Building Footprints **$135,000**, Premium + Secondary Addresses $125,000; State (Data Store) $8,000 / $12,000 / $20,000 / $17,000; County $300 / $500 / $800 / $600. Today `regrid.com/pricing` redirects to "Contact enterprise sales" (https://regrid.com/global/us-parcels): no current public nationwide price | Data Store Licence (https://app.regrid.com/store/license): "LICENSEE shall not, and shall not permit any third party to, use the Data for the purposes of : (a) creating, offering, or selling a website, product, or service that may compete with any of Loveland’s products or services; (b) reselling the Data to third parties; (c) sublicensing the Data to third parties; or (d) otherwise making the Data available to third parties." It allows: "Render high resolution parcel polygons on maps viewable by Licensee’s customers, but in a manner that reasonably prevents the customers from obtaining a copy of a substantial portion of the parcel geometries." Credit "on each public facing webpage ... Public facing shall include pages accessible via password." One-year term, then "cease all use of the data or delete the data entirely." AI clause bars training except internal models. | Self-serve (https://support.regrid.com/changelog/self-serve-api-plans, v2025.9.23): "A monthly plan comes with 2,000 parcel records and 200,000 tiles", then "$0.10 per additional parcel record returned" (Standard), "$0.15" (Premium), "$0.001 per additional tile". Enterprise if you "Need access to more than 10,000 parcel records per month" (https://support.regrid.com/reference/plan-options.md). Monthly base price not shown without sign-in. FAQ: "all APIs are intended for use by software developers and are not for end users’ direct use" | Exists, by quote. Kalibrate: "The Regrid parcel data is licensed as an optional module in KLI and is priced per user" (https://regrid.com/blog/introducing-regrid-parcel-data-in-kalibrate-location-intelligence). The competing-product clause (a) is a live risk for a parcel-search product |
| **ReportAll USA** (Real Estate Portal USA LLC) | Store (https://reportallusa.com/store/texas): **Texas statewide $7,000**, counties $200–$250; Georgia $9,000; Virginia $5,000. ReportAll Online "Subscribe for $2.99/day or $49.99 per month" (https://reportallusa.com/store) | ToS (https://reportallusa.com/terms-of-service): "Your account created on the Site grants you permission to use the Site and Services, for both commercial and non-commercial purposes. You are not permitted to distribute, sell, or resell any of the Data that is provided through the Site." | No public per-record price. API page: "Flexible pricing that scales with your needs and usage", "free 30-day trial" (https://reportallusa.com/products/api). A search snippet of a "packages start as low as $1,875" figure is not on the current page: unverified | **Yes, advertised:** "Reseller licenses to offer our data as part of your broader services" (https://reportallusa.com/products/national-parcel-layer). Price by quote |
| **LightBox** (SmartFabric) | None public; "Contact Us" (https://www.lightboxre.com/data/bulk-data-delivery) | Not public | None public | Not stated publicly |
| **ATTOM** | "Basic Property API plans start at $500 per month" (https://www.attomdata.com/solutions/delivery/property-data-api/pricing/). Boundaries sit in Premium: "Premium provides access to more comprehensive property, historical, valuation, transaction and boundary data"; Premium is "Contact us for Pricing", and "Parcel Tiles" is listed "Available as add-on" | Site ToU (https://www.attomdata.com/terms-of-use/): "This license does not include the right for you to modify, resell, or make derivative works of any information on this Site"; data "is subject to the terms of a separate license agreement", not public | Not public per record | "Real Estate Data Platforms" is a listed segment; terms not public |
| **Cotality** (formerly CoreLogic), Parcel Essentials | "Pricing varies based on volume, geography, and delivery." (https://www.cotality.com/products/parcel-essentials) | Not public; "more than 157 million U.S. parcel boundaries and 150+ standardized property attributes" | Not public | Not stated publicly |
| **Land Records** (Systems of Record LLC, Virginia) | Texas: "One-time Download $2,500.00 Annual License $4,375.00 Hosted Web API $200/mo" (https://landrecords.us/products/us-tx-parcels) | Data terms (https://landrecords.us/terms-of-service): "such data purchased or licensed hereunder MAY NOT BE DISTRIBUTED, posted, re-sold ..." and "Private Use Only: This data is for private use only and under no circumstances may this data be made publicly available by any means." The hosted-API terms on the same page allow transfer only "as an integrated part of your own final user applications", but read like an unfilled template ("governed by the laws of Place", terms "found at File") | $200/month hosted API (row metering in its docs, https://landrecords.us/documentation/web-api) | Unclear: the two sets of terms conflict. A written clarification is needed before any use |

**Not researched:** Precisely, First American DataTree and others; their terms are not in this pass.

### 3.3 What follows

*Inference, high confidence:* **no self-serve licence on any public page lets Inframation show parcel attributes to
its subscribers.** Regrid's store licence comes closest: it allows rendering polygons to customers with copy
protection, but nothing more. Platform licences exist (Regrid's per-user module for Kalibrate, ReportAll's reseller
licences) and are quote-only. A paid product built on a self-serve end-user licence would breach it. This is why the
free state layers in §2 carry v1.

*Inference, moderate:* Regrid's clause (a), "a website, product, or service that may compete with any of Loveland’s
products", reaches a parcel search tool, since Regrid sells the Regrid Property App. Raise it explicitly in any
Regrid negotiation.

---

## 4. Personal data

### 4.1 What is personal here

The owner name, care-of name and mailing address of a **natural person**, and a home's situs address, are personal
data. Values and acreage become personal data once linked to a named individual. Names of companies, partnerships,
governments and utilities are not: the statutes below protect a "consumer" or an "individual", meaning a natural
person. Trusts named after people ("<name> Revocable Living Trust") are treated as personal, as in `docs/13` §5.5.2.

### 4.2 CCPA: the "publicly available" exemption and its limits

California Consumer Privacy Act as posted by the CPPA (effective 2025-01-01 text),
https://cppa.ca.gov/regulations/pdf/ccpa_statute.pdf, §1798.140(v)(2):

> (A) “Personal information” does not include publicly available information or lawfully obtained, truthful
> information that is a matter of public concern. (B) (i) For purposes of this paragraph, “publicly available” means
> any of the following: (I) Information that is lawfully made available from federal, state, or local government
> records. (II) Information that a business has a reasonable basis to believe is lawfully made available to the
> general public by the consumer or from widely distributed media. (III) Information made available by a person to
> whom the consumer has disclosed the information if the consumer has not restricted the information to a specific
> audience.

§1798.140(v)(1)(K) lists among personal information:

> Inferences drawn from any of the information identified in this subdivision to create a profile about a consumer
> reflecting the consumer’s preferences, characteristics, ...

**Limits, kept apart from the text.**

1. *Who it protects:* California residents only. Texas parcels have few California-resident owners, but absentee
   owners exist. (Inference.)
2. *"Lawfully made available":* a record a CAD should have withheld under Tax Code §25.025 (§4.4) was not lawfully
   made available, so the exemption does not cover it. Inframation cannot tell which records those are. (Inference,
   moderate.)
3. *Derived data:* an owner's total holdings across counties, or a "likely seller" flag, is an inference. Whether an
   inference built only from public records stays exempt is for counsel; the text lists inferences as personal
   information. (Inference, moderate.)
4. *Thresholds:* the CCPA binds a "business" over $25,000,000 revenue (adjusted), or one that "annually buys,
   sells, or shares the personal information of 100,000 or more consumers or households", or that "Derives 50
   percent or more of its annual revenues from selling or sharing consumers’ personal information"
   (§1798.140(d)(1)). Not met at launch.
5. *Delete Act:* a "data broker" must register and, "beginning August 1, 2026", process deletion requests from the
   DROP every 45 days (https://cppa.ca.gov/data_brokers/). Because the Delete Act relies on the CCPA definitions,
   publicly available information falls outside it on the text. Counsel to confirm (§6 item 3).

### 4.3 Texas privacy statutes

**Texas Data Privacy and Security Act**, Bus. & Com. Code ch. 541,
https://tcss.legis.texas.gov/resources/BC/htm/BC.541.htm:

> "Personal data" means any information, including sensitive data, that is linked or reasonably linkable to an
> identified or identifiable individual. ... The term does not include deidentified data or publicly available
> information.

> "Publicly available information" means information that is lawfully made available through government records, or
> information that a business has a reasonable basis to believe is lawfully made available to the general public
> through widely distributed media, by a consumer, or by a person to whom a consumer has disclosed the information,
> unless the consumer has restricted the information to a specific audience.

It applies to a person that is "not a small business as defined by the United States Small Business Administration"
(§541.002(a)(3)). *Inference:* Inframation is a small business today.

**Texas data broker law**, Bus. & Com. Code **ch. 510** (redesignated from ch. 509 on 2025-09-01; amended by SB 2121
and SB 1343), https://tcss.legis.texas.gov/resources/BC/htm/BC.510.htm:

> (4) "Data broker" means a business entity that collects, processes, or transfers personal data that the business
> entity did not collect directly from the individual linked or linkable to the data.

> (11) "Personal data" ... The term does not include deidentified data, employee data, or publicly available
> information.

§510.002(b) also excludes "(3) publicly available information; (4) inferences made exclusively from multiple
independent sources of publicly available information that do not reveal sensitive data with respect to an
individual". §510.003(a) applies the chapter to a data broker that derives "(1) more than 50 percent of the data
broker's revenue directly from processing or transferring personal data not collected by the data broker directly
from the individuals ...; or (2) revenue directly from processing or transferring the personal data of more than
50,000 individuals not collected by the data broker directly". If it applies: registration with the Secretary of
State at $300 a year (§510.005) and a website notice that "states that the entity maintaining the website or
application is a data broker" (§510.004).

*Inference, moderate:* on the text, appraisal-district owner names are "publicly available information" and outside
both Texas statutes. Under option (a), though, Inframation would sell access to the names of millions of Texans, and
prong (2)'s 50,000 is far below that. The exemption would be doing all the work. Options (b) and (c) do not depend
on it.

**Colorado Privacy Act.** The operating entity is in Denver (`docs/00-PLAN.md` open question 3, answered
2026-09-18). A secondary summary gives the CPA's "publicly available information" exclusion and its thresholds
(100,000 consumers, or 25,000 with sale revenue): https://www.bytebacklaw.com/?p=3639. Statute text not read; counsel
item.

### 4.4 Texas appraisal-district confidentiality

Tax Code ch. 25, https://tcss.legis.texas.gov/resources/TX/htm/TX.25.htm (the legislature's file server; the
`statutes.capitol.texas.gov` page is a script-rendered shell over it). The form and content of appraisal records,
§25.02(a)(1):

> the name and address of the owner or, if the name or address is unknown, a statement that it is unknown;

§25.025(b), confidentiality of certain home addresses:

> Information in appraisal records under Section 25.02 is confidential and is available only for the official use of
> the appraisal district, this state, the comptroller, and taxing units and political subdivisions of this state if:
> (1) the information identifies the home address of a named individual to whom this section applies; and (2) the
> individual: (A) chooses to restrict public access to the information on the form prescribed for that purpose by
> the comptroller under Section 5.07 ; or (B) is a federal or state judge, or the spouse of a federal or state judge
> ...

> (c) A choice made under Subsection (b) remains valid until rescinded in writing by the individual. (d) This section
> does not prohibit the public disclosure of information in appraisal records that identifies property according to
> an address if the information does not identify an individual who has made an election under Subsection (b) in
> connection with the individual's address.

Subsection (a) lists **37 classes**, among them current or former peace officers and their families, victims of
family violence, sexual assault, stalking or trafficking, participants in the attorney general's address
confidentiality program, judges and their families, prosecutors, elected public officers, firefighters and EMS
personnel, and combat-zone veterans. The section was last amended effective 2025-12-04 (H.B. 16, 2nd C.S.).

§25.026 makes confidential the address of "a family violence shelter center, a sexual assault program, or a victims
of trafficking shelter center". §25.027(a):

> Information in appraisal records may not be posted on the Internet if the information: (1) is a photograph, sketch,
> or floor plan of an improvement to real property that is designed primarily for use as a human residence; or (2)
> indicates the age of a property owner, including information indicating that a property owner is 65 years of age
> or older.

**What follows.**

1. *Text:* TxGIO's description says "No values from the original data have been changed or removed". *Inference,
   moderate-high:* TxGIO does not screen for §25.025 elections. Protection depends on each CAD's export, and a CAD
   that ships an unredacted file publishes it statewide. The election is visible only to the CAD ("available only
   for the official use of" government bodies), so Inframation cannot screen for it either.
2. *Inference:* under option (a), every CAD export error becomes Inframation's republication of a protected
   person's home address, in a product sold for contacting owners. Under (b), Inframation holds no names. The deep
   link sends the user to the CAD's own record, which reflects the CAD's redaction.
3. *Text:* §25.027 bars posting residence photos, sketches and floor plans, and owner age (the over-65 exemption).
   The TxGIO schema has no such fields. Any later CAD enrichment (exemption codes, building sketches) must not carry
   them. *Inference:* the provision is written in the passive ("may not be posted") without naming who it binds;
   treat it as binding Inframation.

### 4.5 Retention and deletion (inputs for the privacy notice)

- **Stored:** geometry and the §1.3 non-personal fields per vintage; an owner key (`HMAC-SHA256(secret,
  normalise(OWNER_NAME) + "|" + normalise(MAIL_ADDR))`). The secret is held outside the database, as the suppression
  list's salt is (`docs/13` §5.4 rule 6). Owner name, care-of and mailing address are read in memory at ingest and
  never written.
- **Retention:** the current vintage plus the previous one, so ownership-change signals can be computed; older
  vintages are deleted. Keys are stable across vintages. That is what makes "this assemblage changed hands" possible,
  and it is the one reason to keep the secret stable.
- **Deletion:** a request naming a parcel or an owner removes the owner key from the affected parcels (they show
  "owner withheld") and adds the key to a suppression list that survives re-ingest. Same machinery as `docs/13`
  §5.5.5.
- **Privacy notice inputs:** source (TxGIO from appraisal districts), purpose (identifying land assemblages for site
  screening), what is held (no names; a keyed owner identifier), the CAD link, and how to have a parcel's owner key
  removed.

### 4.6 If a user contacts landowners

Inframation's own outreach is governed by `docs/13-legal-outreach-and-social.md`, and parcel data never feeds it:
`docs/13` §5.4 rule 4 ("Never use ingested personal data for outreach") applies, and the parcel store has no join
to the CRM. Users who contact owners are the senders and bear the duties. The product should still neither invite
nor ease unlawful contact:

| Channel | Rule | Source | Product consequence |
|---|---|---|---|
| Postal letter | No federal opt-out regime for mail. Arizona (§39-121.03(D)) and, per a vendor's terms, Idaho, Kansas and South Carolina restrict owner lists used for solicitation | §2 above | In AZ/ID/KS/SC, owner data is shown only under the source's commercial terms |
| Email | CAN-SPAM: accurate headers, postal address, opt-out honoured within 10 business days | `docs/13-legal-outreach-and-social.md` §1 | No owner emails exist in TxGIO; Inframation provides none |
| Calls and texts | 47 C.F.R. §64.1200(a)(1): no autodialed or prerecorded call **or text** to a mobile without prior express consent, whatever the purpose. Texas ch. 304 covers a "telemarketing call ... to: (A) solicit a sale of a consumer good or service"; ch. 302 covers a "telephone solicitation ... to induce a person to purchase, rent, claim, or receive an item", text messages included since SB 140 (2025) (https://tcss.legis.texas.gov/resources/BC/htm/BC.302.htm, `/BC.304.htm`) | `docs/13-legal-outreach-and-social.md` §3; Texas codes | Inframation provides no phone numbers and no skip-tracing. *Inference, moderate:* an offer to buy or lease someone's land is probably not a sale of a "consumer good or service" (ch. 304) or a solicitation to "purchase" (ch. 302); the TCPA consent rule applies regardless. Counsel item |
| Protected owners | §25.025 | §4.4 | The CAD link shows only what the CAD discloses |

**Customer terms clause (input for `docs/13-legal-customer-terms.md`, not drafted here):** the subscriber may use
parcel information only for evaluating, acquiring or leasing land. It may not build mailing or calling lists for
consumer marketing. It complies with the TCPA, CAN-SPAM, Texas Bus. & Com. Code chs. 302 and 304, and state
public-records commercial-use laws (A.R.S. §39-121.03 named). It may not use the service to locate a person, and
Inframation may suspend accounts that do.

### 4.7 The three options, and the recommendation

| Option | What it is | For | Against |
|---|---|---|---|
| **(a)** Store and show owner names to paid users | Pro shows `OWNER_NAME` (and mailing address) | Most useful to the buyer; the field is public at the CAD; competitors show it (Regrid: "Ownership and mailing information", https://regrid.com/global/us-parcels) | Republishes CAD redaction failures (§4.4); "data broker" analysis rests entirely on the public-records exemption (§4.3); contradicts the logged rule "acreage and owner counts, never owner names" (`docs/00-PLAN.md` 2026-10-22 prospect-score row); erasure and suppression duties for millions of people |
| **(b)** No names stored; parcel id, acreage, land use, value, owner count for contiguous same-owner assemblages, deep link to the CAD record | Owner identity exists only as an HMAC key used for grouping | Holds no personal data in clear; nothing to leak or erase but a key; fits the logged rule; the CAD shows the owner with the CAD's own redactions; the licence question disappears for those columns | One click per assemblage to read the owner; deep links need a URL template for each of 253 CADs (about a dozen vendor platforms) with a search-page fallback |
| **(c)** (b) plus names of **organisation** owners | Classify each owner with `services/personal_names.py` (`docs/13` §5.5.3); show the name only when it is an organisation (LLC, Inc., LP, Ltd, government, utility); show "Private owner" otherwise | Large rural tracts are often held by entities (ranch partnerships, investment LLCs); corporate names are not personal data under CCPA, TDPSA or ch. 510 | Misclassification shows a person's name; sole traders ("<name> Farms") are a grey zone (`docs/13` §5.5.2); the classifier is tuned on registers, not CAD rolls |

**Recommendation, confidence moderate-high: (b) for v1, with (c) as a measured upgrade.**

- (b) gives developers what the owner asked for: large tracts, contiguous same-owner holdings and acreage. It holds
  no names in clear, and it matches the decision already logged.
- Move to (c) only after three things happen: the classifier is measured on a hand-checked random sample of 500
  Texas `OWNER_NAME` values, with no more than 1 in 500 natural persons classified as organisations; counsel
  confirms (§6 item 4); and the owner records the change in the decisions log.
- (a) is not recommended at any stage without counsel's sign-off. The value it adds over (c) is the names of
  individuals, which is the same set of names that carries the §25.025 risk.

---

## 5. Recommendation

### 5.1 Site finder Pro v1: Texas

1. **Source:** TxGIO 2026 statewide zip (`stratmap26-landparcels_48_lp.zip`). Watch `publication_date` on the
   catalogue API weekly for the next vintage. Watch the `most_recent` service for county refreshes between vintages.
2. **Store:** geometry; `PROP_ID`, `GEO_ID`, `COUNTY`, `FIPS`, `LEGAL_AREA` (+unit), `GIS_AREA` (+unit) and computed
   acres; `STAT_LAND_USE`; `LAND_VALUE`, `IMP_VALUE`, `MKT_VALUE`; `TAX_YEAR`, `DATE_ACQ`, `SOURCE`; the owner key.
   Situs fields only for non-residential categories.
3. **Derive:** assemblages are connected components of parcels sharing an owner key and touching within a small
   tolerance. Whether a county road splits an assemblage is a product parameter for the owner. For each: parcel
   count, total acres, land-use mix, value per acre; for any selection, the number of distinct owner keys.
4. **Show (Pro):** the derived fields, a "verify at the appraisal district" deep link per parcel, the credit and
   notice of §1.1, and the caveats TxGIO itself states ("not survey grade", "basemap and cartographic data").
   Absence of a land-use code reads "unknown".
5. **Before launch:** TxGIO's written confirmation of commercial display (§7); measured fill rates by county (§1.4);
   CAD deep-link templates; counsel items 1 and 2.

Confidence that this path is lawful and sufficient for a Texas-only Pro: **moderate-high**. The residual risk is a
county or vendor copyright claim that TxGIO's CC0 cannot waive. TxGIO's exclusion of restricted counties reduces it.

### 5.2 The next three states

| Rank | State | Why | What is missing | Confidence |
|---|---|---|---|---|
| 1 | **Virginia** | Largest data-centre market; statewide geometry free and current (2026 Q3) | Owner and acreage attributes only via locality tables, different per locality; no licence stated, so ask VGIN in writing first | moderate |
| 2 | **Ohio** | Statewide standardized layer from county assessment records; `LandArea` and `StateLUC` for filters; "available for public use" | No owner name in the public view, so group by a key over `MailAddressAll` instead (same HMAC rule); check vintage | moderate |
| 3 | **North Carolina** | Clearest terms of any state ("free and unrestricted use"; no release agreements); all 100 counties; owner, acreage, use code and value; weekly refresh | none material | high on terms |

Next in line: **Wisconsin** (V12, full attributes; "free for public consumption" to confirm for commercial use),
**Indiana** (geometry and class code, no owners, credit requested) and **Minnesota** (59 of 87 counties, "any
purpose"). **Florida** has full attributes, but the GIO asked UF to remove its parcel layers "until ... guidance on
what may be permissible": ask the Florida GIO first. **Georgia, Arizona, Illinois, Pennsylvania, Oklahoma,
Louisiana, Nevada, New Mexico:** licence needed. Arizona also needs assessor commercial-purpose terms (§2).

### 5.3 When a nationwide licence pays for itself: arithmetic

**Assumptions, each stated:**

| # | Assumption | Value | Basis |
|---|---|---|---|
| A1 | Pro price | $149 per seat-month | `docs/14-financial-model.md` inputs table; Founding Pro $150 is within rounding |
| A2 | Payment cost | 2.9% + $0.30 per charge, plus 0.7% Stripe Billing | `docs/14` (https://stripe.com/pricing, https://stripe.com/billing/pricing) |
| A3 | Net revenue per seat | $149 − ($4.32 + $0.30) − $1.04 = **$143.34/month = $1,720/year** | A1, A2 |
| A4 | Marginal cost per seat other than data | $0 | Hosting is fixed at this scale (`docs/14` ceiling ≤ $415/month); a simplification |
| A5 | Nationwide **end-user** licence | $50,000/yr (Regrid Standard), $80,000 (Premium), 2023 public prices | §3.2. Not usable for Pro; shown as the floor |
| A6 | Nationwide **platform** licence | **Unknown.** Scenarios: $100,000, $150,000, $250,000 a year | Assumption: 1.25–3× the end-user prices. No vendor publishes it; a quote replaces this row |
| A7 | Share of seats that need licensed-only states | Scenarios 100% and 50% | Assumption. Measure later: the share of Pro searches outside TX plus §5.2's free states |
| A8 | Year-1 Pro seats | 40–80 | `docs/11` §2.3 (an estimate there, not a measurement) |
| A9 | Per-state platform licence | $16,000–$24,000 a year | Assumption: 2× Regrid's 2023 end-user state price ($8,000–$12,000) |

**Break-even seats** = licence cost ÷ $1,720 (A3):

| Licence a year | Seats needed if 100% attributable | Seats needed if 50% attributable | Inside the year-1 band (40–80)? |
|---|---|---|---|
| $50,000 (end-user, not usable) | 30 | 59 | reference only |
| $100,000 | **59** | **117** | only at 100% attribution, near the top of the band |
| $150,000 | 88 | 175 | no |
| $250,000 | 146 | 291 | no |

**Per-state against nationwide** (A9): a per-state platform licence at about $20,000 breaks even at about 12 seats in
that state. Nationwide at $100,000 equals about 5 states at $20,000 (6 at $16,000). **Above five or six licensed
states, nationwide is cheaper.**

**Per-record API is not a substitute for area screening.** Take a 1-mile band either side of a 20-mile line: 40
square miles, or 25,600 acres. At an assumed average rural parcel of 100 acres, a search returns about 256 parcels;
at Regrid's $0.10 a record, that is $25.60 per search. Six searches a month exceed a seat's $143 net. Per-record
pricing suits click-through lookups of single parcels: at 200 clicks per seat-month, it costs $20, or 14% of net
revenue. Whether Regrid's API terms allow showing those results to subscribers is not public ("not for end users’
direct use").

**The point at which it pays for itself.** On these assumptions: when **about 60 paying seats depend on states
without a free layer** (at a $100,000 quote), or when the product needs **about six or more licensed states**,
whichever comes first. With half the seats attributable, that is about 117 total seats. That is **year 2 at the
earliest** against `docs/11`'s band. It is consistent with the owner's rule: "A nationwide licence waits until Pro
revenue covers it."

**Confidence:** high in the arithmetic; low in A6 and A7, which decide it. Get two quotes (Regrid and ReportAll,
who both advertise platform or reseller terms) before the first licensed state is promised to a customer.

### 5.4 What would change this

- TxGIO says CC0 does not cover commercial display: Texas then needs a licence (ReportAll's Texas store price is
  $7,000 end-user, and its display terms would have to be quoted), and v1 slips.
- A platform quote under about $70,000 a year: the nationwide licence pays for itself within the year-1 band.
- Counsel says the public-records exemptions cover option (a) and the owner wants names: §4.7's (a) row becomes
  possible, still with the §25.025 exposure.

---

## 6. Items requiring counsel

1. **TxGIO CC0 and CAD/vendor rights** (§1.2): can a county appraisal district or its GIS vendor assert copyright
   in parcel maps it supplied to TxGIO, given the Public Information Act? Does TxGIO's CC0 designation protect a
   good-faith commercial user?
2. **§25.025 exposure** (§4.4): Inframation's position if a CAD export republished through TxGIO contains a
   protected individual's name and home address, under options (b) and (c), where only organisation names are shown.
3. **Public-records exemptions for a paid product** (§4.2–4.3): CCPA §1798.140(v)(2), TDPSA, Texas ch. 510, the
   Delete Act and the Colorado Privacy Act, applied to (i) owner keys and assemblage inferences under (b), and (ii)
   organisation names under (c).
4. **Option (c) classifier standard** (§4.7): is a 1-in-500 person-as-organisation error rate defensible?
5. **Calls and texts to landowners** (§4.6): whether offers to buy or lease land are "telemarketing" or "telephone
   solicitation" under Texas chs. 302 and 304, and the customer-terms clause.
6. **Arizona §39-121.03** (§2): whether vendor-licensed Arizona parcel data in a paid product incurs (C)'s liability,
   and what each assessor's commercial-purpose terms require.
7. **Regrid's competing-product clause** (§3.3), if Regrid is the vendor.

## 7. Owner actions

1. **Email TxGIO** (https://www.geographic.texas.gov/contact.html): "Does the CC0 1.0 designation on the StratMap
   Land Parcels collection cover display of derived parcel data and parcel geometry to paying subscribers of a
   commercial web service, and has any appraisal district asked TxGIO to restrict commercial reuse of its data?" Save
   the reply for this register.
2. Download the 2026 statewide zip and the supplemental report in a browser if the ingest egress stays blocked
   (CloudFront 403 here).
3. **Email VGIN and the Florida GIO** the same question before Virginia or Florida is promised.
4. Ask Regrid and ReportAll for **platform (display to subscribers) licence quotes**: Texas-only, five states,
   nationwide, so that A6 and A9 become numbers.

## 8. As built (lane parcels, 2026-10-23; fix round the same day)

Owner decisions 2026-10-23: Site finder Pro gets land parcels, Texas first, and Inframation serves the owner's own
powered-land deals first. Code: `pipeline/context/txgio_parcels.py` (builder), `services/context_sample/parcels.py`
(reader), `services/site_finder/parcels.py` and `cad_links.py` (per-segment figures, appraisal links),
`services/api/site_finder.py` and `web/site_finder.py` (API and page), `.github/workflows/parcels-layer.yml` with
`infra/scripts/build_parcels_layer.sh` (the statewide build). Manifest row `us.tx.txgio.parcels` (the id this lane
was given; §1.6 proposed `us.tx.txgio.land_parcels`), `docs/13` §6.

### 8.1 Source and route, measured from this sandbox

| Request (platform user-agent) | Answer |
|---|---|
| `api.tnris.org/api/v1/collections/?search=parcels` | 200: seven Land Parcels vintages; newest `1d33dafb-6e9c-40ac-887d-f4577909ed9b`, acquired 2026-09-25, published 2026-10-01, `CC0-1.0`, spatial reference 4326, Donley excluded |
| `api.tnris.org/api/v1/resources/?collection_id=...` | 200: 254 files, 253 county zips and the statewide zip; sizes as §1.5 |
| `api.tnris.org/robots.txt`, `feature.geographic.texas.gov/robots.txt` | 404 (no rules) |
| `data.geographic.texas.gov`: `robots.txt`, HEAD and GET of the smallest county zip (Kenedy, 352,951 bytes) | 403 "Request blocked" (CloudFront) |
| `stratmap_land_parcels_48_most_recent/MapServer/0/query`: features, `returnCountOnly`, `returnIdsOnly`, `outStatistics` | 400 "Requested operation is not supported by this service" |

So no TxGIO parcel was read here, and §1.4's proposed fill-rate statistics query is not possible on this service.
Identify, find, WMS GetFeatureInfo, the zips' storage origin and web archives were not tried: each would get around
an access control the publisher set. The route is the county zips, downloaded by the GitHub Actions build (§8.6)
from GitHub's runners, one at a time per job with five seconds between counties, at most two jobs at once; the
catalogue at no more than 0.5 requests a second and the file host at 0.2 (`RATE_LIMITS`), through the shared
`PoliteSession` (robots.txt honoured where a host serves one; Inframation user-agent with contact URL). The
statewide 2.7 GB zip is never fetched. The recorded catalogue answers are the tests' fixtures
(`pipeline/context/fixtures/txgio_parcels/collections.json`, `resources.json`, trimmed to the counties used).

Terms recorded: `reuse: open`, `publication: derived_only` (a privacy rule, §8.2), credit and no-endorsement notice
as §1.6, and the note that **written confirmation of commercial display is requested from TxGIO** (§7 item 1; not
yet sent or answered).

**Where the build's files live (fix round).** The per-parcel file is the source table less its owner columns, so
under `derived_only` ("do not mirror the source table", `docs/13`) it is never on a public host. The build writes
to the private object store (`R2_BUCKET`, prefix `data/normalized/context/us.tx.txgio.parcels/`, the bucket and
prefix scheme the flood build uses), never to the public tiles bucket, and a server pulls with its own object-store
credentials. The public reads parcels only through the API, behind the register gate: withholding the source
(`publish_state`, or TxGIO declining commercial display) withholds every parcel figure and outline at once. If the
owner later decides that per-parcel rows are `raw_ok`, the register row changes first and the files may move.

### 8.2 The privacy rule as implemented (option (b), stricter)

This supersedes §0's and §4.7's "keyed hash" and §5.1 item 2's "owner key", "situs for non-residential" and
"show `LEGAL_DESC`":

- **Read:** the owner name is the only personal field read, into memory, for one county at a time. `NAME_CARE`,
  `MAIL_*`, `SITUS_*`, `LEGAL_DESC` and `GEO_ID` are never read (`FIELD_NAMES` names the columns read; the zip
  reader asks GDAL for those only). The in-memory frame leaves the owner list out of its repr, so no debug print or
  traceback that shows locals can show a name.
- **Screened, grouped, then dropped:** the name is normalised (`owner_key`: case, accents, punctuation and spacing
  ignored, "&" read as AND, corporate suffixes written one way) and screened (`owner_screen`). A placeholder
  ("NOT ON FILE", "NO NAME", "VARIOUS", "ROW", "R.O.W.", "PUBLIC ROAD", "SEE ACCT 12345", digits only and the like)
  or a confidentiality marker ("CONFIDENTIAL" and its misspellings and abbreviations, "25.025", "RESTRICTED",
  "PROTECTED", "WITHHELD", "PRIVATE" alone) names nobody. The rest are turned into integer codes and the strings are
  discarded before any geometry work. Parcels with the same code that touch or lie within 30 m of each other
  (equal-area plane, transitive) form a **holding**. No hash, code or name survives the build.
- **Counted, never written:** each county's summary counts the named, blank, withheld and placeholder owners, and
  the kept names that still look like a marker (`_SUSPECT`: "ROW", "ROAD", "PRIVATE", "ACCT", an account-like id),
  with how many multi-parcel holdings those form. Counts only: the strings never leave memory. The first real build
  shows whether a district writes a placeholder the lists lack.
- **Written:** a holding id that is random per build (`secrets`, `h` + 12 hex), never derived from the owner;
  per holding its parcel count (distinct outlines) and accounts, acres, owner count (always 1: a holding is one
  owner's contiguous land), land-use category by acreage, values, the property id of its largest parcel (the
  appraisal link), its kind (§8.3) and the land it shares with other holdings. Per band: how many distinct holdings
  touch it. Two holdings of one owner more than 30 m apart stay two, and nothing says they share an owner.
- **Confidential, placeholder or blank owners** never join a holding: each is a holding of one, indistinguishable
  from any other single-parcel holding. Which parcels lack an owner is never written; the county summary gives only
  counts.
- **Tested:** the fixture counties (`synthetic_county.geojson`, `stacked_outlines.geojson`, every value synthetic)
  carry owner, care-of, situs, mailing and legal-description strings; tests assert none of them, nor any fragment of
  them, appears in the parquet files (rows and metadata), the county summary, the manifest, the build's log, the
  frame's repr, the stored segment contexts, the API's responses or the page (`pipeline/context/test_txgio_parcels.py`,
  `services/site_finder/test_parcels.py`, `services/api/test_site_finder.py`, `web/test_site_finder.py`).

### 8.3 What a parcel and a holding carry

Per parcel (`parcels/<fips>.parquet`, GeoParquet 1.1, WKB with a `bbox` covering column, rows in Z-order, row
groups of 2,048, zstd), only: TxGIO record id, county FIPS, the appraisal district's property id (`PROP_ID`), acres
computed in an Albers equal-area projection on GRS80 (standard parallels 27.5° and 35°, centre 18° N 100° W; a test
checks it against the exact ellipsoidal area of a quadrangle) with the stated `GIS_AREA` in acres beside it, the
state land-use code and its category (PTAD's categories: A, B residential; C1, C2 vacant lot; D1, D2 open-space and
ag; E rural non-ag; F1 commercial; F2 industrial; G minerals; J utilities; L, M, O, S, X; a blank is "not stated"),
land and market value with the tax year, the holding's id, acres, parcel count, land use, value per acre, link
property id, kind and shared land, the outline simplified to about 1 m for display, and the source quartet. The
geographic id is no longer kept (spec (3) does not list it; the record link uses `PROP_ID`).

Per holding (`holdings/<fips>.parquet`): the figures above plus accounts, `overlap_acres` (land another holding's
outline also covers), `overlap_partners`, `mean_width_m`, its box and a point on its largest parcel. Each county
also gets `counties/<fips>.json`: counts, fill shares, the owner screen's counts, the kinds, the stacked and
overlapping outlines (`outlines`), timings and the files' sha256.

**Stacked and overlapping outlines (fix round).** Texas rolls draw several accounts on one geography (a land
account and the mobile home or improvement account on it; each heir's undivided interest; condominium units), and
TxGIO changes no value. So:

- accounts on one outline (equal after rounding to about 1 cm, whatever the start vertex or direction) are **one
  tract**: a holding's acres count each outline once, its parcel count is its distinct outlines, and the outline's
  land use is its land account's (`USE_PRIORITY`: land before a mobile home, personal property or minerals; equal
  acreages decided the same way); its record link is the land account's;
- one owner's outlines that overlap count as their **union**;
- land two holdings both cover (two owners on one outline; an account drawn inside another's outline) is listed
  pairwise on both holdings (`holding_overlaps`: the partner and the acres shared, at most 32, largest first, and
  the rest as one figure, `holding_overlap_rest_acres`). The site finder subtracts it when the second of the two
  joins an assemblage, so one tract is never counted twice. Overlaps under 4 m² or 0.1% of the smaller outline are
  drawing noise (two edges straight in degrees are not quite straight in the equal-area plane; adjacent tracts
  digitised apart) and are ignored;
- the county summary counts accounts on a repeated outline, repeats within one holding, outlines shared by holdings,
  overlapping pairs (and those over 10% of the smaller), their acres, and holdings measured as a union.

**Kinds (fix round).** A holding is `land`, or one of the kinds left out of every acreage figure: a right-of-way
`strip` (20 acres or more, on average under 60 m across: twice its union's area over its perimeter; a 100 ft railroad
right of way reads about 30 m, a square 20-acre tract 142 m), `exempt` land (category X) or `utility` land (category
J, railroads included). A fee-owned railroad or road right of way along a line would otherwise top "largest
holding" and pass "contiguous acres at least 500".

### 8.4 Appraisal-district links, verified county by county (2026-10-23)

Links only: no request is made to an appraisal district's site by any code or job. For each county the district's
website is the one the Comptroller's county directory names; only its robots.txt, home page and search script were
read (and, in the fix round, a HEAD of the Pecos and Ward search pages over HTTPS), and no property record was
requested.

| County (FIPS) | District site | Link given | Evidence |
|---|---|---|---|
| Newton (48351) | esearch.newtoncad.org | record: `/Property/View/<PROP_ID>` | the platform's `/js/_site.js` builds `/property/view/${id}` |
| Jasper (48241) | esearch.jaspercad.org | record, same route | same script |
| Hardin (48199) | esearch.hardin-cad.org | record, same route | same script |
| Jefferson (48245) | esearch.jcad.org | record, same route | same script |
| Orange (48361) | esearch.orangecad.net | search page and the property id to copy | an older release whose script shows no record route |
| Pecos (48371) | https://www.pecoscad.org/home/PropertyAppraisal | search page and the property id | a keyword search form; no record route seen; HTTPS answers 200 |
| Ward (48475) | https://www.wardcad.org/home/PropertyAppraisal | search page and the property id | as Pecos |
| every other county | Comptroller directory page `county-directory/<name>.php` | the directory page and the property id | the directory names each district's site |

**Not verified:** that TxGIO's `PROP_ID` is the id the esearch route takes (no TxGIO record could be read here).
The first real build should open three record links per esearch county by hand; if they fail, set those counties
to search-page links in `cad_links.py`.

### 8.5 In the site finder

- **Stored** in each segment's existing context (`SiteCandidate.context["parcels"]`): no migration, so no 0043.
  Per width (0.25, 0.5, 1, 2 mi): coverage (`ok`, `partial`, `none`, with the counties not loaded), the count of
  distinct holdings touching the band (`n`) and of those left out by kind (`x`), the largest contiguous acres from
  at most 1, 3, 5 and 10 owners, and the counted holdings of 20 acres or more plus each width's ten largest (at most
  150), with land use, value per acre and the link id for the largest. Holdings are keyed by county and id together
  (`<fips>:<id>`): ids are drawn per county build.
- **Criteria:** "Contiguous acres at least" (none, 20, 50, 100, 200, 500, 1,000) and "Owners at most" (any, 1, 3,
  5; it applies with a minimum acreage). With 1 owner the figure is the largest single holding touching the band;
  with 3 or 5, and with any (which assembles up to 10 holdings), it is a greedy join of neighbouring holdings
  (parcels within 30 m), each touching the band, with land two holdings share counted once: a lower bound, said so
  in the drawer. A page URL with another number snaps to the stricter offered choice (at least 75 acres reads as
  100; at most 2 owners as 1; 10 or more owners as any). A segment in a county whose parcels the server does not
  hold never passes an acreage criterion; the summary counts those miles (`parcels_unavailable_mi`) and the band
  names the Texas counties ("parcels not loaded for this county"), or says "Parcels cover Texas only" for a place
  outside Texas. New sort: largest holding, descending; bands without parcels last.
- **Drawer:** "Land beside the line": the largest holding, how many holdings touch the band (exact for a band of one
  segment or of large holdings only, else "at least": a band of several segments stores only its large holdings by
  id), how many reach the chosen acreage (or 100 acres), the assembly figure, how many right-of-way, exempt or
  utility holdings are left out, and the top holdings with acres, parcel count, land-use category, value per acre
  and the appraisal link, under "Owner names are on the appraisal district's record; Inframation does not store
  them." A band's figures are merged from its segments': exact for the largest holding, holdings of 20 acres or more
  and each width's ten largest; "at least" for smaller holdings and the left-out count.
- **Map:** parcel outlines from zoom 12 (`GET /v1/site-finder/parcels`, a box at most 0.5° wide, at most 4,000
  outlines, ranked and cut in Arrow so only the kept outlines become Python objects); a click opens the parcel's
  card with acres, land use, its holding, whether its kind is left out, and the appraisal link.
- **Refresh after a pull:** on the server the monthly `context_build` runs the pull, and the full
  `site_finder_refresh` after the load measures what it wrote (§8.9). By hand:
  `python -m pipeline.context.txgio_parcels pull` (counties from `PARCELS_COUNTIES`; the private object store
  from `R2_BUCKET`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY` and `R2_ENDPOINT` or `R2_ACCOUNT_ID`, the names the
  beta deploy already renders), then `python -m services.site_finder --parcels-only` re-measures the served run in
  place. With `--counties`, only segments whose midpoint lies in those counties are
  re-measured; a neighbouring county's band that reaches in is updated by a run without `--counties`. Each
  measurement stamps the run with a new `measure_id`; the API keys its parcel caches on it and drops every cache
  built from the run's segments when it changes, so a re-measure that keeps the layer's version (a wider
  `PARCELS_COUNTIES` pulled from the same `latest.json`, a `--counties` run) is served from the new figures at the
  next request.

### 8.6 The statewide build (`.github/workflows/parcels-layer.yml`)

Dispatched by a person (no schedule): `counties` (`all`, the default, or FIPS codes), `dry_run`, and
`allow_dropped_counties`. The dry run uses no secret: it plans from the catalogue, HEADs every county zip and
reports each answer, failures as warnings. A real run plans and uploads `plan.json`, builds the counties in batches
(two at a time), uploads each county's files and then its summary, and publishes: `manifest.json` carries forward
every county this build did not build or failed to build from the published `latest.json`, and the manifest is
refused (exit 1, `latest.json` untouched) when a published county would be lost or a county's parcels fall by more
than half. Every failed county is listed in the step summary with its reason; a county whose build process is
killed (out of memory) is recorded as failed and the batch goes on. Everything goes to the private object store
(`R2_BUCKET`, `data/normalized/context/us.tx.txgio.parcels/`) with the R2 values `flood-layer.yml` uses: no new
secret. The script refuses to run (exit 2, nothing written) when `R2_BUCKET` names the tiles bucket, and the last
step checks `latest.json` in the private store, not on a public host.

### 8.7 Measured (development; synthetic parcels, real Texas lines)

TxGIO's files cannot be read here (§8.1), so the counties below are **synthetic**: rectangles split to a lognormal
size, clipped to each county's real TIGER outline, owners clustered so that multi-parcel holdings exist, written by
GDAL as a TxGIO zip (geodatabase and shapefile). In the fix round 3% of outlines also carry the same owner's
mobile-home account and 1% another owner's account, as stacked accounts do. The synthetic zips (6.5 to 9.5 MB) are
in the order of the real ones (5 to 17 MB). Counties: Newton, Jasper and Hardin (the top default bands, Layfield to
Hartburg 500 kV) and Pecos (Permian). This sandbox, 4 cores shared with other lanes. Fix-round figures (the first
round's, before the overlap work, in brackets):

| County | Rows (accounts) | Distinct outlines | Holdings | Multi-parcel | Largest, acres | ≥ 100 ac | ≥ 1,000 ac | Kinds left out (strip / exempt / utility) | Build, s | Peak memory | Output |
|---|---|---|---|---|---|---|---|---|---|---|---|
| Newton 48351 | 68,402 | 65,772 | 34,110 | 2,015 | 3,023 | 1,024 | 36 | 38 / 662 / 330 | 14.2 (8.5) | 488 MB (437) | 8.0 MB |
| Jasper 48241 | 59,810 | 57,510 | 27,721 | 1,987 | 3,176 | 1,011 | 53 | 29 / 524 / 272 | 12.6 (5.8) | 446 MB (428) | 7.0 MB |
| Hardin 48199 | 57,574 | 55,361 | 28,733 | 1,864 | 3,069 | 938 | 54 | 26 / 544 / 267 | 13.3 (8.3) | 441 MB (471) | 6.8 MB |
| Pecos 48371 | 45,556 | 43,804 | 16,646 | 1,180 | 17,384 | 3,958 | 789 | 2 / 313 / 286 | 11.0 (3.8) | 389 MB (298) | 4.8 MB |

Build is the whole `county` command (read, group, measure, write). The fix round adds the outline keys (about 1 s a
county), the overlap search (about 3 to 4 s: every pair of outlines that touch is tested for a shared interior) and
the pairwise lists. In Newton 2,630 accounts sit on a repeated outline (1,934 within one holding, 696 outlines
shared by two holdings, 1,275 holdings sharing land), no distinct outlines overlap, and the owner screen counts 711
withheld and 657 blank owners and no placeholder (the synthetic names hold none). *Extrapolation, low-moderate
confidence:* about 4.4 KB of memory per row above about 185 MB, so Harris (an estimated 1.4 to 1.6 million parcels;
TxGIO publishes no count) needs about 7 GB: within a 16 GB runner, at the edge of a 7 GB one (a failure there is
reported and its published version kept), and about 5 minutes. About 115 bytes of output per row: about 1.6 GB for
the state's ~14 million parcels, in the private store and on a server that pulls `all`.

Serving, on a copy of the Texas development store (120,699 segments; SQLite, in process; deleted after):

| Step | Result |
|---|---|
| Pull of the four counties from the object store (8 files, sha256 checked) | 0.16 s |
| Re-measure every segment (`--parcels-only`) | 67 s (62), 4,608 segments written, peak 639 MB (617) |
| Re-measure with `--counties` (the four) | 47 s (41), 2,611 segments |
| List, defaults (cold / warm) | 6.0 s / 0.88 s (6.5 / 0.89) |
| List, Newton | 1.0 s / 0.32 s |
| List, `min_acres=100` | 2.4 s / 0.22 s: 113 bands, 392 miles; 30,400 miles in counties without parcels |
| List, `min_acres=500&max_owners=1` | 2.2 s / 0.20 s: 111 bands, 325 miles |
| List, `min_acres=200&max_owners=3` | 1.6 s / 0.22 s: 113 bands, 391 miles |
| List, sorted by largest holding | 2.4 s / 0.40 s |
| One band by id (the drawer's data) | 0.21 s / 0.05 s |
| Parcel outlines, zoom 12 / 13 / 14 | 0.45 s (4,000 of 16,162 in view) / 0.27 s (2,107) / 0.11 s (696), warm |
| Parcel outlines, the widest view allowed (0.5° by 0.31°) | 0.63 s, 4,000 of 27,437 in view |
| Page `/site-finder`, defaults | 3.0 s / 0.65 s |
| Page with `min_acres=100` | 0.46 s / 0.25 s |
| Drawer fragment, a Newton band | 0.17 s / 0.05 s |

The default list's first band is Layfield to Hartburg 500 kV in Newton County: largest holding 1,866 acres
(synthetic), 55 holdings of 100 acres or more, at least 398 holdings touching, 9 left out by kind. A Newton band's
top holding links to `https://esearch.newtoncad.org/Property/View/<id>` (a synthetic id). On the same 0.5° Newton
view (39,239 parcels), the outline ranking takes 546 ms against the first round's 806 ms (every row into Python
lists and sorted there); the difference grows with the rows in view, so a zoom-12 view over a city (Harris, once
pulled: several hundred thousand rows) costs Arrow time, not Python time, for all but the 4,000 drawn. Not measured
on a real city's density.

**Real run, to measure the same on TxGIO's files:** dispatch `parcels-layer.yml` with `dry_run: true`, then with
`counties: 48351,48241,48199,48371`; pull with `PARCELS_COUNTIES=48351,48241,48199,48371` (and the server's `R2_*`
values) and run `python -m services.site_finder --parcels-only`. Read each county's `outlines` and `owners` counts:
they say how often the rolls stack accounts and whether a placeholder the screen lacks appears.

### 8.8 Limits and open items

1. **No real TxGIO record has been read** (403 here). Field names are handled as the schema PDF and the live
   service spell them, with the shapefile's ten-character truncations; a county zip missing `PROP_ID` or the owner
   column fails with the columns named. The first dispatched build is the validation.
2. **`PROP_ID` and the esearch record id** are assumed to be the same (§8.4).
3. **Holdings stop at county lines:** each county is built alone, so one owner's land across a county line is two
   holdings.
4. **Assembly is greedy** (a lower bound), and touching means within 30 m in the appraisal roll's outlines, which
   are not survey grade.
5. ~~**Go-live wiring**~~ **Done 2026-10-23 (lane parcels-wire), §8.9:** the monthly pull with the server's
   `R2_*` values, `PARCELS_COUNTIES` (default `all`), the full refresh after it, the api's read-only mount. Open:
   the API's and the worker's memory with all of Texas, and Harris in particular, are estimates (§8.9), not
   measurements.
6. **Cold requests:** the first list after a re-measure or for a new width is 1.6 to 6.0 s here; warm 0.2 to 0.9 s.
7. **TxGIO's written confirmation** that CC0 covers display to paying subscribers (§7 item 1) and counsel items 1
   and 2 (§6) before Pro is sold.
8. **Heuristics to check on the first real build** (fix round): the strip rule (20 acres, 60 m mean width), the
   overlap noise floor (4 m², 0.1% of the smaller outline), the placeholder and marker lists, and the kinds left out
   (exempt and utility land; a reader may want public land listed apart rather than dropped). Each is one constant in
   `pipeline/context/txgio_parcels.py` or `services/site_finder/parcels.py`.
9. **Harris** (the largest county) is estimated at about 7 GB peak in the build (§8.7); if a 7 GB runner fails it,
   the failure is reported and a larger runner is the fix.

### 8.9 On the server (go-live wiring, lane parcels-wire, 2026-10-23)

- **When.** `python -m pipeline.context.txgio_parcels pull` is the last of the four pulls in the worker's monthly
  `context_build` (`infra/scheduler/jobs.py` `CONTEXT_BUILDERS`, the 4th at 02:43 UTC; deploy-beta's `seed` queues
  the same build). `context_load` and then the full `site_finder_refresh` follow it, so each month's run measures
  the parcels pulled minutes before; `--parcels-only` stays for a pull made by hand. `parcels-layer.yml` has no
  schedule: a build a person dispatches mid-month reaches the server at the next `seed` or the next 4th.
- **What.** The counties `PARCELS_COUNTIES` names: a GitHub Environment variable that `infra/ci/beta.py` renders
  into the env file, `all` when unset, else Texas county FIPS codes (`48` and three digits, the pull's own rule);
  the inputs check refuses anything else. Unset on a server whose env file lacks it (staging and production take
  it from their SOPS file), the pull refuses to run and the build reports one failed builder; the others go on.
- **Not published.** No `latest.json` in the private bucket (before the first build), or none of the server's
  counties in it: the pull prints `status: not_published`, exits 0, and leaves what the server held. The build's
  report lists it under `not_published`, never `failed`. A county file the manifest names but the bucket lacks,
  a failed sha256 or a refused read is a failure, and `current.json` does not move.
- **Where.** The pull writes to `services.context_sample.parcels.parcels_dir()`: `PARCELS_DATA_DIR` when set, else
  `<INFRAQUE_DATA_DIR>/normalized/context/us.tx.txgio.parcels` (`/var/lib/infraque/data/...` in the image), the
  directory `compute.load_parcels(connector_data_root())` and the API's `default_layer` open. Before this lane it
  ignored `PARCELS_DATA_DIR`; nothing sets that variable on the beta, so the paths agreed there already. The files
  come from the private bucket only, with the server's own credentials, and never touch the tiles host.
- **The API.** On the single host the api container now mounts the worker's `connector_data` volume read-only at
  the same path (`infra/compose/compose.single.yml`); before, its data directory was the image's empty one, so the
  parcel outlines (`GET /v1/site-finder/parcels`), a band's flood pieces and `GET /v1/context/sample` had nothing to
  read on the beta. The three-VM topology shares no volume between VMs and has no such mount.
- **Disk.** About 115 bytes a parcel (§8.7): about 1.6 GB for the state's ~14 million parcels. The pull keeps the
  current and the previous version, and a new `latest.json` version is downloaded whole (unchanged counties too),
  so up to about 3.2 GB on the `connector_data` volume after the first rebuild. `docs/64` sizes no disk.
- **Memory, estimated (low confidence; not measured on real parcels or a real city).** Measured so far: the full
  Texas run without parcels peaked at 1.2 GB (`docs/25` §7), and the four synthetic rural counties' re-measure at
  639 MB (§8.7). The per-chunk parcel read adds, for each chunk of 10 segments, the parcels within about 2 miles
  of it (a box of about 90 to 150 km²) as Arrow columns (about 220 bytes a parcel measured on the fixtures, more
  for real outlines with more vertices), then shapely geometries and Python lists (about 1 to 2 KB a parcel,
  assumed). Rural Texas holds about 27 parcels per km² (Newton's synthetic count), so a few thousand per chunk;
  central Houston perhaps 1,000 to 2,000 per km², so 0.1 to 0.3 million per chunk and about 0.1 to 0.6 GB on top
  of the run's 1.2 GB. That stays inside the worker's 3 GB limit, but above the 1.2 GB peak that
  `SITE_FINDER_MIN_MEMORY_BYTES` (2 GiB) was derived from. The API is the tighter one: its 1 GB container peaked at 606 to 638 MB without parcels
  (`docs/60` §2). The outlines route (`services/api/site_finder.py::_parcel_features`) therefore never holds a
  view's outlines: it ranks the view on `parcel_key`, `holding_acres` and the box alone (about 60 to 70 bytes a
  parcel, so about 70 MB for a million), then reads the outlines of the 4,000 it keeps, `READ_CHUNK_GROUPS` row
  groups (65,536 parcels) at a time, each chunk filtered before the next (`services/context_sample/parcels.py`;
  coordinator, 2026-10-23, after the parcels-wire lane estimated that a one-pass read of a 0.5° central-Houston
  view, 0.5 to 1 million parcels, could pass 1 GB). Estimated peak on top of the api's own: about 0.1 GB at the
  widest Houston view; unmeasured on real outlines. Check the api's memory (`docker stats`) after the first pull
  that includes Harris (48201). No limit changed.

## Assumptions

- The DataHub API at `api.tnris.org` is the catalogue behind `data.geographic.texas.gov`. Evidence: the records'
  asset and resource URLs are on that host. The DataHub page itself was unreadable (403).
- The 2026 statewide zip has the same 38-field schema as the live service. The service and the schema PDF agree; the
  zip was not opened.
- Texas parcel count is about 14 million, from three vendors' counts (§1.5); TxGIO publishes none.
- Vendor prices are public-page prices on 2026-10-23. Regrid's nationwide prices are from its 2023 sheet and may have
  changed; its current site shows none.
