# 36 — Tallgrass briefing pack: measured facts for the owner's conversation

**Status:** briefing pack only. **No outreach is drafted here** — the 2026-09-26 decision on this pack said
pack only, no message. Every number below is measured from the repo's own data by this lane, 2026-09-26; none
is copied from a prior brief without being re-derived.
**Reads:** `docs/02-data-sources.md` §11.3, §12; `docs/24-asset-identity-and-provenance.md` §2 (ethanol
duplication); `docs/00-PLAN.md` decisions log, 2026-09-20 row (Blackstone/Tallgrass ownership edge) and
2026-09-25 rows (GHGRP, CCS source map).

**Data used:** `data/normalized/context/us.eia.atlas.ethanol_plants.parquet` (EIA US Energy Atlas, 197
ethanol-plant points, exact lon/lat), `us.eia.atlas.gas_pipelines.parquet` (EIA US Energy Atlas gas pipelines,
Trailblazer's own `MultiLineString`), `us.epa.ghgrp.parquet` (11,281 RY2023 facility-year rows) and
`us.epa.ghgrp.matches.parquet` (asset-matching results). No PJM/MISO/SPP/NYISO row appears anywhere in this
pack — none is used.

---

## (a) Ethanol plants within 80 miles of the Trailblazer pipeline

**Method.** Trailblazer's own geometry (source_asset_id `trailblazer-pipeline-co-interstate`, 39 line parts,
bounding box lon −104.79 to −96.74 / lat 40.23 to 41.06 — the Cheyenne, WY to Beatrice/Steele City, NE
corridor) was parsed from its WKT and projected into an azimuthal-equidistant projection (`+proj=aeqd`)
centred on the mean of its own vertices (lon0 = −100.60, lat0 = 40.71) — a projection that is locally
distance-true around that centre point, adequate at this scale (a single ~150-mile-wide corridor) without
needing a national equal-area basis. Every ethanol plant's point (EIA Atlas, `us.eia.atlas.ethanol_plants.parquet`,
197 rows, all with an exact coordinate) was projected the same way, and planar point-to-line distance (metres
→ miles, 1 mi = 1609.344 m) was computed against the full multi-part line, not just its endpoints. The
**capacity-report parquet was excluded from the geography**: it carries no real lon/lat (all 191 rows null),
only a state-level placeholder centroid, which is not accurate enough for an 80-mile cut — using it would
silently relocate plants by up to a state's width. This matches what the coordinator's original count is
understood to have used (the Atlas layer, the only one of the two sources with real points).

**Result: 27 plants, 2,080 MMgal/yr combined nameplate capacity — reproduces the earlier measurement
exactly.** 26 plants sit strictly under 80.0 miles (1,950 MMgal/yr); the 27th, **Southwest Iowa Renewable
(Council Bluffs, IA, 130 MMgal/yr)**, measures **80.09–80.12 miles** by two independent methods (the
AEQD-projected planar distance above, and a direct WGS84 ellipsoidal geodesic via `pyproj.Geod` to the same
nearest point on the line) — a few hundred feet over a strict cutoff, and the plant that rounds the count to
27 / 2,080 at "within 80 miles" stated to the nearest mile. No difference beyond that rounding boundary was
found.

