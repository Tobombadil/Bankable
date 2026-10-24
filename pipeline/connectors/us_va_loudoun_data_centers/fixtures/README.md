# Recorded fixture: us.va.loudoun.data_centers

| Fixture | Retrieved from | Trim |
|---|---|---|
| `loudoun_pipeline_data_centers.json` | the connector's own snapshot of run 6e6fe541, **2026-10-24T00:39:56Z** (sha256 `849240ad…aafa`, the same bytes as run 89b7dd10 at 00:00:48Z): the licence on ArcGIS item `2a084dcc039249f286685205f1f4b33b`, the metadata of `Pipeline_Data_Center_Areas/FeatureServer/1` (data last edited 2026-05-21), its 85 rows, and the 206 Virginia DEQ data-centre air sites of `EDMA/MapServer/294` | the connector's current redaction applied first (`withhold_subdivision`: 4 of the 24 kept applications have their subdivision nulled and flagged `subdivision_withheld`), then 24 of the 85 applications and 12 of the 206 DEQ air sites kept, re-serialised with `indent=1`. Chosen to cover the four pairs of applications on the same land, the five sites that hold a DEQ air site (two Planned, three Operating), neighbours that share a sliver of boundary, DEQ points 1 to 35 m outside a site, a large legislative application beside several campuses, both Town of Leesburg numbers, a ZMAP, multi-ring land, every status value, a trailing space in an application number, a null acreage, two DEQ sites outside Loudoun. No other value edited |

Personal data: the fixture holds only the layer's six data fields (`Application`, `FIRST_Status`,
`FIRST_Subdivision`, `FIRST_Zoning_Case_Num`, `FIRST_Overall_SQ_FT`, `SUM_Pipeline_Acres`) plus `OBJECTID`, and
five DEQ columns with no name in them. Every subdivision it still holds is one whose structure proves an
organisation or place; the four others were withheld before the file was written, so their values are not in it.
The ArcGIS item's other fields (the publishing account among them) were never stored. The tests that prove an
owner or editor column is dropped, and that a person-shaped subdivision is withheld before storage, use made-up
names in memory (`FakeArcGIS` serves one where the fixture's value was withheld).
