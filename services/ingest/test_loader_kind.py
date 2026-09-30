"""The loader takes a run's kind from its connector class, not from its columns (lane FX2, 2026-09-30).

The defect: `load_from_files` loaded any frame with a `lifecycle_state` column as proposals. Document
frames carry a constant `lifecycle_state = "filed"` (`pipeline.connectors.base.DOCUMENT_COLUMNS`), so a
FERC eLibrary run published its filings as "Untitled" proposals and an EIA-860 ownership run published
5,680 more. Every frame here is built from a recorded fixture through the real connector's `parse` and
`normalize`, written where `python -m pipeline.connectors run` writes it, and loaded with the real
registry, which is the path the scheduler's `load_source` job takes.
"""

from __future__ import annotations

import pathlib
from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from conftest import connector_for, snapshot
from pipeline.connectors.registry import Registry
from services.db.models import Document, Opportunity, OpportunitySource, Proposal, ProposalSource, Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import loader

TS = "20260912T180000Z"

FIXTURES = {
    "us.ferc.elibrary": (
        "ferc_elibrary_search.json",
        "https://elibrary.ferc.gov/eLibrarywebapi/api/Search/AdvancedSearch",
        "application/json",
    ),
    "us.eia.860": (
        "eia860_owner_2024_sample.zip",
        "https://www.eia.gov/electricity/data/eia860/xls/eia8602024.zip",
        "application/zip",
    ),
    "us.iso.ercot.gen_queue": (
        "ercot_gis_report.xlsx",
        "https://www.ercot.com/misapp/GetReports.do?reportTypeId=15933",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    "eu.ted.api": ("ted_search.json", "https://api.ted.europa.eu/v3/notices/search", "application/json"),
}


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _frame(source_id: str) -> pd.DataFrame:
    name, url, content_type = FIXTURES[source_id]
    connector = connector_for(source_id)
    raw = snapshot(name, url, content_type)
    return connector.normalize(connector.parse(raw), raw)


def _write(root: pathlib.Path, source_id: str, frame: pd.DataFrame) -> None:
    out = root / "normalized" / source_id
    out.mkdir(parents=True)
    frame.to_parquet(out / f"{TS}.parquet", index=False)


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def _load_refused(session: Session, root: pathlib.Path, source_id: str, **kw: object) -> Exception | None:
    """Run the generic load; return what it raised (the refusal), or None when it loaded."""
    try:
        loader.load_from_files(session, source_id, TS, data_root=root, registry=Registry(), **kw)  # type: ignore[arg-type]
    except Exception as exc:
        return exc
    session.flush()
    return None


@pytest.mark.parametrize("source_id", ["us.ferc.elibrary", "us.eia.860"])
def test_a_document_kind_frame_loads_zero_proposals(
    source_id: str, tmp_path: pathlib.Path, session: Session
) -> None:
    """FERC eLibrary filings and the EIA-860 ownership sheet: both connectors declare `document`,
    both frames carry `lifecycle_state`. Before the fix each loaded one public proposal per row."""
    frame = _frame(source_id)
    assert len(frame) > 0 and "lifecycle_state" in frame.columns  # the shape that fooled the old check
    _write(tmp_path, source_id, frame)

    refused = _load_refused(session, tmp_path, source_id)

    assert _count(session, Proposal) == 0
    assert _count(session, ProposalSource) == 0
    assert _count(session, Opportunity) == 0
    assert _count(session, Document) == 0  # nothing writes documents yet; refused, not rerouted
    assert type(refused).__name__ == "KindRefused", refused
    assert "document" in str(refused)
    # Refused before anything was written: not even the `source` row.
    assert session.get(Source, source_id) is None


def test_a_document_source_cannot_be_loaded_as_proposals_by_naming_the_kind(
    tmp_path: pathlib.Path, session: Session
) -> None:
    _write(tmp_path, "us.ferc.elibrary", _frame("us.ferc.elibrary"))
    refused = _load_refused(session, tmp_path, "us.ferc.elibrary", kind="proposal")
    assert _count(session, Proposal) == 0
    assert type(refused).__name__ == "KindRefused", refused


def test_a_proposal_frame_still_loads_as_proposals(tmp_path: pathlib.Path, session: Session) -> None:
    frame = _frame("us.iso.ercot.gen_queue")
    _write(tmp_path, "us.iso.ercot.gen_queue", frame)
    result = loader.load_from_files(
        session, "us.iso.ercot.gen_queue", TS, data_root=tmp_path, registry=Registry()
    )
    assert result.proposals_created == len(frame) == _count(session, Proposal)
    assert _count(session, Opportunity) == 0
    assert result.kind == "proposal"  # the load report now says what it wrote


@pytest.mark.parametrize("stray_lifecycle_state", [False, True])
def test_an_opportunity_frame_still_loads_as_opportunities(
    stray_lifecycle_state: bool, tmp_path: pathlib.Path, session: Session
) -> None:
    """With a stray `lifecycle_state` column too: the connector says `opportunity`, so the columns
    do not decide (the old check would have loaded these notices as proposals)."""
    frame = _frame("eu.ted.api")
    if stray_lifecycle_state:
        frame["lifecycle_state"] = "filed"
    _write(tmp_path, "eu.ted.api", frame)
    result = loader.load_from_files(session, "eu.ted.api", TS, data_root=tmp_path, registry=Registry())
    assert _count(session, Proposal) == 0
    assert result.opportunities_created == len(frame) == _count(session, Opportunity)
    assert _count(session, OpportunitySource) == len(frame)
    assert result.kind == "opportunity"


def test_every_implemented_connector_is_loaded_or_refused_by_its_declared_kind() -> None:
    """The scheduler's pre-check (`kind_refusal`) and the loader agree for every real connector:
    proposal/opportunity load, anything else (the document sources today) is refused."""
    registry = Registry()
    seen: dict[str, str] = {}
    for row in registry.status():
        if row["state"] != "implemented":
            continue
        source_id = str(row["id"])
        kind = str(registry.connector_class(source_id).kind)
        seen[source_id] = kind
        refusal = loader.kind_refusal(registry, source_id)
        if kind in ("proposal", "opportunity"):
            assert refusal is None, (source_id, refusal)
            assert loader.generic_load_kind(registry, source_id) == kind
        else:
            assert refusal and kind in refusal, (source_id, refusal)
            with pytest.raises(loader.KindRefused):
                loader.generic_load_kind(registry, source_id)
    assert {s for s, k in seen.items() if k == "document"} >= {
        "us.ferc.elibrary",
        "us.eia.860",
        "us.epa.ghgrp",
    }


def test_load_dataframe_refuses_a_kind_it_does_not_write(session: Session) -> None:
    registry = Registry()
    source = loader.upsert_licence_and_source(session, registry.get("us.ferc.elibrary"), registry.version)
    with pytest.raises(loader.KindRefused):
        loader.load_dataframe(session, source, "document", _frame("us.ferc.elibrary"), None)  # type: ignore[arg-type]
    assert _count(session, Proposal) == 0
