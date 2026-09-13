# `pipeline/` — connectors, normalise, resolve, diff

Runtime stages of `docs/20-architecture.md` §3. The Sprint 1 deliverable is
`pipeline/connectors/`: one connector per `data/sources.yaml` id behind a common interface, with
snapshot storage, change-event diffing, data-quality gates and publication gating enforced in code.
`normalize.py`, `resolve.py` and `diff.py` are the Phase 2 prototype they build on; design, measured
results and failure cases are in `docs/22-entity-resolution-and-change-detection.md`.

## Install

```
python -m venv .venv && .venv/bin/pip install -r requirements.txt
```

## Run connectors

```
.venv/bin/python -m pipeline.connectors list                       # registry: implemented / unimplemented / gated / excluded
.venv/bin/python -m pipeline.connectors run us.iso.ercot.gen_queue # one source
.venv/bin/python -m pipeline.connectors run --all                  # every implemented, non-gated source
.venv/bin/python -m pipeline.connectors run us.iso.spp.gen_queue --allow-restricted   # quarantined, see "Gating"
.venv/bin/python -m pipeline.connectors run --all --data-dir /tmp/bankable-data
```

Output is structured JSON log lines (`docs/04` E-18); everything else lands on disk under `data/`:

| Path | What |
|---|---|
| `data/snapshots/{source_id}/{retrieved_at}.{ext}` | raw bytes exactly as fetched — evidence, never published |
| `data/normalized/{source_id}/{retrieved_at}.parquet` | canonical records with the provenance quartet and `raw` |
| `data/events/{source_id}/{retrieved_at}.parquet` | change events vs the previous normalised snapshot |
| `data/held/{source_id}/{retrieved_at}.parquet` | output of a run held by a DQ gate — not publishable |
| `data/runs/{source_id}/{retrieved_at}.json` | the `source_run` record (`docs/21` §4.2): counts, duration, status, DQ results |
| `data/quarantine/…` | the same tree for `reuse: restricted \| unknown` sources |

Everything except `data/eval/` and `data/probes/` is regenerable and git-ignored.

Running a source twice is free: if the payload hash is unchanged the run records `unchanged` and
stops (`docs/20` §3.2). When the payload does change, the run diffs the new normalised snapshot
against the previous one and writes `new / removed / status_change / capacity_change / cod_change /
withdrawn` events.

## Connectors implemented (Sprint 1)

| `source_id` | Kind | What it fetches |
|---|---|---|
| `us.iso.ercot.gen_queue` | proposal | GIS Report xlsx (EMIL PG7-200-ER, reportTypeId 15933) via the MIS document list |
| `us.iso.ercot.large_load_queue` | proposal | EMIL catalogue watch — the Protocol 3.2.7 report is not published yet (see the registry note) |
| `us.iso.caiso.gen_queue` | proposal | Public Queue Report xlsx |
| `us.iso.nyiso.gen_queue` | proposal | Interconnection Queue workbook |
| `us.eia.860m` | proposal | EIA-860M "Planned" sheet, link scraped from the index page |
| `gb.neso.tec_register` | proposal | TEC Register CSV via CKAN `package_show` |
| `us.grants_gov.search2` | opportunity | Search2 API, keyword `energy`, forecast + posted |
| `eu.ted.api` | opportunity | TED search, energy CPV, rolling 3-day window |
| `gb.find_a_tender` | opportunity | OCDS release packages, energy CPV, rolling 2-day window |
| `mdb.worldbank.procnotices` | opportunity | Procurement notices, `sector.sector_code` energy codes, 14-day window |
| `us.ferc.elibrary` | document | eLibrary `AdvancedSearch` JSON backend, ER/CP docket filings, 30-day window |
| `us.permits_dashboard` | proposal | FAST-41 Permitting Dashboard full milestone CSV, energy/transmission sectors |

SPP, ISO-NE, PJM and MISO are deliberately **not** implemented this sprint (`docs/13` §6).

