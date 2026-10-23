# Vendored: gridstatus 0.36.0 queue parsers

`queues.py` holds the three interconnection-queue parsers our ISO connectors used from the
gridstatus library, so that library (and the `cryptography<47`, `lxml~=5.3`, `setuptools<79`,
`virtualenv<21` pins it carried) is no longer a dependency. Decision and evidence: `docs/00-PLAN.md`
decisions log (the row that closes the 2026-09-15 / 2026-10-06 gridstatus pin exception) and `docs/63` §5.2.

| | |
|---|---|
| Upstream | https://github.com/gridstatus/gridstatus (PyPI `gridstatus==0.36.0`, released 2026-04-21) |
| Licence | BSD 3-Clause, Copyright 2022 James Max Kanter — [`LICENSE`](LICENSE), copied unchanged from the 0.36.0 wheel's `licenses/LICENSE` |
| Vendored on | 2026-10-07 |
| Used by | `pipeline/connectors/iso_queue.py` → `us_iso_caiso_gen_queue`, `us_iso_ercot_gen_queue`, `us_iso_nyiso_gen_queue` |

## What was taken (0.36.0 file, lines, SHA-256 of the file)

| Our function | Upstream | File SHA-256 |
|---|---|---|
| `caiso_queue` | `gridstatus/caiso/caiso.py:1714-1812` `CAISO.get_interconnection_queue` | `337db64b…b503` |
| `ercot_queue` | `gridstatus/ercot.py:1515-1643` `Ercot.get_interconnection_queue` | `63fc49dd…5369` |
| `nyiso_queue` | `gridstatus/nyiso.py:696-901` `NYISO.get_interconnection_queue` | `61604ab0…6b37` |
| `format_interconnection_df` | `gridstatus/utils.py:274-298` | `1063b632…ee43` |
| `INTERCONNECTION_COLUMNS`, status labels | `gridstatus/base.py:66-92` (`InterconnectionQueueStatus`, `_interconnection_columns`) | `144b6a28…bfac` |

Nothing else: no HTTP client, no ISO class, no other ISO (SPP, ISO-NE, MISO, PJM) — none of those
has a connector here. The SPP "fallback" in `pipeline/normalize.py` is a status-map rule over
already-pulled rows and never called gridstatus.

## Changes from upstream

1. Input is the workbook bytes the connector fetched (`fetch()` in each connector), not an HTTP
   download inside the parser; nothing here touches the network.
2. Methods became module functions with typed signatures; the per-ISO lookup tables moved to
   module constants. Values are unchanged, including NYISO's `"SW": "=Solid Waste"` typo.
3. `format_interconnection_df` raises `QueueLayoutError` (a `ValueError`) where upstream used
   `assert`, so the layout check survives `python -O`; messages name the missing columns.
4. Logging and upstream's TODO comments dropped.

The output frame is unchanged: column-by-column equality (values, dtypes, order, row count) with
gridstatus 0.36.0 was checked on the committed fixtures and on the stored real snapshots of
2026-09-13 before the dependency was removed. Each connector's `test_connector.py`
(`test_vendored_parser_reproduces_the_gridstatus_0_36_frame`) pins the fixture frame to a digest of
gridstatus's own output, so any drift in this file fails CI.

## Updating

There is no upstream to track automatically. If an ISO changes its workbook layout, fix the
parser here (a `QueueLayoutError` or `KeyError` fails the run closed, docs/04 E-17), list the
change above, and record a fixture of the new layout.

# Vendored: gridstatus main (5c48466) price parsers

`prices.py` holds the parse steps of gridstatus's ERCOT, CAISO, NYISO and SPP price methods, for
the price connectors of docs/29 slice 1 (`pipeline/connectors/prices/`). The library is not a
dependency (decision above); these are copied functions, parse-only.

| | |
|---|---|
| Upstream | https://github.com/gridstatus/gridstatus, `main` at commit `5c48466720a865f0f90c3cc9f7ed67f84d059b71` (committed 2026-10-22T12:48:42-05:00) |
| Licence | BSD 3-Clause, Copyright 2022 James Max Kanter — the same [`LICENSE`](LICENSE) (SHA-256 `a68566bd…f474a`, identical at that commit) |
| Vendored on | 2026-10-22 |
| Used by | `pipeline/connectors/prices/ercot.py`, `caiso.py`, `nyiso.py`, `spp.py` |

## What was taken (file at 5c48466, lines, SHA-256 of the file)

| Our function or constant | Upstream | File SHA-256 |
|---|---|---|
| `REAL_TIME_5_MIN` … `DAY_AHEAD_HOURLY` | `gridstatus/base.py:29-46` (`Markets`) | `03f24d96…af65` |
| `ErcotDocument` | `gridstatus/ercot.py:483-490` (`Document`) | `34b1e830…cda` |
| `ercot_timestamp_from_friendly_name` | `gridstatus/ercot.py:493-516` | (same file) |
| `ercot_documents` | `gridstatus/ercot.py:7040-7164` (`Ercot._get_documents`) | |
| `_ercot_ambiguous_based_on_dstflag`, `ercot_parse_doc` | `gridstatus/ercot.py:7466-7632` | |
| `_ercot_settlement_point_name_and_type`, `ercot_finalize_spp` | `gridstatus/ercot.py:2434-2535` | |
| `ercot_annual_spp` | `gridstatus/ercot.py:1548-1588` (`Ercot._get_spp_from_annual`, the parse half) | |
| `caiso_oasis_frame` | `gridstatus/caiso/caiso.py:349-470` (`CAISO.get_oasis_dataset`, the parse half) | `b1fa1b10…7724` |
| `caiso_lmp` | `gridstatus/caiso/caiso.py:1483-1640` (`CAISO.get_lmp`, the frame half) | |
| `nyiso_handle_time` | `gridstatus/nyiso.py:123-182` (`NYISO._handle_time`) | `e7a1f24a…29d4` |
| `nyiso_lmp` | `gridstatus/nyiso.py:746-865` (`NYISO.get_lmp`, the frame half) | |
| `spp_market_end_to_interval` | `gridstatus/spp.py:1140-1167` | `b491429b…5a80` |
| `spp_finalize_lmp` | `gridstatus/spp.py:1777-1860` (`SPP._finalize_spp_df`) | |
| `_first_member` | `gridstatus/utils.py:211-213` (the zip read in `read_csv_from_zip`) | `4b1e7fff…a757` |

