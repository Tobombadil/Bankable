"""Load the context layers from a data root's `normalized/context/` (and the EIA-860M retirements
frame) into the store: the existing plants, the midstream and fuels asset layers with their operator
edges, owner shares, asset features and the organisation graph.

Two callers, one path: `web/dev_up.py` when it builds a local store, and the scheduler's
`context_load` job (`infra/scheduler/app.py`) on a deployed one, which runs after the monthly
`context_build` has rebuilt the files (docs/64 §7). It lives here rather than in `web/` because
the worker image carries `services` and `pipeline` but not `web`.

Order (`load_context_layers`): the EIA-860M plants first, then the retirements onto them, then the
asset files with their operator edges, the ethanol plants (merged across two registries), the
EIA-860 owner shares, the GHGRP shares, the derived features, the curated parents and last the
organisation graph, because each step reads what the steps before it created. Every input is
optional: a file that is absent is one log line, never a failure of the steps after it, so a data
root holding only some layers loads those. Every load is idempotent, so a repeat changes nothing.
Writes no proposal-opportunity match: the matcher is `services.match.run`, which the scheduler runs
after each resolve pass (`match_tick`) and which `services.ingest` may not import (it reads the
API's visibility rules; infra/importlinter.ini).
"""

from __future__ import annotations

import logging
import pathlib
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from services.ingest import organizations
from services.ingest.assets import UnsupportedAssetTypeError, load_assets_parquet, load_ethanol_plants
from services.ingest.enrich import apply_context_features
from services.ingest.ghgrp import load_ghgrp_parquet
from services.ingest.loader import GateRefused, load_from_files
from services.ingest.midstream import load_operator_edges, load_operator_edges_parquet, load_parents
from services.ingest.ownership import load_owner_shares_parquet
from services.ingest.plants import load_plants_parquet
from services.ingest.retirements import SOURCE_ID as RETIREMENTS_SOURCE_ID

log = logging.getLogger(__name__)

PLANTS_FILE = "us.eia.860m.plants.parquet"
OWNERS_FILE = "us.eia.860.owners.parquet"
GHGRP_FILE = "us.epa.ghgrp.parquet"
GLEIF_PARENTS_FILE = "global.gleif.lei.parents.parquet"

#: `normalized/context/<file>` -> `asset_type` for the midstream and fuels context layers (owner
#: option (a), 2026-09-19; file names and types as the data lanes landed them, coordinator note
#: 2026-09-19). Two files may feed one type (RNG from EPA LMOP and AgSTAR); the `*.proposals.parquet`
#: siblings of the EPA files hold planned rows that are proposals, not assets, and are not listed
#: here. `ethanol_plant` is loaded separately (`_load_ethanol_plants`, docs/24 §5(a)): its two files
#: are resolved to one asset per plant, not one row per file.
CONTEXT_ASSET_FILES: tuple[tuple[str, str], ...] = (
    ("us.eia.atlas.gas_pipelines.parquet", "gas_pipeline"),
    ("us.eia.atlas.gas_processing_plants.parquet", "gas_processing_plant"),
    ("us.eia.atlas.gas_storage.parquet", "gas_storage"),
    ("us.eia.atlas.lng_terminals.parquet", "lng_terminal"),
    ("us.epa.lmop.parquet", "rng_project"),
    ("us.epa.agstar.parquet", "rng_project"),
    # Grid lane G2, 2026-09-28: LBNL's FERC x HIFLD transmission lines (CC BY 4.0), one row per line.
    # The edge step writes nothing for it: the owner rides in `attributes` until LBNL's owner
    # strings have a reviewed alias table (pipeline/context/lbnl_transmission.py docstring).
    ("us.lbnl.ferc_hifld_transmission_lines.parquet", "transmission_line"),
)

ETHANOL_ATLAS_FILE = "us.eia.atlas.ethanol_plants.parquet"
ETHANOL_CAPACITY_FILE = "us.eia.ethanol_capacity.parquet"