| # | Plant | State | Owner (as recorded, EIA Atlas) | Capacity (MMgal/yr) | Distance (mi) |
|---|---|---|---|---:|---:|
| 1 | Mid America Agri Products (Madrid) | NE | Mid America Agri Products LLC | 50 | 1.7 |
| 2 | Kaapa Ethanol (Minden) | NE | Kaapa Ethanol LLC | 87 | 5.7 |
| 3 | Chief Ethanol Fuels (Lexington) | NE | Chief Ethanol Fuels Inc | 52 | 8.4 |
| 4 | Chief Ethanol Fuels (Hastings) | NE | Chief Ethanol Fuels Inc | 70 | 8.7 |
| 5 | Flint Hills Resources Fairmont | NE | Flint Hills Resources Fairmont LLC | 127 | 15.1 |
| 6 | Green Plains Wood River | NE | Green Plains Wood River LLC | 110 | 20.1 |
| 7 | E Energy Adams | NE | E Energy Adams LLC | 101 | 20.1 |
| 8 | Midwest Renewable Energy (Sutherland) | NE | Midwest Renewable Energy LLC | 28 | 22.3 |
| 9 | Sterling Ethanol | CO | Sterling Ethanol LLC | 50 | 23.7 |
| 10 | Nebraska Corn Processing | NE | Nebraska Corn Processing LLC | 50 | 28.5 |
| 11 | Pacific Ethanol Aurora East | NE | Pacific Ethanol Aurora East LLC | 45 | 30.4 |
| 12 | Pacific Ethanol Aurora West | NE | Pacific Ethanol Aurora West LLC | 110 | 30.6 |
| 13 | Kaapa Ethanol Ravenna | NE | Kaapa Ethanol Ravenna LLC | 125 | 32.4 |
| 14 | Front Range Energy (Windsor) | CO | Front Range Energy LLC | 40 | 33.5 |
| 15 | Nesika Energy | KS | Nesika Energy LLC | 10 | 42.7 |
| 16 | Bridgeport Ethanol | NE | Bridgeport Ethanol LLC | 54 | 44.4 |
| 17 | Trenton Agri Products | NE | Trenton Agri Products LLC | 55 | 45.0 |
| 18 | Green Plains Central City | NE | Green Plains Central City LLC | 100 | 47.2 |
| 19 | Yuma Ethanol | CO | Yuma Ethanol LLC | 50 | 51.9 |
| 20 | Prairie Horizon Agri Energy | KS | Prairie Horizon Agri Enrgy LLC | 40 | 56.5 |
| 21 | Alten | NE | Alten LLC | 25 | 64.6 |
| 22 | ADM Columbus NE — Wet Mill | NE | ADM Columbus NE Wet Mill | 100 | 70.4 |
| 23 | ADM Columbus NE — Dry Mill | NE | ADM Columbus NE Dry Mill | 313 | 70.7 |
| 24 | Green Plains Ord | NE | Green Plains Ord LLC | 57 | 72.2 |
| 25 | Golden Triangle Energy | MO | Golden Triangle Energy LLC | 21 | 72.6 |
| 26 | Green Plains Inc (Shenandoah) | IA | Green Plains Inc | 80 | 79.5 |
| 27 | Southwest Iowa Renewable (Council Bluffs) | IA | Southwest Iowa Renewable | 130 | 80.1 |

**Total: 2,080 MMgal/yr.**

Two caveats worth having in the room. **First**, rows 22–23 are ADM's Columbus, NE site recorded as two
separate Atlas points 0.27 miles apart (a dry mill and a wet mill, the same two-process-train pattern EIA
records at ADM Decatur, IL as a single combined row) — one physical complex, two Atlas rows, 413 MMgal/yr
combined. **Second**, `docs/24` §2 measured that EIA's two ethanol sources (this Atlas layer and a separate
capacity-report table) are 48.2% cross-source redundant nationally — but that duplication cannot double-count
this table, because the capacity-report rows were excluded from the geography entirely (no coordinate to
place them by). This 27/2,080 count is Atlas-only and internally has zero known redundancy.

---

## (b) The 22 GHGRP facilities with Tallgrass among the parents

**Method:** `us.epa.ghgrp.parquet` (RY2023, EPA Envirofacts `pub_dim_facility`), filtered on `parents`
containing "TALLGRASS" (case-insensitive substring on the parsed parent-string field; `docs/02` §12.2's
grammar: `NAME (pct%); NAME (pct%)…`). **22 distinct `ghgrp_facility_id` values**, all RY2023, confirming
`docs/02` §12.2 exactly:

- **18 compressor stations** (NAICS 486210), each at **Tallgrass Development LP (75%); Phillips 66 (25%)**
  — Arlington (McFadden, WY), Bainbridge (IN), Bertrand (Loomis, NE), Big Hole (Craig, CO), Blue Mound (IL),
  Chandlersville (Philo, OH), Cheyenne Hub 905 (Carr, CO), Columbus (Ashville, OH), Compressor Station 601
  (Peetz, CO) — *100% Tallgrass, no Phillips 66 share on this one* — Echo Springs (Sweetwater County, WY),
  Meeker (Rifle, CO), Mexico (MO), **Rockies Express Pipeline** (Lakewood, CO), Seneca (Quaker City, OH),
  St. Paul (IN), Steele City (Odell, NE), Wamsutter (WY), Washington Court House (OH).
- **4 gas plants / gathering** (NAICS 211130), each at **Tallgrass Development LP (100%)** — Casper Gas
  Plant (WY), Douglas Gas Plant (WY), Tallgrass Powder River Basin Gathering System (Lakewood, CO), West
  Frenchie Draw Amine Plant (Lysite, WY).

