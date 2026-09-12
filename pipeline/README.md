# pipeline/ — pull → normalise → resolve → diff (prototype)

Prototype of the stages in `docs/20-architecture.md` §3 over the five reachable ISO queues
(CAISO, ERCOT, SPP, NYISO, ISO-NE via gridstatus) and the EIA-860M *Planned* sheet. Design, measured
results and failure cases: `docs/22-entity-resolution-and-change-detection.md`.

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python pipeline/pull.py                 # -> data/eval/raw/<source>.<date>.parquet + manifest
.venv/bin/python pipeline/normalize.py            # -> data/eval/normalized.parquet   (counts per source/state)
.venv/bin/python pipeline/resolve.py --sweep      # -> data/eval/matches.parquet, clusters.parquet (+ P/R if labels.csv exists)
.venv/bin/python pipeline/diff.py --demo          # -> data/eval/events.parquet from a seeded perturbation
.venv/bin/python -m pytest tests/ -q
```

| File | What it does | Knobs |
|---|---|---|
| `pull.py` | gridstatus queues + EIA-860M xlsx (index-page scrape, first real zip wins) into parquet | `--date`, `--only caiso,ercot` |
| `status_map.yaml` | per-source raw status → canonical lifecycle state, refine rules, caveats (data, not code) | edit and re-run normalize |
| `normalize.py` | canonical proposal columns (docs/02 §5): kind, technology, capacity, state/county, sponsor, lifecycle_state, dates, keys | `--date`, `--out` |
| `resolve.py` | D1 EIA id → D2 queue id+ISO → D3 cited queue id → blocks B1/B2/B3 → fuzzy score → clusters | `--threshold 75`, `--name-scorer mean`, `--no-eia-rollup`, `--sweep`, `--labels` |
| `diff.py` | two normalised snapshots → events: new, status_change, capacity_change, cod_change, withdrawn, removed | `--before/--after`, `--demo --seed` |

Outputs under `data/eval/` are kept below 20 MB (`resolve.py --max-rows` down-samples rejected
pairs if needed; today's run writes 2.9 MB unsampled). `labels.csv` is the hand-labelled pair set
(label 1 / 0 / -1 = uncertain, excluded from metrics). PJM (key) and MISO (403) are out of scope
until the gates in `docs/02` §2 are cleared. Nothing here publishes anything; licence terms for
SPP/NYISO/ISO-NE are still "unknown" in `data/sources.yaml`.