def context_dir(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "normalized" / "context"


def expected_files() -> list[str]:
    """Every `normalized/context/` file a load reads directly, for the job's report. The feature
    inputs (`services.ingest.enrich`) are read and reported by that step itself."""
    return [
        PLANTS_FILE,
        *(name for name, _ in CONTEXT_ASSET_FILES),
        ETHANOL_ATLAS_FILE,
        ETHANOL_CAPACITY_FILE,
        OWNERS_FILE,
        GHGRP_FILE,
        GLEIF_PARENTS_FILE,
    ]


def load_plants_layer(session: Session, data_dir: pathlib.Path) -> None:
    """The existing-plants context layer (docs/00-PLAN.md 2026-09-14/15 owner decision), through the
    plants ingest lane's own `load_plants_parquet(session, path)`."""
    parquet_path = context_dir(data_dir) / PLANTS_FILE
    if not parquet_path.exists():
        log.info("plants context layer: %s not found, skipping", parquet_path)
        return
    report = load_plants_parquet(session, parquet_path)
    log.info("plants context layer: loaded from %s (%s)", parquet_path, report)


def load_retirements(
    session: Session, data_dir: pathlib.Path, sources_yaml: pathlib.Path | None = None
) -> None:
    """Generator retirements (lane R1, docs/27 §R1) onto the `power_plant` assets `load_plants_layer`
    just loaded, through the scheduler's own path: `services.ingest.loader.load_from_files`, which
    routes `us.eia.860m.retirements` to `services/ingest/retirements.py` (`SPECIALISED_LOADERS`).
    That loader updates each plant's `status`, `retirement_year` and `attributes.retirement` and
    writes retirement events; it never creates a proposal or an asset, so it must run after the
    plants layer. The scheduler's `load_source` also loads each new retirements run as it is
    fetched, but a run loaded before the plants layer existed matched no asset; reloading the
    latest run here, after the plants, is idempotent and closes that gap. A first load (no previous
    run to diff against) writes no events by construction. The connector's latest frame missing, or
    the source gated in `sources.yaml`, is one log line."""
    files = sorted((data_dir / "normalized" / RETIREMENTS_SOURCE_ID).glob("*.parquet"))
    if not files:
        log.info(
            "retirements: no %s frame under %s, skipping", RETIREMENTS_SOURCE_ID, data_dir / "normalized"
        )
        return
    registry = Registry(sources_yaml) if sources_yaml is not None else Registry()
    try:
        result = load_from_files(
            session, RETIREMENTS_SOURCE_ID, files[-1].stem, data_root=data_dir, registry=registry
        )
    except GateRefused as exc:
        log.info("retirements: %s refused (%s), skipping", RETIREMENTS_SOURCE_ID, exc)
        return
    log.info("retirements: loaded %s (%s)", files[-1].name, result)


def load_ownership(session: Session, data_dir: pathlib.Path) -> None:
    """ADR 0008 task item 4: the EIA-860 Schedule 4 ownership shares, through the ownership lane's
    own `load_owner_shares_parquet(session, path)`. They join onto the `power_plant` rows, so this
    runs after the plants and after every other asset and edge load."""
    parquet_path = context_dir(data_dir) / OWNERS_FILE
    if not parquet_path.exists():
        log.info("asset ownership: %s not found, skipping", parquet_path)
        return
    report = load_owner_shares_parquet(session, parquet_path)
    # `as_report()` where it exists: its unmatched-plant list is a sample, so this stays one line.
    summary = report.as_report() if hasattr(report, "as_report") else report
    log.info("asset ownership: loaded from %s (%s)", parquet_path, summary)


def load_ghgrp(session: Session, data_dir: pathlib.Path) -> None:
    """docs/02 §12: EPA GHGRP parent-company shares as `asset_owner` edges (`as_of` = 31 December of
    the reporting year) and `asset.attributes["ghgrp"]` on assets the other loaders already created,
    through the GHGRP lane's own `load_ghgrp_parquet(session, path)`, which never inserts an `asset`
    row (docs/24). Runs after the EIA-860 owner shares (both write owner edges; GHGRP's are the
    dated ones) and before the features pass, so anything derived from ownership sees these too."""
    parquet_path = context_dir(data_dir) / GHGRP_FILE
    if not parquet_path.exists():
        log.info("ghgrp ownership: %s not found, skipping", parquet_path)
        return
    result, _matches = load_ghgrp_parquet(session, parquet_path)
    summary = result.as_report() if hasattr(result, "as_report") else result
    log.info("ghgrp ownership: loaded from %s (%s)", parquet_path, summary)


def apply_features(session: Session, data_root: pathlib.Path) -> None:
    """The enrichment lane's `apply_context_features(session, data_root)` (derived asset features
    such as pipeline mileage, capacity factors and fuel pathways), once, after every asset, edge and
    owner-share load and before the curated parent links. A missing input is one log line."""
    try:
        report = apply_context_features(session, data_root)
    except FileNotFoundError as exc:
        log.info("context features: input not found (%s), skipping", exc)
        return
    log.info("context features: applied (%s)", report)


def load_organization_graph(session: Session, data_dir: pathlib.Path) -> None:
    """The ownership graph's organisation layer (docs/22 §17), after every asset, edge, owner-share
    and curated-parent load because it reads what they created: GLEIF Level 2 parent links, then the
    curated alias file, then the curated merge file. A missing parquet or an unreadable YAML is one
    log line each. GLEIF runs after the curated parents and wins where it has a record;
    `services/ingest/midstream.py::load_parents` defers to it on a later re-run, so the order here
    is a preference, not a correctness requirement."""
    parquet_path = context_dir(data_dir) / GLEIF_PARENTS_FILE
    if not parquet_path.exists():
        log.info("organisation graph: %s not found, skipping GLEIF parents", parquet_path)
    else:
        report = organizations.load_gleif_parents_parquet(session, parquet_path)
        log.info("organisation graph: GLEIF parents from %s (%s)", parquet_path, report.as_report())
    try:
        aliases = organizations.load_aliases(session)
    except (FileNotFoundError, ValueError) as exc:
        log.info("organisation graph: curated alias file unusable (%s), skipping", exc)
    else:
        log.info("organisation graph: curated aliases applied (%s)", aliases.as_report())
    # Curated merges run last: they read the rows every loader above created, and a merge is the
    # decision the alias loader refuses to make (its `conflicts`). docs/22 §20.5.
    try:
        merges = organizations.load_merges(session)
    except (FileNotFoundError, ValueError) as exc:
        log.info("organisation graph: curated merge file unusable (%s), skipping", exc)
    else:
        log.info("organisation graph: curated merges applied (%s)", merges.as_report())


def _load_ethanol_plants(session: Session, data_dir: pathlib.Path) -> bool:
    """`ethanol_plant`: one asset per real plant, not one row per registry (docs/24 §5(a), measured
    48.2% cross-source duplication with no resolution). Both files are read together through
    `services.ingest.assets.load_ethanol_plants`; either file missing is one log line.

    **Operator edges** (coordinator correction, 2026-09-26, docs/24 §7): `load_ethanol_plants`
    itself writes the `operator` edge for every merged asset, from the capacity report's current
    name where it states one (docs/24 §7.1: a merged asset's operator must be current, not stale by
    construction because Atlas happens to be the primary source). The generic per-file loader
    (`services.ingest.midstream.load_operator_edges`) is still the right tool for the two remaining
    cases, the Atlas-only and capacity-only singles, so it still runs, but:

    - against the **full** Atlas file, because every merged asset also keeps the Atlas source's own
      `(source_id, source_asset_id)` identity (docs/24 §5(a): Atlas is primary), so the Atlas edge
      the generic loader writes for a merged row is a second, correctly attributed source view
      beside the capacity-derived one, not a duplicate of it (they differ on `source_id`, and often
      on the organisation too: docs/24 §7.1's stale-owner finding made visible as two edges);
    - against the capacity file **with the merged rows filtered out** first. Without the filter
      nothing is corrupted (`load_operator_edges` filters on `Asset.source_id == source.id`, which
      no merged row matches under the capacity source), but every run would report those ids as
      unmatched; filtering removes that reliance on an incidental property of another module.
      (`services/ingest/test_context_layers.py::
      test_capacity_operator_edges_are_not_requested_for_merged_rows_after_the_fix`.)"""
    directory = context_dir(data_dir)
    atlas_path, capacity_path = directory / ETHANOL_ATLAS_FILE, directory / ETHANOL_CAPACITY_FILE
    if not atlas_path.exists() or not capacity_path.exists():
        log.info(
            "context asset layers: ethanol_plant needs both %s and %s, skipping", atlas_path, capacity_path
        )
        return False
    atlas_df = pd.read_parquet(atlas_path)
    capacity_df = pd.read_parquet(capacity_path)
    report = load_ethanol_plants(session, atlas_df, capacity_df)
    log.info(
        "context asset layers: loaded ethanol_plant from %s + %s (%s)", atlas_path, capacity_path, report
    )
    atlas_edges = load_operator_edges(session, atlas_df, "ethanol_plant")
    log.info("context asset layers: operator edges from %s (%s)", atlas_path, atlas_edges)
    merged = set(report.merged_capacity_source_asset_ids)
    capacity_singles_df = capacity_df[~capacity_df["source_asset_id"].astype(str).isin(merged)]
    capacity_edges = load_operator_edges(session, capacity_singles_df, "ethanol_plant")
    log.info(
        "context asset layers: operator edges from %s, %d merged rows excluded (%s)",
        capacity_path,
        len(merged),
        capacity_edges,
    )
    return True


def load_asset_layers(session: Session, data_dir: pathlib.Path) -> None:
    """The midstream/fuels asset layers through the ingest lanes' own loaders. Per file, in order:
    `load_assets_parquet(session, path, asset_type)` (the rows) then `load_operator_edges_parquet`
    (the `asset_owner` operator edges); then the ethanol plants; then `load_ownership`,
    `load_ghgrp`, `apply_features`, the curated parent links (`load_parents`, over every
    organisation the loads above created) and last `load_organization_graph`. A file that is not
    there, or an `asset_type` the loader has not wired, is one log line with the reason; every
    successful step logs its counts."""
    directory = context_dir(data_dir)
    loaded = 0
    for file_name, asset_type in CONTEXT_ASSET_FILES:
        parquet_path = directory / file_name
        if not parquet_path.exists():
            log.info("context asset layers: %s not found, skipping", parquet_path)
            continue
        try:
            report = load_assets_parquet(session, parquet_path, asset_type)
        except UnsupportedAssetTypeError as exc:
            log.info("context asset layers: %s skipped (%s)", file_name, exc)
            continue
        loaded += 1
        log.info("context asset layers: loaded %s rows from %s (%s)", asset_type, file_name, report)
        edges = load_operator_edges_parquet(session, parquet_path, asset_type)
        log.info("context asset layers: operator edges from %s (%s)", file_name, edges)
    if _load_ethanol_plants(session, data_dir):
        loaded += 1
    load_ownership(session, data_dir)
    load_ghgrp(session, data_dir)
    apply_features(session, data_dir)
    if loaded:
        try:
            parents = load_parents(session)
        except FileNotFoundError as exc:
            log.info("context asset layers: curated parents file not found (%s), skipping", exc)
        else:
            log.info("context asset layers: curated parents applied (%s)", parents)
    load_organization_graph(session, data_dir)
    if not loaded:
        log.info("context asset layers: no midstream/fuels parquet under %s yet, skipping", directory)


def load_context_layers(
    session: Session, data_dir: pathlib.Path, *, sources_yaml: pathlib.Path | None = None
) -> dict[str, Any]:
    """Everything above, in order (module docstring), in the caller's session; the caller commits.
    Returns which of the expected `normalized/context/` files were there to load."""
    directory = context_dir(data_dir)
    present = [name for name in expected_files() if (directory / name).exists()]
    missing = [name for name in expected_files() if name not in present]
    load_plants_layer(session, data_dir)
    load_retirements(session, data_dir, sources_yaml)
    load_asset_layers(session, data_dir)
    return {"data_dir": str(data_dir), "files_loaded": len(present), "files_missing": missing}
