# ERCOT GIS history fixtures

Recorded 2026-10-23 (lane trends-history) from ERCOT's public MIS routes; trimmed so the tests stay small.
Each workbook keeps every sheet; a sheet with a project header (`INR`) keeps its notes rows and header
verbatim and a sample of project rows chosen to cover each fuel, technology, study phase, zone and
milestone shape, plus its last three rows; other sheets keep their first 30 rows (the Summary's "as of"
date is among them). Values only: cell styles are not copied.

| Fixture | Real file | Retrieved | Original sha256 | Layout |
|---|---|---|---|---|
| `GIS_Report_December_2018_sample.xlsx` | `https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=721696919` (`GIS_Report_December_2018.xlsx`, 427,950 bytes, published 2020-06-29) | 2026-10-23T21:58Z | `31e276f8e19daec036c007815c907409d3500f07b24678746be4c820cc6d98b5` | oldest: one "Project Details" sheet, header on row 22, "GINR Study Phase" |
| `GIS_Report_March_2020_sample.xlsx` | `https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=721696780` (`GIS_Report_March_2020.xlsx`, 373,714 bytes, published 2020-06-29); trimmed 2026-10-24 (fix round) from the lane's stored snapshot of that download, by the same rule | 2026-10-23T22:05Z | `58bde6f8d6071fcfc1b26a19ad04040bae79b0ed61afb306d6413347bea2dba4` | middle: one "Project Details" sheet with the excludes-Inactive note (header on row 25, "GINR Study Phase"); holds the archive's only `COAL ST` row (493 MW), the coal unit the classifier read as `gas_steam` |
| `GIS_Report_September2026_sample.xlsx` | `https://www.ercot.com/misdownload/servlets/mirDownload?doclookupId=1281327706` (`GIS_Report_September2026.xlsx`, 673,369 bytes, published 2026-10-01) | 2026-10-23T21:58Z | `016cddbb7082fd30e02ede6242e3f33adc79ca0f2e7fb49bc9079ebde2671d58` | current: "Project Details - Large Gen" (header on row 31, "GIM Study Phase") and "Project Details - Small Gen" |
| `ercot_gis_doclist_sample.json` | `https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=15933` (81,054 bytes, 173 documents) | 2026-10-23T21:55Z | `810ac456162b53641a7cb1c4f66eecf70f37945e5bf2afd9bfce41046b3c33fd` | the real listing's 14 documents the tests read: December 2018, January 2019, April 2020 and its `_revised` re-issue, June 2020 and its `_Correction`, July 2020 and both corrections, September 2024 (no underscore before the year), `Jun2026` (abbreviated month), August and September 2026, and a co-located battery report |
