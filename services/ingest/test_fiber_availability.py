"""Tests for `services/ingest/fiber_availability.py` on a parquet built from the synthetic FCC BDC
fixture (`pipeline/context/fixtures/fcc_bdc_fixed_summary_sample.zip`, see its test module)."""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest
import yaml
from sqlalchemy.orm import Session

from pipeline.connectors.registry import SOURCES_YAML, Registry
from pipeline.connectors.store import Store
from pipeline.context import fcc_bdc
from services.db.models import Source
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.fiber_availability import (
    SOURCE_ID,
    FiberBatchInvalid,
    load_county_fiber,
    read_county_fiber,
)
from services.ingest.loader import GateRefused
from services.ingest.vintage import ARTEFACT_FILENAME

FIXTURE = pathlib.Path(fcc_bdc.__file__).with_name("fixtures") / "fcc_bdc_fixed_summary_sample.zip"

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


@pytest.fixture(scope="module")
def parquet(tmp_path_factory) -> pathlib.Path:
    root = tmp_path_factory.mktemp("fiber")
    out = root / "fiber.parquet"
    fcc_bdc.run(snapshot=FIXTURE, out=out, store=Store(root))
    return out


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _rewrite(parquet: pathlib.Path, tmp_path: pathlib.Path, change) -> pathlib.Path:
    df = pd.read_parquet(parquet)
    change(df)
    out = tmp_path / "changed.parquet"
    df.to_parquet(out, index=False)
    return out


def _manifest_with(tmp_path: pathlib.Path, **fields) -> Registry:
    doc = yaml.safe_load(SOURCES_YAML.read_text(encoding="utf-8"))
    entry = next(s for s in doc["sources"] if s.get("id") == SOURCE_ID)
    entry.update(fields)
    path = tmp_path / "sources.yaml"
    path.write_text(yaml.safe_dump({"version": doc["version"], "sources": [entry]}), encoding="utf-8")
    return Registry(path)


def test_batch_carries_rows_provenance_and_the_filing_as_vintage(parquet):
    batch = read_county_fiber(parquet)
    assert batch.source_id == SOURCE_ID
    assert (batch.vintage.value, batch.vintage.basis, batch.vintage.label) == (
        "2025-12",
        ARTEFACT_FILENAME,
        "December 2025",
    )
    assert len(batch.rows) == 8
    autauga = batch.by_fips["01001"]
    assert autauga["fiber_share"] == pytest.approx(0.412346)
    assert autauga["bsl_units"] == 28718 and isinstance(autauga["bsl_units"], int)
    assert batch.by_fips["02164"]["fiber_share"] is None
    for row in batch.rows:
        assert all(row[k] for k in ("source_id", "source_url", "retrieved_at", "licence"))
        assert "raw" not in row  # the source cells stay in the parquet (publication: derived_only)


def test_a_row_without_provenance_is_refused(parquet, tmp_path):
    def blank(df):
        df.loc[0, "source_url"] = None

    with pytest.raises(FiberBatchInvalid, match="source_url"):
        read_county_fiber(_rewrite(parquet, tmp_path, blank))


def test_duplicate_counties_are_refused(parquet, tmp_path):
    def dup(df):
        df.loc[1, "county_fips"] = df.loc[0, "county_fips"]

    with pytest.raises(FiberBatchInvalid, match="duplicate"):
        read_county_fiber(_rewrite(parquet, tmp_path, dup))


def test_a_share_outside_zero_one_is_refused(parquet, tmp_path):
    def pct(df):
        df.loc[0, "fiber_share"] = 41.2

    with pytest.raises(FiberBatchInvalid, match="fiber_share"):
        read_county_fiber(_rewrite(parquet, tmp_path, pct))


def test_mixed_filings_are_refused(parquet, tmp_path):
    def mixed(df):
        df.loc[0, "vintage"] = "2025-06"

    with pytest.raises(FiberBatchInvalid, match="one BDC filing"):
        read_county_fiber(_rewrite(parquet, tmp_path, mixed))


def test_a_gated_manifest_entry_loads_nothing(parquet, tmp_path):
    registry = _manifest_with(tmp_path, reuse="unknown", publication="none")
    with pytest.raises(GateRefused):
        read_county_fiber(parquet, registry)


def test_load_registers_the_source_and_its_filing(parquet, session):
    batch = load_county_fiber(session, parquet)
    source = session.get(Source, SOURCE_ID)
    assert source is not None
    assert (source.vintage, source.vintage_basis) == ("2025-12", ARTEFACT_FILENAME)
    assert len(batch.rows) == 8
