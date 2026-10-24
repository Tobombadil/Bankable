# EIA-860M history fixtures

Recorded 2026-10-23 (lane trends-history) from real files; trimmed so the tests stay small. Trimming
keeps every sheet, its title and notes rows and its header row verbatim, and a sample of data rows chosen
to cover each status code, each technology label and the rows without a capacity, plus each sheet's last
three rows (EIA's trailing note rows). Values only: cell styles are not copied. The originals are not in
the repository.

| Fixture | Real file | Retrieved | Original sha256 | Layout |
|---|---|---|---|---|
| `september_generator2026_sample.xlsx` | `https://www.eia.gov/electricity/data/eia860m/xls/september_generator2026.xlsx` (14,013,970 bytes, `Last-Modified: Fri, 23 Oct 2026 15:32:18 GMT`, `ETag: "0ad2b0363dd1:0"`) | 2026-10-23T22:00Z | `bcef74e1af991680731aeecfda967f5950db4ce739bb584edd8b8774c7e6da17` | current: title row, a blank row, header on the third row; nameplate, net summer and winter MW; `*_PR` sheets |
| `eia860m-2015-07_sample.xlsx` | member `eia860m-2015-07.xlsx` of `eia860m-2015.zip` in Catalyst Cooperative's Zenodo record 10.5281/zenodo.22995197 (`https://zenodo.org/records/22995197/files/eia860m-2015.zip`, 14,177,917 bytes, md5 `bc6a0c5b0aaefd81f5b4cca847bccff0` as the record states), EIA's July 2015 workbook (`archive/xls/july_generator2015.xlsx` on eia.gov, which robots.txt keeps crawlers out of) | 2026-10-23T21:57Z | zip `fee6630cd44470619b73eb8c4ef4da6c30093e72e94ca7c1714d397d9b57d511` | oldest: title row, header on the second row; net summer MW only (no nameplate, county, coordinates); no Puerto Rico |
| `eia860m_index_sample.html` | `https://www.eia.gov/electricity/data/eia860m/` (93,623 bytes) | 2026-10-23T21:52Z | `b61978c0a6bc90a58ac1f206e072912b4e5e7a6d6e213fe8365e3ad1c910679f` | the page's release-date lines and its link rows for 2026 and 2015, verbatim, in a minimal document |
| `zenodo_datapackage.json` | `https://zenodo.org/records/22995197/files/datapackage.json` (the record's own file list: names, sizes, md5, months per zip) | 2026-10-23T21:56Z | `3c37d536102a3748ebfbf600b8b4585683e01c62a437f57e1f500005283adf02` | unmodified |

The mirror tests wrap `eia860m-2015-07_sample.xlsx` in a zip built in the test (named as the record's
members are); that container is synthetic, the workbook inside it is the recorded one.
