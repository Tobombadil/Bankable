"""Energy prices in the store (docs/29 §7.1, slice 1: hubs and zones, and EIA state retail prices).

* `models.py` — the five tables (migration 0041): `price_location`, `price_daily`, `price_stat`,
  `price_latest`, `retail_price`. Prices are context data: their own tables, linked by id, never
  written into `asset.attributes` (docs/29 §1 point 3, the ADR 0008 §4 amendment).
* `daily.py` — interval prices to one summary per location, market and market-local day (pure).
* `stats.py` — the trailing statistics from the daily summaries (pure, plus the job).
* `load.py` — a price run's interval store into the tables (the loader the scheduler queues).

Every row carries `source_id` and `licence_id` (foreign keys to `source`/`licence`), so the API's
visibility predicate gates it exactly as it gates a record (`services/api/prices.py`).
"""
