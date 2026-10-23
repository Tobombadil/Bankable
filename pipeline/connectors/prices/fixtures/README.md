# Price connector fixtures

Real files from each source, recorded 2026-10-22 (UTC) with the connectors' own User-Agent, for
the parser and runner tests (docs/04 E-6). Values are never edited. Where a file was cut down to
fixture size, the rows kept are copied verbatim and the cut is stated below. The tests read them
through `conftest.py` (`fixture_bytes`, `snap`, `FakeHttp`); nothing in a test touches the network.

| Fixture | Source URL (as fetched) | Cut |
|---|---|---|
| `ercot_real_time_spp.html` | `https://www.ercot.com/content/cdr/html/real_time_spp.html` | none |
| `ercot_20261021_real_time_spp.html` | `…/content/cdr/html/20261021_real_time_spp.html` | none |
| `ercot_20251102_real_time_spp.html` | `…/20251102_real_time_spp.html` (the day DST ended: 100 intervals) | none |
| `ercot_20260308_real_time_spp.html` | `…/20260308_real_time_spp.html` (the day DST started: 92 intervals) | none |
| `ercot_20261022_dam_spp.html` | `…/20261022_dam_spp.html` | none |
| `ercot_20260308_dam_spp.html` | `…/20260308_dam_spp.html` (23 hours) | none |
| `ercot_20251102_dam_spp.html` | `…/20251102_dam_spp.html` (ERCOT keeps the page but no longer fills it) | none |
| `ercot_doclist_13060.json`, `ercot_doclist_13061.json` | `https://www.ercot.com/misapp/servlets/IceDocListJsonWS?reportTypeId=13060` / `13061` | none |
| `ercot_rtm_annual_2025_nov02.zip` | NP6-785-ER `RTMLZHBSPP_2025.zip` (DocID 1177737535) | the `Nov` sheet's rows for 11/02/2025 only, re-zipped |
| `ercot_rtm_annual_2026_sample.zip` | NP6-785-ER `RTMLZHBSPP_2026.zip` (DocID 1287900508) | 03/08/2026 (`Mar`) and 10/01/2026 (`Oct`) only |
| `ercot_dam_annual_2025_nov02.zip` | NP4-180-ER `DAMLZHBSPP_2025.zip` (DocID 1177667469) | 11/02/2025 only |
| `ercot_dam_annual_2026_sample.zip` | NP4-180-ER `DAMLZHBSPP_2026.zip` (DocID 1287898588) | 03/08/2026 and 10/01/2026 only |
| `spp_DA_Features_hubs.json` | `https://pricecontourmap.spp.org/arcgis/rest/services/PCM/DA_Features/MapServer/2/query?where=PNODETYPE='H'&outFields=*&outSR=4326&f=json` | none |
| `spp_RTBM_Features_hubs.json` | `…/PCM/RTBM_Features/MapServer/2/query` (same query) | none |
| `spp_da_sl_20261021_trimmed.csv` | `https://portal.spp.org/file-browser-api/download/da-lmp-by-settlement-location?path=/2026/10/By_Day/DA-LMP-SL-202610210100.csv` (4,262,072 bytes) | header, every hub row, and the first 48 other rows |
| `caiso_dam_2d.zip` | OASIS `SingleZip` `PRC_LMP` v12 `DAM`, the seven nodes, 2026-10-19 and 2026-10-20 | none |
| `caiso_rtpd_1d.zip` | OASIS `SingleZip` `PRC_RTPD_LMP` v3 `RTPD`, the seven nodes, 2026-10-20 | none |
| `nyiso_20261021damlbmp_zone.csv`, `nyiso_20261021rtlbmp_zone.csv` | `https://mis.nyiso.com/public/csv/damlbmp/20261021damlbmp_zone.csv` (and `rtlbmp`) | none |
| `nyiso_20251101damlbmp_zone_csv.zip`, `nyiso_20251101rtlbmp_zone_csv.zip` | `…/damlbmp/20251101damlbmp_zone_csv.zip` (and `rtlbmp`): November 2025, DST included | none |
| `elexon_system_prices_20261021.json` | `https://data.elexon.co.uk/bmrs/api/v1/balancing/settlement/system-prices/2026-10-21?format=json` | none |
| `elexon_system_prices_20260329.json` | `…/system-prices/2026-03-29?format=json` (UK clocks forward: 46 periods) | none |
| `eia_sales_revenue_trimmed.xlsx` | `https://www.eia.gov/electricity/data/eia861m/xls/sales_revenue.xlsx` (EIA-861M) | `Monthly-States` sheet only: its three header rows, 2025-07 to 2026-07 for TX, CA, NY, OK, NE and DC, and the notes block; other sheets dropped |

Re-recording: fetch the same URL with `pipeline.connectors.http.PoliteSession`, keep the file as
fetched where it is small, cut it as above where it is not, and update the facts the tests name
(prices copied from the first rows, interval counts). ERCOT's annual workbooks count against the
"three times within a 12-month period" rule (docs/13 §6): re-cut them from a stored snapshot,
never by downloading again.
