"""Load connector output into a SQLite `services.db` store for `web/` to read through the API,
via the real `services/ingest/loader.py` path -- this module never invents its own record shape.

Two entry points:
  - `load_dev_database()` -- the nine real per-source `data/normalized/<source_id>/*.parquet`
    files this site has always served (docs/00-PLAN.md Sprint 2 prototype), for `web/dev_up.py`.
  - `load_test_database()` -- `data/eval/normalized.parquet` (task item 6's named fixture) for
    proposals, plus the same real opportunity sources, for the pytest suite.

Both apply three small, explicitly-documented corrections *on top of* what the loader wrote,
because three known gaps in `services/ingest/loader.py` (recorded in `services/README.md`'s "Open
decisions" and repeated below) would otherwise leave the site with nothing to show or with
defect-C/D-9 behaviour that can never trigger. None of this edits `services/` code -- it is a
data-loading-layer workaround, applied from the frontend's own loader, pending the real fix
upstream (see web/README.md "Missing from the API" for what to ask the backend team for).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from services.db.models import (
    Event,
    Location,
    Opportunity,
    OpportunitySource,
    Proposal,
    ProposalSource,
    Source,
)
from services.ingest.loader import GateRefused, load_dataframe, load_from_files, upsert_licence_and_source
from web.build_data import (
    COUNTY_CENTROID_TSV,
    EVAL_SHORT_ID_MAP,
    OPPORTUNITY_SOURCE_IDS,
    PROPOSAL_SOURCE_IDS,
    CountyGazetteer,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = REPO_ROOT / "data"
DEFAULT_SOURCES_YAML = REPO_ROOT / "data" / "sources.yaml"
DEFAULT_EVAL_PARQUET = REPO_ROOT / "data" / "eval" / "normalized.parquet"

# services/README.md open decision #11: "allows_raw_publication is True for every ingested source
# this sprint ... needs the per-source override wired from the registry". CAISO and NYISO are the
# two sources the legal register (docs/00-PLAN.md, 2026-09-12) calls derived-only.
DERIVED_ONLY_SOURCE_IDS: tuple[str, ...] = ("us.iso.caiso.gen_queue", "us.iso.nyiso.gen_queue")


def _flip_publish_state_public(session: Session, source_id: str) -> None:
    """services/README.md open decision #5: a newly-seen source loads as `publish_state =
    "api_only"` until an admin publish workflow flips it -- which does not exist yet. Every source
    this module loads has already cleared `data/sources.yaml`'s open/attribution gate (the loader
    itself would have refused it otherwise), so flipping it here is the same call `build_data.py`'s
    prototype made implicitly by construction, just made explicit now that a real `publish_state`
    column exists to set.
    """
    source = session.get(Source, source_id)
    if source is not None and source.publish_state != "public":
        source.publish_state = "public"
        session.flush()


def load_real_normalized_sources(
    session: Session,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    sources_yaml: Path = DEFAULT_SOURCES_YAML,
    source_ids: Iterable[str] | None = None,
) -> dict[str, str]:
    """Load the latest per-source parquet under `data_root/normalized/<source_id>/` for each id in
    `source_ids` (default: the five proposal + four opportunity sources this site has always
    served) via `services.ingest.loader.load_from_files` -- the same function a real scheduled
    ingestion run would call. Returns `{source_id: "loaded" | "missing" | "skipped: <reason>"}`.
    """
    registry = Registry(sources_yaml)
    ids = list(source_ids) if source_ids is not None else [*PROPOSAL_SOURCE_IDS, *OPPORTUNITY_SOURCE_IDS]
    status: dict[str, str] = {}
    for source_id in ids:
        source_dir = data_root / "normalized" / source_id
        files = sorted(source_dir.glob("*.parquet")) if source_dir.is_dir() else []
        if not files:
            status[source_id] = "missing"
            continue
        ts = files[-1].stem
        try:
            load_from_files(session, source_id, ts, data_root=data_root, registry=registry)
        except GateRefused as exc:
            status[source_id] = f"skipped: {exc}"
            continue
        _flip_publish_state_public(session, source_id)
        status[source_id] = "loaded"
    session.commit()
    return status


def load_eval_fixture(
    session: Session,
    *,
    eval_parquet: Path = DEFAULT_EVAL_PARQUET,
    sources_yaml: Path = DEFAULT_SOURCES_YAML,
) -> dict[str, int]:
    """Load `data/eval/normalized.parquet` (proposals only) into `session`, restricted to
    `web.build_data.EVAL_SHORT_ID_MAP` -- SPP and ISO-NE rows are dropped, never remapped, per
    CLAUDE.md's guardrail and the legal register. Returns `{source_id: rows_created}`.
    """
    df = pd.read_parquet(eval_parquet)
    registry = Registry(sources_yaml)
    counts: dict[str, int] = {}
    for short_id, source_id in EVAL_SHORT_ID_MAP.items():
        frame = df[df["source_id"] == short_id].copy()
        if frame.empty:
            continue
        entry = registry.get(source_id)
        source = upsert_licence_and_source(session, entry, registry.version)
        _flip_publish_state_public(session, source_id)
        result = load_dataframe(session, source, "proposal", frame, None)
        counts[source_id] = result.proposals_created
    session.commit()
    return counts


def apply_derived_only_licence_correction(
    session: Session, source_ids: Iterable[str] = DERIVED_ONLY_SOURCE_IDS
) -> int:
    """Correct the loader's hardcoded `allows_raw_publication=True` (open decision #11 above) for
    sources the legal register calls derived-only, and stamp their already-loaded `location` rows
    with `precision_reason="licence"` so `serialize_location`'s stored column (which the loader
    never sets for any source, not only these two -- another instance of the same gap) has real
    data for the frontend's restricted-precision note (docs/04 D-9) to render. Returns the number
    of `location` rows stamped.
    """
    updated = 0
    for source_id in source_ids:
        source = session.get(Source, source_id)
        if source is None:
            continue
        licence = source.licence
        if licence.allows_raw_publication:
            licence.allows_raw_publication = False
            licence.allows_derived_publication = True
        for loc in session.scalars(select(Location).where(Location.source_id == source_id)):
            if loc.precision == "county_centroid":
                loc.precision_reason = "licence"
                updated += 1
    session.commit()
    return updated


def backfill_locations(session: Session, *, gaz: CountyGazetteer | None = None) -> int:
    """`services/ingest/loader.py` never geocodes (README open decision #2: `location.geom` is
    always null), so nothing plots on the map at all without this. Uses the same public-domain US
    Census Gazetteer county-centroid table `web/build_data.py`'s prototype vendored -- a frontend
    stopgap, not a substitute for a real geocoder in the ingest pipeline (see web/README.md).
    """
    gaz = gaz or CountyGazetteer.load(COUNTY_CENTROID_TSV)
    updated = 0
    for loc in session.scalars(select(Location).where(Location.geom.is_(None))):
        if loc.country != "US":
            continue
        state = (loc.state_code or "").rsplit("-", 1)[-1] or None
        point = gaz.county_point(state, loc.county_name) if loc.county_name and state else None
        if point is None and state:
            point = gaz.state_point(state)
            if point is not None:
                loc.precision = "state_centroid"
        if point is not None:
            lat, lon = point
            loc.geom = (lon, lat)
            updated += 1
    session.commit()
    return updated


def backfill_eia_exact_points(session: Session, *, source_id: str = "us.eia.860m") -> int:
    """EIA-860M's `raw` JSON (stored on `proposal_source.raw` by the loader) carries an exact
    `Latitude`/`Longitude` pair the loader does not project onto `location.geom`. Promote it here
    so EIA-860M keeps the `exact` placement precedence (docs/04 D-8) `build_data.py`'s prototype
    gave it, instead of falling back to a county centroid like every other source.
    """
    updated = 0
    rows = session.scalars(select(ProposalSource).where(ProposalSource.source_id == source_id))
    for row in rows:
        raw: dict[str, Any] = row.raw or {}
        lat, lon = raw.get("Latitude"), raw.get("Longitude")
        if not isinstance(lat, int | float) or not isinstance(lon, int | float):
            continue
        proposal = session.get(Proposal, row.proposal_id)
        if proposal is None or proposal.location is None:
            continue
        proposal.location.geom = (float(lon), float(lat))
        proposal.location.precision = "exact"
        updated += 1
    session.commit()
    return updated


def apply_preview_lag_override(
    session: Session, *, source_ids: Iterable[str] | None = None
) -> int:
    """Dev/test-only: pull `public_at` back to "now" for rows whose real `public_at` (computed by
    the loader as `published_at + lag_days`, `services/ingest/lag.py`) is still in the future,
    because they were just ingested. Docs/00-PLAN.md task item 5: "add a dev-only override flag so
    today's rows can be previewed, clearly labelled in the UI when active" -- `web/app.py` renders
    that label whenever this has run (`app.state.preview_active`), separate from the always-on
    delayed-tier notice, which keeps stating the real configured lag regardless.

    This does not touch `services/api/visibility.py`'s predicate -- it changes the stored value
    the predicate reads, exactly as a record would look once it had genuinely aged past its lag.
    """
    now = dt.datetime.now(dt.UTC)
    grace = now - dt.timedelta(seconds=1)
    updated = 0

    proposal_stmt = select(Proposal)
    opportunity_stmt = select(Opportunity)
    if source_ids is not None:
        ids = list(source_ids)
        proposal_stmt = proposal_stmt.join(
            ProposalSource, ProposalSource.proposal_id == Proposal.id
        ).where(ProposalSource.source_id.in_(ids))
        opportunity_stmt = opportunity_stmt.join(
            OpportunitySource, OpportunitySource.opportunity_id == Opportunity.id
        ).where(OpportunitySource.source_id.in_(ids))

    for proposal in session.scalars(proposal_stmt):
        if proposal.public_at is not None and proposal.public_at > now:
            proposal.public_at = grace
            updated += 1
    for opportunity in session.scalars(opportunity_stmt):
        if opportunity.public_at is not None and opportunity.public_at > now:
            opportunity.public_at = grace
            updated += 1
    for event in session.scalars(select(Event)):
        if event.public_at is not None and event.public_at > now:
            event.public_at = grace
            updated += 1
    session.commit()
    return updated


def load_dev_database(
    session: Session,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    sources_yaml: Path = DEFAULT_SOURCES_YAML,
    preview: bool = False,
) -> dict[str, Any]:
    """Everything `web/dev_up.py` needs: the nine real sources, the two data-layer corrections
    above, and (only with `preview=True`) the lag override."""
    status = load_real_normalized_sources(session, data_root=data_root, sources_yaml=sources_yaml)
    licence_rows_stamped = apply_derived_only_licence_correction(session)
    locations_backfilled = backfill_locations(session)
    eia_exact_points = backfill_eia_exact_points(session)
    preview_rows_advanced = apply_preview_lag_override(session) if preview else 0
    return {
        "sources": status,
        "licence_rows_stamped": licence_rows_stamped,
        "locations_backfilled": locations_backfilled,
        "eia_exact_points": eia_exact_points,
        "preview_rows_advanced": preview_rows_advanced,
    }


def load_test_database(
    session: Session,
    *,
    eval_parquet: Path = DEFAULT_EVAL_PARQUET,
    sources_yaml: Path = DEFAULT_SOURCES_YAML,
    data_root: Path = DEFAULT_DATA_ROOT,
    include_opportunities: bool = True,
) -> dict[str, Any]:
    """What `tests/test_web_*.py` calls: `data/eval/normalized.parquet` for proposals (task item
    6's named fixture) plus the real opportunity sources for provenance/detail-page coverage, both
    corrected the same way `load_dev_database` is, and always with the preview override applied
    (the eval fixture's `retrieved_at` is "today", so nothing would be visible otherwise).
    """
    proposal_counts = load_eval_fixture(session, eval_parquet=eval_parquet, sources_yaml=sources_yaml)
    opportunity_status = (
        load_real_normalized_sources(
            session,
            data_root=data_root,
            sources_yaml=sources_yaml,
            source_ids=OPPORTUNITY_SOURCE_IDS,
        )
        if include_opportunities
        else {}
    )
    apply_derived_only_licence_correction(session)
    backfill_locations(session)
    apply_preview_lag_override(session)
    return {"proposals": proposal_counts, "opportunities": opportunity_status}
