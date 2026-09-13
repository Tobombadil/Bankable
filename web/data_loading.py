"""Load connector output into a SQLite `services.db` store for `web/` to read through the API,
via the real `services/ingest/loader.py` path -- this module never invents its own record shape.

`load_dev_database()` -- the nine real per-source `data/normalized/<source_id>/*.parquet` files
this site has always served (docs/00-PLAN.md Sprint 2 prototype) -- is the one entry point
`web/dev_up.py`, `web/test_e2e.py` and `tests/test_web_default_view.py` all now use, unsampled by
default (the full ~11,400-row set across all nine sources): `services/README.md`'s "Sprint 2
fixes" closed the `services/api/visibility.py` performance gap that used to force this site onto a
sample (0.127s/page and 0.35-0.53s/geo-request on the full load, both measured there), so there is
no longer a reason to test or develop against anything smaller by default. `sample_per_state`
(exposed as `web/dev_up.py --sample`) still caps the load to a handful of rows per lifecycle
state/status per source when that's useful for a fast local edit-reload loop -- unrelated to the
performance fix; `services/ingest/loader.py` upserts row by row (no bulk path) at roughly 100
rows/second regardless of query speed, so a full load still costs a couple of minutes wall clock,
which the sample flag exists to skip when a developer doesn't need the real volume.

`load_eval_fixture()`/`load_test_database()` remain below as a way to load the separate
entity-resolution evaluation fixture (`data/eval/normalized.parquet`, docs/22) into the same store
shape, should something else need that specific fixture; nothing in `web/` or `tests/test_web_*.py`
calls them any more now that the real `data/normalized/*` load is fast enough to test against
directly.

No correction remains on top of what the loader writes -- all four this module used to apply are
gone (see below, and `services/README.md`'s "Sprint 2 fixes" / "EIA exact-point promotion" for the
backend's own verdict on each):

- **Geocoding, CAISO/NYISO's derived-only licence flag and `precision_reason`, and source
  `publish_state`** are now all done correctly by `services/ingest/loader.py` itself (it geocodes
  at ingest time, derives `allows_raw_publication`/`precision_reason` per source from the registry
  rather than hardcoding them, and sets `publish_state` from the registry's reuse class) -- the
  `backfill_locations()`, `apply_derived_only_licence_correction()` and `_flip_publish_state_public()`
  functions that used to patch around those three gaps from this side are removed, not merely
  unused, because re-running them would now be redundant work over a store the loader already got
  right.
- **`backfill_eia_exact_points()` is gone too (Sprint 3).** `services/ingest/loader.py` now
  promotes a row's raw `Latitude`/`Longitude` (EIA-860M today, any other source the same way should
  its raw payload carry the same keys) to `exact` location precision itself, at ingest time,
  respecting the same docs/04 D-9 derived-only gate the county/state path already honoured --
  `services/README.md`'s "EIA exact-point promotion" section has the before/after loader
  measurement. Re-running this function on top would now be a redundant no-op over a store the
  loader already got right, exactly like the other three; `load_dev_database`'s report dropped the
  `eia_exact_points` key along with it (no test asserts that key -- checked
  `tests/test_web_default_view.py`, `web/test_e2e.py`, and every `tests/test_web_*.py`/`web/*.py`
  reference to `load_dev_database`'s return value).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Literal

import pandas as pd
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.connectors.registry import Registry
from services.api.common import ensure_aware
from services.db.models import Event, Opportunity, OpportunitySource, Proposal, ProposalSource
from services.ingest.loader import GateRefused, load_dataframe, load_from_files, upsert_licence_and_source
from web.build_data import EVAL_SHORT_ID_MAP, OPPORTUNITY_SOURCE_IDS, PROPOSAL_SOURCE_IDS

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATA_ROOT = REPO_ROOT / "data"
DEFAULT_SOURCES_YAML = REPO_ROOT / "data" / "sources.yaml"
DEFAULT_EVAL_PARQUET = REPO_ROOT / "data" / "eval" / "normalized.parquet"


def load_real_normalized_sources(
    session: Session,
    *,
    data_root: Path = DEFAULT_DATA_ROOT,
    sources_yaml: Path = DEFAULT_SOURCES_YAML,
    source_ids: Iterable[str] | None = None,
    sample_per_state: int | None = None,
) -> dict[str, str]:
    """Load the latest per-source parquet under `data_root/normalized/<source_id>/` for each id in
    `source_ids` (default: the five proposal + four opportunity sources this site has always
    served) via `services.ingest.loader.load_from_files` -- the same function a real scheduled
    ingestion run would call. Returns `{source_id: "loaded" | "missing" | "skipped: <reason>"}`.

    `sample_per_state`, when given, loads only `_stratified_sample`'s per-lifecycle-state/status
    cap instead of the whole file (bypassing `load_from_files` to call
    `services.ingest.loader.load_dataframe` directly on the sampled frame -- the same upsert
    function either way, just skipping the whole-file read). Not a performance workaround any more
    (`services/README.md` "Sprint 2 fixes" closed the query-time gap this used to exist for) --
    kept purely as `web/dev_up.py --sample`'s fast local-iteration path, since
    `services/ingest/loader.py`'s row-by-row upsert (no bulk path) stays ~100 rows/second
    regardless of query speed, so a full ~11,400-row load still costs a couple of minutes.
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
        path = files[-1]
        try:
            if sample_per_state is None:
                load_from_files(session, source_id, path.stem, data_root=data_root, registry=registry)
            else:
                _load_sampled_parquet(session, source_id, path, registry, sample_per_state)
        except GateRefused as exc:
            status[source_id] = f"skipped: {exc}"
            continue
        status[source_id] = "loaded"
    session.commit()
    return status


