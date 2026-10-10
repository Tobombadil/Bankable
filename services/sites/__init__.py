"""Sites: the parent entity over proposals that share a place by unique identifier (owner decision
2026-10-10, answering docs/51 §7 Q6; docs/21 §3.25).

"This is an entity relationship problem. Use unique identifiers: if records share an address, put
them under the latest and largest filing for that address, list the others as subprojects in the
same site, and do your best to label the relationships." (owner, 2026-10-10)

No source publishes a street address for a queue request, so "address" means the identifiers the
store holds: an EIA plant id, an exact point, an interconnection point (POI). A site is a parent
with a stable id of its own; its members are existing proposals, which keep their ids, URLs, alerts
and change feed. The resolver's merges are untouched: a site groups live records, it never merges
them.

* `rules.py` -- pure: union-find over grouping edges, the lead ordering, the relationship labels,
  and how a rebuilt cluster inherits an existing site id. No store, no pipeline import, so the API
  can re-run the lead and label rules over the members a caller may see.
* `evidence.py` -- the per-record facts the rules read (EIA plant ids from the 860M record ids, name
  stems and phase markers, technology families) and the grouping edges (rules a-c).
* `build.py` -- the store pass: read live proposals, build, write `site`/`site_member` idempotently,
  retire sites that lost every member. Run at the end of every resolve tick and by
  `python -m services.sites`.
"""

from __future__ import annotations
