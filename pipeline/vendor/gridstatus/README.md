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