def _load_sampled_parquet(
    session: Session, source_id: str, path: Path, registry: Registry, sample_per_state: int
) -> None:
    frame = pd.read_parquet(path)
    state_column = "lifecycle_state" if "lifecycle_state" in frame.columns else "status"
    is_proposal = state_column == "lifecycle_state"
    kind: Literal["proposal", "opportunity"] = "proposal" if is_proposal else "opportunity"
    sampled = _stratified_sample(frame, per_state=sample_per_state, column=state_column)
    entry = registry.get(source_id)
    source = upsert_licence_and_source(session, entry, registry.version)
    load_dataframe(session, source, kind, sampled, None)


def _stratified_sample(
    frame: pd.DataFrame, *, per_state: int, column: str = "lifecycle_state"
) -> pd.DataFrame:
    """Up to `per_state` rows per distinct value of `column`, so a capped sample still exercises
    every state actually present (in particular: both active and withdrawn/cancelled rows for the
    product defect A tests) rather than whatever a plain `.head()` happens to contain."""
    if frame.empty:
        return frame
    groups = [group.head(per_state) for _, group in frame.groupby(column, sort=False)]
    return pd.concat(groups, ignore_index=True)


def load_eval_fixture(
    session: Session,
    *,
    eval_parquet: Path = DEFAULT_EVAL_PARQUET,
    sources_yaml: Path = DEFAULT_SOURCES_YAML,
    sample_per_state: int | None = None,
) -> dict[str, int]:
    """Load `data/eval/normalized.parquet` (proposals only) into `session`, restricted to
    `web.build_data.EVAL_SHORT_ID_MAP` -- SPP and ISO-NE rows are dropped, never remapped, per
    CLAUDE.md's guardrail and the legal register. Returns `{source_id: rows_created}`.

    `sample_per_state`, when given, caps each source to `_stratified_sample`'s per-lifecycle-state
    sample instead of the full ~9,500-row set -- `services/ingest/loader.py` upserts row by row
    (no bulk path; each row is several flushes, docs/21 §6.1's idempotency design), which measures
    at roughly 100 rows/second in this environment, so loading the full fixture costs about 90s.
    The pytest suite (tests/test_web_default_view.py) passes a small cap to stay fast; a full,
    unsampled load is still one call away for anything that needs the real volume.
    """
    df = pd.read_parquet(eval_parquet)
    registry = Registry(sources_yaml)
    counts: dict[str, int] = {}
    for short_id, source_id in EVAL_SHORT_ID_MAP.items():
        frame = df[df["source_id"] == short_id].copy()
        if frame.empty:
            continue
        if sample_per_state is not None:
            frame = _stratified_sample(frame, per_state=sample_per_state)
        entry = registry.get(source_id)
        source = upsert_licence_and_source(session, entry, registry.version)
        result = load_dataframe(session, source, "proposal", frame, None)
        counts[source_id] = result.proposals_created
    session.commit()
    return counts