Correction to a plain re-count: the row above named "Compressor Station 601" is 100% Tallgrass, not the
75/25 split; 17 of the 18 compressor stations carry the Phillips 66 share, not all 18. **None of the 22 is
under Subpart RR** (no CO₂ sequestration reporting), **none carries an MRV plan**, and a facility-name search
for "Eastern Wyoming" returns zero — Tallgrass's own Eastern Wyoming CO₂ storage-hub concept has no GHGRP
footprint yet.

**Only one of the 22 matches an asset already on our map**: `us.epa.ghgrp.matches.parquet` shows exactly one
accepted match against these 22 facility ids — **Douglas Gas Plant → Douglas Plant** (an existing
`gas_processing_plant` asset from `us.eia.atlas.gas_processing_plants`), method `geo_name`, **distance 0.6
metres**, name score 1.0, NAICS-compatible, score 1.0. The other 21 (17 compressor stations plus 3 more gas
plants/gathering) have no accepted match — they sit in the GHGRP parquet only, which is consistent with
`docs/02` §12.3's finding that pipeline compressor stations (NAICS 486) are the largest unmatched residue in
the whole GHGRP overlay (1,048 of 8,619 unmatched facilities nationally), because no point-asset layer in this
platform currently carries compressor stations as their own type.

---

## (c) What the public record does not contain — only Tallgrass can supply this

Per `docs/02` §11.3 ("What has no public primary source"), specifically:

1. **Lateral geometry and named laterals** (§11.3 item 1). PHMSA's Part D hazardous-liquid annual report
   gives operator/state/diameter *mileage totals* only, no geometry. The only public breakdown of
   Trailblazer's own route — mainline 349.1 mi + ADM Columbus lateral 83.1 mi + SIRE lateral 130.0 mi +
   smaller laterals, totalling 734.0 mi — comes from operator statements, FERC abandonment/conversion
   filings and project pages stitched by hand, not from any single register; NPMS/PIMMA, the federal
   pipeline-geometry system, is **closed to the public and to companies alike** ("The general public and
   private companies may not access PIMMA"). The EIA Atlas line used in part (a) above is Trailblazer's
   *whole* corridor as one un-differentiated `MultiLineString` — it cannot say which segment is the
   83.1-mile ADM Columbus lateral (notably: ADM's Columbus, NE ethanol complex is row 22–23 in part (a),
   70.4–70.7 miles from the mapped line) versus the mainline versus any other named lateral.
2. **The contracted-plant list** (§11.3 item 2) — which of the 27 ethanol plants in part (a), if any beyond
   what is publicly announced, are actually signed shippers to Trailblazer or a CO₂ offtake project on it.
   No register exists; the public trail is sponsor press releases, 8-Ks for the listed minority, and state
   docket exhibits, and it does not resolve to a list.

---

## (d) Questions tied to the facts above

1. **On the 27-plant / 2,080 MMgal/yr list (part a):** which of these are actually under contract or in
   active discussion for CO₂ transport, and are there others outside 80 miles that changed the mainline
   routing decision? (Ties to the missing contracted-plant list, part c item 2.)
2. **On the ADM Columbus lateral (parts a and c):** the public record gives an 83.1-mile figure for the ADM
   Columbus lateral but no geometry — does that lateral in fact run to the Columbus, NE complex we have
   mapped at 70.4–70.7 miles from the corridor, and would Tallgrass share the lateral's actual route so it
   can be placed correctly rather than folded into the undifferentiated mainline?
3. **On the GHGRP ownership split (part b):** 17 of the 18 Tallgrass compressor stations, plus Rockies
   Express Pipeline itself, report **75% Tallgrass Development LP / 25% Phillips 66** — is that the current
   post-Blackstone-acquisition (2019) structure, or a legacy EPA filing that has not been refreshed since,
   and does the same 75/25 split apply to Trailblazer itself (which does not appear as a GHGRP reporter at
   all, since it moves CO₂/products rather than combusting fuel)?
4. **On asset placement (part b):** only Douglas Gas Plant (WY) among the 22 GHGRP-reporting facilities
   matches a point we already carry (0.6 m from our existing EIA-sourced coordinate); the other 17 compressor
   stations have no coordinate on our map at all today. Would Tallgrass share coordinates or site names for
   the Trailblazer-corridor compressor stations, so those 17 can be placed precisely rather than left off the
   map entirely?
