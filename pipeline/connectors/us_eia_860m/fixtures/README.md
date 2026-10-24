# us.eia.860m recorded fixtures

| Fixture | Retrieved from | Trim |
|---|---|---|
| `september_generator2026_planned_and_canceled.xlsx` | `https://www.eia.gov/electricity/data/eia860m/xls/september_generator2026.xlsx`, fetched 2026-10-23 21:41 UTC (HTTP 200, 14,013,970 bytes, sha256 `bcef74e1af991680731aeecfda967f5950db4ce739bb584edd8b8774c7e6da17`, `Last-Modified: Fri, 23 Oct 2026 15:32:18 GMT`) | Two of the workbook's seven sheets, values copied cell for cell, no value edited. **Planned**: the two title rows, the header, the first 12 of 2,358 units and then the first unit of each technology not yet included (28 units, 19 technologies including one unit with no technology), then the sheet's trailing blank and NOTES rows. **Canceled or Postponed**: the two title rows ("Inventory of Canceled or Indefinitely Postponed Projects as of September 2026"), the header, the first unit of each of the sheet's 27 technologies in file order (27 of 1,745 units), then its trailing blank and NOTES rows. Map hyperlinks are not kept (their cell text, "Map", is). Entity names are companies; no column names a person |

Re-record by fetching the current month's workbook with the connector and trimming it the same way
(the script that cut this one copied rows with `openpyxl` in read-only mode and wrote a new workbook).
