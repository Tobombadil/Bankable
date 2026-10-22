"""Curated-issuer RFP records reach the opportunity table (US-301..303, docs/28).

Each frame is built from a recorded bundle (`pipeline/connectors/us_rfp/fixtures`) through the real
connector's `parse` and `normalize`, written where a run writes it, and loaded with the real
registry: the path the scheduler's `load_source` job takes.

The four AEP rows are `noncommercial` (docs/28 §3.3): refused under the commercial posture,
published under the noncommercial one, which is the deployed posture (docs/26). The loader resolves
each record's issuer to an organisation marked a curated issuer; other opportunity sources keep
their issuer as text, as before.
"""

from __future__ import annotations

import datetime as dt
import pathlib
from collections.abc import Iterator

import pandas as pd
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from pipeline.connectors.base import RawSnapshot
from pipeline.connectors.registry import Registry
from services import posture
from services.db.models import Licence, Opportunity, OpportunitySource, Organization, OrganizationAlias
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest import loader

FIXTURES = pathlib.Path(__file__).resolve().parents[2] / "pipeline" / "connectors" / "us_rfp" / "fixtures"
TS = "20261021T160000Z"
AT = dt.datetime(2026, 10, 21, 16, 0, tzinfo=dt.UTC)


@pytest.fixture()
def session() -> Iterator[Session]:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


@pytest.fixture()
def noncommercial(monkeypatch: pytest.MonkeyPatch) -> None:
    """The deployed posture, for the loader's two gates (as `tests/test_platform_posture.py` does)."""
    monkeypatch.setattr(
        loader, "PUBLISHABLE_REUSE", frozenset(posture.publishable_reuse_classes("noncommercial"))
    )
    monkeypatch.setattr(loader, "GATED_REUSE", frozenset(posture.gated_reuse_classes("noncommercial")))


def _write(root: pathlib.Path, source_id: str) -> pd.DataFrame:
    reg = Registry()
    connector = reg.connector_class(source_id)(reg.get(source_id))
    slug = source_id.removeprefix("us.rfp.")
    raw = RawSnapshot(
        content=(FIXTURES / f"bundle_{slug}_20261021.json").read_bytes(),
        content_type="application/json",
        url=reg.get(source_id).url,
        retrieved_at=AT,
        http_status=200,
        ext="json",
    )
    frame = connector.normalize(connector.parse(raw), raw)
    out = root / "normalized" / source_id
    out.mkdir(parents=True)
    frame.to_parquet(out / f"{TS}.parquet", index=False)
    return frame


def _count(session: Session, model: type) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_an_aep_row_is_refused_under_the_commercial_posture(tmp_path: pathlib.Path, session: Session) -> None:
    _write(tmp_path, "us.rfp.pso")
    with pytest.raises(loader.GateRefused, match="noncommercial"):
        loader.load_from_files(session, "us.rfp.pso", TS, data_root=tmp_path, registry=Registry())
    assert _count(session, Opportunity) == 0


@pytest.mark.usefixtures("noncommercial")
def test_aep_records_load_as_rfps_under_the_noncommercial_posture(
    tmp_path: pathlib.Path, session: Session
) -> None:
    frame = _write(tmp_path, "us.rfp.swepco")
    result = loader.load_from_files(session, "us.rfp.swepco", TS, data_root=tmp_path, registry=Registry())
    session.flush()
    assert result.kind == "opportunity" and result.opportunities_created == len(frame) == 4
    rows = session.scalars(select(Opportunity)).all()
    assert {o.kind for o in rows} == {"rfp"}
    assert {o.min_reuse_class for o in rows} == {"noncommercial"}
    assert {o.jurisdiction for o in rows} == {"US"}
    allsource = next(o for o in rows if o.title == "2026 All-Source RFP")
    assert float(allsource.capacity_sought_mw) == 3000.0 and allsource.technologies == []
    assert allsource.status == "closed" and allsource.due_at is not None

    licence = session.get(Licence, Registry().get("us.rfp.swepco").licence_id)
    assert licence is not None and licence.reuse_class == "noncommercial"
    assert licence.attribution_text == "Source: SWEPCO (AEP), Energy RFPs"
    assert licence.allows_commercial_use is False and licence.allows_raw_publication is False
    assert licence.allows_api_redistribution is False and licence.allows_bulk_export is False

    links = session.scalars(select(OpportunitySource)).all()
    assert {link.source_id for link in links} == {"us.rfp.swepco"}
    assert all(link.source_url.startswith("https://www.swepco.com/") for link in links)
    assert all(link.licence_id == licence.id and link.retrieved_at is not None for link in links)


