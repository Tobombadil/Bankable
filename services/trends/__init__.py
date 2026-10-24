"""Activity over time: the front page's numbers recorded once a UTC day, the official monthly archives
before the clock started, and the series `GET /v1/trends` serves, so the site can show whether the boom is
growing, shrinking or holding (owner, 2026-10-23: "If we can track any of these numbers over time as well
then that would be compelling").

- `services.trends.snapshot` computes one day's document from the store exactly as the public tier
  sees it (the visibility predicate, the list filters, values as served);
- `services.trends.store` writes it to `<data root>/normalized/trends/daily/YYYY-MM-DD.json`, mirrors
  it to the private object store when one is configured, and reads the series back;
- `services.trends.series` reads the daily documents and the monthly archive documents
  (`pipeline/connectors/history`, EIA-860M and ERCOT's GIS reports) as named series, one source per
  series, with changes computed only like for like.

The scheduler's `trends_tick` (`infra/scheduler/jobs.py::trends_tick_job`) runs the first two;
`infra/scheduler/history.py` runs the archives (`docs/24-trends.md`).
"""