Each function's docstring names its upstream lines again. Nothing else was taken: no HTTP
client, no ISO class, no PJM, MISO or ISO-NE code (none of those markets is fetched, docs/29).

## Changes from upstream

1. Input is the bytes (or the frame) a connector fetched through `PoliteSession`; nothing here
   touches the network. Upstream's one network call inside a parser, ERCOT's settlement-point
   mapping (`_get_settlement_point_mapping`, which only decides the "Resource Node" label), is the
   `resource_nodes` argument; the price connectors read hubs and zones only and pass none.
2. Methods became module functions with typed signatures; per-ISO constants are module constants.
3. `PriceLayoutError` (a `ValueError`) where upstream used `assert` or raised a bare `ValueError`
   (`ercot_documents` with no match returns `[]` instead of raising; the date filters, which the
   connectors do not use, are dropped), so a layout check survives `python -O`.
4. `filter_lmp_locations` is not applied: each connector keeps its own location list
   (`pipeline/connectors/prices/locations.py`).
5. `ercot_annual_spp` drops empty sheets before concatenating (pandas 2 warns on concatenating an
   empty frame) and renames `Settlement Point Type` to `SettlementPointType`: the 2025 and 2026
   annual workbooks spell the column with spaces, which upstream's finaliser then fails to find.
6. `ErcotDocument` gains `doc_id` and `content_size` (the document list carries both): the
   connector stores each workbook once by DocID and records its published size.
7. `nyiso_lmp` keeps the hourly markets only (the five-minute real-time branch is not used).
8. `caiso_oasis_frame` checks for OASIS's no-data error document on the bytes it was given.
9. Logging and upstream TODO comments dropped.
10. `ercot_annual_spp_sheets` (added 2026-10-23, review of lane price-hubs) is `ercot_annual_spp`
    one monthly sheet at a time, with a `keep_sheet` filter: upstream reads every sheet, then
    concatenates and parses the year in one frame, about 400 MB for one real-time workbook, and a
    backfill's four did not fit the worker's 1 GB container. The steps are upstream's, applied to
    each sheet: with the workbook's "Repeated Hour Flag" present they are row by row, and the
    year's "at most one null row" check is a running count over the sheets read.
    `test_prices_vendored.py` holds the two equal on the recorded workbooks.

## Pinning

`test_prices_vendored.py` pins the vendored output on the recorded fixtures
(`pipeline/connectors/prices/fixtures/`) to digests of the frames, so any change to `prices.py`
that moves a value, a time stamp or a column fails CI. Two independent routes to the same ERCOT
prices (the annual workbook through this parser, the cdr display page through the connector's
own) agree on every interval of 2025-11-02 (`pipeline/connectors/prices/test_parsers.py`).

**Against upstream's own output (2026-10-23, review of lane price-hubs).** Upstream at
5c48466 was installed outside the repository (`pip install --no-deps --target <scratch>
git+https://github.com/gridstatus/gridstatus@5c48466720a865f0f90c3cc9f7ed67f84d059b71`, plus its
pure-Python imports; its file hashes match the table above) and run on the same fixtures with
only its network calls stubbed: `Ercot.get_rtm_spp` / `get_dam_spp` with `_get_document`,
`utils.get_zip_file` and `_get_settlement_point_mapping` answering from the fixture,
`CAISO._get_oasis` with `requests.get` answering the fixture and `CAISO._get_lmp` over it,
`NYISO._handle_time` + `_process_lmp_data`, and `SPP._finalize_spp_df`. Result, compared with
`pandas.testing.assert_frame_equal` (dtypes included):

| Fixture | Vendored function | Upstream |
|---|---|---|
| `ercot_dam_annual_2025_nov02.zip`, `ercot_dam_annual_2026_sample.zip` | `ercot_annual_spp` (and `ercot_annual_spp_sheets`, concatenated) | identical |
| `ercot_rtm_annual_2025_nov02.zip`, `ercot_rtm_annual_2026_sample.zip` | the same | identical once change 5 is undone: upstream names the 800 and 1,504 energy-weighted zone rows like the zones themselves (`LZ_HOUSTON`, type "Load Zone"), the bug change 5 fixes |
| `caiso_dam_2d.zip`, `caiso_rtpd_1d.zip` | `caiso_oasis_frame`, `caiso_lmp` | identical |
| `nyiso_20261021damlbmp_zone.csv`, `nyiso_20261021rtlbmp_zone.csv`, the 2025-11-02 file of `nyiso_20251101rtlbmp_zone_csv.zip` (the day DST ends) | `nyiso_handle_time`, `nyiso_lmp` | identical |
| `spp_da_sl_20261021_trimmed.csv` | `spp_finalize_lmp` | identical |

Upstream is not a dependency, so CI cannot repeat this; rerun it the same way when the vendored
commit moves (Updating, above).
