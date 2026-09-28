"""The `transmission_line` context layer through the generic asset and edge loaders (grid lane G2,
2026-09-28): the recorded LBNL fixture, normalised by `pipeline/context/lbnl_transmission.py`,
loads as one asset per HIFLD line with its line geometry and provenance; the HIFLD owner rides in
`attributes` and mints no organisation (LBNL's owner strings do not key-match the registry --
`pipeline/context/lbnl_transmission.py` docstring). `substation` stays unwired (docs/13 §2.19)."""

from __future__ import annotations

import pathlib

import pandas as pd
import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.context import lbnl_transmission as lt
from pipeline.context.eia_atlas import Provenance
from services.db.models import Asset, AssetOwner, Licence, Organization
from services.db.session import get_engine, get_sessionmaker, init_db
from services.ingest.assets import UnsupportedAssetTypeError, load_assets
from services.ingest.midstream import load_operator_edges

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")

FIXTURE = pathlib.Path(lt.__file__).parent / "fixtures" / "lbnl_ferc_hifld_transmission_lines_sample.csv"


@pytest.fixture()
def session() -> Session:
    engine = get_engine("sqlite+pysqlite:///:memory:")
    init_db(engine)
    with get_sessionmaker(engine)() as s:
        yield s


def _frame() -> pd.DataFrame:
    prov = Provenance(lt.SOURCE_ID, lt.DOWNLOAD_URL, "2026-09-28T13:55:17Z", "CC BY 4.0")
    return lt.normalise(lt.parse(FIXTURE.read_bytes()), prov)


def test_lines_load_with_geometry_provenance_and_attribution(session):
    result = load_assets(session, _frame(), "transmission_line")
    assert (result.inserted, result.placed) == (32, 32)
    row = session.scalars(select(Asset).where(Asset.source_asset_id == "100024")).one()
    assert row.asset_type == "transmission_line" and row.status == "unknown"
    assert row.name == "Scriba - Fitzpatrick 345 kV"
    assert row.geom_line is not None and row.geom is not None
    assert (row.source_id, row.source_url) == (lt.SOURCE_ID, lt.DOWNLOAD_URL)
    assert row.retrieved_at is not None and row.licence_id.startswith(f"{lt.SOURCE_ID}#")
    assert row.attributes["voltage_kv"] == 345.0 and row.attributes["sub_2"] == "Fitzpatrick"
    licence = session.get(Licence, row.licence_id)
    assert licence.reuse_class == "attribution" and licence.attribution_required
    assert "Lawrence Berkeley National Laboratory" in (licence.attribution_text or "")
    again = load_assets(session, _frame(), "transmission_line")
    assert (again.inserted, again.updated) == (0, 32)


def test_owner_is_shown_but_mints_no_organisation(session):
    frame = _frame()
    load_assets(session, frame, "transmission_line")
    result = load_operator_edges(session, frame, "transmission_line")
    assert (result.edges_written, result.organizations_created) == (0, 0)
    assert session.scalars(select(AssetOwner)).all() == []
    assert session.scalars(select(Organization)).all() == []
    row = session.scalars(select(Asset).where(Asset.source_asset_id == "100024")).one()
    assert row.attributes["owner"] == "Niagara Mohawk Power"


def test_substation_is_not_wired():
    with pytest.raises(UnsupportedAssetTypeError):
        load_assets(Session(), _frame().iloc[:0], "substation")
