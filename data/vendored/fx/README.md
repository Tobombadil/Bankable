# ECB euro foreign exchange reference rates (seed)

`eurofxref-hist-from-2025.csv` is the European Central Bank's reference-rate history cut to the
fixings from 2025-01-02 onward (461 business days, to 2026-10-21), with the currencies that are
`N/A` on every kept day removed. Nothing else is edited: one row per fixing, units of each currency
per euro, `N/A` where the ECB did not quote it (BGN after Bulgaria joined the euro on 2026-01-01).

- Source: `eu.ecb.eurofxref` in `data/sources.yaml`, file
  `https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist.zip` (member `eurofxref-hist.csv`).
- Retrieved: 2026-10-22T01:01:38Z, HTTP 200, 641,589 bytes, sha256
  `93bac351c4a60e612e641150f2392db9f85fe497336ee9d71c4d241f70063eda`, `Last-Modified: Wed, 21 Oct 2026
  13:56:26 GMT`.
- Terms: the ECB's "Disclaimer & Copyright" allows free use if the information appears accurately and
  the ECB is cited, and asks that a modification be stated; the quote is in `data/sources.yaml`. The
  USD cross rates computed from this file are ours, and every surface that shows one says so
  (`changes_statement`).

Why it is vendored: `pipeline/fx.py` converts a notice's stated value to an approximate USD
equivalent for sorting. Until the first `python -m pipeline.fx refresh` on a data root, this seed
answers; afterwards the fetched history (`<data root>/snapshots/eu.ecb.eurofxref/eurofxref-hist.csv`)
replaces it date by date. A notice dated after the seed's last fixing converts at that fixing and
says so (`fx_date`).

Re-cut: download the zip, keep rows dated 2025-01-01 or later, drop columns that are `N/A` on every
kept row, keep the header order.
