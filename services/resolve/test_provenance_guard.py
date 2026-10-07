"""Guard: no event the resolver writes on a realistic run lacks its provenance quartet (docs/22 §13.7).

The run is the production chain on committed data (`data/eval/normalized.parquet`, the 2026-09-12
pull): the gated loader (`services.resolve.report.build_store`; SPP and ISO-NE refused), the
unmodified `pipeline.resolve.run`, `build_clusters` and `apply_all_clusters` under the confidence
gate, `resolve_organizations`, an unmerge of several proposal and organisation merges, and the
reviewed suppression list (`data/vendored/data_centres/icis_air_not_data_centres.yaml`) applied to
ICIS-Air rows carrying each listed registry id. To keep it to seconds it runs on every record of
the resolver's 279 loadable multi-member clusters plus every EIA generator of their plants; the
full-file run is measured in docs/22 §13.7.

A resolver event is one with an idempotency key the resolver writes: `merge:`, `unmerge:`,
`suppress:`.
"""

from __future__ import annotations

import contextlib
import io
import json
import pathlib
import uuid as _uuid
from collections import Counter
from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from pipeline import resolve as resolve_module
from pipeline.connectors.registry import SourceEntry
from pipeline.normalize import org_key
from services.db.base import Base
from services.db.models import Event, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.loader import load_dataframe, upsert_licence_and_source
from services.resolve import merge as merge_mod
from services.resolve import provenance, report
from services.resolve.suppress import apply_suppressions, load_suppressions

ROOT = pathlib.Path(__file__).resolve().parents[2]
EVAL = ROOT / "data" / "eval"
LOADABLE = ("caiso", "ercot", "nyiso", "eia860m")
UNMERGE_EACH = 10


def _subset(tmp: pathlib.Path) -> pathlib.Path:
    clusters = pd.read_parquet(EVAL / "clusters.parquet")
    real = clusters[~clusters["is_rollup"].astype(bool)]
    loadable = real[real["source_id"].isin(LOADABLE)]
    sizes = loadable.groupby("cluster_id").size()
    ids = set(loadable[loadable["cluster_id"].isin(sizes[sizes >= 2].index)]["record_id"])
    df = pd.read_parquet(EVAL / "normalized.parquet")
    plants = set(df[df["record_id"].isin(ids) & df["eia_plant_id"].notna()]["eia_plant_id"])
    keep = df["record_id"].isin(ids) | (df["source_id"].eq("eia860m") & df["eia_plant_id"].isin(plants))
    # A few gated rows, so the run proves the loader refuses them rather than never seeing them.
    keep |= df.index.isin(df[df["source_id"].isin(["spp", "isone"])].index[:50])
    path = tmp / "normalized.parquet"
    df[keep].reset_index(drop=True).to_parquet(path, index=False)
    return path


def _icis_rows(source: Source) -> pd.DataFrame:
    rows = []
    for i, s in enumerate(load_suppressions()):
        rows.append(
            {
                "record_id": f"icis:{i}",
                "source_id": source.id,
                "source_record_id": f"VA{i:05d}",
                "source_url": f"https://echo.epa.gov/detailed-facility-report?fid={s.registry_id}",
                "retrieved_at": "2026-10-06T05:00:00Z",
                "licence_id": "unused",
                "kind": "load",
                "name_canonical": s.name,
                "name_norm": s.name.lower(),
                "sponsor_name": None,
                "sponsor_norm": None,
                "technology": "load",
                "technology_raw": "Data Center",
                "capacity_mw": None,
                "storage_mwh": None,
                "iso": None,
                "state": "VA",
                "county": None,
                "county_norm": None,
                "lifecycle_state": "built",
                "status_raw": "Operating",
                "queue_date": None,
                "proposed_cod": None,
                "queue_id": None,
                "eia_plant_id": None,
                "eia_generator_id": None,
                "raw": json.dumps({"REGISTRY_ID": s.registry_id, "select_basis": "naics_518210"}),
            }
        )
    return pd.DataFrame(rows)


