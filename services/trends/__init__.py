"""The daily metrics snapshot: the front page's numbers recorded once a UTC day, so the site can later
show whether the boom is growing, shrinking or holding (owner, 2026-10-23: "If we can track any of
these numbers over time as well then that would be compelling").

- `services.trends.snapshot` computes one day's document from the store exactly as the public tier
  sees it (the visibility predicate, the list filters, values as served);
- `services.trends.store` writes it to `<data root>/normalized/trends/daily/YYYY-MM-DD.json`, mirrors
  it to the private object store when one is configured, and reads the series back.

The scheduler's `trends_tick` (`infra/scheduler/jobs.py::trends_tick_job`) runs both. This package
only records: the trends API, the charts and any backfill from official archives come later
(`docs/24-trends.md`).
"""
