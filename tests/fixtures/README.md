# Recorded connector fixtures

Every file here is a **real** response, fetched on **2026-09-12** by the connector that parses it and
then trimmed to a few dozen rows (docs/04 E-6: ≤ 1 MB, one fixture per format and layout variant, no
personal data, no rows from a `restricted`/`unknown` source). `parse()` runs against these in CI;
`fetch()` never does. Re-record by running the connector and trimming its snapshot the same way.

| Fixture | Source id | Retrieved from | Trim |
|---|---|---|---|
| `ercot_gis_report.xlsx` | `us.iso.ercot.gen_queue` | `https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1269363208` (GIS_Report_August2026, EMIL PG7-200-ER / reportTypeId 15933) | sheet "Project Details - Large Gen" only, first 25 data rows |
| `ercot_emil_catalogue.json` | `us.iso.ercot.large_load_queue` | `https://www.ercot.com/api/1/services/read/common/filter-emil-items-search.json` | 3 products; contains **no** Protocol 3.2.7 large-load report, which is the state of the catalogue on the retrieval date |
| `ercot_emil_catalogue_with_report.json` | `us.iso.ercot.large_load_queue` | as above, **plus one synthetic product row** | layout variant proving the selector fires when ERCOT publishes the report; the synthetic row is the only fabricated record in this directory and is never presented as real data |
| `caiso_public_queue_report.xlsx` | `us.iso.caiso.gen_queue` | `https://www.caiso.com/PublishedDocuments/PublicQueueReport.xlsx` | 3 sheets, ~20/10/10 rows, legend rows kept so the parser's trailing-row trim is exercised |
| `nyiso_interconnection_queue.xlsx` | `us.iso.nyiso.gen_queue` | `https://www.nyiso.com/documents/20142/1407078/NYISO-Interconnection-Queue.xlsx` | 5 parsed sheets, 10–20 rows each, including the padding rows with no identity |
| `eia860m_planned.xlsx` | `us.eia.860m` | `https://www.eia.gov/electricity/data/eia860m/xls/july_generator2026.xlsx` | "Planned" sheet, title rows + header + 30 rows |
| `neso_tec_register.csv` | `gb.neso.tec_register` | CKAN resource `tec-register-11-september-2026.csv` | 38 rows including the staged project and the two unstaged rows that share a Project ID |
| `grants_gov_search2.json` | `us.grants_gov.search2` | `POST https://api.grants.gov/v1/api/search2` | 12 hits; facet blocks and the response token removed |
| `grants_gov_search2_error.json` | `us.grants_gov.search2` | same endpoint, shape of its error body | the "HTTP 200 with an error body" case (docs/02 §7, docs/04 E-6) |
| `ted_search.json` | `eu.ted.api` | `POST https://api.ted.europa.eu/v3/notices/search` | 13 notices across 6 notice types; 24-language title maps cut to English + source language; no contact fields are requested from the API |
| `find_a_tender_ocds.json` | `gb.find_a_tender` | `https://www.find-tender.service.gov.uk/api/1.0/ocdsReleasePackages` | 8 energy-CPV releases + 3 non-energy (the CPV filter must reject those); `contactPoint` already stripped by the connector's `redact()` (docs/13 §5.4) |
| `worldbank_procnotices.json` | `mdb.worldbank.procnotices` | `https://search.worldbank.org/api/v2/procnotices` with `sector.sector_code=LU^LH^…` | 9 notices across 4 notice types; `contact_*` fields other than `contact_organization` are never requested |
| `ferc_elibrary_search.json` | `us.ferc.elibrary` | `POST https://elibrary.ferc.gov/eLibrarywebapi/api/Search/AdvancedSearch` | 5 pages (ER26/CP26 docket-number search plus 3 description-term searches), 23 unique accessions before the window/docket-class filter, 17 survive it |
| `ferc_elibrary_search_error.json` | `us.ferc.elibrary` | same endpoint, `sortBy: "filed_date"` | a real captured `success: false` response (the docs/02 §7 "HTTP 200 with an error body" case) — this exact `sortBy` value 500s server-side, which is why the connector always sends `sortBy: ""` |
| `permits_dashboard_projects.csv` | `us.permits_dashboard` | `https://data.permits.performance.gov/api/views/mcm3-xbid/rows.csv?accessType=DOWNLOAD` | 224 milestone rows across 12 projects (10 in the six energy/transmission sectors the connector keeps, 2 out of scope — Aviation and Surface Transportation — to exercise the sector filter) |

No fixture contains a row from a `restricted` or `unknown` source: SPP, ISO-NE, PJM and MISO have no
connector this sprint, and the gate tests use a stub rather than their data.
