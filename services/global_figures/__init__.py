"""Global electricity figures in the store (`docs/26-global-figures.md`; owner, 2026-10-23).

Ember's operating capacity, generation and shares for World, regions and countries, as the connector
`pipeline/connectors/global_ember_electricity` normalises them:

* `models.py` — two tables (migration 0045): `global_electricity_load`, one row per dataset and route
  currently served, and `global_electricity_value`, its values, each with the provenance quartet.
* `load.py` — a run's normalised values into the tables, replacing the dataset's previous load in one
  transaction, so a failed load (or a failed download upstream) leaves the last good figures served.
* `read.py` — the reader the API serves from: an area's latest year with year-on-year change, the series,
  the clean share, the countries adding the most wind and solar, and the coverage of the latest year.

Why tables and not files (the prices slice does the same, `services/prices`): the API process reads
Postgres in every topology, and in the three-VM one it shares no volume with the worker that fetches
(`infra/compose/compose.single.yml` notes the read-only mount exists on the single host only). The
package is not called `global`, which is a Python keyword and cannot be imported.

These are operating figures, never a pipeline: nothing here writes a `proposal`, an `event` or a feed item.
"""