`us.ferc.elibrary` is the first `kind = "document"` connector (`docs/21` §3.8): a filing, not a
lifecycle entity, so `lifecycle_state`/`capacity_mw`/`proposed_cod` are placeholders that exist
only so it flows through the same DQ-gate/diff machinery as `proposal` and `opportunity` (see
`base.DOCUMENT_COLUMNS` and the connector's own docstring for why).

## Docket linkage

`pipeline/link_dockets.py` proposes links between `us.ferc.elibrary` documents and
`us.iso.*.gen_queue` records (explicit queue-id/name match, and a sponsor-vs-filer rapidfuzz
match), writing `data/eval/docket_links.parquet`. It is the first measurement of the "FERC docket
linkage" resolution key `docs/22-entity-resolution-and-change-detection.md` §10 proposes adding to
the M-1 recalibration — read that script's module docstring before changing the matching rules,
several of them (stripping "INTERCONNECTION", stripping US state names) exist because an earlier,
looser version measurably false-matched on shared boilerplate words:

```
.venv/bin/python pipeline/link_dockets.py [--threshold 85] [--out data/eval/docket_links.parquet]
```

## Writing a connector

Create `pipeline/connectors/<source_id with dots as underscores>/connector.py` exposing a class
named `Connector` that subclasses `pipeline.connectors.base.Connector`:

```python
class Connector(BaseConnector):
    source_id: ClassVar[str] = "us.example.thing"
    kind: ClassVar[Kind] = "proposal"  # or "opportunity"
    ext: ClassVar[str] = "csv"

    def fetch(self) -> RawSnapshot: ...  # network; never runs in CI
    def parse(self, raw) -> list[dict]: ...  # source-shaped rows; no network
    def normalize(self, rows, raw) -> DataFrame:
        return self.finalize(df, rows, raw)  # stamps provenance, record_id and `raw`
```

`finalize()` supplies `source_id`, `source_url`, `retrieved_at`, `licence_id`, `record_id` and the
`raw` payload on every row, so a connector only maps source columns to canonical ones. Status
harmonisation is data, not code: put a `status_map.yaml` next to the connector (`docs/04` DA-5) and
point `status_map_path` at it, or reuse `pipeline/status_map.yaml` for the ISO queues. Document the
`source_record_id` strategy in the module docstring (`docs/20` §3.1). Add a trimmed fixture under
`tests/fixtures/` (see its README) and a `test_connector.py` beside the connector.

## Gating

`reuse: restricted` or `unknown` in `data/sources.yaml` means the connector refuses to run:

```
$ python -m pipeline.connectors run us.iso.pjm.gen_queue
{"level": "error", "event": "gate refused", "source_id": "us.iso.pjm.gen_queue", ...}
```

`--allow-restricted` is the only way past, and it routes every output into `data/quarantine/`; the
store refuses to write a publishable path from a non-publishable store, so gated rows cannot reach
`data/normalized` or `data/events` even by accident (`docs/21` §8). Private aggregators
(`category: aggregator` + `reuse: restricted`) fail to register at all (`docs/20` §4.3).

## Data-quality gates

Every run ends in the `docs/04` DA-6 gates, recorded on the `source_run`: row-count drift vs the
previous successful run (warn ±10 %, hold >30 %), status vocabulary drift (warn on any new or
unmapped value, hold above 5 % unmapped), null spikes on required fields (warn +5 pp, hold +10 pp
vs the trailing median of five runs), duplicate `record_id` (hold), provenance completeness (hold)
and source schema drift (warn; hold when a column feeding a canonical field disappears). A held run
keeps its snapshot, writes its records to `data/held/`, emits no events and records why.

## Politeness

One browser-like User-Agent that names the platform and a contact URL (`{{DOMAIN}}` until the owner
names the product; set `BANKABLE_DOMAIN` to replace it). Per-host token bucket from the registry
(`rate_limit_rps`, a limit quoted in the entry's notes, or the `docs/02` §7 defaults — ERCOT and
FERC 0.5 rps, GDELT one per 5 s, PJM 6/min). Retries with exponential backoff and jitter on 5xx,
429 and connection errors, honouring `Retry-After`. `robots.txt` is honoured for site fetches; JSON
APIs opt out explicitly. A challenge page is reported as `blocked` — never bypassed.

## Prototype stages

```
.venv/bin/python pipeline/normalize.py            # -> data/eval/normalized.parquet (evaluation set)
.venv/bin/python pipeline/resolve.py --sweep      # -> data/eval/matches.parquet, clusters.parquet
.venv/bin/python pipeline/diff.py --demo          # -> data/eval/events.parquet from a seeded perturbation
```

| File | What it does | Knobs |
|---|---|---|
| `connectors/` | fetch → snapshot → parse → normalise → DQ → diff → store, per source | `--all`, `--allow-restricted`, `--data-dir` |
| `status_map.yaml` | ISO raw status → canonical lifecycle state, refine rules, caveats (data, not code) | edit and re-run |
| `normalize.py` | canonical proposal columns: kind, technology, capacity, state/county, sponsor, lifecycle_state, dates, keys | `--date`, `--out` |
| `resolve.py` | D1 EIA id → D2 queue id+ISO → D3 cited queue id → blocks → fuzzy score → clusters | `--threshold 75`, `--sweep`, `--labels` |
| `diff.py` | two normalised snapshots → change events | `--before/--after`, `--demo --seed` |

The evaluation-set loader that `normalize.py` reads (`data/eval/raw/*.parquet`) was produced by the
Phase 2 `pull.py`, which the connector framework replaces; re-create those inputs with
`python -m pipeline.connectors run --all`.

## Tests and gates

```
.venv/bin/python -m pytest                # 208 tests: parsers, gating, DQ, politeness, ERCOT end-to-end
.venv/bin/ruff check . && .venv/bin/ruff format --check pipeline tests conftest.py
.venv/bin/mypy
```

`parse()` and `normalize()` run against recorded fixtures only; `fetch()` never runs in CI.
