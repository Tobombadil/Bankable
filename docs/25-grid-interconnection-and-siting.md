# 25 — Grid interconnection points and siting layers

Owner request (2026-09-28): track where projects and data centres connect to the grid, and the fiber that
data centres need. The owner clarified "interconnection sites" as **grid** interconnection points (the
substation or bus a queue position connects into), not internet exchanges or carrier hotels. Decision row:
`docs/00-PLAN.md` decisions log, 2026-09-28.

**Scope, in the order it is built.** Each section below is written by the lane that builds it; the coordinator
verifies and merges.

1. **Interconnection points (G1).** Every US ISO queue row already carries its point of interconnection as text
   (`Interconnection Location`: ERCOT 1,778 rows, CAISO 2,278, NYISO 1,804, measured 2026-09-27 on the stored
   frames), and every NESO TEC row its `Connection Site` (2,198). Surface it and roll up queued MW per point.
   No new source.
2. **Substations and transmission lines (G2).** Built-infrastructure context layers, and the coordinates that
   place an interconnection point on the map. Candidate source: the HIFLD Open layers, archived after DHS
   discontinued HIFLD Open on 2025-08-26 — usable only after their terms are read and recorded (CLAUDE.md).
3. **Data-centre demand (G3).** Public filings that reveal a large load before it is built: state air permits for
   backup generators, utility large-load tariff dockets, and large-load queues where published as data.
4. **Fiber availability (G4).** An area-level "is fiber service present here" layer from the FCC Broadband Data
   Collection. **No fiber routes**: there is no open, current, route-level national dataset (InterTubes is
   2014–15 vintage; carrier maps are the carriers'), and precise routes are security-sensitive.

**Out of scope.** Internet exchanges and colocation facilities (PeeringDB's AUP forbids commercial application
and bulk redistribution: https://www.peeringdb.com/aup). Anything from the private aggregators named in
CLAUDE.md. OpenStreetMap substations and lines (ODbL share-alike; excluded in `data/sources.yaml`).

## 1. Interconnection points

*Pending: lane G1.*

## 2. Substations and transmission lines

*Pending: lane G2.*

## 3. Data-centre demand signals

*Pending: lane G3.*

## 4. Fiber availability by area

*Pending: lane G4.*

## Assumptions

- A queue's point-of-interconnection text names a substation or bus, sometimes with a bus number and voltage
  ("59903 Bearkat 345kV"), sometimes a line tap ("Cortland - Fenner 115kV"). A line tap has no single
  substation; it is kept as text and not placed.
- A point of interconnection is where a project connects, not where it is built. Placing a proposal at its
  substation would be wrong; the point is its own record, linked from the proposal (the same rule
  `docs/21` §3.7 applies to NESO connection sites).
