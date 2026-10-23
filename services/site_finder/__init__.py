"""The powered-land site finder (owner decision 2026-10-22, docs/00-PLAN.md): corridor bands within an
adjustable distance (0.25-2 miles, default 1) of a 69 kV-or-higher transmission line, within an
adjustable distance (1-10 miles, default 5) of a substation, outside the FEMA floodplain where FEMA has
mapped it, with the voltage floor adjustable too.

A site is a candidate corridor band, not a land parcel: parcels are licensed county data. Each result
links out to the county, FEMA's flood map viewer, Google Maps and the OpenStreetMap elements.

- `compute`: the job. Cuts every OpenStreetMap line of 69 kV and up (and every line of unstated
  voltage, flagged) into segments of about half a mile and stores per segment what the finder filters
  and ranks on (`site_candidate`, migration 0039). Run after each context load
  (`infra/scheduler/app.py` `site_finder_refresh`) or by hand: `python -m services.site_finder`.
- `geometry`: segmenting, a local metric plane, point-to-line distances.
- `flood`: floodplain shares and flood-data coverage per band half-width, from the FEMA NFHL build
  (`pipeline/context/fema_nfhl.py`; its `FloodLayer` is the reference rule).
- `counties`: Census TIGER/Line counties (`us.census.tiger_counties`), with the vendored 1:20m counties
  as a labelled fallback.
- `prospect`: the prospecting layers per segment (`services.context_sample.sample_line`), stored as
  numbers, merged per band for the API.
- `provenance`: the provenance shape for values read from files.

The API (`services/api/site_finder.py`) merges adjacent passing segments of one line into bands at
request time, and reads everything tier-dependent (nearby requests, queue totals) per request.
"""