@pytest.fixture(scope="module")
def realistic_run(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Session]:
    path = _subset(tmp_path_factory.mktemp("provenance_guard"))
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    Base.metadata.create_all(engine)
    with get_sessionmaker(engine)() as session:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            loaded = report.build_store(session, path)
            matches, clusters = resolve_module.run(merge_mod.MERGE_SCORE_THRESHOLD, path)
        members, edges = report.build_clusters(
            pd.read_parquet(path), matches, clusters, loaded, report.build_link_index(session)
        )
        report.apply_all_clusters(session, {k: v for k, v in members.items() if len(v) >= 2}, edges)
        merge_mod.resolve_organizations(session, org_key)
        for subject, undo in (
            ("proposal", merge_mod.unmerge_proposal),
            ("organization", merge_mod.unmerge_organization),
        ):
            merges = session.scalars(
                select(Event)
                .where(Event.event_type == "merged", Event.subject_type == subject)
                .order_by(Event.seq)
                .limit(UNMERGE_EACH)
            ).all()
            for event in merges:
                undo(session, event.id, reason="provenance guard")
        icis = upsert_licence_and_source(
            session,
            SourceEntry.from_yaml(
                {
                    "id": "us.epa.echo.icis_air",
                    "name": "EPA ECHO ICIS-Air",
                    "jurisdiction": "US",
                    "category": "permit",
                    "operator": "US EPA",
                    "url": "https://echo.epa.gov/",
                    "access": "bulk_file",
                    "reuse": "open",
                    "cadence": "weekly",
                    "tier": 1,
                    "license": "US federal public domain",
                }
            ),
            "provenance-guard",
        )
        load_dataframe(session, icis, "proposal", _icis_rows(icis), None)
        apply_suppressions(session)
        session.flush()
        yield session


def _resolver_events(session: Session) -> list[Event]:
    return list(
        session.scalars(
            select(Event).where(
                or_(
                    Event.idempotency_key.like("merge:%"),
                    Event.idempotency_key.like("unmerge:%"),
                    Event.idempotency_key.like("suppress:%"),
                )
            )
        ).all()
    )


def test_the_run_writes_every_resolver_event_type(realistic_run: Session) -> None:
    kinds = Counter((e.subject_type, e.event_type) for e in _resolver_events(realistic_run))
    assert set(kinds) == {
        ("proposal", "merged"),
        ("proposal", "unmerged"),
        ("organization", "merged"),
        ("organization", "unmerged"),
        ("proposal", "unpublished"),
    }, kinds
    assert kinds[("proposal", "merged")] >= 100
    assert kinds[("proposal", "unpublished")] == len(load_suppressions())


def test_no_resolver_event_lacks_any_of_the_quartet(realistic_run: Session) -> None:
    missing = Counter(
        (e.subject_type, e.event_type)
        for e in _resolver_events(realistic_run)
        if not provenance.is_complete(e.source_id, e.source_url, e.retrieved_at, e.licence_id)
    )
    assert not missing, missing


def test_every_resolver_quartet_is_one_registered_sources_own(realistic_run: Session) -> None:
    sources = {s.id: s for s in realistic_run.scalars(select(Source))}
    bad = [
        (e.event_type, e.source_id, e.licence_id)
        for e in _resolver_events(realistic_run)
        if e.source_id not in sources or sources[e.source_id].licence_id != e.licence_id
    ]
    assert not bad, bad[:5]


def test_every_link_a_standing_merge_moved_names_it_and_every_unmerged_link_is_back(
    realistic_run: Session,
) -> None:
    """docs/21 §6.3: a re-pointed link's `link_event_id` is the merge that moved it; an unmerge puts
    each such link back on the absorbed record with the value it had before (null here)."""
    merges = realistic_run.scalars(
        select(Event).where(Event.event_type == "merged", Event.subject_type == "proposal")
    ).all()
    reversed_ids = set(
        realistic_run.scalars(select(Event.reverses_event_id).where(Event.event_type == "unmerged"))
    )
    standing = Counter[str]()
    for event in merges:
        absorbed = (event.before or {})["absorbed"]
        for raw in absorbed["proposal_source_ids"]:
            link = realistic_run.get(ProposalSource, _uuid.UUID(raw))
            assert link is not None
            if event.id in reversed_ids:
                ok = link.proposal_id == _uuid.UUID(absorbed["id"]) and link.link_event_id is None
                standing["unmerged ok" if ok else "unmerged wrong"] += 1
            else:
                ok = link.proposal_id == event.subject_id and link.link_event_id == event.id
                standing["merged ok" if ok else "merged wrong"] += 1
    assert set(standing) == {"merged ok", "unmerged ok"}, standing
    assert standing["unmerged ok"] >= UNMERGE_EACH
