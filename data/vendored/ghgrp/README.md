# GHGRP ↔ ORIS power-plant crosswalk (vendored, public domain)

`oris_crosswalk.csv` — one row per `(ghgrp_facility_id, oris_code)` pair, 2,168 pairs for 2,131 GHGRP
facilities (29 with more than one ORIS code), trimmed from EPA's
`ghgrp_oris_power_plant_crosswalk_12_13_21.xlsx`
(https://www.epa.gov/system/files/documents/2022-04/ghgrp_oris_power_plant_crosswalk_12_13_21.xlsx,
272,305 bytes, sha256 `f4ec8ff08d3d964f8531a011705dde1b228266d39c3f73dec1062c64adf4526d`, retrieved
2026-09-25), sheet "ORIS Crosswalk", columns `GHGRP Facility ID` and `ORIS CODE` … `ORIS CODE 5`.
The other columns (facility name, city, state, power-plant-sector flag, reported subparts RY10–RY20)
are dropped; `pipeline/context/ghgrp.py::crosswalk_from_xlsx` regenerates this file from the workbook.

An ORIS code is the EIA plant code that `asset.source_asset_id` carries for `power_plant` rows
(`us.eia.860m`), so the pair is a deterministic, publisher-stated key from a GHGRP facility to an
existing asset. The workbook is dated 13 December 2021 and covers facilities through reporting year
2020; plants that began reporting later are matched by `pipeline/context/ghgrp.py`'s geo+name path.

US federal government work, 17 U.S.C. §105 (EPA hedge, docs/13 §2.12).