# `apply_derived_only_licence_correction()` and `backfill_locations()` used to live here. Both are
# removed, not merely unused: `services/ingest/loader.py` now derives `allows_raw_publication`/
# `location.precision_reason` per source from the registry and geocodes county/state centroids at
# ingest time (`services/README.md` "Sprint 2 fixes" #2 and #3), so re-running either correction on
# top would be redundant work over a store the loader already got right -- confirmed there as an
# idempotent no-op against this sprint's loader before the fix landed.


def apply_preview_lag_override(session: Session, *, source_ids: Iterable[str] | None = None) -> int:
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
        proposal_stmt = proposal_stmt.join(ProposalSource, ProposalSource.proposal_id == Proposal.id).where(
            ProposalSource.source_id.in_(ids)
        )
        opportunity_stmt = opportunity_stmt.join(
            OpportunitySource, OpportunitySource.opportunity_id == Opportunity.id
        ).where(OpportunitySource.source_id.in_(ids))

    for proposal in session.scalars(proposal_stmt):
        if proposal.public_at is not None and ensure_aware(proposal.public_at) > now:
            proposal.public_at = grace
            updated += 1
    for opportunity in session.scalars(opportunity_stmt):
        if opportunity.public_at is not None and ensure_aware(opportunity.public_at) > now:
            opportunity.public_at = grace
            updated += 1
    for event in session.scalars(select(Event)):
        if event.public_at is not None and ensure_aware(event.public_at) > now:
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
    sample_per_state: int | None = None,
) -> dict[str, Any]:
    """Everything `web/dev_up.py`, `web/test_e2e.py` and `tests/test_web_default_view.py` need: the
    nine real sources under `data/normalized/*` through the real loader (which now geocodes,
    promotes EIA-860M's exact points, and derives licence/publish-state correctly on its own, per
    the module docstring above), and (only with `preview=True`) the lag override. No frontend-side
    correction remains on top (Sprint 3 removed the last one, `backfill_eia_exact_points`).

    `sample_per_state` defaults to `None` -- the full, real ~11,400-row set across all nine
    sources, viable now that `services/api/visibility.py`'s query-time gap is fixed
    (`services/README.md` "Sprint 2 fixes"). Pass a small cap (`web/dev_up.py --sample`) only for a
    fast local edit-reload loop; the loader's own row-by-row upsert rate (~100 rows/second,
    independent of that fix) is what a full load still costs time on.
    """
    status = load_real_normalized_sources(
        session, data_root=data_root, sources_yaml=sources_yaml, sample_per_state=sample_per_state
    )
    preview_rows_advanced = apply_preview_lag_override(session) if preview else 0
    return {
        "sources": status,
        "preview_rows_advanced": preview_rows_advanced,
    }


def load_test_database(
    session: Session,
    *,
    eval_parquet: Path = DEFAULT_EVAL_PARQUET,
    sources_yaml: Path = DEFAULT_SOURCES_YAML,
    data_root: Path = DEFAULT_DATA_ROOT,
    include_opportunities: bool = True,
    sample_per_state: int | None = 60,
) -> dict[str, Any]:
    """Loads the separate entity-resolution evaluation fixture (`data/eval/normalized.parquet`,
    docs/22) for proposals, plus the real opportunity sources, into `session` -- available for
    whatever else wants that specific fixture, but **not called by anything in `web/` or
    `tests/test_web_*.py` any more**: `tests/test_web_default_view.py` now loads the real
    `data/normalized/*` data through `load_dev_database` directly, the same full-data path
    `web/dev_up.py` and `web/test_e2e.py` use, since that data is now fast enough to test against
    (see the module docstring above).

    `sample_per_state` defaults to a small per-lifecycle-state cap (see `load_eval_fixture`) so a
    caller that does want this fixture can stay fast; pass `None` for an unsampled load of the full
    ~9,500-row fixture (still ~90s through `services/ingest/loader.py`'s row-at-a-time upsert).
    """
    proposal_counts = load_eval_fixture(
        session,
        eval_parquet=eval_parquet,
        sources_yaml=sources_yaml,
        sample_per_state=sample_per_state,
    )
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
    apply_preview_lag_override(session)
    return {"proposals": proposal_counts, "opportunities": opportunity_status}