@pytest.mark.usefixtures("noncommercial")
def test_the_issuer_becomes_one_curated_issuer_organisation(tmp_path: pathlib.Path, session: Session) -> None:
    _write(tmp_path, "us.rfp.appalachian_power")
    result = loader.load_from_files(
        session, "us.rfp.appalachian_power", TS, data_root=tmp_path, registry=Registry()
    )
    session.flush()
    orgs = session.scalars(select(Organization)).all()
    assert len(orgs) == 1 and result.organizations_created == 1
    org = orgs[0]
    assert org.name_canonical == "Appalachian Power Company"
    assert org.type == "utility" and org.is_curated_issuer is True
    assert {o.issuer_org_id for o in session.scalars(select(Opportunity))} == {org.id}
    alias = session.scalars(select(OrganizationAlias)).one()
    assert alias.source_id == "us.rfp.appalachian_power"


@pytest.mark.usefixtures("noncommercial")
def test_reloading_the_same_run_changes_nothing(tmp_path: pathlib.Path, session: Session) -> None:
    _write(tmp_path, "us.rfp.pso")
    loader.load_from_files(session, "us.rfp.pso", TS, data_root=tmp_path, registry=Registry())
    session.commit()
    again = loader.load_from_files(session, "us.rfp.pso", TS, data_root=tmp_path, registry=Registry())
    session.commit()
    assert again.opportunities_created == 0 and again.opportunities_updated == 2
    assert again.organizations_created == 0 and again.records_changed == 0
    assert _count(session, Opportunity) == 2 and _count(session, Organization) == 1


def test_a_state_procurement_loads_under_the_default_posture(
    tmp_path: pathlib.Path, session: Session
) -> None:
    _write(tmp_path, "us.rfp.ct_deep")
    loader.load_from_files(session, "us.rfp.ct_deep", TS, data_root=tmp_path, registry=Registry())
    session.flush()
    rows = {o.title: o for o in session.scalars(select(Opportunity))}
    zero = rows["DEEP Announces 2026 Zero Carbon Request for Proposals"]
    assert zero.jurisdiction == "US-CT" and zero.min_reuse_class == "attribution"
    assert zero.open_at == dt.date(2026, 1, 30)
    assert zero.issuer is not None and zero.issuer.type == "agency"
    assert zero.issuer.name_canonical == "Connecticut Department of Energy and Environmental Protection"
    assert rows["DEEP Launches New Procurement For Energy Storage"].status == "announced"


def test_an_operator_override_of_the_issuer_survives_a_reload(
    tmp_path: pathlib.Path, session: Session
) -> None:
    _write(tmp_path, "us.rfp.nj_bpu")
    loader.load_from_files(session, "us.rfp.nj_bpu", TS, data_root=tmp_path, registry=Registry())
    session.commit()
    record = session.scalars(select(Opportunity)).one()
    record.issuer_org_id = None
    record.overrides = {"issuer_org_id": {"by": "operator", "reason": "test"}}
    session.commit()
    loader.load_from_files(session, "us.rfp.nj_bpu", TS, data_root=tmp_path, registry=Registry())
    session.commit()
    assert session.scalars(select(Opportunity)).one().issuer_org_id is None


def test_other_opportunity_sources_keep_their_issuer_as_text() -> None:
    reg = Registry()
    for source_id in ("eu.ted.api", "gb.find_a_tender", "us.grants_gov.search2", "mdb.worldbank.procnotices"):
        assert loader.connector_issuer_org_type(reg, source_id) is None
    assert loader.connector_issuer_org_type(reg, "us.rfp.nj_bpu") == "agency"
    assert loader.connector_issuer_org_type(reg, "us.rfp.pso") == "utility"
    assert loader.connector_issuer_org_type(reg, "us.utility_rfps") is None
